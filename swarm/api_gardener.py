"""Gardener route handlers and scheduling for the Swarm API."""

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from flask import jsonify, request

from swarm import db
from swarm.gardener_knowledge import load as knowledge_load

# Module-level globals -- wired from api.py via register_routes
app_ref: Any = None       # Flask app (used for internal /api/... calls)
config_ref: Dict = {}      # shared config dict
_orchestrator_mod: Any = None  # swarm.orchestrator module ref

# Scheduler state
_scheduler_timer: threading.Timer | None = None
_scheduler_lock = threading.Lock()
_last_gardener_run_ts: float = 0.0  # unix timestamp of last gardener run

# Default schedule: every 6 hours
DEFAULT_SCHEDULE_SECS = 6 * 3600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_ts() -> float:
    return time.time()


def _last_run_ts(config: Dict) -> float:
    return float(config.get("_gardener_last_run_ts", 0.0))


def _set_last_run_ts(config: Dict, ts: float) -> None:
    config["_gardener_last_run_ts"] = ts


def _get_report(data_dir: Path) -> str | None:
    report_path = data_dir / "GARDENER_REPORT.md"
    if report_path.exists():
        try:
            return report_path.read_text(encoding="utf-8")
        except Exception:
            pass
    return None


def _run_gardener_task(config: Dict, data_dir: Path) -> str:
    """Create a gardener task and return its task_id."""
    task_id = f"gardener-{int(time.time())}"
    db.task_upsert({
        "id": task_id,
        "project": "swarm-controller",
        "type": "gardener",
        "description": (
            "Run the gardener agent to audit, prune, and improve the swarm-controller "
            "project. Read prompts/gardener.yaml for the full prompt. "
            "Focus on knowledge cleanup, dead code removal, and test maintenance."
        ),
        "priority": 60,
        "status": "pending",
        "dependencies": [],
        "metadata": {"auto_spawned": True},
        "attempts": 0,
        "max_attempts": 1,
    })
    _set_last_run_ts(config, _current_ts())
    # Persist last run timestamp
    _persist_state(data_dir, {"_gardener_last_run_ts": _current_ts()})
    return task_id


def _persist_state(data_dir: Path, updates: Dict) -> None:
    state_file = data_dir / "gardener_state.json"
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
    state_file = data_dir / "gardener_state.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def _schedule_gardener(app: Any, config: Dict, data_dir: Path) -> None:
    """Schedule the next gardener run based on gardener_schedule interval."""
    global _scheduler_timer, _last_gardener_run_ts

    enabled = config.get("gardener_enabled", False)
    if not enabled:
        return

    interval = config.get("gardener_schedule", DEFAULT_SCHEDULE_SECS)
    if isinstance(interval, str):
        # Support cron-like string for future croniter use (fallback to 6h)
        try:
            interval = int(interval)
        except ValueError:
            interval = DEFAULT_SCHEDULE_SECS
    interval = max(interval, 300)  # minimum 5 minutes

    def _fire():
        try:
            # Trigger via internal task creation (not HTTP to avoid loop)
            _run_gardener_task(config, data_dir)
            print(f"[Gardener] Scheduled run fired -- task created at {datetime.now(timezone.utc).isoformat()}")
        except Exception as exc:
            print(f"[Gardener] Scheduled run failed: {exc}")
        finally:
            # Reschedule
            with _scheduler_lock:
                global _scheduler_timer
                _scheduler_timer = threading.Timer(interval, _fire)
                _scheduler_timer.daemon = True
                _scheduler_timer.start()

    with _scheduler_lock:
        if _scheduler_timer is not None:
            _scheduler_timer.cancel()
        _scheduler_timer = threading.Timer(interval, _fire)
        _scheduler_timer.daemon = True
        _scheduler_timer.start()
    print(f"[Gardener] Scheduler started -- interval {interval}s, enabled={enabled}")


def _stop_scheduler() -> None:
    global _scheduler_timer
    with _scheduler_lock:
        if _scheduler_timer is not None:
            _scheduler_timer.cancel()
            _scheduler_timer = None


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register_routes(app, config: Dict, data_dir: Path,
               config_file: Path | None = None,
               _config_write_lock: threading.Lock | None = None) -> None:
    """Register gardener routes on the Flask app."""
    global app_ref, config_ref, _orchestrator_mod

    app_ref = app
    config_ref = config

    # Load persisted last-run state
    state = _load_state(data_dir)
    config["_gardener_last_run_ts"] = state.get("_gardener_last_run_ts", 0.0)

    @app.route("/api/gardener/run", methods=["POST"])
    def run_gardener():
        """Trigger a gardener task immediately."""
        task_id = _run_gardener_task(config, data_dir)
        print(f"[Gardener] Manual run triggered -- task_id={task_id}")
        return jsonify({"task_id": task_id, "status": "created"})

    @app.route("/api/gardener/status", methods=["GET"])
    def gardener_status():
        """Return gardener status: last_run_ts, last_report, knowledge_count, enabled."""
        last_ts = _last_run_ts(config)
        report = _get_report(data_dir)
        enabled = config.get("gardener_enabled", False)
        try:
            entries = knowledge_load()
            knowledge_count = len(entries)
        except Exception:
            knowledge_count = 0
        return jsonify({
            "last_run_ts": last_ts,
            "last_report": report,
            "knowledge_count": knowledge_count,
            "enabled": enabled,
        })

    @app.route("/api/gardener/knowledge", methods=["GET"])
    def gardener_knowledge():
        """Return all knowledge entries from the gardener store."""
        try:
            entries = knowledge_load()
            return jsonify({"entries": entries})
        except Exception as exc:
            return jsonify({"entries": [], "error": str(exc)}), 200

    @app.route("/api/gardener/config", methods=["GET"])
    def gardener_config_get():
        """Return current gardener configuration keys."""
        return jsonify({
            "gardener_enabled": config.get("gardener_enabled", False),
            "gardener_schedule": config.get("gardener_schedule", DEFAULT_SCHEDULE_SECS),
            "gardener_max_tasks_per_run": config.get("gardener_max_tasks_per_run", 10),
            "gardener_skip_projects": config.get("gardener_skip_projects", []),
        })

    @app.route("/api/gardener/config", methods=["POST"])
    def gardener_config_set():
        """Update gardener configuration keys and persist to config.json."""
        global _last_gardener_run_ts
        data = request.json or {}
        gardener_keys = {
            "gardener_enabled",
            "gardener_schedule",
            "gardener_max_tasks_per_run",
            "gardener_skip_projects",
        }
        for key in gardener_keys:
            if key in data:
                config[key] = data[key]

        # Sync to orchestrator module globals
        _sync_gardener_globals(config)

        # Persist to config.json
        _persist_config(config, config_file, _config_write_lock)

        # Restart scheduler if enabled changed or schedule changed
        _schedule_gardener(app, config, data_dir)

        return jsonify({
            "gardener_enabled": config.get("gardener_enabled", False),
            "gardener_schedule": config.get("gardener_schedule", DEFAULT_SCHEDULE_SECS),
            "gardener_max_tasks_per_run": config.get("gardener_max_tasks_per_run", 10),
            "gardener_skip_projects": config.get("gardener_skip_projects", []),
        })

    # Start the scheduler after routes are registered
    _schedule_gardener(app, config, data_dir)


def _sync_gardener_globals(config: Dict) -> None:
    """Sync gardener config values to the orchestrator module."""
    try:
        from swarm import orchestrator as _orch
        _orch.GARDENER_ENABLED = config.get("gardener_enabled", False)
        _orch.GARDENER_MAX_TASKS = config.get("gardener_max_tasks_per_run", 10)
        _orch.GARDENER_SKIP_PROJECTS = config.get("gardener_skip_projects", [])
    except Exception:
        pass


def _persist_config(config: Dict, config_file: Path | None = None,
                      _config_write_lock: threading.Lock | None = None) -> None:
    """Persist gardener keys to config.json via the write lock."""
    if config_file is None or _config_write_lock is None:
        return
    with _config_write_lock:
        try:
            cfg = {}
            if config_file.exists():
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
            for key in ("gardener_enabled", "gardener_schedule",
                        "gardener_max_tasks_per_run", "gardener_skip_projects"):
                if key in config:
                    cfg[key] = config[key]
            config_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
