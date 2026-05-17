"""Agent/runtime reconciliation helpers."""

from __future__ import annotations

from typing import Any, Callable, Mapping


def reconcile_agent_runtime_state(
    *,
    db: Any,
    active_handles: Mapping[str, Any],
    finish_agent: Callable[..., None],
    is_pid_running: Callable[[int], bool],
    active_agent_matches_task: Callable[[dict, dict | None], bool],
    task_mutations: Any,
    prune_history: Callable[[], None],
    logger: Callable[[str], None] = print,
    prune: bool = True,
    finishing_agents: frozenset | None = None,
) -> dict:
    """Repair drift between DB-tracked agents and live task ownership."""
    repaired_agents = []
    reset_tasks = []
    known = set(active_handles.keys()) | (finishing_agents or frozenset())
    for agent in db.agent_get_active():
        aid = agent["id"]
        if aid in known:
            continue
        task_id = agent.get("task_id")
        task = db.task_get(task_id) if task_id else None
        if task is None or not active_agent_matches_task(agent, task):
            logger(f"[Swarm] Stale agent/task ownership for {aid[:8]} - failing agent and resetting task if needed")
            try:
                db.agent_update_status(aid, "failed", exit_code=agent.get("exit_code") or -1)
                repaired_agents.append(aid)
                if task_id and task and task.get("status") == "in_progress":
                    task_mutations.reset_task_to_pending(db, task_id, reset_attempts=False)
                    reset_tasks.append(task_id)
            except Exception as exc:
                logger(f"[Swarm] Could not repair stale ownership for {aid[:8]}: {exc}")
            continue
        pid = agent.get("pid")
        if pid and is_pid_running(pid):
            continue
        exit_code = agent.get("exit_code") or 1
        try:
            finish_agent(
                aid, exit_code,
                agent.get("project"), agent.get("task_id"),
                agent.get("script_path"), agent.get("log_path"),
            )
            repaired_agents.append(aid)
        except Exception as exc:
            logger(f"[Swarm] Error finishing stale agent {aid[:8]}: {exc} - forcing to failed")
            try:
                db.agent_update_status(aid, "failed", exit_code=exit_code)
                repaired_agents.append(aid)
                task_id = agent.get("task_id")
                if task_id:
                    task = db.task_get(task_id)
                    if task and task.get("status") == "in_progress":
                        task_mutations.reset_task_to_pending(db, task_id, reset_attempts=False)
                        reset_tasks.append(task_id)
            except Exception as exc2:
                logger(f"[Swarm] Could not even force-fail agent {aid[:8]}: {exc2}")

    in_prog = db.task_get_by_status("in_progress")
    active_agents_by_task = {
        agent["task_id"]: agent for agent in db.agent_get_active() if agent.get("task_id")
    }
    for task in in_prog:
        active_agent = active_agents_by_task.get(task["id"])
        if not active_agent or not active_agent_matches_task(active_agent, task):
            logger(f"[Swarm] Watchdog: task {task['id']} has invalid agent ownership - resetting")
            task_mutations.reset_task_to_pending(db, task["id"], reset_attempts=False)
            reset_tasks.append(task["id"])

    if prune:
        prune_history()
    return {
        "repaired_agent_ids": list(dict.fromkeys(repaired_agents)),
        "reset_task_ids": list(dict.fromkeys(reset_tasks)),
    }

