"""Plan snapshot maintenance helpers."""

from __future__ import annotations

from typing import Any, Iterable


def project_plan_subtasks(db: Any, project: str, planner_task_id: str) -> list[dict]:
    return [
        t for t in db.task_get_all()
        if t.get("project") == project
        and (t.get("metadata") or {}).get("parent_task_id") == planner_task_id
    ]


def matching_plan_ids(db: Any, project: str, task_ids: Iterable[str]) -> list[str]:
    task_id_set = {tid for tid in task_ids if isinstance(tid, str) and tid}
    if not task_id_set:
        return []
    out: list[str] = []
    for plan in db.plan_get_by_project(project):
        if any(tid in task_id_set for tid in (plan.get("task_ids") or [])):
            out.append(plan["id"])
    return out


def matching_plan_ids_for_planner(db: Any, project: str, planner_task_id: str) -> list[str]:
    if not planner_task_id:
        return []
    return [
        plan["id"]
        for plan in db.plan_get_by_project(project)
        if plan.get("planner_task_id") == planner_task_id
    ]


def delete_matching_plans(db: Any, project: str, task_ids: Iterable[str]) -> list[str]:
    deleted: list[str] = []
    for plan_id in matching_plan_ids(db, project, task_ids):
        if db.plan_delete(plan_id):
            deleted.append(plan_id)
    return deleted


def delete_plans_for_planner(db: Any, project: str, planner_task_id: str) -> list[str]:
    deleted: list[str] = []
    for plan_id in matching_plan_ids_for_planner(db, project, planner_task_id):
        if db.plan_delete(plan_id):
            deleted.append(plan_id)
    return deleted


def plan_staleness_reasons(db: Any, project: str, plan: dict) -> list[str]:
    reasons: list[str] = []
    if plan.get("project") != project:
        reasons.append("project_mismatch")

    planner_task_id = plan.get("planner_task_id")
    planner_task = db.task_get(planner_task_id) if planner_task_id else None
    completed_ids = db.task_get_completed_ids()
    if not planner_task_id:
        reasons.append("missing_planner_task_id")
    elif planner_task is None:
        if planner_task_id not in completed_ids:
            reasons.append("missing_planner_task")
    else:
        if planner_task.get("project") != project:
            reasons.append("planner_project_mismatch")
        if planner_task.get("type") != "project_plan":
            reasons.append("planner_type_mismatch")

    task_ids = [tid for tid in (plan.get("task_ids") or []) if isinstance(tid, str) and tid]
    live_count = 0
    for task_id in task_ids:
        task = db.task_get(task_id)
        if task is None:
            continue
        live_count += 1
        if task.get("project") != project:
            reasons.append(f"task_project_mismatch:{task_id}")
        if task.get("plan_id") and task.get("plan_id") != plan.get("id"):
            reasons.append(f"task_reassigned:{task_id}")

    if task_ids and live_count == 0:
        reasons.append("no_live_plan_tasks")

    return reasons


def cleanup_stale_plans(db: Any, project: str) -> list[dict]:
    deleted: list[dict] = []
    for plan in db.plan_get_by_project(project):
        reasons = plan_staleness_reasons(db, project, plan)
        if not reasons:
            continue
        if db.plan_delete(plan["id"]):
            deleted.append({
                "plan_id": plan["id"],
                "reasons": reasons,
            })
    return deleted


def latest_renderable_plan_tasks(db: Any, project: str) -> list[dict]:
    for plan in db.plan_get_by_project(project):
        if plan_staleness_reasons(db, project, plan):
            continue
        task_graph = plan.get("task_graph") or []
        if isinstance(task_graph, list):
            return task_graph
    return []
