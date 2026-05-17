"""Bounded stalled-project escalation bundle generation."""

from __future__ import annotations

from typing import Any

from swarm import db
from swarm.closure.regressions import summarize_project_recurrence
from swarm.closure.runs import list_verification_runs
from swarm.closure.specs import normalize_project_spec


def build_stalled_project_bundle(project_name: str) -> dict[str, Any]:
    project = db.project_get(project_name)
    if not project:
        raise ValueError(f"project not found: {project_name}")

    closure_spec = normalize_project_spec(project.get("closure_spec"), project.get("profile"))
    verification_runs = list_verification_runs(project_name, limit=10)
    recurrence = summarize_project_recurrence(
        project_name,
        stall_threshold=int(closure_spec["autonomy"]["stall_threshold"] or 0),
    )
    regressions = db.regression_list_by_project(project_name, status="open")
    repair_tasks = []
    for task in db.task_get_by_project(project_name):
        metadata = task.get("metadata") or {}
        if metadata.get("is_closure_repair_task") or task.get("type") == "triage":
            repair_tasks.append(task)

    return {
        "project": project_name,
        "generated_from": "closure_stalled_escalation",
        "closure_state": {
            "mode": project.get("closure_mode", "build"),
            "status": project.get("closure_status", "yellow"),
            "last_verification_at": project.get("last_verification_at"),
            "last_verification_status": project.get("last_verification_status"),
            "open_regression_count": int(project.get("open_regression_count") or 0),
            "stall_count": int(project.get("stall_count") or 0),
        },
        "closure_spec": closure_spec,
        "latest_verification_run": verification_runs[0] if verification_runs else None,
        "recent_verification_runs": verification_runs[:5],
        "recurrence_summary": recurrence,
        "open_regressions": regressions,
        "repair_history": sorted(
            repair_tasks,
            key=lambda task: (task.get("completed") or task.get("created") or "", task.get("id") or ""),
            reverse=True,
        )[:10],
        "operator_guidance": [
            "Review the latest verification run and recurrence summary first.",
            "Check whether open regressions still map to a bounded repair path.",
            "If repair tasks are looping without reducing occurrences, create or run a triage task instead of new feature work.",
        ],
    }
