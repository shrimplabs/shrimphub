"""Librarian route handlers and scheduling for the Swarm API."""

from __future__ import annotations

import json
import threading
import time
from datetime import timezone
from pathlib import Path
from typing import Any, Dict

from flask import jsonify, request

from swarm import db
from swarm.task_chains import chain_to_project_head

# Module-level globals -- wired from api.py via register_routes
app_ref: Any = None
config_ref: Dict = {}

# Scheduler state
_scheduler_lock = threading.Lock()

# Default config values
DEFAULT_TRIGGER_INTERVAL = 50  # fire after this many task completions
DEFAULT_MAX_PROMPT_TASKS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _persist_state(data_dir: Path, updates: Dict) -> None:
    state_file = data_dir / "librarian_state.json"
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
    state_file = data_dir / "librarian_state.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _get_report(data_dir: Path) -> str | None:
    report_path = data_dir / "LIBRARIAN_REPORT.md"
    if report_path.exists():
        try:
            return report_path.read_text(encoding="utf-8")
        except Exception:
            pass
    return None


def _run_librarian_task(config: Dict, data_dir: Path) -> str:
    """Create a librarian task and return its task_id."""
    task_id = f"librarian-{int(time.time())}"
    project = "swarm-controller"
    deps = chain_to_project_head(db, project, task_id=task_id)
    # Inject LIBRARIAN_AUTONOMOUS_EDITS into task metadata so the agent reads it
    # as a config variable (the prompt reads it via the librarian_autonomous_edits
    # placeholder, injected into task context by swarm_runner just like QA_MAX_CYCLES).
    autonomous_edits = config.get("librarian_autonomous_edits", False)
    db.task_upsert({
        "id": task_id,
        "project": project,
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
        "metadata": {
            "auto_spawned": True,
            "librarian_autonomous_edits": autonomous_edits,
        },
        "attempts": 0,
        "max_attempts": 1,
    })
    ts = time.time()
    _persist_state(data_dir, {"_librarian_last_run_ts": ts})
    return task_id


def _increment_completion_counter(data_dir: Path) -> int:
    """Increment and persist the librarian completion counter. Returns new value."""
    state = _load_state(data_dir)
    count = state.get("completion_counter", 0) + 1
    _persist_state(data_dir, {"completion_counter": count})
    return count


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register_routes(app, config: Dict, data_dir: Path,
               config_file: Path | None = None,
               _config_write_lock: threading.Lock | None = None) -> None:
    """Register librarian routes on the Flask app."""
    global app_ref, config_ref
    app_ref = app
    config_ref = config

    # Load persisted completion counter
    state = _load_state(data_dir)
    config["_librarian_completion_counter"] = state.get("completion_counter", 0)

    @app.route("/api/librarian/status", methods=["GET"])
    def librarian_status():
        """Return librarian status: completion counter, last report, enabled, autonomous_edits."""
        report = _get_report(data_dir)
        enabled = config.get("librarian_enabled", False)
        trigger_interval = config.get("librarian_trigger_interval", DEFAULT_TRIGGER_INTERVAL)
        counter = config.get("_librarian_completion_counter", 0)
        next_trigger_at = (counter // trigger_interval + 1) * trigger_interval
        last_run_ts = float(config.get("_librarian_last_run_ts", 0.0))
        return jsonify({
            "completion_counter": counter,
            "trigger_interval": trigger_interval,
            "next_trigger_at": next_trigger_at,
            "last_report": report,
            "enabled": enabled,
            "autonomous_edits": config.get("librarian_autonomous_edits", False),
            "last_run_ts": last_run_ts,
        })

    @app.route("/api/librarian/run", methods=["POST"])
    def run_librarian():
        """Trigger a librarian task immediately."""
        task_id = _run_librarian_task(config, data_dir)
        print(f"[Librarian] Manual run triggered -- task_id={task_id}")
        return jsonify({"task_id": task_id, "status": "created"})

    @app.route("/api/librarian/config", methods=["GET"])
    def librarian_config_get():
        """Return current librarian configuration keys."""
        return jsonify({
            "librarian_enabled": config.get("librarian_enabled", False),
            "librarian_trigger_interval": config.get("librarian_trigger_interval", DEFAULT_TRIGGER_INTERVAL),
            "librarian_max_prompt_tasks": config.get("librarian_max_prompt_tasks", DEFAULT_MAX_PROMPT_TASKS),
            "librarian_autonomous_edits": config.get("librarian_autonomous_edits", False),
        })

    @app.route("/api/librarian/config", methods=["POST"])
    def librarian_config_set():
        """Update librarian configuration keys and persist to config.json."""
        data = request.json or {}
        librarian_keys = {
            "librarian_enabled",
            "librarian_trigger_interval",
            "librarian_max_prompt_tasks",
            "librarian_autonomous_edits",
        }
        for key in librarian_keys:
            if key in data:
                config[key] = data[key]

        # Sync to orchestrator module globals
        _sync_librarian_globals(config)

        # Persist to config.json
        _persist_config(config, config_file, _config_write_lock)

        return jsonify({
            "librarian_enabled": config.get("librarian_enabled", False),
            "librarian_trigger_interval": config.get("librarian_trigger_interval", DEFAULT_TRIGGER_INTERVAL),
            "librarian_max_prompt_tasks": config.get("librarian_max_prompt_tasks", DEFAULT_MAX_PROMPT_TASKS),
            "librarian_autonomous_edits": config.get("librarian_autonomous_edits", False),
        })

    @app.route("/api/librarian/autonomous-edits", methods=["POST"])
    def librarian_autonomous_edits_toggle():
        """Toggle librarian autonomous edits mode and persist to config.json.

        When autonomous edits are enabled, the librarian agent directly edits
        prompts/*.yaml files and commits via git (git is the rollback safety net).
        When disabled (default), the librarian creates refactor tasks for human review.
        """
        data = request.json or {}
        enabled = bool(data.get("enabled", False))
        config["librarian_autonomous_edits"] = enabled
        _sync_librarian_globals(config)
        _persist_config(config, config_file, _config_write_lock)
        return jsonify({
            "librarian_autonomous_edits": enabled,
        })


def _sync_librarian_globals(config: Dict) -> None:
    """Sync librarian config values to the orchestrator module."""
    try:
        from swarm import orchestrator as _orch
        _orch.LIBRARIAN_ENABLED = config.get("librarian_enabled", False)
        _orch.LIBRARIAN_TRIGGER_INTERVAL = config.get("librarian_trigger_interval", DEFAULT_TRIGGER_INTERVAL)
        _orch.LIBRARIAN_MAX_PROMPT_TASKS = config.get("librarian_max_prompt_tasks", DEFAULT_MAX_PROMPT_TASKS)
        _orch.LIBRARIAN_AUTONOMOUS_EDITS = config.get("librarian_autonomous_edits", False)
    except Exception:
        pass


def _persist_config(config: Dict, config_file: Path | None = None,
                      _config_write_lock: threading.Lock | None = None) -> None:
    """Persist librarian keys to config.json via the write lock."""
    if config_file is None or _config_write_lock is None:
        return
    with _config_write_lock:
        try:
            cfg = {}
            if config_file.exists():
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
            for key in ("librarian_enabled", "librarian_trigger_interval",
                        "librarian_max_prompt_tasks", "librarian_autonomous_edits"):
                if key in config:
                    cfg[key] = config[key]
            config_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API: called by orchestrator/agent_lifecycle to increment counter
# ---------------------------------------------------------------------------

def increment_librarian_counter(data_dir: Path) -> int:
    """Increment the librarian completion counter. Returns new count."""
    return _increment_completion_counter(data_dir)
