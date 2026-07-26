"""
Swarm Orchestrator

Centralises the coordination logic that was previously scattered across
swarm_runner.py:

- spawn_agent
- check_agent_status / _finish_agent
- fill_slots
- prune_history
- check_quota_limit
- get_active_count
- update_project_registry

All state is read/written via swarm.db (SQLite) so multiple threads are safe.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from swarm import db
from swarm import worktree
from swarm import constants
from swarm import validation as _validation
from swarm import learnings as _learnings
from swarm import agent_lifecycle
from swarm.godot_bootstrap import GUT_VERSION
from swarm.task_chains import chain_to_project_head
from swarm.constants import (
    MAX_ACTIVE_AGENTS, MAX_LINES, AGENT_TIMEOUT, QUOTA_LIMIT_PERCENT,
    QA_AUTO_THRESHOLD, IGNORE_DIRS, IGNORE_EXTENSIONS,
    MAX_TOOL_LOOPS, API_PORT, QA_MAX_CYCLES,
)
from swarm.integrity import can_task_accept_agent
from swarm.dependencies import is_dependency_met

# ---------------------------------------------------------------------------
# Module-level config -- set by the caller (e.g. create_app or swarm_runner)
# ---------------------------------------------------------------------------

WORKSPACE: Path = Path(".")
DATA_DIR = Path("data")
HISTORY_FILE: Path = DATA_DIR / "agent-history.jsonl"

LOCK_PROJECT: bool = False
USE_WORKTREES: bool = True
MCP_SERVERS: dict = {}
IGNORE_EXTENSIONS: set = set()
MANAGED_PROJECTS: list = []
PAUSED_PROJECTS: list = []
AUTO_REPLAN_PROJECTS: list = []  # projects that auto-get a project_plan when they run out of tasks
# When False (default), the swarm-controller project itself is excluded from
# managed work assignments. Set to True in config.json to enable self-modification.
# End users should leave this off to prevent agents accidentally breaking their own instance.
ALLOW_SELF_MODIFICATION: bool = False
TASK_SELECTION_STRATEGY: str = "least_recently_worked"
WEBHOOK_URL: str = ""
LLM_PROVIDER: str = "minimax"
FALLBACK_PROVIDERS: list = []
MINIMAX_API_KEY: str = ""
MINIMAX_BASE_URL: str = constants.MINIMAX_BASE_URL

# Max agents to spawn per fill_slots call -- prevents burst spawning after a cooldown.
# Set low (e.g. 2-3) so slots fill gradually across monitor cycles rather than all at once.
SPAWN_PER_CYCLE: int = 1

# Auto-scaling: when enabled, the monitor adjusts MAX_ACTIVE_AGENTS dynamically based
# on observed 429 pressure, up to the configured ceiling (max_active_agents in config).
# MAX_ACTIVE_AGENTS becomes the live count; AUTO_SCALE_CEILING is the user's cap.
AUTO_SCALE: bool = False
AUTO_SCALE_CEILING: int = 60  # hard cap -- never exceed this regardless of 429 pressure
_auto_scale_floor: int = 1    # never drop below this

# Auto-scale state (managed by monitor thread)
_auto_scale_current: int = 3          # current dynamic max (starts at floor, ramps up)
_auto_scale_last_change: float = 0.0  # timestamp of last increment/decrement
_auto_scale_clean_cycles: int = 0     # consecutive cycles with zero 429s

_fill_slots_lock = threading.Lock()

# Auto-QA: track completed (non-QA) tasks per Godot project since last QA spawn
# Only used for projects NOT in AUTO_REPLAN_PROJECTS (sprint-based projects use sprint-boundary QA)
_qa_completion_counter: Dict[str, int] = {}

# Sprint cycle: tracks which auto_replan projects have completed QA since their last sprint.
# Sprint flow: queue empty → QA → bugs fixed → queue empty → planner → next sprint
_projects_sprint_qa_done: set = set()

# Gardener: automated maintenance agent
# GARDENER_ENABLED gates idle-trigger and scheduling
# GARDENER_MAX_TASKS limits tasks created per gardener run
# GARDENER_SKIP_PROJECTS lists projects the gardener should ignore
# LAST_GARDENER_RUN_TS is synced from config at startup and updated by api_gardener.py
# META_MODE_ENABLED is the master toggle for all meta-agents (Gardener, Cartographer,
# Librarian, Archaeologist, Auditor, Scheduler). When False, no meta-agent scheduling
# fires regardless of individual agent enabled flags.
GARDENER_ENABLED: bool = False
GARDENER_MAX_TASKS: int = 10
GARDENER_SKIP_PROJECTS: list = []
LAST_GARDENER_RUN_TS: float = 0.0
META_MODE_ENABLED: bool = False

# Librarian: prompt-audit agent
# LIBRARIAN_ENABLED gates auto-trigger
# LIBRARIAN_TRIGGER_INTERVAL fires after this many task completions (default 50)
# LIBRARIAN_MAX_PROMPT_TASKS limits refactor tasks created per run (default 3)
# LIBRARIAN_COMPLETION_COUNTER tracks completed tasks -- incremented by agent_lifecycle
# LIBRARIAN_AUTONOMOUS_EDITS: when True, librarian directly edits prompts/*.yaml and commits
LIBRARIAN_ENABLED: bool = False
LIBRARIAN_TRIGGER_INTERVAL: int = 50
LIBRARIAN_MAX_PROMPT_TASKS: int = 3
LIBRARIAN_COMPLETION_COUNTER: int = 0
LIBRARIAN_AUTONOMOUS_EDITS: bool = False

# Auditor: weekly structural-audit agent
# META_AUDITOR_ENABLED gates scheduled runs (default False)
# META_AUDITOR_INTERVAL_DAYS controls the weekly cadence (default 7)
# META_AUDITOR_MAX_TASKS limits tasks created per run (default 20)
META_AUDITOR_ENABLED: bool = False
META_AUDITOR_INTERVAL_DAYS: int = 7
META_AUDITOR_MAX_TASKS: int = 20

# Cartographer: every-2-hours swarm-state cartographer
# CARTOGRAPHER_ENABLED gates auto-trigger
# CARTOGRAPHER_INTERVAL_HOURS controls the cadence (default 2)
CARTOGRAPHER_ENABLED: bool = False
CARTOGRAPHER_INTERVAL_HOURS: int = 2

# Archaeologist: stall-detection agent
# ARCHAEOLOGIST_ENABLED gates auto-trigger and scheduled check
# ARCHAEOLOGIST_STALL_THRESHOLD_HOURS triggers after N hours without success (default 72)
# ARCHAEOLOGIST_MAX_CONCURRENT limits simultaneous archaeologist tasks (default 2)
ARCHAEOLOGIST_ENABLED: bool = False
ARCHAEOLOGIST_STALL_THRESHOLD_HOURS: int = 72
ARCHAEOLOGIST_MAX_CONCURRENT: int = 2

# Scheduler: dynamic orchestrator config adjuster
SCHEDULER_ENABLED: bool = False
SCHEDULER_INTERVAL_MINUTES: int = 15
SCHEDULER_ALLOW_PAUSE: bool = True
SCHEDULER_ALLOW_AGENT_CEILING_ADJUST: bool = True
SCHEDULER_OFF_PEAK_HOURS: list = [0, 6]
# ---------------------------------------------------------------------------
# Webhook helper
# ---------------------------------------------------------------------------

def _fire_task_webhook(event: str, **kwargs):
    """Fire a task-level webhook event if a URL is configured.

    Thin wrapper around the canonical implementation in
    ``swarm.task_mutations._fire_task_webhook`` that injects this module's
    ``WEBHOOK_URL``. Tests may patch ``swarm.orchestrator._fire_task_webhook``
    directly to intercept the call.
    """
    from swarm.task_mutations import _fire_task_webhook as _impl
    return _impl(event, webhook_url=WEBHOOK_URL, **kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Quota checking
# ---------------------------------------------------------------------------

_quota_cache: Tuple[bool, float, float, int, int] = (False, 0.0, 100.0, 0, 4500)
_quota_cache_ts: float = 0.0
_QUOTA_CACHE_TTL: float = 30.0  # seconds


def check_quota_limit() -> Tuple[bool, float, float, int, int]:
    """
    Returns (over_limit, pct_used, pct_remaining, used_count, total).
    Never raises; returns safe defaults on error.
    Result is cached for _QUOTA_CACHE_TTL seconds to avoid blocking the
    monitor hot path with repeated network calls.
    """
    global _quota_cache, _quota_cache_ts
    import time as _time
    now = _time.monotonic()
    if now - _quota_cache_ts < _QUOTA_CACHE_TTL:
        return _quota_cache
    if LLM_PROVIDER != "minimax":
        return False, 0.0, 100.0, 0, 4500
    if not MINIMAX_API_KEY:
        return False, 0.0, 100.0, 0, 4500
    try:
        import certifi
        import requests
        resp = requests.get(
            "https://www.minimax.io/v1/api/openplatform/coding_plan/remains",
            headers={"Authorization": f"Bearer {MINIMAX_API_KEY}"},
            timeout=10,
            verify=certifi.where(),  # use certifi CA bundle to avoid macOS SSL chain issues
        )
        if resp.status_code == 200:
            data = resp.json()
            model_remains = data.get("model_remains", [])
            # Find the "general" model row; fall back to first entry
            general = next((m for m in model_remains if m.get("model_name") == "general"), None)
            row = general or (model_remains[0] if model_remains else None)
            if row:
                # Prefer direct remaining_percent field (new API format)
                remaining_pct = row.get("current_interval_remaining_percent")
                total = row.get("current_interval_total_count", 0)
                usage_count = row.get("current_interval_usage_count", 0)
                if remaining_pct is not None:
                    pct_remaining = float(remaining_pct)
                    pct_used = 100.0 - pct_remaining
                    # Derive counts from percent when total is 0 (MiniMax new format)
                    if total > 0:
                        used = total - usage_count
                    else:
                        used = usage_count
                else:
                    # Legacy format: total/usage counts
                    used = total - usage_count if total > 0 else 0
                    pct_used = (used / total * 100) if total > 0 else 0.0
                    pct_remaining = 100.0 - pct_used
            else:
                pct_used, pct_remaining, used, total = 0.0, 100.0, 0, 4500
            result = pct_used >= QUOTA_LIMIT_PERCENT, pct_used, pct_remaining, used, total
            _quota_cache = result
            _quota_cache_ts = now
            return result
    except Exception as e:
        print(f"[Quota] check failed: {e}")
    return False, 0.0, 100.0, 0, 4500


def check_rate_limit_flags() -> Optional[str]:
    """
    Check for rate-limit flag files written by agent subprocesses.
    Returns the name of the provider that was rate-limited, or None.
    Deletes the flag after reading it.
    """
    try:
        for flag in DATA_DIR.glob("rate_limited_*.flag"):
            provider = flag.stem.replace("rate_limited_", "")
            flag.unlink(missing_ok=True)
            return provider
    except Exception:
        pass
    return None


def rotate_provider(rate_limited_provider: str, llm_providers: dict) -> Optional[str]:
    """
    Pick the next available provider from FALLBACK_PROVIDERS that isn't rate-limited.
    Returns the new provider name, or None if no fallback available.
    """
    if not FALLBACK_PROVIDERS:
        return None
    candidates = [p for p in FALLBACK_PROVIDERS if p != rate_limited_provider and p in llm_providers]
    if not candidates:
        return None
    # Prefer providers whose key is actually set
    for p in candidates:
        key_env = llm_providers[p].get("api_key_env", "")
        import os
        if key_env and os.environ.get(key_env):
            return p
    return candidates[0]


def auto_scale_step(recent_429_count: int) -> None:
    """
    Adjust MAX_ACTIVE_AGENTS dynamically based on observed 429 pressure.
    Called once per monitor cycle when AUTO_SCALE is enabled.

    Strategy:
    - Any 429s in the last 2 min → decrement by 1 (min cooldown: 60s between decrements)
    - Zero 429s for 3 consecutive cycles → increment by 1 (min cooldown: 120s between increments)
    - Never go below _auto_scale_floor or above AUTO_SCALE_CEILING
    """
    global MAX_ACTIVE_AGENTS, _auto_scale_current, _auto_scale_last_change, _auto_scale_clean_cycles

    if not AUTO_SCALE:
        return

    now = time.time()
    ceiling = AUTO_SCALE_CEILING

    # Clamp current value immediately if ceiling was lowered (e.g. user changed max agents)
    if _auto_scale_current > ceiling:
        _auto_scale_current = ceiling
        MAX_ACTIVE_AGENTS = _auto_scale_current
        print(f"[AutoScale] Ceiling lowered to {ceiling} -- clamping to {_auto_scale_current}")

    if recent_429_count > 0:
        _auto_scale_clean_cycles = 0
        # Decrement: at most once per 60s to avoid thrashing
        if now - _auto_scale_last_change >= 60 and _auto_scale_current > _auto_scale_floor:
            _auto_scale_current -= 1
            _auto_scale_last_change = now
            MAX_ACTIVE_AGENTS = _auto_scale_current
            print(f"[AutoScale] {recent_429_count} 429s -- reducing to {_auto_scale_current} agents")
    else:
        _auto_scale_clean_cycles += 1
        # Increment: only after 3 clean cycles AND at least 120s since last change
        if (_auto_scale_clean_cycles >= 3
                and now - _auto_scale_last_change >= 120
                and _auto_scale_current < ceiling):
            _auto_scale_current += 1
            _auto_scale_last_change = now
            MAX_ACTIVE_AGENTS = _auto_scale_current
            _auto_scale_clean_cycles = 0
            print(f"[AutoScale] Clean -- increasing to {_auto_scale_current} agents (ceiling={ceiling})")


# ---------------------------------------------------------------------------
# Fill slots (core auto-mode loop)
# ---------------------------------------------------------------------------

_circuit_breaker: dict = {
    "open": False,          # True = spawning paused
    "opened_at": 0.0,       # time.time() when tripped
    "reason": "",
    "cooldown": 60,         # seconds before auto-reset attempt
}
_INFRA_FAILURE_RATE_THRESHOLD = 0.5   # >50% of recent tasks are infra failures → trip
_INFRA_FAILURE_WINDOW = 10            # number of recent tasks to sample

# Auto-healer state — tracks repair attempts per service to prevent restart loops
_healer_state: dict = {
    "shrimp_router": {"attempts": 0, "last_attempt": 0.0},
    "headroom_8888": {"attempts": 0, "last_attempt": 0.0},
}
_HEALER_MAX_ATTEMPTS = 3
_HEALER_BACKOFF = [30, 60, 120]  # seconds between attempts

# Service definitions: port → how to check and restart
_SERVICES = {
    "shrimp_router": {
        "port": 8090,
        "check_url": "http://localhost:8090/health",
        "restart_cmd": "bash /Users/costas/workspace/shrimp-router/scripts/start-router.sh",
        "log_file": "/tmp/shrimp-router-autohealer.log",
    },
    "headroom_8888": {
        "port": 8888,
        "check_url": None,  # TCP check only
        "restart_cmd": (
            "/Users/costas/workspace/headroom-venv/bin/headroom proxy "
            "--port 8888 --mode cache --backend anthropic "
            "--anthropic-api-url https://api.minimax.io/anthropic "
            "--intercept-tool-results --no-telemetry "
            "--log-file /Users/costas/Documents/Projects/paraxenia/swarm-controller/data/headroom.log"
        ),
        "log_file": "/Users/costas/Documents/Projects/paraxenia/swarm-controller/data/headroom-server.log",
    },
}


def _check_service_port(port: int) -> bool:
    """TCP-level check: is something listening on localhost:port?"""
    try:
        with socket.create_connection(("localhost", port), timeout=3):
            return True
    except Exception:
        return False


def _try_auto_heal(reason: str) -> None:
    """Fire-and-forget: detect which services are down and restart them.

    Called in a background thread when the circuit breaker opens.
    Respects per-service attempt limits and backoff to avoid restart loops.
    """
    import subprocess
    import threading
    now = time.time()

    def _heal():
        for svc_name, svc in _SERVICES.items():
            state = _healer_state[svc_name]

            # Back off between attempts
            backoff = _HEALER_BACKOFF[min(state["attempts"], len(_HEALER_BACKOFF) - 1)]
            if state["attempts"] >= _HEALER_MAX_ATTEMPTS:
                print(f"[AutoHealer] {svc_name}: max attempts ({_HEALER_MAX_ATTEMPTS}) reached — giving up")
                continue
            if now - state["last_attempt"] < backoff:
                remaining = int(backoff - (now - state["last_attempt"]))
                print(f"[AutoHealer] {svc_name}: backoff — {remaining}s remaining before next attempt")
                continue

            # Check if actually down
            if _check_service_port(svc["port"]):
                print(f"[AutoHealer] {svc_name}: port {svc['port']} is up — skipping restart")
                state["attempts"] = 0  # reset counter since it recovered
                continue

            # It's down — attempt restart
            state["attempts"] += 1
            state["last_attempt"] = now
            print(f"[AutoHealer] {svc_name}: port {svc['port']} is DOWN — restart attempt {state['attempts']}/{_HEALER_MAX_ATTEMPTS}")

            try:
                log_path = svc.get("log_file", "/tmp/autohealer.log")
                with open(log_path, "a") as lf:
                    proc = subprocess.Popen(
                        svc["restart_cmd"],
                        shell=True,
                        stdout=lf,
                        stderr=lf,
                        start_new_session=True,
                    )
                print(f"[AutoHealer] {svc_name}: started pid {proc.pid}")

                # Wait briefly then verify
                time.sleep(5)
                if _check_service_port(svc["port"]):
                    print(f"[AutoHealer] {svc_name}: recovered (port {svc['port']} now responding)")
                    state["attempts"] = 0
                else:
                    print(f"[AutoHealer] {svc_name}: still down after restart attempt {state['attempts']}")
            except Exception as e:
                print(f"[AutoHealer] {svc_name}: restart failed: {e}")

    threading.Thread(target=_heal, daemon=True, name="auto-healer").start()


def _check_circuit_breaker() -> tuple[bool, str]:
    """Return (should_pause, reason). Checks router health + recent infra failure rate.

    Opens the breaker when:
      - The shrimp router is unreachable (all backends would fail immediately), OR
      - >50% of the last 10 completed/failed tasks were infrastructure failures.

    Auto-resets after cooldown_seconds if conditions clear.
    """
    cb = _circuit_breaker
    now = time.time()

    # Auto-reset after cooldown
    if cb["open"] and (now - cb["opened_at"]) > cb["cooldown"]:
        cb["open"] = False
        cb["reason"] = ""
        print("[CircuitBreaker] Cooldown elapsed — resetting, will re-evaluate")

    # Check shrimp router health (30s cached via api_agents)
    try:
        from swarm.api_agents import _check_shrimp_router  # type: ignore
        router = _check_shrimp_router()
        if router.get("ok") is False:
            reason = "shrimp-router unreachable"
            if not cb["open"]:
                cb["open"] = True
                cb["opened_at"] = now
                cb["reason"] = reason
                print(f"[CircuitBreaker] OPEN — {reason}")
                _try_auto_heal(reason)
            return True, reason
    except Exception:
        pass  # router check not available — skip

    # Check recent infra failure rate
    try:
        recent = db.task_get_recent_by_statuses(("completed", "failed"), limit=_INFRA_FAILURE_WINDOW)
        if len(recent) >= 3:
            infra_count = sum(
                1 for t in recent
                if (t.get("metadata") or {}).get("infrastructure_failure_count", 0) > 0
                or (t.get("metadata") or {}).get("last_failure", "").startswith("Infrastructure")
            )
            rate = infra_count / len(recent)
            if rate > _INFRA_FAILURE_RATE_THRESHOLD:
                reason = f"infra failure rate {rate:.0%} ({infra_count}/{len(recent)} recent tasks)"
                if not cb["open"]:
                    cb["open"] = True
                    cb["opened_at"] = now
                    cb["reason"] = reason
                    print(f"[CircuitBreaker] OPEN — {reason}")
                    _try_auto_heal(reason)
                return True, reason
    except Exception:
        pass

    # Conditions clear — ensure breaker is closed
    if cb["open"]:
        cb["open"] = False
        cb["reason"] = ""
        # Reset healer attempt counters so next incident gets a fresh set of retries
        for svc in _healer_state.values():
            svc["attempts"] = 0
        print("[CircuitBreaker] CLOSED — infra looks healthy")
    return False, ""


def _check_llm_connectivity() -> bool:
    """TCP-level reachability check for the active LLM provider. No quota consumed."""
    try:
        import swarm_runner as _runner
        provider_name = getattr(_runner, "LLM_PROVIDER", "minimax")
        providers = getattr(_runner, "LLM_PROVIDERS", {})
        base_url = (providers.get(provider_name) or {}).get("base_url", "https://api.minimax.io")
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        host = parsed.hostname or "api.minimax.io"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=5):
            return True
    except Exception:
        return False



def check_infra_freeze(db, config: dict) -> None:
    """Force-escalate pending tasks that are stuck in infrastructure failure loops.

    Tasks where _is_infrastructure_failure() fired repeatedly never consume an
    attempt (by design -- infra failures aren't charged). Without this check they
    can spin forever, starving the delivery queue. When a task accumulates
    INFRA_FREEZE_THRESHOLD consecutive infra failures, we charge one real attempt
    so it eventually exhausts and triggers the research-feeder path.
    """
    threshold = int(config.get("infra_freeze_threshold", constants.INFRA_FREEZE_THRESHOLD))
    try:
        pending_tasks = db.task_get_by_status("pending")
        _managed = set(MANAGED_PROJECTS) if MANAGED_PROJECTS else None
        tasks = [t for t in pending_tasks if _managed is None or t.get("project") in _managed]
        for t in tasks:
            meta = t.get("metadata") or {}
            if not meta.get("infrastructure_retry_pending"):
                continue
            count = int(meta.get("infrastructure_failure_count") or 0)
            if count < threshold:
                continue
            attempts = int(t.get("attempts") or 0)
            max_attempts = int(t.get("max_attempts") or 3)
            print(
                f"[Swarm] Infra freeze detected for {t['id']} "
                f"({count} consecutive infra failures) — charging attempt {attempts + 1}/{max_attempts}"
            )
            new_meta = dict(meta)
            new_meta["infrastructure_failure_count"] = 0
            new_meta["infrastructure_retry_pending"] = False
            new_meta["last_failure"] = (
                meta.get("last_infrastructure_failure", "")[-500:]
                or "Infrastructure freeze: forced attempt charge after repeated infra failures."
            )
            db.task_update(t["id"], {
                "attempts": attempts + 1,
                "metadata": new_meta,
            })
    except Exception as e:
        print(f"[Swarm] check_infra_freeze error: {e}")


def fill_slots(generate_script_fn, max_spawn: Optional[int] = None) -> Tuple[List[str], List[str]]:
    """
    Spawn agents until MAX_ACTIVE_AGENTS is reached or no tasks remain.
    Returns (spawned_ids, skipped_projects).
    A lock prevents concurrent fill_slots calls from picking the same task.
    """
    if not _check_llm_connectivity():
        print("[Swarm] LLM endpoint unreachable -- skipping fill_slots (will retry next cycle)")
        return [], []

    tripped, cb_reason = _check_circuit_breaker()
    if tripped:
        print(f"[Swarm] Circuit breaker OPEN ({cb_reason}) — skipping fill_slots")
        return [], []

    with _fill_slots_lock:
        spawned: List[str] = []
        skipped: List[str] = []
        limit = max_spawn if max_spawn is not None else 9999
        # Projects we already triggered idle closure verification for in this
        # call -- the bottom-of-function _run_idle_closure_verification_cycle
        # should skip them to avoid double-triggering the same project in one
        # fill_slots call.
        skipped_projects_for_idle_verification: set[str] = set()

        # Sprint cycle for auto_replan projects:
        #   queue empty → QA → bugs fixed → queue empty → planner → next sprint
        # Guard: skip sprint QA/planner spawning when over quota -- these tasks
        # consume LLM calls and queueing them when quota is exhausted just means
        # they'll start immediately on the next fill_slots cycle and burn remaining quota.
        _sprint_over_quota, *_ = check_quota_limit()
        _plan_candidates = set() if _sprint_over_quota else (set(AUTO_REPLAN_PROJECTS) - set(PAUSED_PROJECTS))
        if _sprint_over_quota:
            print("[Swarm] Sprint QA/planner spawn skipped -- over quota")
        if _plan_candidates:
            all_tasks = db.task_get_all(projects=list(_plan_candidates))
            projects_with_tasks = {t["project"] for t in all_tasks}
            for proj in _plan_candidates:
                if proj in projects_with_tasks:
                    continue  # still has work to do
                proj_path = WORKSPACE / proj
                if not (proj_path / "project.godot").exists():
                    continue
                if not (proj_path / "GAME_DESIGN.md").exists():
                    continue

                if proj not in _projects_sprint_qa_done:
                    # Queue empty, QA hasn't run yet -- spawn QA first
                    has_harness = (proj_path / "autoload" / "test_harness.gd").exists()
                    qa_type = "harness_qa" if has_harness else "qa"
                    qa_id = f"qa-sprint-{proj}-{int(time.time())}"
                    db.task_upsert({
                        "id": qa_id,
                        "project": proj,
                        "type": qa_type,
                        "description": (
                            "Sprint QA: run synchronous checkpoint tests against the game logic "
                            "to verify the completed sprint before planning the next one."
                            if has_harness else
                            "Sprint QA: playtest and verify the completed sprint is functional "
                            "before planning the next one."
                        ),
                        "priority": 90,
                        "status": "pending",
                        "dependencies": chain_to_project_head(db, proj, task_id=qa_id, ensure_head=True),
                        "metadata": {"sprint_qa": True},
                        "attempts": 0,
                        "max_attempts": 2,
                    })
                    print(f"[Swarm] Sprint complete for {proj} -- spawned QA before next sprint plan")
                else:
                    # QA done and any bugs fixed -- spawn the next sprint planner
                    _projects_sprint_qa_done.discard(proj)
                    plan_id = f"project-plan-{proj}-{int(time.time())}"
                    plan_deps = chain_to_project_head(db, proj, task_id=plan_id, ensure_head=True)
                    db.task_upsert({
                        "id": plan_id,
                        "project": proj,
                        "type": "project_plan",
                        "description": (
                            f"Plan the next sprint for {proj}. Read GAME_DESIGN.md and the "
                            f"current codebase state, identify the most valuable next sprint "
                            f"goal, and create 5-8 focused tasks with correct dependencies."
                        ),
                        "priority": 100,
                        "status": "pending",
                        "dependencies": plan_deps,
                        "metadata": {},
                        "attempts": 0,
                        "max_attempts": 2,
                    })
                    print(f"[Swarm] Sprint QA passed for {proj} -- spawned next sprint planner")

        # Cap per-cycle spawning to avoid burst after cooldown expiry.
        cycle_limit = min(limit, SPAWN_PER_CYCLE)
        _tried_task_ids: set = set()  # avoid retrying the same failing task in one cycle
        while len(spawned) < cycle_limit:
            if get_active_count() >= MAX_ACTIVE_AGENTS:
                break

            task = _get_next_task(exclude_ids=_tried_task_ids)
            if not task:
                break

            project = task["project"]

            # When the only ready task for a project is an expansion task blocked by
            # closure status (frozen/stalled with open regressions) and there is no
            # recovery/repair alternative in the queue, do NOT spawn the agent --
            # instead, opportunistically trigger closure verification for the project
            # so triage/qa can clear the gate. This implements the
            # "_fill_slots_triggers_idle_verification_when_only_frozen_expansion_remains"
            # behavior without re-introducing the e1801839 deadlock fix: we only short-
            # circuit here at the fill_slots caller level when the picked task itself
            # is expansion-blocked, leaving _get_next_task's "deadlock-free return
            # ready[0]" guarantee untouched.
            project_row = db.project_get(project)
            project_rows_block = {project: project_row} if project_row else {}
            if (
                project_row is not None
                and _is_expansion_blocked(task, project_rows_block)
            ):
                ready_peers = db.task_get_all(projects=[project])
                ready_peers = [t for t in ready_peers if t["status"] == "pending"]
                has_repair = any(
                    not _is_expansion_blocked(t, project_rows_block)
                    for t in ready_peers
                )
                if not has_repair:
                    print(
                        f"[Swarm] fill_slots: only blocked expansion task for "
                        f"{project} (closure_status={project_row.get('closure_status')}, "
                        f"open_regressions={project_row.get('open_regression_count')}); "
                        f"triggering idle closure verification"
                    )
                    try:
                        _validation.run_closure_verification(project, run_type="periodic")
                    except Exception as exc:
                        print(f"[Swarm] Idle closure verification failed for {project}: {exc}")
                    _tried_task_ids.add(task["id"])
                    skipped_projects_for_idle_verification.add(project)
                    break

            _tried_task_ids.add(task["id"])
            if LOCK_PROJECT:
                db.project_set_locked(project, True)

            agent_id = spawn_agent(task, generate_script_fn)
            if agent_id:
                spawned.append(agent_id)
            else:
                print(f"[Swarm] spawn_agent returned None for task {task['id'][:12]} project={project} -- skipping and trying next")
                if LOCK_PROJECT:
                    db.project_set_locked(project, False)
                skipped.append(project)
                # Do NOT break -- try the next available task

        # Meta agents: never fire when over quota -- they consume LLM calls
        # just like regular agents and make quota exhaustion worse.
        _over_quota, _pct_used, *_ = check_quota_limit()
        if not _over_quota:
            _fire_idle_librarian()
            if not spawned and get_active_count() == 0:
                _run_idle_closure_verification_cycle(skipped_projects_for_idle_verification)
                _fire_idle_gardener()
                _fire_weekly_auditor()
                _fire_idle_archaeologist()
                _fire_idle_scheduler()
        else:
            print(f"[Meta] Quota at {_pct_used:.1f}% -- skipping all meta agent triggers")
        return spawned, skipped


def _run_idle_closure_verification_cycle(projects_already_verified: set | None = None) -> list[str]:
    """Opportunistically verify projects when the controller is otherwise idle.

    This is intentionally conservative: it only touches managed, unpaused projects
    that either have never been verified or still have closure pressure
    (regressions / non-green status). The closure run guard layer is responsible
    for suppressing duplicate or over-frequent executions.
    """
    triggered: list[str] = []
    projects = db.project_get_all()
    for project_name, project_row in projects.items():
        if project_name in PAUSED_PROJECTS:
            continue
        if not project_row.get("managed", True):
            continue
        if project_row.get("locked"):
            continue
        if project_name in projects_already_verified:
            continue
        if (
            project_row.get("last_verification_at")
            and int(project_row.get("open_regression_count") or 0) <= 0
            and project_row.get("closure_status") == "green"
        ):
            continue
        try:
            run = _validation.run_closure_verification(project_name, run_type="periodic")
        except Exception as exc:
            print(f"[Swarm] Idle closure verification failed for {project_name}: {exc}")
            continue
        if run is not None:
            triggered.append(project_name)
    return triggered


def _fire_idle_gardener() -> None:
    """Fire the gardener agent when the system is idle and tasks keep failing.

    Triggers when:
    - gardener is enabled via config
    - no agents are currently running
    - max(last_failure_ts across all tasks) > LAST_GARDENER_RUN_TS

    This catches projects that have been failing repeatedly without gardener
    having run to prune stale knowledge and clean up dead code.
    """
    if not GARDENER_ENABLED:
        return
    if not META_MODE_ENABLED:
        return
    if get_active_count() > 0:
        return

    all_tasks = db.task_get_all()
    if not all_tasks:
        return

    # Find the most recent failure timestamp
    failure_ts = 0.0
    for t in all_tasks:
        if t.get("status") == "failed":
            # failure_ts stored in metadata or derived from updated_at
            meta = t.get("metadata") or {}
            fts = meta.get("failure_ts", 0.0)
            if fts and fts > failure_ts:
                failure_ts = fts

    # If there's no stored failure_ts, fall back to the updated_at field
    if not failure_ts:
        for t in all_tasks:
            if t.get("status") == "failed":
                ts_str = t.get("updated_at", "") or t.get("created_at", "")
                if ts_str:
                    try:
                        from datetime import datetime as _dt
                        fts = _dt.fromisoformat(ts_str).timestamp()
                        if fts > failure_ts:
                            failure_ts = fts
                    except Exception:
                        pass

    if failure_ts and failure_ts > LAST_GARDENER_RUN_TS:
        # Don't fire if a gardener task is already pending, in_progress, or failed
        already_running = any(
            t.get("type") == "gardener"
            and t.get("status") in ("pending", "in_progress", "failed")
            for t in all_tasks
        )
        if not already_running:
            task_id = f"gardener-{int(time.time())}"
            deps = chain_to_project_head(db, "swarm-controller", task_id=task_id, ensure_head=True)
            db.task_upsert({
                "id": task_id,
                "project": "swarm-controller",
                "type": "gardener",
                "description": (
                    "Run the gardener meta-agent. Survey all active projects in the swarm, "
                    "identify cross-project failure patterns, and create targeted fix tasks "
                    "where the same bug is affecting multiple projects. "
                    "Write findings to data/swarm_knowledge.jsonl and data/GARDENER_REPORT.md."
                ),
                "priority": 60,
                "status": "pending",
                "dependencies": deps,
                "metadata": {"auto_spawned": True, "idle_trigger": True},
                "attempts": 0,
                "max_attempts": 1,
            })
            print(f"[Gardener] Idle trigger fired -- created gardener task {task_id} "
                  f"(last failure at {failure_ts}, last gardener at {LAST_GARDENER_RUN_TS})")


def _fire_idle_librarian() -> None:
    """Fire the librarian agent after every N task completions.

    Triggers when:
    - librarian is enabled via config
    - no agents are currently running
    - LIBRARIAN_COMPLETION_COUNTER >= LIBRARIAN_TRIGGER_INTERVAL
    - META_MODE_ENABLED is True

    This runs the librarian periodically to catch recurring prompt quality
    issues before they compound across many tasks.
    """
    global LIBRARIAN_COMPLETION_COUNTER
    if not LIBRARIAN_ENABLED:
        return
    if not META_MODE_ENABLED:
        return
    if LIBRARIAN_COMPLETION_COUNTER < LIBRARIAN_TRIGGER_INTERVAL:
        return

    all_tasks = db.task_get_all()
    # Don't fire if a librarian task is already pending, in_progress, or failed
    already_running = any(
        t.get("type") == "librarian"
        and t.get("status") in ("pending", "in_progress", "failed")
        for t in all_tasks
    )
    if not already_running:
        LIBRARIAN_COMPLETION_COUNTER = 0
        task_id = f"librarian-{int(time.time())}"
        deps = chain_to_project_head(db, "swarm-controller", task_id=task_id, ensure_head=True)
        db.task_upsert({
            "id": task_id,
            "project": "swarm-controller",
            "type": "librarian",
            "description": (
                "Run the librarian meta-agent. Scan recent task failures from swarm.db, "
                "group recurring failure patterns by task type, identify instruction gaps "
                "in prompts/*.yaml, and create up to 3 refactor tasks on swarm-controller "
                "describing targeted prompt edits (before/after diff in description). "
                "Write findings to data/LIBRARIAN_REPORT.md."
            ),
            "priority": 60,
            "status": "pending",
            "dependencies": deps,
            "metadata": {"auto_spawned": True, "idle_trigger": True},
            "attempts": 0,
            "max_attempts": 1,
        })
        print(f"[Librarian] Idle trigger fired -- created librarian task {task_id} "
              f"(completion_counter={LIBRARIAN_COMPLETION_COUNTER}, "
              f"threshold={LIBRARIAN_TRIGGER_INTERVAL})")


_last_auditor_run_ts: float = 0.0
_last_scheduler_run_ts: float = 0.0


def _fire_weekly_auditor() -> None:
    """Fire the auditor agent on a weekly cadence (controlled by meta_auditor_interval_days).

    Triggers when:
    - meta_auditor is enabled via config
    - META_MODE_ENABLED is True
    - no agents are currently running
    - at least META_AUDITOR_INTERVAL_DAYS have passed since LAST_AUDITOR_RUN_TS

    This runs the auditor weekly to detect template drift, missing StateServer
    registration, GUT without tests, and structural anti-patterns across all
    managed Godot projects.
    """
    global _last_auditor_run_ts

    if not META_AUDITOR_ENABLED:
        return
    if not META_MODE_ENABLED:
        return
    if get_active_count() > 0:
        return

    interval_days = getattr(sys.modules.get('swarm.orchestrator', None),
                           'META_AUDITOR_INTERVAL_DAYS', 7) or 7
    interval_secs = max(interval_days * 86400, 86400)

    now = time.time()
    if now - _last_auditor_run_ts < interval_secs:
        return

    all_tasks = db.task_get_all()
    already_running = any(
        t.get("type") == "meta_auditor"
        and t.get("status") in ("pending", "in_progress", "failed")
        for t in all_tasks
    )
    if already_running:
        return

    task_id = f"meta-auditor-{int(now)}"
    deps = chain_to_project_head(db, "swarm-controller", task_id=task_id, ensure_head=True)
    managed = list(MANAGED_PROJECTS)
    db.task_upsert({
        "id": task_id,
        "project": "swarm-controller",
        "type": "meta_auditor",
        "description": (
            "Run the Auditor meta-agent. Survey all managed Godot projects for "
            "systemic issues: template drift in autoload/state_server.gd and "
            "test_harness.gd, missing StateServer registration in project.godot, "
            "GUT installed without tests, and structural anti-patterns. "
            "Create coordinated fix tasks (max 20) chained by project, and write "
            "findings to data/AUDIT_REPORT.md."
        ),
        "priority": 60,
        "status": "pending",
        "dependencies": deps,
        "metadata": {
            "auto_spawned": True,
            "managed_projects": managed,
            "idle_trigger": True,
        },
        "attempts": 0,
        "max_attempts": 1,
    })
    _last_auditor_run_ts = now
    print(f"[Auditor] Weekly trigger fired -- created meta_auditor task {task_id} "
          f"(managed projects: {len(managed)})")


def _fire_idle_scheduler() -> None:
    """Fire the scheduler agent on a periodic cadence (controlled by scheduler_interval_minutes).

    Triggers when:
    - scheduler is enabled via config
    - META_MODE_ENABLED is True
    - no agents are currently running
    - at least SCHEDULER_INTERVAL_MINUTES have passed since _last_scheduler_run_ts

    The scheduler observes task queue, agent slot usage, project priorities, and
    quota pressure -- then adjusts config for the orchestrator (pausing/unpausing
    projects, adjusting agent ceiling, scheduling expensive tasks for off-peak).
    """
    global _last_scheduler_run_ts

    if not SCHEDULER_ENABLED:
        return
    if not META_MODE_ENABLED:
        return
    if get_active_count() > 0:
        return

    interval_secs = SCHEDULER_INTERVAL_MINUTES * 60
    now = time.time()
    if now - _last_scheduler_run_ts < interval_secs:
        return

    all_tasks = db.task_get_all()
    already_running = any(
        t.get("type") == "meta_scheduler"
        and t.get("status") in ("pending", "in_progress", "failed")
        for t in all_tasks
    )
    if already_running:
        return

    task_id = f"meta-scheduler-{int(now)}"
    deps = chain_to_project_head(db, "swarm-controller", task_id=task_id, ensure_head=True)
    db.task_upsert({
        "id": task_id,
        "project": "swarm-controller",
        "type": "meta_scheduler",
        "description": (
            "Run the Scheduler meta-agent. Review agent distribution, task type "
            "breakdown, quota usage (GET /api/quota-limit), project health scores, "
            "time of day, and data/PROJECT_MAP.md. Make config adjustments: "
            "pause/unpause projects, adjust max_active_agents ceiling, set "
            "run_after on expensive tasks (research, harness_qa) for off-peak hours. "
            "Log each adjustment with reasoning to data/SCHEDULER_LOG.md. "
            "Never kill running agents."
        ),
        "priority": 50,
        "status": "pending",
        "dependencies": deps,
        "metadata": {
            "auto_spawned": True,
            "idle_trigger": True,
        },
        "attempts": 0,
        "max_attempts": 1,
    })
    _last_scheduler_run_ts = now
    print(f"[Scheduler] Periodic trigger fired -- created meta_scheduler task {task_id}")


_META_TASK_TYPES = frozenset({
    "librarian", "gardener", "meta_auditor", "meta_scheduler",
    "archaeologist", "cartographer",
})


def _get_next_task(exclude_ids: set | None = None) -> Optional[Dict]:
    """Select the next pending task with met dependencies, using TASK_SELECTION_STRATEGY."""
    db.backfill_completed_task_ids()
    # Scope to managed projects when set -- avoids scanning thousands of historical
    # tasks from dead experiment runs (old void-patrol-runN arms) on every cycle.
    _managed = list(MANAGED_PROJECTS) if MANAGED_PROJECTS else None
    all_tasks = db.task_get_all(projects=_managed)
    pending = [t for t in all_tasks if t["status"] == "pending"]

    # Meta agents must not run when over quota -- they consume LLM calls and
    # make quota exhaustion worse.  Filter them out of the candidate pool.
    over_quota, _pct, *_ = check_quota_limit()
    if over_quota:
        pending = [t for t in pending if t.get("type") not in _META_TASK_TYPES]
    # Completed tasks now stay in the tasks table (immutable history), so
    # completed_ids is derived directly from the live table.
    completed_ids = {t["id"] for t in all_tasks if t["status"] == "completed"}
    all_task_ids = {t["id"] for t in all_tasks}

    paused = set(PAUSED_PROJECTS)

    # Count in_progress vision QA tasks (type="qa").
    # Cap concurrent vision QA at 2 to allow some parallelism while preventing
    # a flood. harness_qa/hybrid_qa have no vision dependency and are not
    # restricted. Port collisions handled by dynamic allocation.
    qa_active_count = sum(
        1 for t in all_tasks
        if t["status"] == "in_progress" and t.get("type") == "qa"
    )
    _MAX_CONCURRENT_VISION_QA = 2

    # Collect worktree paths currently in use by active agents -- tasks that inherit
    # the same worktree must wait until the current occupant finishes.
    active_worktrees = {
        d["worktree_path"]
        for d in _get_active_handles().values()
        if d.get("worktree_path")
    }

    now_iso = datetime.now().isoformat()
    ready = []
    project_rows: Dict[str, Dict[str, Any]] = {}
    for t in pending:
        if t.get("type") == "phase_gate":
            continue
        # Guard against malformed rows (for example pending tasks carrying a stale
        # completed timestamp or agent_id after a restart). These should be repaired
        # by startup reconciliation, but they should never poison ready-task
        # selection if one slips through.
        if not can_task_accept_agent(t):
            continue
        proj = t["project"]
        if proj in paused:
            continue
        # Block self-modification unless explicitly enabled in config.
        # Prevents agents from accidentally breaking their own swarm instance.
        if proj == "swarm-controller" and not ALLOW_SELF_MODIFICATION:
            continue
        project_row = project_rows.get(proj)
        if project_row is None and proj:
            project_row = db.project_get(proj)
            if project_row is not None:
                project_rows[proj] = project_row
        if project_row and not project_row.get("managed", True) and t.get("type") not in ("manager", "project_create"):
            continue
        # Cap concurrent vision QA agents to avoid overloading mlx-vlm.
        if t.get("type") == "qa" and qa_active_count >= _MAX_CONCURRENT_VISION_QA:
            continue
        # Skip tasks scheduled for the future
        run_after = t.get("run_after")
        if run_after and run_after > now_iso:
            continue
        locked = db.project_get(proj)
        if locked and locked.get("locked"):
            continue
        # Skip tasks whose inherited worktree is already occupied by another active agent
        task_wt = (t.get("metadata") or {}).get("worktree_path")
        if task_wt and task_wt in active_worktrees:
            continue
        deps = t.get("dependencies", [])
        if exclude_ids and t["id"] in exclude_ids:
            continue
        if all(is_dependency_met(d, all_task_ids, completed_ids) for d in deps):
            ready.append(t)

    if not ready:
        return None

    _annotate_scheduler_blocks(ready, project_rows)
    _sort_by_strategy(ready, project_rows=project_rows)
    if not ready:
        return None

    # Block expansion tasks when project is frozen/stalled with open regressions.
    # The sort puts repair tasks first and expansion tasks last within each project.
    # If the top task is an expansion-blocked task but non-blocked alternatives exist,
    # pick the first non-blocked task. If ALL ready tasks are blocked, allow the
    # top task through to avoid deadlock (the closure policy "no deadlock" rule).
    if _is_expansion_blocked(ready[0], project_rows):
        non_blocked = [t for t in ready if not _is_expansion_blocked(t, project_rows)]
        if non_blocked:
            ready[:] = non_blocked
            return ready[0]
        return ready[0]  # All tasks are expansion-blocked -- allow top task through to avoid deadlock

    return ready[0]


# Task type priority used by refactor_first strategy
_TYPE_PRIORITY = {"refactor": 0, "bug": 1, "feature": 2, "polish": 3}


def _is_expansion_blocked(t: Dict, project_rows: Dict[str, Dict[str, Any]]) -> bool:
    """Return True if task is a feature/polish/refactor expansion blocked by frozen/stalled closure.

    Frozen/stalled projects with open regressions block expansion tasks unless:
    - The task is itself a recovery or repair task
    - There are zero open regressions (nothing concrete to repair)
    """
    project_row = project_rows.get(t.get("project") or "")
    if not project_row:
        return False
    closure_status = (project_row.get("closure_status") or "").strip().lower()
    if closure_status not in {"frozen", "stalled"}:
        return False
    open_regressions = int(project_row.get("open_regression_count") or 0)
    if open_regressions == 0:
        return False
    metadata = t.get("metadata") or {}
    if metadata.get("is_recovery_task") or metadata.get("is_closure_repair_task"):
        return False
    task_type = (t.get("type") or "").strip().lower()
    return task_type in {"feature", "polish", "refactor"}


def _closure_expansion_block(task: Dict, project_rows: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return metadata explaining why closure policy blocks this ready task."""
    project_name = task.get("project") or ""
    project_row = project_rows.get(project_name)
    if not project_row:
        return None
    if not _is_expansion_blocked(task, project_rows):
        return None
    return {
        "reason": "closure_expansion_gate",
        "project": project_name,
        "closure_status": (project_row.get("closure_status") or "").strip().lower(),
        "open_regression_count": int(project_row.get("open_regression_count") or 0),
        "message": (
            "Task is ready, but the project is frozen/stalled with open regressions. "
            "Expansion tasks wait until a repair/recovery task clears the closure gate, "
            "or this task is explicitly marked is_closure_repair_task."
        ),
    }


def _annotate_scheduler_blocks(ready_tasks: List[Dict], project_rows: Dict[str, Dict[str, Any]]) -> None:
    """Persist scheduler block reasons so pending work is not silently skipped."""
    for task in ready_tasks:
        metadata = dict(task.get("metadata") or {})
        current = metadata.get("scheduler_blocked")
        block = _closure_expansion_block(task, project_rows)
        if block:
            if current != block:
                metadata["scheduler_blocked"] = block
                db.task_update(task["id"], {"metadata": metadata})
                task["metadata"] = metadata
            continue

        if isinstance(current, dict) and current.get("reason") == "closure_expansion_gate":
            metadata.pop("scheduler_blocked", None)
            db.task_update(task["id"], {"metadata": metadata})
            task["metadata"] = metadata


def _sort_by_strategy(tasks: List[Dict], *, project_rows: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    """Sort tasks in-place according to TASK_SELECTION_STRATEGY.
    Conflict-resolution tasks (priority >= 200) always sort first regardless of strategy."""
    project_rows = project_rows or {}

    # Conflict resolution tasks jump to the front in all strategies
    def _conflict_first(t: Dict) -> int:
        return 0 if t.get("priority", 50) >= 200 else 1

    def _complexity_adjusted_priority(t: Dict) -> int:
        """Return effective priority with complexity adjustment.
        complex tasks get +5 so they're picked slightly earlier (more buffer time).
        simple tasks get -5 so they're deferred in favour of more demanding peers."""
        base = t.get("priority", 50)
        complexity = (t.get("metadata") or {}).get("complexity", "")
        if complexity == "complex":
            return base + 5
        if complexity == "simple":
            return base - 5
        return base

    def _closure_policy_rank(t: Dict) -> tuple[int, int]:
        project_row = project_rows.get(t.get("project") or "")
        if not project_row:
            return (1, 1)

        closure_status = (project_row.get("closure_status") or "").strip().lower()
        open_regressions = int(project_row.get("open_regression_count") or 0)
        unhealthy = closure_status in {"yellow", "red", "frozen", "stalled"} and open_regressions > 0
        if not unhealthy:
            return (1, 1)

        metadata = t.get("metadata") or {}
        task_type = (t.get("type") or "").strip().lower()
        is_closure_repair = bool(metadata.get("is_closure_repair_task"))
        is_repair_task = is_closure_repair or task_type in {"bug", "triage"}
        is_expansion_task = task_type in {"feature", "polish", "refactor"}
        if is_repair_task:
            return (0, 0)
        if is_expansion_task:
            return (2, 2)
        return (1, 1)

    def _expansion_blocked(t: Dict) -> bool:
        return _is_expansion_blocked(t, project_rows)

    if TASK_SELECTION_STRATEGY == "refactor_first":
        tasks.sort(key=lambda t: (
            _conflict_first(t),
            _closure_policy_rank(t),
            _TYPE_PRIORITY.get(t.get("type", ""), 99),
            -_complexity_adjusted_priority(t),
            t.get("created", ""),
        ))
    elif TASK_SELECTION_STRATEGY == "round_robin":
        tasks.sort(key=lambda t: (_conflict_first(t), _closure_policy_rank(t), t.get("project", ""), -_complexity_adjusted_priority(t)))
    elif TASK_SELECTION_STRATEGY == "least_recently_worked":
        tasks.sort(key=lambda t: (_conflict_first(t), _closure_policy_rank(t), t.get("created", ""), -_complexity_adjusted_priority(t)))
    else:  # "priority" (default) and "dependency_aware"
        tasks.sort(key=lambda t: (_conflict_first(t), _closure_policy_rank(t), -_complexity_adjusted_priority(t), t.get("created", "")))
    # Only filter out expansion tasks when non-blocked alternatives exist.
    # If ALL ready tasks are expansion-blocked the closure policy would create a
    # deadlock (bug tasks depend on blocked feature tasks → nothing can run).
    # In that case allow expansion tasks through so the chain can make progress.
    non_blocked = [task for task in tasks if not _expansion_blocked(task)]
    if non_blocked:
        tasks[:] = non_blocked
    # else: leave tasks as-is to avoid deadlock


# ---------------------------------------------------------------------------
# Auto-setup helpers
# ---------------------------------------------------------------------------

def _maybe_spawn_gut_setup(project_name: str, project_path: Path):
    """If this is a Godot project without GUT installed, spawn a setup task (once)."""
    if not (project_path / "project.godot").exists():
        return  # not a Godot project
    if (project_path / "addons" / "gut").exists():
        return  # GUT already installed

    task_id = f"setup-gut-{project_name}"
    if db.task_get(task_id):
        return  # already queued or done

    controller_root = Path(__file__).resolve().parent.parent
    install_instruction = (
        f"Run the controller cache installer from the swarm-controller repo:\n"
        f"  run_command(\"cd {str(controller_root)!r} && python3 - <<'PY'\\n"
        f"from pathlib import Path\\n"
        f"from swarm.godot_bootstrap import install_gut_into_project\\n"
        f"install_gut_into_project(Path({str(project_path)!r}))\\n"
        f"print('GUT installed')\\n"
        f"PY\")\n"
        f"  # This will download GUT {GUT_VERSION} once into the local cache if needed,\n"
        f"  # then copy it into {project_path}/addons/gut.\n"
    )

    task = {
        "id": task_id,
        "project": project_name,
        "type": "feature",
        "priority": 90,
        "description": (
            f"Install GUT (Godot Unit Testing) addon into {project_name}.\n\n"
            f"STEPS:\n"
            f"1. Create the addons directory:\n"
            f"   run_command(\"mkdir -p {project_path}/addons\")\n"
            f"2. {install_instruction}"
            f"3. Enable the GUT plugin in project.godot -- add these lines if not present:\n"
            f"   [editor_plugins]\n"
            f"   enabled=PackedStringArray(\"res://addons/gut/plugin.cfg\")\n"
            f"   Write the updated project.godot using write_file.\n"
            f"4. Create the tests/ directory with a placeholder:\n"
            f"   write_file(\"{project_path}/tests/.gitkeep\", \"\")\n"
            f"5. Run the script parse check to confirm GUT loads cleanly:\n"
            f"   (write and run _swarm_check.gd as per standard validation)\n"
            f"6. Commit and push: git_commit(\"Add GUT testing addon and tests/ directory\")"
        ),
        "status": "pending",
        "metadata": {"auto_setup": "gut"},
    }
    db.task_upsert(task)
    print(f"[Swarm] Auto-queued GUT setup task for {project_name}")


# ---------------------------------------------------------------------------
# Project registry update (scan file sizes + git commits)
# ---------------------------------------------------------------------------

def update_project_registry(file_extensions=(".gd",)):
    """Scan managed projects and refresh db."""
    for project_name in MANAGED_PROJECTS:
        project_path = WORKSPACE / project_name
        if not project_path.exists():
            continue

        # Upsert project record if missing
        if db.project_get(project_name) is None:
            db.project_upsert({"name": project_name, "status": "active"})

        # Scan files
        files = {}
        for ext in file_extensions:
            for fp in project_path.rglob(f"*{ext}"):
                if any(ig in fp.parts for ig in IGNORE_DIRS):
                    continue
                try:
                    lines = len(fp.read_text().splitlines())
                    files[str(fp.relative_to(project_path))] = lines
                except Exception:
                    pass

        # Git commits
        try:
            result = subprocess.run(
                ["git", "log", "--pretty=format:%h|%s|%ar", "-5"],
                cwd=str(project_path), capture_output=True, text=True, timeout=5,
            )
            commits = []
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    parts = line.split("|", 2)
                    if len(parts) == 3:
                        commits.append({"hash": parts[0], "message": parts[1], "age": parts[2]})
        except Exception:
            commits = []

        conn = db._connect()
        conn.execute(
            """UPDATE projects SET files=?, last_update=?, recent_commits=?,
               last_commit=?, last_commit_msg=? WHERE name=?""",
            (
                json.dumps(files),
                datetime.now().isoformat(),
                json.dumps(commits),
                commits[0]["hash"] if commits else None,
                commits[0]["message"] if commits else None,
                project_name,
            ),
        )
        conn.commit()

        # Auto-setup: spawn GUT install task for Godot projects missing it
        _maybe_spawn_gut_setup(project_name, project_path)


# ---------------------------------------------------------------------------
# Re-export worktree functions for backward compatibility
# ---------------------------------------------------------------------------

cleanup_orphaned_worktrees = worktree.cleanup_orphaned_worktrees
check_ghost_merge_tasks = worktree.check_ghost_merge_tasks

# Re-export validation functions for backward compatibility
_post_task_validation = _validation._post_task_validation
_post_task_validation_in_worktree = _validation._post_task_validation_in_worktree
_spawn_validation_bug_task = _validation._spawn_validation_bug_task
_llm_summarise_fix_attempt = _validation._llm_summarise_fix_attempt


# ---------------------------------------------------------------------------
# Re-export agent lifecycle functions
#
# Public orchestration API -- these are intentional re-exports that api.py,
# the monitor thread, and swarm_runner use.  They are NOT scheduled for removal.
# ---------------------------------------------------------------------------

spawn_agent                  = agent_lifecycle.spawn_agent
check_agent_status           = agent_lifecycle.check_agent_status
reconcile_agent_runtime_state = agent_lifecycle.reconcile_agent_runtime_state
cleanup_recovery_branches    = agent_lifecycle.cleanup_recovery_branches
get_active_count             = agent_lifecycle.get_active_count
prune_history                = agent_lifecycle.prune_history
check_dep_violations         = agent_lifecycle.check_dep_violations
_get_active_handles          = agent_lifecycle.get_active_handles

# ---------------------------------------------------------------------------
# Compatibility shims -- retire once callers are updated
#
# _finish_agent, _spawn_review_task, _handle_task_failure:
#   Internal pipeline helpers.  Callers outside agent_lifecycle should import
#   from swarm.agent_finish / swarm.agent_recovery directly.
#   Retirement: remove after any remaining test/API references are updated.
#
# _active_handles, _handle_lock:
#   Tests that need the handle registry should import from swarm.agent_lifecycle.
#   All first-party tests have been updated (test_lifecycle, test_fill_slots,
#   test_prune).  Remove these once no external caller reads them via orchestrator.
# ---------------------------------------------------------------------------

_finish_agent        = agent_lifecycle._finish_agent        # retire → swarm.agent_finish
_spawn_review_task   = agent_lifecycle._spawn_review_task   # retire → swarm.agent_recovery
_handle_task_failure = agent_lifecycle._handle_task_failure # retire → swarm.agent_recovery
_active_handles      = agent_lifecycle._active_handles      # retire → swarm.agent_lifecycle
_handle_lock         = agent_lifecycle._handle_lock         # retire → swarm.agent_lifecycle

_last_archaeologist_run_ts: float = 0.0


def _fire_idle_archaeologist() -> None:
    """Fire archaeologist tasks for projects that have stalled.

    Triggers when:
    - archaeologist is enabled via config
    - META_MODE_ENABLED is True
    - no agents are currently running
    - a project qualifies as stalled (one of the three criteria)
    - below max concurrent archaeologist tasks

    Projects qualify as stalled when:
    1. No successful task completion in >ARCHAEOLOGIST_STALL_THRESHOLD_HOURS
    2. All tasks are failed/cancelled and the queue is empty
    3. A recovery chain has >5 failed attempts
    """
    global _last_archaeologist_run_ts

    if not ARCHAEOLOGIST_ENABLED:
        return
    if not META_MODE_ENABLED:
        return
    if get_active_count() > 0:
        return

    all_tasks = db.task_get_all()
    # Count active archaeologist tasks (including failed -- don't re-trigger on failure)
    active_arch = [
        t for t in all_tasks
        if t.get("type") == "archaeologist"
        and t.get("status") in ("pending", "in_progress", "failed")
    ]
    if len(active_arch) >= ARCHAEOLOGIST_MAX_CONCURRENT:
        return

    # Find stalled projects
    stalled = _find_stalled_projects(all_tasks)
    if not stalled:
        return

    # Don't fire too frequently (at most once per 10 minutes per project)
    now = time.time()
    if now - _last_archaeologist_run_ts < 600:
        return

    for project_name, stall_reason in stalled[:ARCHAEOLOGIST_MAX_CONCURRENT - len(active_arch)]:
        # Skip if already running for this project
        already_running = any(
            (t.get("metadata") or {}).get("stalled_project") == project_name
            for t in active_arch
        )
        if already_running:
            continue

        task_id = f"archaeologist-{project_name}-{int(now)}"
        deps = chain_to_project_head(db, "swarm-controller", task_id=task_id, ensure_head=True)
        db.task_upsert({
            "id": task_id,
            "project": "swarm-controller",
            "type": "archaeologist",
            "description": (
                f"Run the Archaeologist meta-agent to diagnose why project '{project_name}' "
                f"has stalled and produce a recovery task DAG.\n\n"
                f"STALL REASON: {stall_reason}\n\n"
                f"Investigate the stalled project, read git history, assess code state, "
                f"write ARCHAEOLOGY_REPORT.md to the project root, and create a sequenced "
                f"recovery task DAG via create_tasks()."
            ),
            "priority": 55,
            "status": "pending",
            "dependencies": deps,
            "metadata": {
                "auto_spawned": True,
                "stalled_project": project_name,
                "stall_reason": stall_reason,
            },
            "attempts": 0,
            "max_attempts": 1,
        })
        print(f"[Archaeologist] Idle trigger fired -- created archaeologist task {task_id} "
              f"for '{project_name}' ({stall_reason})")
        active_arch.append({"id": task_id})  # prevent duplicate spawning in same cycle

    _last_archaeologist_run_ts = now


def _find_stalled_projects(all_tasks: list) -> list:
    """Return a list of (project_name, stall_reason) for projects that qualify as stalled.

    Stall criteria (any one):
    1. No successful completion in >ARCHAEOLOGIST_STALL_THRESHOLD_HOURS
    2. All tasks failed/cancelled and queue is empty (no pending tasks)
    3. Recovery chain with >5 failed attempts
    """
    if not all_tasks:
        return []

    # Index tasks by project
    from collections import defaultdict
    project_tasks: dict = defaultdict(list)
    for t in all_tasks:
        if t.get("project"):
            project_tasks[t["project"]].append(t)

    stalled: list = []
    now = time.time()
    threshold_secs = ARCHAEOLOGIST_STALL_THRESHOLD_HOURS * 3600
    managed = set(MANAGED_PROJECTS)

    for project_name, tasks in project_tasks.items():
        if project_name not in managed:
            continue

        # Skip if project is paused
        if project_name in set(PAUSED_PROJECTS):
            continue

        statuses = {t.get("status") for t in tasks}
        completed = [t for t in tasks if t.get("status") == "completed"]
        failed = [t for t in tasks if t.get("status") == "failed"]
        cancelled = [t for t in tasks if t.get("status") == "cancelled"]
        pending_or_active = [
            t for t in tasks
            if t.get("status") in ("pending", "in_progress")
        ]

        # Criterion 1: no successful completion in threshold window
        last_success_ts = 0.0
        for t in completed:
            completed_at = t.get("completed") or t.get("updated_at") or ""
            if completed_at:
                try:
                    from datetime import datetime as _dt
                    ts = _dt.fromisoformat(completed_at).timestamp()
                    if ts > last_success_ts:
                        last_success_ts = ts
                except Exception:
                    pass

        if last_success_ts > 0 and now - last_success_ts > threshold_secs:
            stalled.append((project_name, f"No successful completion in {ARCHAEOLOGIST_STALL_THRESHOLD_HOURS}h"))
            continue

        # Criterion 2: all tasks are failed/cancelled and no pending work
        if statuses.issubset({"failed", "cancelled"}) and not pending_or_active:
            stalled.append((project_name, "All tasks failed/cancelled, queue empty"))
            continue

        # Criterion 3: recovery chain with >5 failed attempts
        recovery_attempts = sum(t.get("attempts", 0) for t in tasks
                                 if (t.get("metadata") or {}).get("stall_recovery"))
        if recovery_attempts > 5:
            stalled.append((project_name, "Recovery chain exceeds 5 failed attempts"))
            continue

    return stalled
