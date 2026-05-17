"""Plans route handlers for the Swarm API."""

import time

from flask import jsonify, request

from swarm.maintenance import plans as plan_cleanup
from swarm.maintenance.project_heads import ensure_project_head, set_project_head


def register_routes(app, db):
    """Register plan routes on the Flask app."""

    @app.route("/api/plans", methods=["GET"])
    def list_all_plans():
        """Return all plans across all projects, newest first."""
        plans = db.plan_get_all()
        return jsonify({
            "plans": [
                {
                    "id":          p["id"],
                    "project":     p["project"],
                    "created_at":  p["created_at"],
                    "task_ids":    p["task_ids"],
                    "task_count":  len(p["task_ids"]),
                }
                for p in plans
            ]
        })

    @app.route("/api/plans/<project>", methods=["GET"])
    def list_project_plans(project):
        """Return plans for a specific project, newest first."""
        plans = db.plan_get_by_project(project)
        return jsonify({
            "project": project,
            "plans": [
                {
                    "id":          p["id"],
                    "created_at":  p["created_at"],
                    "task_ids":    p["task_ids"],
                    "task_count":  len(p["task_ids"]),
                }
                for p in plans
            ]
        })

    @app.route("/api/plans/<project>/reset", methods=["POST"])
    def reset_project_plans(project):
        """Cancel planner output, remove stale plan snapshots, optionally queue a fresh planner."""
        data = request.get_json(silent=True) or {}
        create_replacement = bool(data.get("create_replacement", True))
        delete_cancelled_tasks = bool(data.get("delete_cancelled_tasks", True))

        planner_tasks = [
            t for t in db.task_get_all()
            if t.get("project") == project and t.get("type") == "project_plan"
        ]
        planner_ids = {t["id"] for t in planner_tasks}
        generated = [
            t for t in db.task_get_all()
            if t.get("project") == project
            and (t.get("metadata") or {}).get("parent_task_id") in planner_ids
        ]

        cancelled_ids = []
        deleted_ids = []
        for task in generated:
            if task.get("status") in ("pending", "in_progress"):
                db.task_update_status(task["id"], "cancelled")
                cancelled_ids.append(task["id"])
            if delete_cancelled_tasks and db.task_get(task["id"]) and db.task_get(task["id"]).get("status") == "cancelled":
                db.task_delete(task["id"])
                deleted_ids.append(task["id"])

        for task in planner_tasks:
            if task.get("status") in ("pending", "in_progress"):
                db.task_update_status(task["id"], "cancelled")
                cancelled_ids.append(task["id"])

        deleted_plan_ids = []
        for planner in planner_tasks:
            deleted_plan_ids.extend(plan_cleanup.delete_plans_for_planner(db, project, planner["id"]))
        if not deleted_plan_ids:
            deleted_plan_ids = [p["id"] for p in db.plan_get_by_project(project)]
            for plan_id in deleted_plan_ids:
                db.plan_delete(plan_id)

        replacement_id = None
        proj = db.project_get(project)
        if create_replacement and proj:
            replacement_id = f"project-plan-{project}-{int(time.time())}"
            anchor = ensure_project_head(db, project) or f"{project}-genesis"
            anchor_task = db.task_get(anchor) if anchor else None
            if anchor_task and anchor_task.get("type") == "project_plan":
                anchor = f"{project}-genesis"
            db.task_upsert({
                "id": replacement_id,
                "project": project,
                "type": "project_plan",
                "description": (
                    f"Generate a dependency-ordered task plan for {project}. "
                    f"Read GAME_DESIGN.md and the existing codebase, then create all "
                    f"necessary tasks via the API with proper dependencies so systems "
                    f"are built and wired together in the correct order."
                ),
                "priority": 100,
                "status": "pending",
                "attempts": 0,
                "max_attempts": 2,
                "dependencies": [anchor] if anchor and anchor != replacement_id else [],
                "metadata": {"reset_replacement": True},
            })
            set_project_head(db, project, replacement_id)

        return jsonify({
            "project": project,
            "cancelled_task_ids": cancelled_ids,
            "deleted_task_ids": deleted_ids,
            "deleted_plan_ids": deleted_plan_ids,
            "replacement_planner_id": replacement_id,
        })

    @app.route("/api/plans/<project>/cleanup", methods=["POST"])
    def cleanup_project_plans(project):
        deleted = plan_cleanup.cleanup_stale_plans(db, project)
        return jsonify({
            "project": project,
            "deleted": deleted,
            "deleted_count": len(deleted),
        })
