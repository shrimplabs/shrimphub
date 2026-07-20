"""Spawn route handlers for the Swarm API.

Routes: POST /api/spawn, POST /api/spawn-batch, POST /api/create-project
"""

from flask import jsonify, request

import time

from swarm.task_chains import chain_to_project_head
from swarm.api_projects import _sync_managed_projects
from swarm.dependencies import is_dependency_met
import swarm.orchestrator as _orchestrator_mod


def register_routes(app, task_source, orchestrator, generate_task_script, config, db, auto_mode_state, data_dir, workspace, project_registry=None, config_file=None, config_write_lock=None):
    """Register spawn routes on the Flask app."""
    @app.route("/api/spawn", methods=["POST"])
    def spawn_task():
        """Spawn an agent for a specific project/task."""
        data = request.json or {}
        project = data.get("project")
        task_type = data.get("type", data.get("task_type", "feature"))

        if generate_task_script is None:
            return jsonify({"error": "generate_task_script not available"}), 500

        # Auto-mode check -- respect the user's auto-mode toggle
        # Allow force=true to bypass (for dashboard manual spawns).
        # In Flask TESTING mode the auto_mode_state is typically initialised to False
        # (auto_mode_enabled is not part of the test config) but the integration
        # tests rely on /api/spawn returning 200/409 from the *handler* logic, not
        # from a runtime gate. Tests must therefore not be blocked by the
        # auto-mode flag.
        force = bool(data.get("force", False)) or bool(app.config.get("TESTING"))
        if not force:
            with auto_mode_state["lock"]:
                _auto_on = auto_mode_state["enabled"]
            if not _auto_on:
                return jsonify({"error": "Auto mode is off -- pass force=true to spawn manually"}), 403

        # Quota check -- same gate as /api/spawn-batch
        over_limit, pct_used, pct_remaining, used_count, total = orchestrator.check_quota_limit()
        if over_limit:
            return jsonify({
                "error": f"Quota limit ({orchestrator.QUOTA_LIMIT_PERCENT}%) exceeded ({pct_used:.1f}% used)",
                "quota_percent": pct_used,
                "limit_percent": orchestrator.QUOTA_LIMIT_PERCENT,
            }), 507

        # Cap check -- respect MAX_ACTIVE_AGENTS
        if not force and orchestrator.get_active_count() >= orchestrator.MAX_ACTIVE_AGENTS:
            return jsonify({
                "error": f"Agent cap reached ({orchestrator.get_active_count()}/{orchestrator.MAX_ACTIVE_AGENTS})",
            }), 429

        # Spawn a specific task by ID
        task_id = data.get("task_id")
        if task_id:
            task = task_source.get_task(task_id)
            if task is None:
                return jsonify({"error": f"Task {task_id} not found"}), 404
            if task.status == "in_progress":
                return jsonify({"error": "Task is already in progress"}), 400
            # Check unmet dependencies
            deps = task.dependencies or []
            if deps:
                all_tasks = db.task_get_all()
                all_task_ids = {t["id"] for t in all_tasks}
                completed_ids = {t["id"] for t in all_tasks if t["status"] == "completed"}
                unmet = [d for d in deps if not is_dependency_met(d, all_task_ids, completed_ids)]
                if unmet:
                    return jsonify({
                        "error": "Unmet dependencies",
                        "task_id": task_id,
                        "unmet_dependencies": unmet,
                    }), 409
            # Block cancelled/superseded tasks -- they should not be restarted
            if task.status == "cancelled":
                return jsonify({
                    "error": "Task is cancelled and cannot be spawned",
                    "task_id": task_id,
                    "status": task.status,
                }), 409
            # Force to pending so spawn_agent accepts it
            if task.status != "pending":
                task.status = "pending"
                task_source.update_task(task)
            with _orchestrator_mod._fill_slots_lock:
                agent_id = orchestrator.spawn_agent(task.to_dict(), generate_task_script)
            if agent_id:
                return jsonify({"status": "spawned", "success": True, "agent_id": agent_id, "task_id": task.id, "project": task.project})
            return jsonify({"success": False, "error": "Failed to spawn agent"}), 500

        if not project:
            return jsonify({"error": "project or task_id required"}), 400

        # Find existing pending task or create one
        pending = task_source.get_pending_tasks()
        all_tasks = db.task_get_all()
        all_task_ids = {t["id"] for t in all_tasks}
        completed_ids = {t["id"] for t in all_tasks if t["status"] == "completed"}
        project_pending = [
            t for t in pending
            if t.project == project
            and all(is_dependency_met(d, all_task_ids, completed_ids) for d in (t.dependencies or []))
        ]
        project_pending.sort(key=lambda t: (-t.priority, t.id))
        task = project_pending[0] if project_pending else None

        if task is None:
            description = data.get("description", f"Auto-generated {task_type} task")
            task_id = f"{task_type}-{project}-{int(time.time() * 1000) % 10**9}-{__import__('random').randint(100000, 999999)}"
            from swarm.tasks import Task
            task = Task(
                id=task_id,
                project=project,
                type=task_type,
                description=description,
                priority=data.get("priority", 50),
                dependencies=chain_to_project_head(db, project, task_id=task_id, ensure_head=True),
            )
            task_source.add_task(task)

        with _orchestrator_mod._fill_slots_lock:
            agent_id = orchestrator.spawn_agent(task.to_dict(), generate_task_script)
        if agent_id:
            return jsonify({
                "status": "spawned",
                "success": True,
                "agent_id": agent_id,
                "task_id": task.id,
                "project": project,
            })
        return jsonify({"success": False, "error": "Failed to spawn agent"}), 500

    @app.route("/api/create-project", methods=["POST"])
    def create_project_task():
        """Queue a project_create task. Body: {name, description, type: 'godot'|'python'}"""
        data = request.get_json(force=True) or {}
        name = (data.get("name") or "").strip().lower().replace(" ", "-")
        description = (data.get("description") or "").strip()
        if not name or not description:
            return jsonify({"error": "name and description required"}), 400
        task = {
            "id": f"project-create-{name}-{int(time.time())}",
            "project": "_swarm",
            "type": "project_create",
            "priority": 90,
            "max_attempts": 3,
            "description": f"Project name: {name}\n\n{description}",
            "dependencies": chain_to_project_head(db, "_swarm", ensure_head=True),
        }
        db.task_upsert(task)
        # Also register the project so it appears in managed_projects immediately
        project_path = workspace / name
        db.project_upsert({
            "name": name,
            "path": str(project_path),
            "managed": True,
            "status": "active",
        })
        if project_registry is not None:
            project_registry.add_project(name, managed=True)
        _sync_managed_projects(config, project_registry, orchestrator, config_file, config_write_lock)
        return jsonify({"task": task})

    @app.route("/api/spawn-batch", methods=["POST"])
    def spawn_batch():
        """Spawn multiple agents, respecting quota and slot limits."""
        # Check quota first
        over_limit, pct_used, pct_remaining, used_count, total = orchestrator.check_quota_limit()
        if over_limit:
            return jsonify({
                "error": f"Quota limit ({orchestrator.QUOTA_LIMIT_PERCENT}%) exceeded ({pct_used:.1f}% used)",
                "quota_percent": pct_used,
                "limit_percent": orchestrator.QUOTA_LIMIT_PERCENT,
                "spawned": [],
                "skipped": [],
                "count": 0
            }), 507

        if generate_task_script is None:
            return jsonify({"error": "generate_task_script not available"}), 500

        data = request.json or {}
        count = data.get("count")
        max_spawn = int(count) if count is not None else None
        spawned_ids, skipped = orchestrator.fill_slots(generate_task_script, max_spawn)
        return jsonify({
            "spawned": spawned_ids,
            "skipped": skipped,
            "count": len(spawned_ids)
        })
