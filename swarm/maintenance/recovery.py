"""Recovery-branch maintenance helpers."""

from __future__ import annotations

from typing import Any, Callable


def cleanup_recovery_branches(
    *,
    db: Any,
    project: str,
    task_mutations: Any,
    task_chains: Any,
    spawn_terminal_recovery_continuation: Callable[[dict, int, str], str | None],
) -> dict:
    """Collapse stale recovery trees for a project to one sane live path per branch."""
    if not project:
        return {"project": project, "cancelled_recovery_ids": [], "repaired_dependency_task_ids": [], "created_continuation_ids": []}

    project_tasks = [task for task in db.task_get_all() if task.get("project") == project]
    branch_groups: dict[str, dict[str, list[dict]]] = {}
    for task in project_tasks:
        meta = task.get("metadata") or {}
        if meta.get("is_recovery_task"):
            root = meta.get("recovery_root_task_id") or meta.get("failed_task_id") or task.get("id")
            bucket = branch_groups.setdefault(root, {"recoveries": [], "continuations": []})
            bucket["recoveries"].append(task)
        elif meta.get("branch_continuation") and meta.get("recovery_root_task_id"):
            root = meta["recovery_root_task_id"]
            bucket = branch_groups.setdefault(root, {"recoveries": [], "continuations": []})
            bucket["continuations"].append(task)

    cancelled_recovery_ids = []
    repaired_dependency_task_ids = []
    created_continuation_ids = []

    def _task_sort_key(task: dict) -> tuple:
        return (
            0 if task.get("status") == "in_progress" else 1,
            0 if task.get("status") == "pending" else 1,
            task.get("created") or "",
            task.get("id") or "",
        )

    for _branch_root, bucket in branch_groups.items():
        recoveries = bucket["recoveries"]
        continuations = bucket["continuations"]
        live_continuations = [task for task in continuations if task.get("status") in ("pending", "in_progress", "failed")]
        live_recoveries = [task for task in recoveries if task.get("status") in ("pending", "in_progress", "failed")]

        canonical = None
        if live_continuations:
            canonical = sorted(live_continuations, key=_task_sort_key)[0]
        elif live_recoveries:
            canonical = sorted(live_recoveries, key=_task_sort_key)[0]
        else:
            failed_recoveries = [task for task in recoveries if task.get("status") == "failed"]
            if failed_recoveries:
                latest_failed = sorted(failed_recoveries, key=lambda task: (task.get("completed") or "", task.get("id") or ""))[-1]
                continuation_id = spawn_terminal_recovery_continuation(
                    latest_failed,
                    latest_failed.get("attempts", 0) or 0,
                    (latest_failed.get("metadata") or {}).get("error_log_excerpt", ""),
                )
                if continuation_id:
                    created_continuation_ids.append(continuation_id)
                    canonical = db.task_get(continuation_id)

        if canonical is None:
            continue

        for recovery in recoveries:
            if recovery["id"] == canonical.get("id"):
                continue
            if recovery.get("status") == "pending":
                db.task_update_status(recovery["id"], "cancelled")
                cancelled_recovery_ids.append(recovery["id"])

        blocked_ids = {task["id"] for task in recoveries if task["id"] != canonical.get("id")}
        for task in project_tasks:
            deps = task.get("dependencies") or []
            if not any(dep in blocked_ids for dep in deps):
                continue
            new_deps = task_mutations.replace_task_dependencies(
                db,
                task["id"],
                {dep: canonical["id"] for dep in blocked_ids},
            )
            if new_deps is not None:
                repaired_dependency_task_ids.append(task["id"])

        project_row = db.project_get(project)
        if project_row and project_row.get("head_task_id") in blocked_ids:
            try:
                task_chains.set_project_head(db, project, canonical["id"])
            except Exception:
                pass

    return {
        "project": project,
        "cancelled_recovery_ids": list(dict.fromkeys(cancelled_recovery_ids)),
        "repaired_dependency_task_ids": list(dict.fromkeys(repaired_dependency_task_ids)),
        "created_continuation_ids": list(dict.fromkeys(created_continuation_ids)),
    }
