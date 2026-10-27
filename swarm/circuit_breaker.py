"""
Circuit Breaker for LLM provider backends.

Provides a thread-safe circuit breaker per backend (identified by base_url).
Transitions:
  CLOSED -> OPEN   : after consecutive_failure_threshold consecutive failures
  OPEN   -> HALF-OPEN : after cooldown_secs elapses
  HALF-OPEN -> CLOSED : probe request succeeds
  HALF-OPEN -> OPEN   : probe request fails (resets cooldown)

Only one probe request is permitted through in HALF-OPEN state -- a second
concurrent caller waits for the first probe to resolve rather than sneaking
its own request through.

The breaker state is exposed via CircuitBreaker.get_all_state() for the
/health endpoint.
"""

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


@dataclass
class BreakerStats:
    """Snapshot of breaker counters at a point in time."""
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    last_failure_ts: float = 0.0
    last_success_ts: float = 0.0
    last_transition_ts: float = 0.0


@dataclass
class BreakerBackend:
    """Live breaker state for one backend."""
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    last_failure_ts: float = 0.0
    last_success_ts: float = 0.0
    last_transition_ts: float = 0.0
    # HALF-OPEN state
    half_open_at: float = 0.0          # when the half-open window opened
    half_open_probe_count: int = 0     # how many requests snuck through (should be 0 or 1)
    half_open_probe_done: bool = False  # True once the probe has resolved (success or failure)


class CircuitBreaker:
    """Thread-safe circuit breaker per backend."""

    # Module-level registry of all CircuitBreaker instances (one per backend set)
    _instances: Dict[str, "CircuitBreaker"] = {}
    _instances_lock = threading.Lock()

    def __init__(
        self,
        backend_key: str,
        consecutive_failure_threshold: int = 3,
        cooldown_secs: float = 60.0,
    ):
        self.backend_key = backend_key
        self.consecutive_failure_threshold = consecutive_failure_threshold
        self.cooldown_secs = cooldown_secs
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._total_failures = 0
        self._total_successes = 0
        self._last_failure_ts = 0.0
        self._last_success_ts = 0.0
        self._last_transition_ts = 0.0
        # HALF-OPEN state
        self._half_open_at = 0.0
        self._half_open_probe_count = 0
        self._half_open_probe_done = False
        # Locks for state transitions and probe gating
        self._state_lock = threading.Lock()
        self._probe_lock = threading.Lock()  # ensures only one probe at a time

    # -------------------------------------------------------------------
    # Public API (call before/after each LLM request)
    # -------------------------------------------------------------------

    def is_request_permitted(self) -> bool:
        """Return True if the caller may proceed with a request.


        In OPEN state: always False (unless cooldown has elapsed, in which
        case we atomically transition to HALF-OPEN and return True).
        In HALF-OPEN state: exactly one caller is permitted through as a
        probe; all others wait (via _probe_lock) and then re-check.
        In CLOSED state: always True.
        """
        with self._state_lock:
            now = time.time()

            if self._state == BreakerState.CLOSED:
                return True

            if self._state == BreakerState.OPEN:
                # Check if cooldown has elapsed -- transition to half-open
                if self._half_open_at > 0 and now >= self._half_open_at:
                    self._transition_unlocked(BreakerState.HALF_OPEN, now)
                    # First caller through the half-open door becomes the probe
                    self._half_open_probe_count = 1
                    self._half_open_probe_done = False
                    return True
                return False

            if self._state == BreakerState.HALF_OPEN:
                if self._half_open_probe_done:
                    # Probe already resolved -- re-check state (may have changed)
                    return False
                if self._half_open_probe_count == 0:
                    # No probe yet -- this caller becomes the probe
                    self._half_open_probe_count = 1
                    return True
                # Probe already dispatched; this caller must wait
                return False

            return False

    def wait_for_probe(self, timeout: float = 30.0) -> bool:
        """Block until the in-flight probe resolves.

        Returns True if the probe succeeded (breaker should close),
        False if the probe failed (breaker reopens).

        Called by any request that arrived at a half-open backend while a
        probe was already in-flight.
        """
        acquired = self._probe_lock.acquire(timeout=timeout)
        if not acquired:
            # Timeout -- treat as probe failure
            return False
        try:
            with self._state_lock:
                return self._state == BreakerState.CLOSED
        finally:
            self._probe_lock.release()

    def record_success(self) -> None:
        """Record a successful request.

        CLOSED  -> resets consecutive failure counter
        HALF-OPEN -> closes the breaker (transitions to CLOSED)
        """
        now = time.time()
        with self._state_lock:
            if self._state == BreakerState.HALF_OPEN:
                # Probe succeeded -- close the breaker
                self._transition_unlocked(BreakerState.CLOSED, now)
                self._consecutive_failures = 0
                self._half_open_probe_done = True
                # Release any waiters
                try:
                    self._probe_lock.release()
                except RuntimeError:
                    pass  # was not held
            elif self._state == BreakerState.CLOSED:
                self._consecutive_failures = 0
            self._total_successes += 1
            self._last_success_ts = now

    def record_failure(self) -> None:
        """Record a failed request.

        CLOSED -> increments consecutive failures; trips if threshold reached
        HALF-OPEN -> reopens the breaker and resets cooldown
        """
        now = time.time()
        with self._state_lock:
            self._total_failures += 1
            self._last_failure_ts = now

            if self._state == BreakerState.HALF_OPEN:
                # Probe failed -- reopen the breaker
                self._transition_unlocked(BreakerState.OPEN, now)
                self._half_open_probe_done = True
                self._half_open_at = now + self.cooldown_secs
                # Release any waiters
                try:
                    self._probe_lock.release()
                except RuntimeError:
                    pass  # was not held
                return

            if self._state == BreakerState.CLOSED:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.consecutive_failure_threshold:
                    self._transition_unlocked(BreakerState.OPEN, now)
                    self._half_open_at = now + self.cooldown_secs

    def get_state(self) -> BreakerState:
        """Return current breaker state (no lock needed for simple read)."""
        with self._state_lock:
            return self._state

    def get_stats(self) -> BreakerStats:
        """Return a snapshot of current counters."""
        with self._state_lock:
            return BreakerStats(
                consecutive_failures=self._consecutive_failures,
                total_failures=self._total_failures,
                total_successes=self._total_successes,
                last_failure_ts=self._last_failure_ts,
                last_success_ts=self._last_success_ts,
                last_transition_ts=self._last_transition_ts,
            )

    def get_live_state(self) -> BreakerBackend:
        """Return full live state including half-open counters."""
        with self._state_lock:
            return BreakerBackend(
                state=self._state,
                consecutive_failures=self._consecutive_failures,
                total_failures=self._total_failures,
                total_successes=self._total_successes,
                last_failure_ts=self._last_failure_ts,
                last_success_ts=self._last_success_ts,
                last_transition_ts=self._last_transition_ts,
                half_open_at=self._half_open_at,
                half_open_probe_count=self._half_open_probe_count,
                half_open_probe_done=self._half_open_probe_done,
            )

    def _transition_unlocked(self, new_state: BreakerState, ts: float) -> None:
        """Internal state transition -- caller must hold _state_lock."""
        self._state = new_state
        self._last_transition_ts = ts

    # -------------------------------------------------------------------
    # Module-level registry (for /health endpoint)
    # -------------------------------------------------------------------

    @classmethod
    def get_or_create(
        cls,
        backend_key: str,
        consecutive_failure_threshold: int = 3,
        cooldown_secs: float = 60.0,
    ) -> "CircuitBreaker":
        """Get or create a CircuitBreaker for the given backend key."""
        with cls._instances_lock:
            if backend_key not in cls._instances:
                cls._instances[backend_key] = CircuitBreaker(
                    backend_key=backend_key,
                    consecutive_failure_threshold=consecutive_failure_threshold,
                    cooldown_secs=cooldown_secs,
                )
            return cls._instances[backend_key]

    @classmethod
    def get_all_state(cls) -> Dict[str, dict]:
        """Return breaker state for all registered backends."""
        with cls._instances_lock:
            result = {}
            for key, cb in cls._instances.items():
                live = cb.get_live_state()
                result[key] = {
                    "state": live.state.value,
                    "consecutive_failures": live.consecutive_failures,
                    "total_failures": live.total_failures,
                    "total_successes": live.total_successes,
                    "last_failure_ts": live.last_failure_ts,
                    "last_success_ts": live.last_success_ts,
                    "last_transition_ts": live.last_transition_ts,
                    "half_open_at": live.half_open_at,
                    "cooldown_remaining_secs": max(0.0, live.half_open_at - time.time())
                    if live.state == BreakerState.OPEN else 0.0,
                }
            return result

    @classmethod
    def reset_all(cls) -> None:
        """Clear all instances -- used by tests."""
        with cls._instances_lock:
            cls._instances.clear()


    @classmethod
    def configure_default_threshold(cls, threshold: int) -> None:
        """Set the default consecutive failure threshold for new instances."""
        cls._default_threshold = threshold

    @classmethod
    def configure_default_cooldown(cls, cooldown_secs: float) -> None:
        """Set the default cooldown (seconds) for new instances."""
        cls._default_cooldown = cooldown_secs

    _default_threshold: int = 3
    _default_cooldown: float = 60.0
