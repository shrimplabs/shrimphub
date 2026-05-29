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
app_ref: Any = None       # Flask app
config_ref: Dict = {}      # shared config dict

# Scheduler state
_scheduler_timer: threading.Timer | None = None
_scheduler_lock = threading.Lock()

# Default schedule: every 2 hours
DEFAULT_INTERVAL_SECS = 2 * 3600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_ts() -> float:
    return time.time()


def _last_run_ts(config: Dict) -> float:
    return float(config.get("_cartographer_last_run_ts", 0.0))


def _set_last_run_ts(config: Dict, ts: float) -> None:
    config["_cartographer_last_run_ts"] = ts


def _get_project_map(data_dir: Path) -> str | None:
    path = data_dir / "PROJECT_MAP.md"
    if path.exists():
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            pass
    return None


def _get_summary_json(data_dir: Path) -> Dict | None:
    path = data_dir / "SWARM_SUMMARY.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _run_cartographer_task(config: Dict, data_dir: Path) -> str:
    """Create a cartographer task and return its task_id."""
    task_id = f"cartographer-{int(time.time())}"
    project = "swarm-controller"
    deps = chain_to_project_head(db, project, task_id=task_id)
    db.task_upsert({
        "id": task_id,
        "project": project,
        "type": "cartographer",
        "description": (
            "Run the cartographer meta-agent. Survey all managed projects, collect health "
            "signals, cross-reference known patterns from swarm_knowledge.jsonl, and write "
            "findings to data/PROJECT_MAP.md and data/SWARM_SUMMARY.json."
        ),
        "priority": 60,
        "status": "pending",
        "dependencies": deps,
        "metadata": {"auto_spawned": True},
        "attempts": 0,
        "max_attempts": 1,
    })
    _set_last_run_ts(config, _current_ts())
    _persist_state(data_dir, {"_cartographer_last_run_ts": _current_ts()})
    return task_id


def _persist_state(data_dir: Path, updates: Dict) -> None:
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


def _load_state(data_dir: Path) -> Dict:
    state_file = data_dir / "cartographer_state.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def _schedule_cartographer(app: Any, config: Dict, data_dir: Path) -> None:
    """Schedule the next cartographer run based on cartographer_interval_hours."""
    global _scheduler_timer, _scheduler_lock

    enabled = config.get("cartographer_enabled", False)
    if not enabled:
        return

    interval = config.get("cartographer_interval_hours", 2)
    if isinstance(interval, str):
        try:
            interval = float(interval)
        except ValueError:
            interval = 2.0
    interval = max(interval * 3600, 300)  # minimum 5 minutes

    def _fire():
        try:
            _run_cartographer_task(config, data_dir)
            print(f"[Cartographer] Scheduled run fired -- task created at {datetime.now(timezone.utc).isoformat()}")
        except Exception as exc:
            print(f"[Cartographer] Scheduled run failed: {exc}")
        finally:
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
    print(f"[Cartographer] Scheduler started -- interval {interval}s, enabled={enabled}")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register_routes(app, config: Dict, data_dir: Path,
               config_file: Path | None = None,
               _config_write_lock: threading.Lock | None = None) -> None:
    """Register cartographer routes on the Flask app."""
    global app_ref, config_ref

    app_ref = app
    config_ref = config

    # Load persisted last-run state
    state = _load_state(data_dir)
    config["_cartographer_last_run_ts"] = state.get("_cartographer_last_run_ts", 0.0)

    @app.route("/api/cartographer/status", methods=["GET"])
    def cartographer_status():
        """Return cartographer status: last_run_ts, project_map, summary_json, enabled."""
        last_ts = _last_run_ts(config)
        project_map = _get_project_map(data_dir)
        summary = _get_summary_json(data_dir)
        enabled = config.get("cartographer_enabled", False)
        return jsonify({
            "last_run_ts": last_ts,
            "project_map": project_map,
            "summary": summary,
            "enabled": enabled,
        })

    @app.route("/api/cartographer/run", methods=["POST"])
    def run_cartographer():
        """Trigger a cartographer task immediately."""
        task_id = _run_cartographer_task(config, data_dir)
        print(f"[Cartographer] Manual run triggered -- task_id={task_id}")
        return jsonify({"task_id": task_id, "status": "created"})

    @app.route("/api/cartographer/config", methods=["GET"])
    def cartographer_config_get():
        """Return current cartographer configuration."""
        return jsonify({
            "cartographer_enabled": config.get("cartographer_enabled", False),
            "cartographer_interval_hours": config.get("cartographer_interval_hours", 2),
        })

    @app.route("/api/cartographer/config", methods=["POST"])
    def cartographer_config_set():
        """Update cartographer configuration and persist to config.json."""
        data = request.json or {}
        config_keys = {
            "cartographer_enabled",
            "cartographer_interval_hours",
        }
        for key in config_keys:
            if key in data:
                config[key] = data[key]

        # Sync to orchestrator module globals
        _sync_cartographer_globals(config)

        # Persist to config.json
        _persist_config(config, config_file, _config_write_lock)

        # Restart scheduler if enabled changed or interval changed
        _schedule_cartographer(app, config, data_dir)

        return jsonify({
            "cartographer_enabled": config.get("cartographer_enabled", False),
            "cartographer_interval_hours": config.get("cartographer_interval_hours", 2),
        })

    # Start the scheduler after routes are registered
    _schedule_cartographer(app, config, data_dir)


def _sync_cartographer_globals(config: Dict) -> None:
    """Sync cartographer config values to the orchestrator module."""
    try:
        from swarm import orchestrator as _orch
        _orch.CARTOGRAPHER_ENABLED = config.get("cartographer_enabled", False)
    except Exception:
        pass


def _persist_config(config: Dict, config_file: Path | None = None,
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
