"""Shared helpers for safe task mutations after integrity hardening."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def _task_metadata(task: Mapping | None) -> dict:
    return dict((task or {}).get("metadata") or {})


def _norm_deps(task: Mapping | None) -> list[str]:
    deps = (task or {}).get("dependencies") or []
    out: list[str] = []
    seen: set[str] = set()
    for dep in deps:
        if not isinstance(dep, str):
            continue
        dep_id = dep.strip()
        if not dep_id or dep_id in seen:
            continue
        seen.add(dep_id)
        out.append(dep_id)
    return out


def replace_task_dependencies(
    db: Any,
    task_id: str,
    replacements: Mapping[str, str],
    *,
    drop: Iterable[str] | None = None,
) -> list[str] | None:
    """Rewrite a task dependency list in one normalized path.

    Returns the new dependency list when a write was applied, else ``None``.
    """
    task = db.task_get(task_id)
    if not task:
        return None
    deps = _norm_deps(task)
    drop_set = {d for d in (drop or []) if isinstance(d, str) and d.strip()}
    new_deps = [replacements.get(dep, dep) for dep in deps if dep not in drop_set]
    deduped: list[str] = []
    seen: set[str] = set()
    for dep in new_deps:
        dep_id = dep.strip()
        if not dep_id or dep_id == task_id or dep_id in seen:
            continue
        seen.add(dep_id)
        deduped.append(dep_id)
    if deduped == deps:
        return None
    db.task_update(task_id, {"dependencies": deduped})
    return deduped


def reparent_dependents(
    db: Any,
    old_dependency_id: str,
    new_dependency_id: str,
    *,
    project: str | None = None,
    exclude_task_ids: Iterable[str] | None = None,
) -> list[str]:
    """Replace one dependency edge across matching live tasks."""
    exclude = {tid for tid in (exclude_task_ids or []) if isinstance(tid, str) and tid.strip()}
    updated: list[str] = []
    for task in db.task_get_all():
        if task.get("id") in exclude:
            continue
        if project and task.get("project") != project:
            continue
        deps = _norm_deps(task)
        if old_dependency_id not in deps:
            continue
        new_deps = replace_task_dependencies(
            db,
            task["id"],
            {old_dependency_id: new_dependency_id},
        )
        if new_deps is not None:
            updated.append(task["id"])
    return updated


def reset_task_to_pending(
    db: Any,
    task_id: str,
    *,
    reset_attempts: bool = True,
    clear_agent: bool = True,
) -> dict | None:
    """Reset a task into a canonical pending state."""
    task = db.task_get(task_id)
    if not task:
        return None
    updates: dict = {"status": "pending"}
    if clear_agent:
        updates["agent_id"] = None
    if reset_attempts:
        meta = _task_metadata(task)
        meta.pop("last_failure", None)
        meta.pop("failure_attempt", None)
        updates["attempts"] = 0
        updates["metadata"] = meta
    db.task_update(task_id, updates)
    return updates


def cancel_task_with_metadata(db: Any, task_id: str, extra_metadata: Mapping[str, Any]) -> dict | None:
    """Cancel a task while merging extra metadata in one place."""
    task = db.task_get(task_id)
    if not task:
        return None
    metadata = _task_metadata(task)
    metadata.update(dict(extra_metadata or {}))
    db.task_update(task_id, {"status": "cancelled", "metadata": metadata})
    return metadata
