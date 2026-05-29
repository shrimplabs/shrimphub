"""Cartographer route handlers and scheduling for the Swarm API."""

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
# Scheduler state
_scheduler_timer: threading.Timer | None = None
_scheduler_lock = threading.Lock()
# Default: every 2 hours
DEFAULT_INTERVAL_SECS = 2 * 3600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_ts() -> float:
    return time.time()


def _last_run_ts() -> float:
    return float(config_ref.get("_cartographer_last_run_ts", 0.0))


def _set_last_run_ts(ts: float) -> None:
    config_ref["_cartographer_last_run_ts"] = ts


def _get_report(data_dir: Path) -> str | None:
    report_path = data_dir / "PROJECT_MAP.md"
    if report_path.exists():
        try:
            text = report_path.read_text(encoding="utf-8")
            return text[:500] if text else None
        except Exception:
            pass
    return None


def _run_cartographer_task() -> str:
    """Create a cartographer task and return its task_id."""
    task_id = f"cartographer-{int(time.time())}"
    project = "swarm-controller"
    deps = chain_to_project_head(db, project, task_id=task_id)
    db.task_upsert({
        "id": task_id,
        "project": project,
        "type": "cartographer",
        "description": (
            "Run the Cartographer meta-agent. Survey managed projects in the swarm, "
            "collect health signals (task queue state, agent activity, health scores, "
            "recent commit dates), and cross-reference known patterns from"
            "data/swarm_knowledge.jsonl, and write the narrative PROJECT_MAP.md and "
            "machine-readable SWARM_SUMMARY.json to the swarm data directory. "
            "This is a READ-ONLY survey -- do not edit any project files."
        ),
        "priority": 60,
        "status": "pending",
        "dependencies": deps,
        "metadata": {"auto_spawned": True},
        "attempts": 0,
        "max_attempts": 1,
    })
    ts = _current_ts()
    _set_last_run_ts(ts)
    _persist_state({"_cartographer_last_run_ts": ts})
    print(f"[Cartographer] Task created: {task_id}")
    return task_id


def _persist_state(updates: Dict) -> None:
    if app_ref is None:
        return
    try:
        data_dir = Path(app_ref.config["DATA_DIR"])
    except Exception:
        data_dir = Path(__file__).parent.parent / "data"
    state_file = data_dir / "cartographer_state.json"
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

def _schedule_cartographer() -> None:
    """Schedule the next cartographer run."""
    global _scheduler_timer

    if not config_ref.get("cartographer_enabled", False):
        return

    interval = config_ref.get("cartographer_interval_hours", 2) * 3600
    interval = max(interval, 300)  # minimum 5 minutes


    def _fire():
        try:
            _run_cartographer_task()
            fired = datetime.now(timezone.utc).isoformat()
            print(f"[Cartographer] Scheduled run fired at {fired}")
        except Exception as exc:
            print(f"[Cartographer] Scheduled run failed: {exc}")
        finally:
            with _scheduler_lock:
                global _scheduler_timer
                _schedule_cartographer()  # reschedule

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
    """Register cartographer routes on the Flask app."""
    global app_ref, config_ref

    app_ref = app
    config_ref = config

    # Load persisted last-run state
    try:
        data_dir = Path(app.config["DATA_DIR"])
    except Exception:
        data_dir = Path(__file__).parent.parent / "data"
    state_file = data_dir / "cartographer_state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            ts = state.get("_cartographer_last_run_ts", 0.0)
            config["_cartographer_last_run_ts"] = ts
        except Exception:
            pass

    @app.route("/api/cartographer/status", methods=["GET"])
    def cartographer_status():
        """Return cartographer status."""
        last_ts = _last_run_ts()
        report = _get_report(data_dir)
        enabled = config_ref.get("cartographer_enabled", False)
        interval_hours = config_ref.get("cartographer_interval_hours", 2)
        return jsonify({
            "last_run_ts": last_ts,
            "last_report": report,
            "enabled": enabled,
            "interval_hours": interval_hours,
        })

    @app.route("/api/cartographer/run", methods=["POST"])
    def run_cartographer():
        """Trigger a cartographer task immediately."""
        try:
            from swarm import orchestrator as _orch
            if not getattr(_orch, "META_MODE_ENABLED", False):
                return jsonify({
                    "error": "meta_mode_enabled is false -- cartographer is disabled. "
                             "Enable meta mode via POST /api/meta-mode first."
                }), 400
        except Exception:
            pass

        task_id = _run_cartographer_task()
        print(f"[Cartographer] Manual run triggered -- task_id={task_id}")
        return jsonify({"task_id": task_id, "status": "created"})

    @app.route("/api/cartographer/config", methods=["GET"])
    def cartographer_config_get():
        """Return current cartographer configuration."""
        return jsonify({
            "cartographer_enabled": config_ref.get("cartographer_enabled", False),
            "cartographer_interval_hours": config_ref.get(
                "cartographer_interval_hours", 2
            ),
        })

    @app.route("/api/cartographer/config", methods=["POST"])
    def cartographer_config_set():
        """Update cartographer configuration and persist to config.json."""
        data = request.json or {}
        for key in ("cartographer_enabled", "cartographer_interval_hours"):
            if key in data:
                config_ref[key] = data[key]

        _sync_cartographer_globals(config_ref)
        _persist_config(config_ref, config_file, _config_write_lock)

        if "cartographer_enabled" in data:
            if data["cartographer_enabled"]:
                _schedule_cartographer()
            else:
                _stop_scheduler()
        elif config_ref.get("cartographer_enabled", False):
            _schedule_cartographer()

        return jsonify({
            "cartographer_enabled": config_ref.get("cartographer_enabled", False),
            "cartographer_interval_hours": config_ref.get(
                "cartographer_interval_hours", 2
            ),
        })

    # Start the scheduler after routes are registered
    if config_ref.get("cartographer_enabled", False):
        _schedule_cartographer()



def _sync_cartographer_globals(config: Dict) -> None:
    """Sync cartographer config values to the orchestrator module."""
    try:
        from swarm import orchestrator as _orch
        _orch.CARTOGRAPHER_ENABLED = config.get("cartographer_enabled", False)
        _orch.CARTOGRAPHER_INTERVAL_HOURS = config.get("cartographer_interval_hours", 2)
    except Exception:
        pass



def _persist_config(config: Dict, config_file: Any,
                    _config_write_lock: threading.Lock | None = None) -> None:
    """Persist cartographer keys to config.json via the write lock."""
    if config_file is None or _config_write_lock is None:
        return
    with _config_write_lock:
        try:
            cfg = {}
            if config_file.exists():
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
            for key in ("cartographer_enabled", "cartographer_interval_hours"):
                if key in config:
                    cfg[key] = config[key]
            config_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
