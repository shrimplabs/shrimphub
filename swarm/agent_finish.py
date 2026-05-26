"""
swarm.agent_finish -- agent completion pipeline.

Extracted from agent_lifecycle.py. Contains _finish_agent() and all helpers
that support it: exit-code resolution, log reading, worktree phase, diff
capture, token usage, and cleanup.

All functions access agent_lifecycle state at call-time via _al() to avoid
circular imports and stale values.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Lazy accessor
# ---------------------------------------------------------------------------

def _al():
    import swarm.agent_lifecycle as _mod
    return _mod


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _AgentLogSnapshot:
    full_output: str
    tail_output: str


@dataclass(frozen=True)
class _WorktreeFinishResult:
    success: bool
    validation_failed: bool = False
    validation_error: str = ""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _resolve_agent_exit_code(script_path: Optional[str], exit_code: int) -> int:
    """Prefer the explicit exit file written by the agent runtime when present."""
    if not script_path:
        return exit_code
    exit_file = Path(script_path).with_suffix(".exit")
    if not exit_file.exists():
        return exit_code
    try:
        resolved = int(exit_file.read_text().strip())
        exit_file.unlink()
        return resolved
    except Exception:
        return exit_code


def _read_agent_log(log_path: Optional[str]) -> _AgentLogSnapshot:
    if not log_path or not Path(log_path).exists():
        return _AgentLogSnapshot(full_output="", tail_output="")
    try:
        full_output = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return _AgentLogSnapshot(full_output="", tail_output="")
    return _AgentLogSnapshot(full_output=full_output, tail_output=full_output[-2000:])


def _classify_agent_success(agent_id: str, exit_code: int, full_output: str) -> bool:
    if exit_code == 0:
        return True
    # TASK_COMPLETE may appear before the final tail in long-running agents,
    # but negative runtime messages also mention it, e.g.
    # "Task ended without TASK_COMPLETE". Require a standalone completion
    # marker or the explicit positive runtime completion log.
    has_standalone_complete = bool(re.search(r"(?m)^\s*TASK_COMPLETE\s*$", full_output or ""))
    has_runtime_complete = "[Agent] Task complete!" in (full_output or "")
    if has_standalone_complete or has_runtime_complete:
        print(f"[Swarm] Agent {agent_id[:8]} exited {exit_code} but success marker found -- treating as success")
        return True
    return False


def _active_worktree_handle(agent_id: str) -> tuple[Optional[str], Optional[str]]:
    al = _al()
    with al._handle_lock:
        handle = al._active_handles.get(agent_id, {})
        return handle.get("worktree_path"), handle.get("worktree_branch")


def _finish_worktree_phase(agent_id: str, success: bool, project: Optional[str],
                           task_id: Optional[str], wt_path_str: Optional[str],
                           wt_branch: Optional[str]) -> _WorktreeFinishResult:
    """Validate/merge/cleanup the agent worktree and return the adjusted result."""
    if not (wt_path_str and wt_branch and project):
        return _WorktreeFinishResult(success=success)

    al = _al()
    al._lazy_imports()
    worktree = al.worktree
    _validation = al._validation
    db = al.db

    worktree_path = Path(wt_path_str)
    project_path = al.WORKSPACE / project
    if success:
        _skip_types = {"manager", "project_create", "qa", "research", "harness_qa", "hybrid_qa",
                       "project_plan", "audit", "triage", "art_pass"}
        _task_for_val_early = db.task_get(task_id) if task_id else None
        _task_type_early = _task_for_val_early.get("type", "") if _task_for_val_early else ""
        if _task_for_val_early and _task_type_early not in _skip_types:
            _val_failed, _val_err = _validation._post_task_validation_in_worktree(project, task_id, worktree_path)
            if _val_failed:
                print(f"[Swarm] Pre-merge validation FAILED for agent {agent_id[:8]} -- skipping merge, preserving worktree")
                return _WorktreeFinishResult(
                    success=False,
                    validation_failed=True,
                    validation_error=_val_err,
                )

        merged = worktree._merge_worktree_branch(
            project_path,
            wt_branch,
            agent_id,
            task_id,
            worktree_path=worktree_path,
        )
        if not merged:
            return _WorktreeFinishResult(success=False)
        worktree._cleanup_worktree(project_path, worktree_path, wt_branch)
        return _WorktreeFinishResult(success=True)

    # Agent failed before declaring completion; discard its isolated worktree.
    worktree._cleanup_worktree(project_path, worktree_path, wt_branch)
    return _WorktreeFinishResult(success=False)


def _capture_project_diff_stat(project: Optional[str]) -> str:
    if not project:
        return ""
    al = _al()
    project_path = al.WORKSPACE / project
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD~1"],
            cwd=str(project_path), capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _read_agent_token_usage(task_id: Optional[str], agent_id: str) -> tuple[int, int, int]:
    input_tokens = 0
    output_tokens = 0
    loop_count = 0
    token_key = task_id or agent_id
    token_file = Path(str(_al().DATA_DIR)) / f"agent_{token_key}_tokens.json"
    if token_file.exists():
        try:
            tok = json.loads(token_file.read_text())
            input_tokens = tok.get("input", 0)
            output_tokens = tok.get("output", 0)
            loop_count = tok.get("loop_count", 0)
            token_file.unlink()
        except Exception:
            pass
    return input_tokens, output_tokens, loop_count


def _mark_agent_finished(agent_id: str, success: bool, exit_code: int,
                         output: str, diff_stat: str, input_tokens: int,
                         output_tokens: int, loop_count: int = 0):
    al = _al()
    al._lazy_imports()
    db = al.db
    agent_meta = {"diff_stat": diff_stat} if diff_stat else {}
    db.agent_update_status(
        agent_id,
        "completed" if success else "failed",
        completed_at=datetime.now().isoformat(),
        exit_code=exit_code,
        output=output[-1000:],
        metadata=json.dumps(agent_meta),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        loop_count=loop_count,
    )


def _cleanup_agent_script(script_path: Optional[str]):
    if script_path and Path(script_path).exists():
        try:
            Path(script_path).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Phase functions (called by _finish_agent in order)
# ---------------------------------------------------------------------------

def _phase_release_file_locks(agent_id: str, project: Optional[str]) -> None:
    """Phase 1: release any file locks held by this agent (safety net for killed/crashed agents)."""
    al = _al()
    _project_registry = al._project_registry
    if project and _project_registry is not None:
        try:
            freed = _project_registry.unlock_all_for_agent(project, agent_id)
            if freed:
                print(f"[Swarm] Released {len(freed)} file lock(s) for agent {agent_id[:8]}: {freed}")
        except Exception as _ul_err:
            print(f"[Swarm] WARNING: could not release file locks for agent {agent_id[:8]}: {_ul_err}")


def _phase_reparent_continuation(task_id: str, full_output: str) -> bool:
    """Phase 2: reparent dependents when the agent spawned a continuation task.

    Returns True if a continuation was detected (caller should skip normal completion steps).
    The agent logs "Continuation task created: <id>" -- we swap the original task ID out of
    all downstream dependencies so the chain doesn't unblock prematurely.
    """
    al = _al()
    al._lazy_imports()
    db = al.db
    cont_id_match = re.search(r"Continuation task created: ([^\s(]+)", full_output)
    if not cont_id_match:
        return False
    cont_id = cont_id_match.group(1)
    reparented = 0
    for t in db.task_get_all():
        deps = t.get("dependencies") or []
        if task_id in deps:
            new_deps = [cont_id if d == task_id else d for d in deps]
            db.task_update(t["id"], {"dependencies": new_deps})
            reparented += 1
    if reparented:
        print(f"[Swarm] Reparented {reparented} dependent(s) from {task_id} \u2192 continuation {cont_id}")
    return True


def _phase_complete_task(task_id: str, project: Optional[str], diff_stat: str) -> Optional[dict]:
    """Phase 3: mark task completed in DB; cancel stale recovery tasks; create plan record;
    fire webhook; refresh regression state.

    Returns the task snapshot taken just before marking complete (used by subsequent phases).
    """
    al = _al()
    al._lazy_imports()
    db = al.db
    _plan_cleanup = al._plan_cleanup
    _task_chains = al._task_chains
    _regressions = al._regressions

    from swarm.agent_recovery import _project_plan_subtasks

    task_snapshot_pre_complete = db.task_get(task_id)
    db.task_update_status(task_id, "completed", completed=datetime.now().isoformat())
    db.task_record_completed(task_id, project or "")

    # Cancel stale recovery tasks for this task (it succeeded so they're redundant).
    for rt in db.task_get_all():
        meta = rt.get("metadata") or {}
        if (meta.get("is_recovery_task") and
                meta.get("failed_task_id") == task_id and
                rt.get("status") == "pending"):
            db.task_update_status(rt["id"], "failed")
            print(f"[Swarm] Cancelled stale recovery task {rt['id'][:8]} "
                  f"(original {task_id[:8]} completed successfully)")

    task = db.task_get(task_id)
    if task:
        # Create plan record when a project_plan task completes successfully.
        if task.get("type") == "project_plan":
            plan_id = f"plan-{project}-{int(time.time())}"
            now_iso = datetime.now().isoformat()
            tasks = _project_plan_subtasks(project, task_id)
            _plan_cleanup.delete_plans_for_planner(db, project, task_id)
            _plan_cleanup.delete_matching_plans(db, project, [t["id"] for t in tasks])
            db.plan_upsert({
                "id": plan_id,
                "project": project,
                "planner_task_id": task_id,
                "created_at": now_iso,
                "task_ids": [t["id"] for t in tasks],
                "task_graph": tasks,
            })
            for t in tasks:
                db.task_update(t["id"], {"plan_id": plan_id})
            from swarm.integrity import is_continuity_eligible_task
            continuity_tasks = [t for t in tasks if is_continuity_eligible_task(t)]
            if continuity_tasks:
                continuity_tasks.sort(
                    key=lambda t: (
                        t.get("completed") or t.get("created") or t.get("started") or "",
                        t.get("id") or "",
                    )
                )
                try:
                    _task_chains.set_project_head(db, project, continuity_tasks[-1]["id"])
                except Exception as head_err:
                    print(f"[Swarm] WARNING: could not advance project head after planner {task_id}: {head_err}")
            print(f"[Swarm] Created plan {plan_id} for {project} ({len(tasks)} task(s) assigned)")

        al._fire_task_webhook(
            "task_completed",
            project=project or "",
            task_id=task_id,
            task_type=task.get("type", ""),
            description=task.get("description", "").split("\n")[0][:100],
            diff_stat=diff_stat,
        )

    # Refresh regression state after any successful completion.
    if project and _regressions is not None:
        try:
            proj_row = db.project_get(project)
            if proj_row and (proj_row.get("open_regression_count") or 0) > 0:
                task_type_for_reg = (task_snapshot_pre_complete or {}).get("type", "")
                snap_meta = (task_snapshot_pre_complete or {}).get("metadata") or {}
                if task_type_for_reg in {"bug", "triage"} or (
                    snap_meta.get("is_recovery_task") or snap_meta.get("is_closure_repair_task")
                ):
                    resolved = _regressions.resolve_regressions_for_linked_task(task_id, project)
                    if resolved:
                        print(f"[Swarm] Auto-resolved {len(resolved)} regression(s) for {project} after task {task_id[:8]} completed")
                _regressions.refresh_project_recurrence_state(project)
                refreshed_row = db.project_get(project)
                new_count = (refreshed_row or {}).get("open_regression_count", 0)
                new_status = (refreshed_row or {}).get("closure_status", "")
                print(f"[Swarm] Refreshed regression state for {project}: open_regression_count={new_count} closure_status={new_status}")
        except Exception as reg_err:
            print(f"[Swarm] WARNING: regression state refresh failed for {project}: {reg_err}")

    return task_snapshot_pre_complete


def _phase_handle_worktree_validation_failure(
    agent_id: str, task_id: str, project: str,
    wt_path_str: str, wt_branch: str,
    validation_error_output: str, task_snapshot_early: Optional[dict],
) -> None:
    """Phase 4 (failure path): handle a pre-merge validation failure.

    Cross-checks main to distinguish real regressions from stale worktree errors.
    Spawns a bug task for real regressions; marks complete for stale errors.
    Early-returns after calling _handle_task_failure for config blockers.
    """
    al = _al()
    al._lazy_imports()
    db = al.db
    _validation = al._validation

    from swarm.agent_recovery import _handle_task_failure

    main_failed, main_err = _validation._post_task_validation_in_worktree(
        project, task_id, worktree_path=None
    )
    if _validation.is_controller_config_blocker(main_err):
        print(f"[Swarm] Validation blocked by controller configuration for {project}: {main_err}")
        _handle_task_failure(task_id, project, main_err, _task_snapshot=task_snapshot_early)
        return

    if not main_failed:
        print(f"[Swarm] Worktree validation failed for {task_id} but main is clean "
              f"-- skipping bug task (stale worktree error)")
        db.task_update_status(task_id, "completed", completed=datetime.now().isoformat())
        db.task_record_completed(task_id, project or "")
        return

    # Main also has errors -- real regression, spawn the bug task.
    current_task = db.task_get(task_id)
    task_meta = (current_task.get("metadata") or {}) if current_task else {}
    existing_notes = task_meta.get("fix_notes", [])
    error_before = task_meta.get("error_log", "")
    summary = _validation._llm_summarise_fix_attempt(
        error_before, validation_error_output, Path(wt_path_str)
    )
    fix_notes = existing_notes + ([summary] if summary else [])
    _validation._spawn_validation_bug_task(
        project, task_id, validation_error_output,
        worktree_path=Path(wt_path_str),
        worktree_branch=wt_branch,
        fix_notes=fix_notes,
        original_task=current_task,
    )
    # The bug task now owns the remaining work -- mark the original completed.
    db.task_update_status(task_id, "completed", completed=datetime.now().isoformat())
    db.task_record_completed(task_id, project or "")
    print(f"[Swarm] Marked {task_id} completed (validation bug task takes over)")


def _phase_post_completion_pipeline(
    agent_id: str, task_id: str, project: str,
    full_output: str, diff_stat: str,
    task_snapshot_pre_complete: Optional[dict],
    wt_path_str: Optional[str],
    spawned_continuation: bool,
    task_snapshot_early: Optional[dict],
    exit_code: int,
    log_path: Optional[str],
) -> None:
    """Phase 5 (success path): plan validation, post-validation, auto-integration,
    sprint QA, auto-QA, auto-audit, and learnings extraction.

    All of these run *after* the task is marked completed and the agent is recorded.
    Ordering matters: plan validation may flip success→failure, which suppresses auto-tasks.
    """
    al = _al()
    al._lazy_imports()
    db = al.db
    _validation = al._validation
    _learnings = al._learnings
    _plan_cleanup = al._plan_cleanup

    from swarm.agent_recovery import (
        _handle_task_failure,
        _validate_project_plan_subtasks,
        _project_plan_subtasks,
    )
    from swarm.agent_auto_tasks import (
        auto_spawn_integration_task,
        auto_handle_sprint_qa,
        auto_spawn_qa_task,
    )

    # Learnings extraction: fire-and-forget, doesn't block reaping.
    task_for_learnings = db.task_get(task_id) if task_id else None
    task_type_for_learnings = task_for_learnings.get("type", "") if task_for_learnings else ""
    if log_path and project and task_type_for_learnings:
        _learnings.extract_learnings_async(
            task_id, task_type_for_learnings, project, log_path, exit_code, str(al.DATA_DIR)
        )

    success = True  # Will be set False if plan validation fails.

    # Plan validation: project_plan tasks must create tagged subtasks with real dep IDs.
    task_for_san = db.task_get(task_id)
    if task_for_san and task_for_san.get("type") == "project_plan":
        plan_errors = _validate_project_plan_subtasks(project, task_id)
        if plan_errors:
            print(f"[Swarm] project_plan {task_id} invalid \u2014 treating as failure")
            for err in plan_errors[:10]:
                print(f"[Swarm]   plan error: {err}")
            subs = _project_plan_subtasks(project, task_id)
            for sub in subs:
                if sub.get("status") == "pending":
                    db.task_update_status(sub["id"], "cancelled")
            deleted_for_planner = _plan_cleanup.delete_plans_for_planner(db, project, task_id)
            deleted = _plan_cleanup.delete_matching_plans(db, project, [t["id"] for t in subs])
            all_deleted = list(dict.fromkeys(deleted_for_planner + deleted))
            if all_deleted:
                print(f"[Swarm] Deleted stale plan snapshot(s): {all_deleted}")
            attempts = (task_for_san.get("attempts") or 0) + 1
            max_att = task_for_san.get("max_attempts", 3)
            db.task_update_status(task_id, "pending" if attempts < max_att else "failed", attempts=attempts)
            success = False

    if not success:
        return  # Plan validation failed -- no auto-tasks.

    # Post-validation for non-worktree tasks (worktree tasks validated pre-merge above).
    _skip_types = {"manager", "project_create", "qa", "research", "harness_qa", "hybrid_qa",
                   "project_plan", "audit", "art_pass"}
    # Re-fetch the task but fall back to pre-completion snapshot if monitor pruned it.
    task_for_val = db.task_get(task_id) or task_snapshot_pre_complete
    task_type_finished = task_for_val.get("type", "") if task_for_val else ""
    validation_failed_after_completion = False

    if not (task_for_val and task_type_finished in _skip_types) and not spawned_continuation:
        if not wt_path_str:
            task_snapshot_for_validation = dict(task_for_val) if task_for_val else None
            main_failed, main_err = _validation._post_task_validation_in_worktree(
                project, task_id, worktree_path=None,
            )
            if main_failed and main_err:
                validation_failed_after_completion = True
                if _validation.is_controller_config_blocker(main_err):
                    print(f"[Swarm] Post-validation blocked by controller configuration for {project}; not spawning validation bug")
                    _handle_task_failure(task_id, project, main_err, _task_snapshot=task_snapshot_early)
                else:
                    print(f"[Swarm] Post-validation FAILED for {project} {task_id} -- spawning validation bug before follow-on work")
                    _validation._spawn_validation_bug_task(
                        project, task_id, main_err,
                        original_task=task_snapshot_for_validation,
                    )
            else:
                try:
                    _validation.run_closure_verification(project, task_id)
                except Exception as closure_err:
                    print(f"[Swarm] Closure verification trigger failed for {project} {task_id}: {closure_err}")

    task_metadata_for_qa = (task_for_val or {}).get("metadata") or {}
    is_recovery_task = task_metadata_for_qa.get("is_recovery_task", False)

    auto_spawn_integration_task(
        project=project,
        task_id=task_id,
        task_type_finished=task_type_finished,
        task_for_val=task_for_val,
        diff_stat=diff_stat,
        workspace=al.WORKSPACE,
        validation_failed=validation_failed_after_completion,
    )

    auto_handle_sprint_qa(
        project=project,
        task_type_finished=task_type_finished,
        auto_replan_projects=al.AUTO_REPLAN_PROJECTS,
        projects_sprint_qa_done=al._projects_sprint_qa_done,
        validation_failed=validation_failed_after_completion,
    )

    auto_spawn_qa_task(
        project=project,
        task_id=task_id,
        task_type_finished=task_type_finished,
        task_for_val=task_for_val,
        workspace=al.WORKSPACE,
        auto_replan_projects=al.AUTO_REPLAN_PROJECTS,
        qa_completion_counter=al._qa_completion_counter,
        qa_auto_threshold=al.QA_AUTO_THRESHOLD,
        validation_failed=validation_failed_after_completion,
        spawned_continuation=spawned_continuation,
        is_recovery_task=is_recovery_task,
    )



# ---------------------------------------------------------------------------
# Main completion handler
# ---------------------------------------------------------------------------

def _finish_agent(agent_id: str, exit_code: int, project: Optional[str],
                  task_id: Optional[str], script_path: Optional[str],
                  log_path: Optional[str]):
    """Teardown for a finished agent.

    Runs the completion pipeline in strict phase order:
      1. Release file locks (safety net for crashed agents)
      2. Resolve exit code, read log, classify success
      3. Worktree phase (validate/merge/cleanup)
      4. Capture diff stat + token usage; mark agent finished in DB
      5. Task outcome path:
         a. Success: reparent continuation dependents → complete task in DB
         b. Failure: handle worktree validation failure or normal failure
      6. Release project lock; clean up script file
      7. Post-completion pipeline (plan validation, post-validation, auto-tasks, learnings)
    """
    al = _al()
    al._lazy_imports()
    db = al.db

    from swarm.agent_recovery import _handle_task_failure

    # Phase 1 -- release file locks.
    _phase_release_file_locks(agent_id, project)

    # Phase 2 -- resolve exit code, read log, classify success.
    exit_code = _resolve_agent_exit_code(script_path, exit_code)
    log_snapshot = _read_agent_log(log_path)
    full_output = log_snapshot.full_output
    output = log_snapshot.tail_output
    success = _classify_agent_success(agent_id, exit_code, full_output)

    # Phase 3 -- worktree phase (validate pre-merge, merge, cleanup).
    wt_path_str, wt_branch = _active_worktree_handle(agent_id)
    worktree_result = _finish_worktree_phase(
        agent_id, success, project, task_id, wt_path_str, wt_branch,
    )
    success = worktree_result.success
    validation_failed_in_worktree = worktree_result.validation_failed
    validation_error_output = worktree_result.validation_error

    # Phase 4 -- diff, tokens, mark agent finished.
    diff_stat = _capture_project_diff_stat(project)
    input_tokens, output_tokens, loop_count = _read_agent_token_usage(task_id, agent_id)
    _mark_agent_finished(agent_id, success, exit_code, output, diff_stat, input_tokens, output_tokens, loop_count)

    # Phase 5 -- task outcome.
    task_snapshot_pre_complete: Optional[dict] = None
    spawned_continuation = False

    if task_id:
        # Snapshot the task now, before status mutations or prune_history() races.
        task_snapshot_early = db.task_get(task_id)

        # Phase 5a -- reparent any continuation task the agent spawned, regardless of
        # success or failure. If this runs only on success, a failed task leaves the
        # continuation depending on the failed task itself — permanent deadlock.
        spawned_continuation = _phase_reparent_continuation(task_id, full_output)

        if success:
            # Phase 5b -- success path.
            task_snapshot_pre_complete = _phase_complete_task(task_id, project, diff_stat)
        else:
            # Phase 5c -- failure path.
            if validation_failed_in_worktree and project and wt_path_str and wt_branch:
                _phase_handle_worktree_validation_failure(
                    agent_id, task_id, project,
                    wt_path_str, wt_branch,
                    validation_error_output, task_snapshot_early,
                )
            else:
                _handle_task_failure(task_id, project, full_output or output,
                                     _task_snapshot=task_snapshot_early)
    else:
        task_snapshot_early = None

    # Phase 6 -- release project lock; clean up script.
    if project and al.LOCK_PROJECT:
        db.project_set_locked(project, False)

    _cleanup_agent_script(script_path)

    print(f"[Swarm] Agent {agent_id[:8]} finished (exit {exit_code})"
          + (f" diff: {diff_stat.splitlines()[-1]}" if diff_stat else ""))

    # Phase 7 -- post-completion pipeline (only for successful tasks).
    if success and task_id and project:
        _phase_post_completion_pipeline(
            agent_id=agent_id,
            task_id=task_id,
            project=project,
            full_output=full_output,
            diff_stat=diff_stat,
            task_snapshot_pre_complete=task_snapshot_pre_complete,
            wt_path_str=wt_path_str,
            spawned_continuation=spawned_continuation,
            task_snapshot_early=task_snapshot_early,
            exit_code=exit_code,
            log_path=log_path,
        )
