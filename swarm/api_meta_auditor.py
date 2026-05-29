"""Auditor route handlers and scheduling for the Swarm API."""

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
_last_auditor_run_ts: float = 0.0

# Default: weekly
DEFAULT_INTERVAL_SECS = 7 * 24 * 3600

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_ts() -> float:
    return time.time()


def _last_run_ts() -> float:
    return float(config_ref.get("_meta_auditor_last_run_ts", 0.0))


def _set_last_run_ts(ts: float) -> None:
    config_ref["_meta_auditor_last_run_ts"] = ts


def _get_report(data_dir: Path) -> str | None:
    report_path = data_dir / "AUDIT_REPORT.md"
    if report_path.exists():
        try:
            return report_path.read_text(encoding="utf-8")
        except Exception:
            pass
    return None


def _run_auditor_task() -> str:
    """Create a meta_auditor task and return its task_id."""
    task_id = f"meta-auditor-{int(time.time())}"
    project = "swarm-controller"
    deps = chain_to_project_head(db, project, task_id=task_id)

    # Fetch managed projects from orchestrator
    managed_projects = []
    try:
        from swarm import orchestrator as _orch
        managed_projects = list(getattr(_orch, "MANAGED_PROJECTS", []))
    except Exception:
        pass

    db.task_upsert({
        "id": task_id,
        "project": project,
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
            "managed_projects": managed_projects,
        },
        "attempts": 0,
        "max_attempts": 1,
    })
    ts = _current_ts()
    _set_last_run_ts(ts)
    _persist_state({"_meta_auditor_last_run_ts": ts})
    print(f"[Auditor] Task created: {task_id} (managed projects: {len(managed_projects)})")
    return task_id


def _persist_state(updates: Dict) -> None:
    if app_ref is None:
        return
    try:
        data_dir = Path(app_ref.config["DATA_DIR"])
    except Exception:
        data_dir = Path(__file__).parent.parent / "data"
    state_file = data_dir / "auditor_state.json"
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

def _schedule_auditor() -> None:
    """Schedule the next auditor run based on meta_auditor_interval_days."""
    global _scheduler_timer, _last_auditor_run_ts

    if not config_ref.get("meta_auditor_enabled", False):
        return

    interval = config_ref.get("meta_auditor_interval_days", 7) * 86400
    interval = max(interval, 86400)  # minimum 1 day

    def _fire():
        try:
            _run_auditor_task()
            print(f"[Auditor] Scheduled run fired at {datetime.now(timezone.utc).isoformat()}")
        except Exception as exc:
            print(f"[Auditor] Scheduled run failed: {exc}")
        finally:
            with _scheduler_lock:
                global _scheduler_timer
                _schedule_auditor()  # reschedule

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
    """Register auditor routes on the Flask app."""
    global app_ref, config_ref, _orchestrator_mod

    app_ref = app
    config_ref = config

    # Load persisted last-run state
    try:
        data_dir = Path(app.config["DATA_DIR"])
    except Exception:
        data_dir = Path(__file__).parent.parent / "data"
    state_file = data_dir / "auditor_state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            config["_meta_auditor_last_run_ts"] = state.get("_meta_auditor_last_run_ts", 0.0)
        except Exception:
            pass

    @app.route("/api/meta-auditor/status", methods=["GET"])
    def meta_auditor_status():
        """Return auditor status: last_run_ts, last_report, enabled, interval_days."""
        last_ts = _last_run_ts()
        report = _get_report(data_dir)
        enabled = config_ref.get("meta_auditor_enabled", False)
        interval_days = config_ref.get("meta_auditor_interval_days", 7)
        max_tasks = config_ref.get("meta_auditor_max_tasks", 20)
        return jsonify({
            "last_run_ts": last_ts,
            "last_report": report,
            "enabled": enabled,
            "interval_days": interval_days,
            "max_tasks": max_tasks,
        })

    @app.route("/api/meta-auditor/run", methods=["POST"])
    def run_meta_auditor():
        """Trigger an auditor task immediately."""
        # Check META_MODE_ENABLED before creating
        try:
            from swarm import orchestrator as _orch
            if not getattr(_orch, "META_MODE_ENABLED", False):
                return jsonify({
                    "error": "meta_mode_enabled is false -- auditor is disabled. "
                             "Enable meta mode via POST /api/meta-mode first."
                }), 400
        except Exception:
            pass

        task_id = _run_auditor_task()
        print(f"[Auditor] Manual run triggered -- task_id={task_id}")
        return jsonify({"task_id": task_id, "status": "created"})

    @app.route("/api/meta-auditor/config", methods=["GET"])
    def meta_auditor_config_get():
        """Return current auditor configuration."""
        return jsonify({
            "meta_auditor_enabled": config_ref.get("meta_auditor_enabled", False),
            "meta_auditor_interval_days": config_ref.get("meta_auditor_interval_days", 7),
            "meta_auditor_max_tasks": config_ref.get("meta_auditor_max_tasks", 20),
        })

    @app.route("/api/meta-auditor/config", methods=["POST"])
    def meta_auditor_config_set():
        """Update auditor configuration and persist to config.json."""
        data = request.json or {}
        config_keys = (
            "meta_auditor_enabled",
            "meta_auditor_interval_days",
            "meta_auditor_max_tasks",
        )
        for key in config_keys:
            if key in data:
                config_ref[key] = data[key]

        # Sync to orchestrator module globals
        _sync_auditor_globals(config_ref)

        # Sync meta mode globals
        _sync_meta_mode_globals(config_ref.get("meta_auditor_enabled", False))

        # Persist to config.json
        _persist_config(config_ref, config_file, _config_write_lock)

        # Restart scheduler if enabled changed
        if "meta_auditor_enabled" in data:
            if data["meta_auditor_enabled"]:
                _schedule_auditor()
            else:
                _stop_scheduler()

        return jsonify({
            "meta_auditor_enabled": config_ref.get("meta_auditor_enabled", False),
            "meta_auditor_interval_days": config_ref.get("meta_auditor_interval_days", 7),
            "meta_auditor_max_tasks": config_ref.get("meta_auditor_max_tasks", 20),
        })

    # Start the scheduler after routes are registered
    if config_ref.get("meta_auditor_enabled", False):
        _schedule_auditor()


def _sync_auditor_globals(config: Dict) -> None:
    """Sync auditor config values to the orchestrator module."""
    try:
        from swarm import orchestrator as _orch
        _orch.META_AUDITOR_ENABLED = config.get("meta_auditor_enabled", False)
        _orch.META_AUDITOR_MAX_TASKS = config.get("meta_auditor_max_tasks", 20)
    except Exception:
        pass


def _sync_meta_mode_globals(auditor_enabled: bool) -> None:
    """Sync meta_mode_enabled to the orchestrator module (called from POST config)."""
    try:
        from swarm import orchestrator as _orch
        _orch.META_MODE_ENABLED = auditor_enabled
    except Exception:
        pass


def _persist_config(config: Dict, config_file: Any,
                    _config_write_lock: threading.Lock | None = None) -> None:
    """Persist auditor keys to config.json via the write lock."""
    if config_file is None or _config_write_lock is None:
        return
    with _config_write_lock:
        try:
            cfg = {}
            if config_file.exists():
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
            for key in ("meta_auditor_enabled", "meta_auditor_interval_days",
                        "meta_auditor_max_tasks"):
                if key in config:
                    cfg[key] = config[key]
            config_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
