"""Scheduler route handlers and periodic scheduling for the Swarm API."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict

from flask import jsonify, request

from swarm import db
from swarm.task_chains import chain_to_project_head

# Module-level globals -- wired from api.py via register_routes
config_ref: Dict = {}
_orchestrator_mod: Any = None

# Scheduler state
_scheduler_timer: threading.Timer | None = None
_scheduler_lock = threading.Lock()
_last_scheduler_run_ts: float = 0.0

# Default config values
DEFAULT_INTERVAL_MINUTES = 15
DEFAULT_ALLOW_PAUSE = True
DEFAULT_ALLOW_AGENT_CEILING_ADJUST = True
DEFAULT_OFF_PEAK_HOURS = [0, 6]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_ts() -> float:
    return time.time()


def _is_scheduler_running(config: Dict) -> bool:
    """Return True if a scheduler task is already pending or in_progress."""
    try:
        all_tasks = db.task_get_all()
        return any(
            t.get("type") == "scheduler"
            and t.get("status") in ("pending", "in_progress")
            for t in all_tasks
        )
    except Exception:
        return False


def _run_scheduler_task(config: Dict, data_dir: Path) -> str:
    """Create a scheduler task and return its task_id."""
    task_id = f"scheduler-{int(time.time())}"
    project = "swarm-controller"
    deps = chain_to_project_head(db, project, task_id=task_id)

    # Build context for the scheduler agent
    try:
        from swarm import orchestrator as _orch
        active = _orch.get_active_count()
        max_agents = _orch.MAX_ACTIVE_AGENTS
    except Exception:
        active = 0
        max_agents = 3

    over_limit, pct_used, *_ = _check_quota_limit()

    db.task_upsert({
        "id": task_id,
        "project": project,
        "type": "scheduler",
        "description": (
            "Run the Scheduler meta-agent. Read agent distribution via GET /api/agents, "
            "quota usage via GET /api/quota-limit, task breakdown via GET /api/tasks, "
            "and project health via GET /api/metrics. "
            "Decide whether to adjust max_active_agents ceiling, pause/unpause projects, "
            "or set run_after on expensive task types. "
            "NEVER kill running agents. NEVER pause all projects simultaneously. "
            "Write decision reasoning to data/SCHEDULER_LOG.md."
        ),
        "priority": 60,
        "status": "pending",
        "dependencies": deps,
        "metadata": {
            "auto_spawned": True,
            "active_agents": active,
            "max_agents": max_agents,
            "quota_percent": pct_used,
        },
        "attempts": 0,
        "max_attempts": 1,
    })

    config["_scheduler_last_run_ts"] = _current_ts()
    _persist_state(data_dir, {"_scheduler_last_run_ts": _current_ts()})
    return task_id


def _check_quota_limit():
    """Proxy to orchestrator.check_quota_limit() so api_scheduler doesn't need
    to replicate the quota-fetching logic."""
    try:
        from swarm import orchestrator as _orch
        return _orch.check_quota_limit()
    except Exception:
        return False, 0.0, 100.0, 0, 4500


def _persist_state(data_dir: Path, updates: Dict) -> None:
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


def _load_state(data_dir: Path) -> Dict:
    state_file = data_dir / "scheduler_state.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _persist_config(config: Dict, config_file: Path | None = None,
                      _config_write_lock: threading.Lock | None = None) -> None:
    """Persist scheduler config keys to config.json via the write lock."""
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


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def _schedule_scheduler(app: Any, config: Dict, data_dir: Path) -> None:
    """Schedule the next scheduler run based on scheduler_interval_minutes."""
    global _scheduler_timer, _last_scheduler_run_ts

    enabled = config.get("scheduler_enabled", False)
    if not enabled:
        return

    interval_minutes = config.get("scheduler_interval_minutes", DEFAULT_INTERVAL_MINUTES)
    try:
        interval_seconds = max(int(interval_minutes) * 60, 60)  # minimum 1 minute
    except (ValueError, TypeError):
        interval_seconds = DEFAULT_INTERVAL_MINUTES * 60

    def _fire():
        try:
            # Check META_MODE_ENABLED before creating a task
            meta_mode_on = False
            try:
                from swarm import orchestrator as _orch
                meta_mode_on = _orch.META_MODE_ENABLED
            except Exception:
                pass
            if not meta_mode_on:
                print("[Scheduler] META_MODE_ENABLED=False -- skipping scheduled run")
            elif _is_scheduler_running(config):
                print("[Scheduler] Scheduler task already running -- skipping")
            else:
                task_id = _run_scheduler_task(config, data_dir)
                print(f"[Scheduler] Scheduled run fired -- task created: {task_id}")
        except Exception as exc:
            print(f"[Scheduler] Scheduled run failed: {exc}")
        finally:
            # Reschedule
            with _scheduler_lock:
                global _scheduler_timer
                _scheduler_timer = threading.Timer(interval_seconds, _fire)
                _scheduler_timer.daemon = True
                _scheduler_timer.start()

    with _scheduler_lock:
        if _scheduler_timer is not None:
            _scheduler_timer.cancel()
        _scheduler_timer = threading.Timer(interval_seconds, _fire)
        _scheduler_timer.daemon = True
        _scheduler_timer.start()
    print(f"[Scheduler] Scheduler started -- interval {interval_seconds}s, enabled={enabled}")


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
               config_file: Path | None = None,
               _config_write_lock: threading.Lock | None = None,
               **kwargs) -> None:
    """Register scheduler routes on the Flask app."""
    global config_ref, _orchestrator_mod
    config_ref = config
    data_dir = kwargs.get("data_dir", Path("data"))

    # Load persisted last-run state
    state = _load_state(data_dir)
    config["_scheduler_last_run_ts"] = state.get("_scheduler_last_run_ts", 0.0)

    @app.route("/api/scheduler/status", methods=["GET"])
    def scheduler_status():
        """Return scheduler status: enabled, last_run_ts, adjustments_count."""
        last_ts = float(config.get("_scheduler_last_run_ts", 0.0))
        enabled = config.get("scheduler_enabled", False)
        return jsonify({
            "scheduler_enabled": enabled,
            "last_run_ts": last_ts,
        })

    @app.route("/api/scheduler/run", methods=["POST"])
    def scheduler_run():
        """Trigger a scheduler task immediately."""
        # Check META_MODE_ENABLED
        meta_mode_on = False
        try:
            from swarm import orchestrator as _orch
            meta_mode_on = _orch.META_MODE_ENABLED
        except Exception:
            pass
        if not meta_mode_on:
            return jsonify({
                "error": "META_MODE_ENABLED is False -- enable Meta Mode first",
            }), 409
        if _is_scheduler_running(config):
            return jsonify({
                "error": "Scheduler task already pending or in_progress",
            }), 409
        task_id = _run_scheduler_task(config, data_dir)
        print(f"[Scheduler] Manual run triggered -- task_id={task_id}")
        return jsonify({"task_id": task_id, "status": "created"})

    @app.route("/api/scheduler/config", methods=["GET"])
    def scheduler_config_get():
        """Return current scheduler configuration keys."""
        return jsonify({
            "scheduler_enabled": config.get("scheduler_enabled", False),
            "scheduler_interval_minutes": config.get(
                "scheduler_interval_minutes", DEFAULT_INTERVAL_MINUTES),
            "scheduler_allow_pause": config.get(
                "scheduler_allow_pause", DEFAULT_ALLOW_PAUSE),
            "scheduler_allow_agent_ceiling_adjust": config.get(
                "scheduler_allow_agent_ceiling_adjust", DEFAULT_ALLOW_AGENT_CEILING_ADJUST),
            "scheduler_off_peak_hours": config.get(
                "scheduler_off_peak_hours", DEFAULT_OFF_PEAK_HOURS),
        })

    @app.route("/api/scheduler/config", methods=["POST"])
    def scheduler_config_set():
        """Update scheduler configuration keys and persist to config.json."""
        data = request.json or {}
        for key in ("scheduler_enabled", "scheduler_interval_minutes",
                    "scheduler_allow_pause", "scheduler_allow_agent_ceiling_adjust",
                    "scheduler_off_peak_hours"):
            if key in data:
                config[key] = data[key]

        # Sync to orchestrator module globals
        _sync_scheduler_globals(config)

        # Persist to config.json
        _persist_config(config, config_file, _config_write_lock)

        # Restart scheduler if enabled changed or interval changed
        _schedule_scheduler(app, config, data_dir)

        return jsonify({
            "scheduler_enabled": config.get("scheduler_enabled", False),
            "scheduler_interval_minutes": config.get(
                "scheduler_interval_minutes", DEFAULT_INTERVAL_MINUTES),
            "scheduler_allow_pause": config.get(
                "scheduler_allow_pause", DEFAULT_ALLOW_PAUSE),
            "scheduler_allow_agent_ceiling_adjust": config.get(
                "scheduler_allow_agent_ceiling_adjust", DEFAULT_ALLOW_AGENT_CEILING_ADJUST),
            "scheduler_off_peak_hours": config.get(
                "scheduler_off_peak_hours", DEFAULT_OFF_PEAK_HOURS),
        })

    # Start the scheduler after routes are registered
    _schedule_scheduler(app, config, data_dir)


def _sync_scheduler_globals(config: Dict) -> None:
    """Sync scheduler config values to the orchestrator module."""
    try:
        from swarm import orchestrator as _orch
        _orch.SCHEDULER_ENABLED = config.get("scheduler_enabled", False)
        _orch.SCHEDULER_INTERVAL_MINUTES = config.get(
            "scheduler_interval_minutes", DEFAULT_INTERVAL_MINUTES)
        _orch.SCHEDULER_ALLOW_PAUSE = config.get(
            "scheduler_allow_pause", DEFAULT_ALLOW_PAUSE)
        _orch.SCHEDULER_ALLOW_AGENT_CEILING_ADJUST = config.get(
            "scheduler_allow_agent_ceiling_adjust", DEFAULT_ALLOW_AGENT_CEILING_ADJUST)
        _orch.SCHEDULER_OFF_PEAK_HOURS = config.get(
            "scheduler_off_peak_hours", DEFAULT_OFF_PEAK_HOURS)
    except Exception:
        pass
