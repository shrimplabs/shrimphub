"""Shared integrity predicates for task, project, and chain state."""

from __future__ import annotations

from typing import Mapping, Optional


ACTIVE_TASK_STATUSES = frozenset({"pending", "in_progress"})
TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})
CONTINUITY_ELIGIBLE_TASK_STATUSES = frozenset({"pending", "in_progress", "completed"})
BLOCKING_FAILURE_TASK_STATUSES = frozenset({"failed", "cancelled"})


def task_status(task: Optional[Mapping]) -> str:
    if not task:
        return ""
    status = task.get("status")
    return status.strip() if isinstance(status, str) else ""


def task_project(task: Optional[Mapping]) -> str:
    if not task:
        return ""
    project = task.get("project")
    return project.strip() if isinstance(project, str) else ""


def is_active_task(task: Optional[Mapping]) -> bool:
    return task_status(task) in ACTIVE_TASK_STATUSES


def is_terminal_task(task: Optional[Mapping]) -> bool:
    return task_status(task) in TERMINAL_TASK_STATUSES


def is_blocking_failure_task(task: Optional[Mapping]) -> bool:
    return task_status(task) in BLOCKING_FAILURE_TASK_STATUSES


def is_continuity_eligible_task(task: Optional[Mapping]) -> bool:
    return task_status(task) in CONTINUITY_ELIGIBLE_TASK_STATUSES


def is_valid_project_head(task: Optional[Mapping], project_name: str) -> bool:
    return bool(task) and task_project(task) == project_name and is_continuity_eligible_task(task)


def task_agent_id(task: Optional[Mapping]) -> str:
    if not task:
        return ""
    agent_id = task.get("agent_id")
    return agent_id.strip() if isinstance(agent_id, str) else ""


def can_task_accept_agent(task: Optional[Mapping]) -> bool:
    return bool(task) and task_status(task) == "pending" and not task_agent_id(task)


def active_agent_matches_task(agent: Optional[Mapping], task: Optional[Mapping]) -> bool:
    if not agent or not task:
        return False
    if task_status(task) != "in_progress":
        return False
    agent_task_id = agent.get("task_id")
    task_id = task.get("id")
    if not isinstance(agent_task_id, str) or not isinstance(task_id, str) or agent_task_id != task_id:
        return False
    if task_project(task) != (agent.get("project") or "").strip():
        return False
    return task_agent_id(task) == (agent.get("id") or "").strip()
