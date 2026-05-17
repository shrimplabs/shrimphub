"""Spawn route handlers for the Swarm API.

Routes: POST /api/spawn, POST /api/spawn-batch, POST /api/create-project
"""

from flask import jsonify, request

import time

from swarm.task_chains import chain_to_project_head


def register_routes(app, task_source, orchestrator, generate_task_script, config, db, auto_mode_state, data_dir, workspace):
    """Register spawn routes on the Flask app."""
    @app.route("/api/spawn", methods=["POST"])
    def spawn_task():
        """Spawn an agent for a specific project/task."""
        data = request.json or {}
        project = data.get("project")
        task_type = data.get("type", data.get("task_type", "feature"))

        if generate_task_script is None:
            return jsonify({"error": "generate_task_script not available"}), 500

        # Spawn a specific task by ID
        task_id = data.get("task_id")
        if task_id:
            task = task_source.get_task(task_id)
            if task is None:
                return jsonify({"error": f"Task {task_id} not found"}), 404
            if task.status == "in_progress":
                return jsonify({"error": "Task is already in progress"}), 400
            # Check unmet dependencies — a dep is met if completed or missing
            deps = task.dependencies or []
            if deps:
                completed_ids = db.task_get_completed_ids()
                active_ids = {t["id"] for t in db.task_get_all()}
                unmet = [d for d in deps if d not in completed_ids and d in active_ids]
                if unmet:
                    return jsonify({
                        "error": "Unmet dependencies",
                        "task_id": task_id,
                        "unmet_dependencies": unmet,
                    }), 409
            # Block cancelled/superseded tasks — they should not be restarted
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
            agent_id = orchestrator.spawn_agent(task.to_dict(), generate_task_script)
            if agent_id:
                return jsonify({"status": "spawned", "success": True, "agent_id": agent_id, "task_id": task.id, "project": task.project})
            return jsonify({"success": False, "error": "Failed to spawn agent"}), 500

        if not project:
            return jsonify({"error": "project or task_id required"}), 400

        # Find existing pending task or create one
        pending = task_source.get_pending_tasks()
        # Use _get_next_task logic: deps met = completed or not in active set
        completed_ids = db.task_get_completed_ids()
        active_ids = {t["id"] for t in db.task_get_all()}
        completed_ids |= {t["id"] for t in db.task_get_all() if t["status"] == "completed"}
        project_pending = [
            t for t in pending
            if t.project == project
            and all(d in completed_ids or d not in active_ids for d in (t.dependencies or []))
        ]
        project_pending.sort(key=lambda t: (-t.priority, t.id))
        task = project_pending[0] if project_pending else None

        if task is None:
            description = data.get("description", f"Auto-generated {task_type} task")
            task_id = f"{task_type}-{project}-{int(time.time() * 1000) % 10**9}-{__import__('random').randint(100, 999)}"
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
