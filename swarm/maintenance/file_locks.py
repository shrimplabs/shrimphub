"""Maintenance helpers for project file locks."""

from __future__ import annotations

import os
from typing import Any


def _lock_value(lock: Any, key: str):
    if isinstance(lock, dict):
        return lock.get(key)
    return getattr(lock, key, None)


def pid_is_running(pid) -> bool:
    try:
        if not pid:
            return False
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError, OSError):
        return False
    except PermissionError:
        return True


def file_lock_owner_is_live(db, project_name: str, lock) -> bool:
    """Return whether a file lock belongs to a currently running task agent.

    The task row is the source of truth. A lock without an in-progress task and
    live active agent is stale and should not block future work.
    """
    if not lock:
        return False

    owner_task_id = _lock_value(lock, "task_id")
    if owner_task_id:
        task = db.task_get(owner_task_id)
        if not task or task.get("project") != project_name or task.get("status") != "in_progress":
            return False

        task_agent_id = (task.get("agent_id") or "").strip()
        for agent in db.agent_get_active():
            if agent.get("project") != project_name:
                continue
            if agent.get("task_id") != owner_task_id:
                continue
            if task_agent_id and agent.get("id") != task_agent_id:
                continue
            if pid_is_running(agent.get("pid")):
                return True
        return False

    owner_agent_id = _lock_value(lock, "locked_by")
    if owner_agent_id:
        agent = db.agent_get(owner_agent_id)
        if agent and agent.get("project") == project_name and agent.get("status") == "active":
            return pid_is_running(agent.get("pid"))

        # Older runtime builds wrote TASK_ID into locked_by. Treat that as live
        # only if the corresponding task still has a running active agent.
        task = db.task_get(owner_agent_id)
        if task and task.get("project") == project_name and task.get("status") == "in_progress":
            task_agent_id = (task.get("agent_id") or "").strip()
            for active in db.agent_get_active():
                if active.get("project") != project_name:
                    continue
                if active.get("task_id") != owner_agent_id:
                    continue
                if task_agent_id and active.get("id") != task_agent_id:
                    continue
                if pid_is_running(active.get("pid")):
                    return True
    return False


def _lock_to_finding(project_name: str, lock) -> dict:
    return {
        "project": project_name,
        "file_path": _lock_value(lock, "file_path"),
        "locked_by": _lock_value(lock, "locked_by"),
        "locked_at": _lock_value(lock, "locked_at"),
        "task_id": _lock_value(lock, "task_id"),
    }


def find_stale_file_locks(db, project_registry, project: str | None = None) -> list[dict]:
    """Return file locks whose owner task/agent is no longer live."""
    if project:
        project_names = [project]
    else:
        project_names = sorted(project_registry.get_all().keys())

    findings: list[dict] = []
    for project_name in project_names:
        for lock in project_registry.get_locked_files(project_name).values():
            if not file_lock_owner_is_live(db, project_name, lock):
                findings.append(_lock_to_finding(project_name, lock))
    return findings


def reconcile_stale_file_locks(db, project_registry, project: str | None = None) -> list[dict]:
    """Remove stale file locks without touching locks whose owner is still live."""
    removed: list[dict] = []
    for finding in find_stale_file_locks(db, project_registry, project=project):
        remove = getattr(project_registry, "remove_file_lock", None)
        if remove is None:
            continue
        if remove(
            finding["project"],
            finding["file_path"],
            previous_locked_by=finding.get("locked_by"),
            previous_task_id=finding.get("task_id"),
        ):
            removed.append(finding)
    return removed
