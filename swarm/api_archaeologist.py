"""Archaeologist route handlers and stall detection for the Swarm API."""

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

# Default config keys
DEFAULT_STALL_THRESHOLD_HOURS = 72
DEFAULT_MAX_CONCURRENT = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_ts() -> float:
    return time.time()


def _is_archaeologist_running(config: Dict) -> bool:
    """Return True if an archaeologist task is already pending or in_progress."""
    try:
        all_tasks = db.task_get_all()
        return any(
            t.get("type") == "archaeologist"
            and t.get("status") in ("pending", "in_progress")
            for t in all_tasks
        )
    except Exception:
        return False


def _run_archaeologist_task(config: Dict, data_dir: Path,
                            project: str, stall_reason: str) -> str:
    """Create an archaeologist task for a specific stalled project and return its task_id."""
    task_id = f"archaeologist-{project}-{int(time.time())}"
    deps = chain_to_project_head(db, "swarm-controller", task_id=task_id)
    db.task_upsert({
        "id": task_id,
        "project": "swarm-controller",
        "type": "archaeologist",
        "description": (
            f"Run the Archaeologist meta-agent to diagnose why project '{project}' has stalled "
            f"and produce a recovery task DAG.\n\n"
            f"STALL REASON: {stall_reason}\n\n"
            f"Investigate the stalled project at the workspace path for '{project}', "
            f"read git history, assess code state, write ARCHAEOLOGY_REPORT.md to the "
            f"project root, and create a sequenced recovery task DAG via create_tasks()."
        ),
        "priority": 55,
        "status": "pending",
        "dependencies": deps,
        "metadata": {
            "auto_spawned": True,
            "stalled_project": project,
            "stall_reason": stall_reason,
        },
        "attempts": 0,
        "max_attempts": 1,
    })
    return task_id


def _persist_config(config: Dict, config_file: Path | None = None,
                      _config_write_lock: threading.Lock | None = None) -> None:
    """Persist archaeologist config keys to config.json via the write lock."""
    if config_file is None or _config_write_lock is None:
        return
    with _config_write_lock:
        try:
            cfg = {}
            if config_file.exists():
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
            for key in ("archaeologist_enabled", "archaeologist_stall_threshold_hours",
                        "archaeologist_max_concurrent"):
                if key in config:
                    cfg[key] = config[key]
            config_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register_routes(app, config: Dict,
               config_file: Path | None = None,
               _config_write_lock: threading.Lock | None = None,
               **kwargs) -> None:
    """Register archaeologist routes on the Flask app."""
    global config_ref, _orchestrator_mod
    config_ref = config
    data_dir = kwargs.get("data_dir", Path("data"))

    @app.route("/api/archaeologist/status", methods=["GET"])
    def archaeologist_status():
        """Return archaeologist status: enabled, stall_threshold_hours, max_concurrent,
        running_tasks."""
        enabled = config.get("archaeologist_enabled", False)
        threshold = config.get("archaeologist_stall_threshold_hours", DEFAULT_STALL_THRESHOLD_HOURS)
        max_conc = config.get("archaeologist_max_concurrent", DEFAULT_MAX_CONCURRENT)
        try:
            all_tasks = db.task_get_all()
            running = [
                {"id": t["id"], "status": t["status"],
                 "stalled_project": (t.get("metadata") or {}).get("stalled_project", "?")}
                for t in all_tasks
                if t.get("type") == "archaeologist"
                and t.get("status") in ("pending", "in_progress")
            ]
        except Exception:
            running = []
        return jsonify({
            "archaeologist_enabled": enabled,
            "archaeologist_stall_threshold_hours": threshold,
            "archaeologist_max_concurrent": max_conc,
            "running_tasks": running,
        })

    @app.route("/api/archaeologist/investigate/<project>", methods=["POST"])
    def archaeologist_investigate(project: str):
        """Manually trigger an archaeologist task for a specific project."""
        data = request.json or {}
        stall_reason = data.get("stall_reason", "manual trigger")

        # Check max concurrent
        max_conc = config.get("archaeologist_max_concurrent", DEFAULT_MAX_CONCURRENT)
        try:
            all_tasks = db.task_get_all()
            active = [
                t for t in all_tasks
                if t.get("type") == "archaeologist"
                and t.get("status") in ("pending", "in_progress")
            ]
        except Exception:
            active = []

        if len(active) >= max_conc:
            return jsonify({
                "error": f"Max concurrent archaeologist tasks ({max_conc}) reached",
                "running_tasks": [t["id"] for t in active],
            }), 409

        # Don't duplicate if one for this project is already running
        existing = [t for t in active if (t.get("metadata") or {}).get("stalled_project") == project]
        if existing:
            return jsonify({
                "error": f"Archaeologist already running for project '{project}'",
                "task_id": existing[0]["id"],
            }), 409

        task_id = _run_archaeologist_task(config, data_dir, project, stall_reason)
        print(f"[Archaeologist] Manual investigate triggered for '{project}' -- task_id={task_id}")
        return jsonify({"task_id": task_id, "status": "created", "project": project})

    @app.route("/api/archaeologist/run", methods=["POST"])
    def archaeologist_run():
        """Auto-scan all managed projects and spawn archaeologist tasks for stalled ones."""
        import swarm.orchestrator as _orch
        try:
            all_tasks = db.task_get_all()
        except Exception as e:
            return jsonify({"error": f"DB error: {e}"}), 500

        stalled = _orch._find_stalled_projects(all_tasks)
        if not stalled:
            return jsonify({"scanned": len(all_tasks), "stalled_found": 0, "spawned": 0, "projects": []})

        # Respect max concurrent
        max_conc = config.get("archaeologist_max_concurrent", DEFAULT_MAX_CONCURRENT)
        try:
            existing = [
                t for t in all_tasks
                if t.get("type") == "archaeologist"
                and t.get("status") in ("pending", "in_progress")
            ]
        except Exception:
            existing = []

        slots = max_conc - len(existing)
        to_spawn = stalled[:max(0, slots)]

        spawned = []
        for project_name, stall_reason in to_spawn:
            # Skip if one is already running for this project
            if any((t.get("metadata") or {}).get("stalled_project") == project_name for t in existing):
                continue
            task_id = _run_archaeologist_task(config, data_dir, project_name, stall_reason)
            spawned.append({"task_id": task_id, "project": project_name, "stall_reason": stall_reason})
            existing.append({"metadata": {"stalled_project": project_name}})

        print(f"[Archaeologist] Auto-scan: spawned {len(spawned)} tasks out of {len(stalled)} stalled projects")
        return jsonify({
            "scanned": len(all_tasks),
            "stalled_found": len(stalled),
            "spawned": len(spawned),
            "projects": spawned,
        })

    @app.route("/api/archaeologist/config", methods=["GET"])
    def archaeologist_config_get():
        """Return current archaeologist configuration keys."""
        return jsonify({
            "archaeologist_enabled": config.get("archaeologist_enabled", False),
            "archaeologist_stall_threshold_hours": config.get(
                "archaeologist_stall_threshold_hours", DEFAULT_STALL_THRESHOLD_HOURS),
            "archaeologist_max_concurrent": config.get(
                "archaeologist_max_concurrent", DEFAULT_MAX_CONCURRENT),
        })

    @app.route("/api/archaeologist/config", methods=["POST"])
    def archaeologist_config_set():
        """Update archaeologist configuration keys and persist to config.json."""
        data = request.json or {}
        for key in ("archaeologist_enabled", "archaeologist_stall_threshold_hours",
                    "archaeologist_max_concurrent"):
            if key in data:
                config[key] = data[key]

        # Sync to orchestrator module globals
        _sync_archaeologist_globals(config)

        # Persist to config.json
        _persist_config(config, config_file, _config_write_lock)

        return jsonify({
            "archaeologist_enabled": config.get("archaeologist_enabled", False),
            "archaeologist_stall_threshold_hours": config.get(
                "archaeologist_stall_threshold_hours", DEFAULT_STALL_THRESHOLD_HOURS),
            "archaeologist_max_concurrent": config.get(
                "archaeologist_max_concurrent", DEFAULT_MAX_CONCURRENT),
        })


def _sync_archaeologist_globals(config: Dict) -> None:
    """Sync archaeologist config values to the orchestrator module."""
    try:
        from swarm import orchestrator as _orch
        _orch.ARCHAEOLOGIST_ENABLED = config.get("archaeologist_enabled", False)
        _orch.ARCHAEOLOGIST_STALL_THRESHOLD_HOURS = config.get(
            "archaeologist_stall_threshold_hours", DEFAULT_STALL_THRESHOLD_HOURS)
        _orch.ARCHAEOLOGIST_MAX_CONCURRENT = config.get(
            "archaeologist_max_concurrent", DEFAULT_MAX_CONCURRENT)
    except Exception:
        pass
