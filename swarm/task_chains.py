"""Helpers for keeping task graphs attached to each project's live chain."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from swarm.maintenance.project_heads import ensure_project_head, get_project_head, infer_project_tail, reconcile_project_head, set_project_head


def _dedupe(ids: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in ids:
        if not isinstance(item, str):
            continue
        task_id = item.strip()
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        out.append(task_id)
    return out


def chain_to_project_head(
    db: Any,
    project_name: str,
    dependencies: Optional[Iterable[str]] = None,
    *,
    task_id: Optional[str] = None,
    ensure_head: bool = False,
) -> list[str]:
    deps = _dedupe(dependencies or [])
    if deps:
        return deps
    head = ensure_project_head(db, project_name) if ensure_head else get_project_head(db, project_name)
    if head and head != task_id:
        return [head]
    return deps


def append_project_head(
    db: Any,
    project_name: str,
    dependencies: Optional[Iterable[str]] = None,
    *,
    task_id: Optional[str] = None,
    ensure_head: bool = False,
) -> list[str]:
    deps = _dedupe(dependencies or [])
    head = ensure_project_head(db, project_name) if ensure_head else get_project_head(db, project_name)
    if head and head != task_id and head not in deps:
        deps.append(head)
    return deps


def anchor_project_batch_roots(
    batch_tasks: Iterable[dict[str, Any]],
    head_id: Optional[str],
) -> list[dict[str, Any]]:
    """Attach the project head only to true batch roots.

    This is for project/task batch creation flows where intra-batch dependencies
    must be preserved exactly as planned. Applying chain_to_project_head() to
    each task independently can accidentally flatten the whole batch if the
    intended dependencies are resolved elsewhere.
    """
    out: list[dict[str, Any]] = []
    for task in batch_tasks:
        current = dict(task)
        deps = _dedupe(current.get("dependencies") or [])
        if not deps and head_id and head_id != current.get("id"):
            deps = [head_id]
        current["dependencies"] = deps
        out.append(current)
    return out
