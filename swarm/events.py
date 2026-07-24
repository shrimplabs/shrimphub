"""Minimal event bus for intra-process agent lifecycle events.

Disabled by default (``EVENT_BUS_ENABLED_DEFAULT = False``).  Enable via
``bus.set_enabled(True)`` or ``agent_lifecycle.configure(event_bus_enabled=True)``.

When disabled, ``publish()`` is a no-op returning ``False`` — callers never
block and exceptions are never raised.

Usage::

    from swarm.events import bus
    bus.subscribe("AGENT_EXITED", my_handler)
    bus.publish("AGENT_EXITED", agent_id="abc", exit_code=0)

Handlers run on a single background dispatcher thread.  They must not block.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


@dataclass
class Event:
    type: str
    payload: Dict[str, Any]
    ts: float = field(default_factory=time.time)


class EventBus:
    def __init__(self) -> None:
        self._enabled = False
        self._queue: queue.Queue[Event] = queue.Queue()
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._lock = threading.Lock()
        self._dispatcher: threading.Thread | None = None
        self._dispatching: bool = False  # True while a handler is executing
        self.stats: Dict[str, Any] = {
            "published": 0,
            "dropped_disabled": 0,
            "handled": 0,
            "handler_errors": 0,
            "by_type": defaultdict(int),
        }

    # ------------------------------------------------------------------
    # Enable / disable
    # ------------------------------------------------------------------

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            if on and not self._enabled:
                self._enabled = True
                self._start_dispatcher()
            elif not on:
                self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Subscribe / publish
    # ------------------------------------------------------------------

    def subscribe(self, event_type: str, handler: Callable[[Event], None]) -> None:
        with self._lock:
            self._subscribers[event_type].append(handler)

    def publish(self, event_type: str, **payload) -> bool:
        """Enqueue an event.  Returns True if enqueued, False if bus is disabled."""
        if not self._enabled:
            self.stats["dropped_disabled"] += 1
            return False
        ev = Event(type=event_type, payload=payload)
        self._queue.put(ev)
        self.stats["published"] += 1
        self.stats["by_type"][event_type] += 1
        return True

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def drain(self, timeout: float = 5.0) -> bool:
        """Block until the queue is empty and the dispatcher is idle."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._queue.empty() and not self._dispatching:
                return True
            time.sleep(0.02)
        return self._queue.empty() and not self._dispatching

    def reset_for_tests(self) -> None:
        """Clear all state.  Call from test setUp / tearDown only."""
        with self._lock:
            self._enabled = False
            self._subscribers.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self.stats = {
            "published": 0,
            "dropped_disabled": 0,
            "handled": 0,
            "handler_errors": 0,
            "by_type": defaultdict(int),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_dispatcher(self) -> None:
        if self._dispatcher and self._dispatcher.is_alive():
            return
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            daemon=True,
            name="event-bus-dispatch",
        )
        self._dispatcher.start()

    def _dispatch_loop(self) -> None:
        while True:
            try:
                ev = self._queue.get(timeout=1.0)
            except queue.Empty:
                if not self._enabled:
                    return
                continue
            with self._lock:
                handlers = list(self._subscribers.get(ev.type, []))
            self._dispatching = True
            try:
                for h in handlers:
                    try:
                        h(ev)
                        self.stats["handled"] += 1
                    except Exception as exc:
                        self.stats["handler_errors"] += 1
                        print(f"[EventBus] handler error for {ev.type}: {exc}")
            finally:
                self._dispatching = False


# Module-level singleton.
bus = EventBus()
