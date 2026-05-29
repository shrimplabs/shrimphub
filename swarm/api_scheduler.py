"""Scheduler route handlers and scheduling for the Swarm API."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from flask import jsonify, request

from swarm import db
from swarm.task_chains import chain_to_project_head

# Module-level globals -- wired from api.py via register_routes
app_ref: Any = None
config_ref: Dict = {}
_orchestrator_mod: Any = None

# Scheduler state
_scheduler_timer: threading.Timer | None = None
_scheduler_lock = threading.Lock()
_last_scheduler_run_ts: float = 0.0

# Default: every 15 minutes
DEFAULT_INTERVAL_SECS = 15 * 60

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_ts() -> float:
    return time.time()


def _last_run_ts() -> float:
    return float(config_ref.get("_scheduler_last_run_ts", 0.0))


def _set_last_run_ts(ts: float) -> None:
    config_ref["_scheduler_last_run_ts"] = ts


def _is_off_peak() -> bool:
    """Check if current hour is in off-peak hours."""
    now = datetime.now(timezone.utc)
    current_hour = now.hour
    off_peak_hours = config_ref.get("scheduler_off_peak_hours", [0, 1, 2, 3, 4, 5, 6])
    return current_hour in off_peak_hours


def _get_log(data_dir: Path) -> str | None:
    log_path = data_dir / "SCHEDULER_LOG.md"
    if log_path.exists():
        try:
            return log_path.read_text(encoding="utf-8")
        except Exception:
            pass
    return None


def _run_scheduler_task() -> str:
    """Create a scheduler task and return its task_id."""
    task_id = f"meta-scheduler-{int(time.time())}"
    project = "swarm-controller"
    deps = chain_to_project_head(db, project, task_id=task_id)

    # Fetch current state from orchestrator
    try:
        from swarm import orchestrator as _orch
        active_agents = getattr(_orch, "ACTIVE_AGENTS", [])
        max_agents = getattr(_orch, "MAX_ACTIVE_AGENTS", 5)
    except Exception:
        active_agents = []
        max_agents = 5

    # Get task type breakdown from DB
    task_type_breakdown = {}
    try:
        for task in db.task_get_all():
            if task.get("status") == "in_progress":
                ttype = task.get("type", "unknown")
                task_type_breakdown[ttype] = task_type_breakdown.get(ttype, 0) + 1
    except Exception:
        pass

    db.task_upsert({
        "id": task_id,
        "project": project,
        "type": "scheduler",
        "description": (
            "Run the Scheduler meta-agent. Assess current load: active agent count "
            "(active_agents=ACTIVE_COUNT max_agents=MAX_COUNT), queue depth per project, "
            "quota pressure, and task type distribution. Identify overloaded conditions "
            "(QUOTA_HIGH QA_HEAVY QUEUE_BUILDUP LOW_HEADROOM) and underserved projects "
            "(healthy no active agent 2 or more pending). Apply minimal adjustments via "
            "internal API (pause/unpause projects adjust agent ceiling set run_after on "
            "expensive tasks). Never kill active agents. Write decisions to "
            "data/SCHEDULER_LOG.md."
        ),
        "priority": 60,
        "status": "pending",
        "dependencies": deps,
        "metadata": {
            "auto_spawned": True,
            "active_agents": len(active_agents),
            "max_agents": max_agents,
            "task_type_breakdown": task_type_breakdown,
        },
        "attempts": 0,
        "max_attempts": 1,
    })
    ts = _current_ts()
    _set_last_run_ts(ts)
    _persist_state({"_scheduler_last_run_ts": ts})
    print(f"[Scheduler] Task created: {task_id}")
    return task_id


def _persist_state(updates: Dict) -> None:
    if app_ref is None:
        return
    try:
        data_dir = Path(app_ref.config["DATA_DIR"])
    except Exception:
        data_dir = Path(__file__).parent.parent / "data"
    state_file = data_dir / "scheduler_state.json"
    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    state.update(updates)
    try:
        state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def _schedule_scheduler() -> None:
    """Schedule the next scheduler run based on scheduler_interval_minutes."""
    global _scheduler_timer, _last_scheduler_run_ts

    if not config_ref.get("scheduler_enabled", False):
        return

    interval = config_ref.get("scheduler_interval_minutes", 15) * 60
    interval = max(interval, 60)  # minimum 1 minute

    def _fire():
        try:
            _run_scheduler_task()
            print(f"[Scheduler] Scheduled run fired at {datetime.now(timezone.utc).isoformat()}")
        except Exception as exc:
            print(f"[Scheduler] Scheduled run failed: {exc}")
        finally:
            with _scheduler_lock:
                global _scheduler_timer
                _schedule_scheduler()  # reschedule

    with _scheduler_lock:
        if _scheduler_timer is not None:
            _scheduler_timer.cancel()
        _scheduler_timer = threading.Timer(interval, _fire)
        _scheduler_timer.daemon = True
        _scheduler_timer.start()


def _stop_scheduler() -> None:
    global _scheduler_timer
    with _scheduler_lock:
        if _scheduler_timer is not None:
            _scheduler_timer.cancel()
            _scheduler_timer = None


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register_routes(app, config: Dict,
               config_file: Any = None,
               _config_write_lock: threading.Lock | None = None) -> None:
    """Register scheduler routes on the Flask app."""
    global app_ref, config_ref, _orchestrator_mod

    app_ref = app
    config_ref = config

    # Load persisted last-run state
    try:
        data_dir = Path(app.config["DATA_DIR"])
    except Exception:
        data_dir = Path(__file__).parent.parent / "data"
    state_file = data_dir / "scheduler_state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            config["_scheduler_last_run_ts"] = state.get("_scheduler_last_run_ts", 0.0)
        except Exception:
            pass

    @app.route("/api/scheduler/status", methods=["GET"])
    def scheduler_status():
        """Return scheduler status: last_run_ts last_log enabled interval_minutes."""
        last_ts = _last_run_ts()
        log = _get_log(data_dir)
        enabled = config_ref.get("scheduler_enabled", False)
        interval = config_ref.get("scheduler_interval_minutes", 15)
        allow_pause = config_ref.get("scheduler_allow_pause", True)
        allow_ceiling = config_ref.get("scheduler_allow_agent_ceiling_adjust", True)
        off_peak = config_ref.get("scheduler_off_peak_hours", [0, 1, 2, 3, 4, 5, 6])
        # Current active agents
        try:
            from swarm import orchestrator as _orch
            active_count = len(getattr(_orch, "ACTIVE_AGENTS", []))
            max_agents = getattr(_orch, "MAX_ACTIVE_AGENTS", 5)
        except Exception:
            active_count = 0
            max_agents = 5
        return jsonify({
            "last_run_ts": last_ts,
            "last_log": log,
            "enabled": enabled,
            "interval_minutes": interval,
            "allow_pause": allow_pause,
            "allow_agent_ceiling_adjust": allow_ceiling,
            "off_peak_hours": off_peak,
            "active_agents": active_count,
            "max_agents": max_agents,
            "is_off_peak": _is_off_peak(),
        })

    @app.route("/api/scheduler/run", methods=["POST"])
    def run_scheduler():
        """Trigger a scheduler task immediately."""
        # Check META_MODE_ENABLED before creating
        try:
            from swarm import orchestrator as _orch
            if not getattr(_orch, "META_MODE_ENABLED", False):
                return jsonify({
                    "error": "meta_mode_enabled is false -- scheduler is disabled. "
                             "Enable meta mode via POST /api/meta-mode first."
                }), 400
        except Exception:
            pass

        task_id = _run_scheduler_task()
        print(f"[Scheduler] Manual run triggered -- task_id={task_id}")
        return jsonify({"task_id": task_id, "status": "created"})

    @app.route("/api/scheduler/config", methods=["GET"])
    def scheduler_config_get():
        """Return current scheduler configuration."""
        return jsonify({
            "scheduler_enabled": config_ref.get("scheduler_enabled", False),
            "scheduler_interval_minutes": config_ref.get("scheduler_interval_minutes", 15),
            "scheduler_allow_pause": config_ref.get("scheduler_allow_pause", True),
            "scheduler_allow_agent_ceiling_adjust": config_ref.get("scheduler_allow_agent_ceiling_adjust", True),
            "scheduler_off_peak_hours": config_ref.get("scheduler_off_peak_hours", [0, 1, 2, 3, 4, 5, 6]),
        })

    @app.route("/api/scheduler/config", methods=["POST"])
    def scheduler_config_set():
        """Update scheduler configuration and persist to config.json."""
        data = request.json or {}
        config_keys = (
            "scheduler_enabled",
            "scheduler_interval_minutes",
            "scheduler_allow_pause",
            "scheduler_allow_agent_ceiling_adjust",
            "scheduler_off_peak_hours",
        )
        for key in config_keys:
            if key in data:
                config_ref[key] = data[key]

        # Sync to orchestrator module globals
        _sync_scheduler_globals(config_ref)

        # Sync meta mode globals
        _sync_meta_mode_globals(config_ref.get("scheduler_enabled", False))

        # Persist to config.json
        _persist_config(config_ref, config_file, _config_write_lock)

        # Restart scheduler if enabled changed
        if "scheduler_enabled" in data:
            if data["scheduler_enabled"]:
                _schedule_scheduler()
            else:
                _stop_scheduler()

        return jsonify({
            "scheduler_enabled": config_ref.get("scheduler_enabled", False),
            "scheduler_interval_minutes": config_ref.get("scheduler_interval_minutes", 15),
            "scheduler_allow_pause": config_ref.get("scheduler_allow_pause", True),
            "scheduler_allow_agent_ceiling_adjust": config_ref.get("scheduler_allow_agent_ceiling_adjust", True),
            "scheduler_off_peak_hours": config_ref.get("scheduler_off_peak_hours", [0, 1, 2, 3, 4, 5, 6]),
        })

    # Start the scheduler after routes are registered
    if config_ref.get("scheduler_enabled", False):
        _schedule_scheduler()


def _sync_scheduler_globals(config: Dict) -> None:
    """Sync scheduler config values to the orchestrator module."""
    try:
        from swarm import orchestrator as _orch
        _orch.SCHEDULER_ENABLED = config.get("scheduler_enabled", False)
        _orch.SCHEDULER_INTERVAL_MINUTES = config.get("scheduler_interval_minutes", 15)
        _orch.SCHEDULER_ALLOW_PAUSE = config.get("scheduler_allow_pause", True)
        _orch.SCHEDULER_ALLOW_AGENT_CEILING_ADJUST = config.get("scheduler_allow_agent_ceiling_adjust", True)
        _orch.SCHEDULER_OFF_PEAK_HOURS = config.get("scheduler_off_peak_hours", [0, 1, 2, 3, 4, 5, 6])
    except Exception:
        pass


def _sync_meta_mode_globals(scheduler_enabled: bool) -> None:
    """Sync meta_mode_enabled to the orchestrator module (called from POST config)."""
    try:
        from swarm import orchestrator as _orch
        _orch.META_MODE_ENABLED = scheduler_enabled
    except Exception:
        pass


def _persist_config(config: Dict, config_file: Any,
                    _config_write_lock: threading.Lock | None = None) -> None:
    """Persist scheduler keys to config.json via the write lock."""
    if config_file is None or _config_write_lock is None:
        return
    with _config_write_lock:
        try:
            cfg = {}
            if config_file.exists():
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
            for key in ("scheduler_enabled", "scheduler_interval_minutes",
                        "scheduler_allow_pause", "scheduler_allow_agent_ceiling_adjust",
                        "scheduler_off_peak_hours"):
                if key in config:
                    cfg[key] = config[key]
            config_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
