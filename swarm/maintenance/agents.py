"""Agent/runtime reconciliation helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Callable


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

    def _drop_active_handle(agent_id: str) -> None:
        try:
            active_handles.pop(agent_id, None)
        except AttributeError:
            pass

    def _terminate_handle(agent_id: str, handle: Any) -> None:
        process = handle.get("process") if isinstance(handle, Mapping) else None
        if not process:
            return
        try:
            logger(f"[SwarmKill] reason=stale_active_handle agent={agent_id[:8]} pid={process.pid}")
            process.kill()
            process.wait(timeout=5)
        except Exception as exc:
            logger(f"[Swarm] Could not terminate stale active handle {agent_id[:8]}: {exc}")

    def _fail_agent_and_repair_task(agent: dict, task: dict | None, reason: str) -> None:
        aid = agent["id"]
        task_id = agent.get("task_id")
        logger(f"[Swarm] {reason} for {aid[:8]} - failing agent and resetting task if needed")
        db.agent_update_status(
            aid,
            "failed",
            completed_at=datetime.now().isoformat(),
            exit_code=agent.get("exit_code") or -1,
        )
        repaired_agents.append(aid)
        if task_id and task and task.get("status") == "in_progress":
            task_mutations.reset_task_to_pending(db, task_id, reset_attempts=False)
            reset_tasks.append(task_id)

    # First repair live in-memory handles. These are skipped by the orphan-agent
    # path below, so they need their own ownership check. Without this, recovery
    # code can mark a task terminal while the subprocess keeps running until it
    # exits naturally.
    for aid, handle in list(active_handles.items()):
        if aid in (finishing_agents or frozenset()):
            continue
        agent = db.agent_get(aid)
        task_id = (agent or {}).get("task_id") or (handle.get("task_id") if isinstance(handle, Mapping) else None)
        task = db.task_get(task_id) if task_id else None
        if agent is None:
            logger(f"[Swarm] Active handle {aid[:8]} has no DB agent row - terminating")
            _terminate_handle(aid, handle)
            _drop_active_handle(aid)
            repaired_agents.append(aid)
            continue
        if not active_agent_matches_task(agent, task):
            _terminate_handle(aid, handle)
            _drop_active_handle(aid)
            try:
                _fail_agent_and_repair_task(agent, task, "Stale live agent/task ownership")
            except Exception as exc:
                logger(f"[Swarm] Could not repair stale live ownership for {aid[:8]}: {exc}")

    for agent in db.agent_get_active():
        aid = agent["id"]
        if aid in known:
            continue
        task_id = agent.get("task_id")
        task = db.task_get(task_id) if task_id else None
        if task is None or not active_agent_matches_task(agent, task):
            try:
                _fail_agent_and_repair_task(agent, task, "Stale agent/task ownership")
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
