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


def _read_agent_token_usage(task_id: Optional[str], agent_id: str) -> tuple[int, int]:
    input_tokens = 0
    output_tokens = 0
    token_key = task_id or agent_id
    token_file = Path(str(_al().DATA_DIR)) / f"agent_{token_key}_tokens.json"
    if token_file.exists():
        try:
            tok = json.loads(token_file.read_text())
            input_tokens = tok.get("input", 0)
            output_tokens = tok.get("output", 0)
            token_file.unlink()
        except Exception:
            pass
    return input_tokens, output_tokens


def _mark_agent_finished(agent_id: str, success: bool, exit_code: int,
                         output: str, diff_stat: str, input_tokens: int,
                         output_tokens: int):
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
    )


def _cleanup_agent_script(script_path: Optional[str]):
    if script_path and Path(script_path).exists():
        try:
            Path(script_path).unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main completion handler
# ---------------------------------------------------------------------------

def _finish_agent(agent_id: str, exit_code: int, project: Optional[str],
                  task_id: Optional[str], script_path: Optional[str],
                  log_path: Optional[str]):
    """Teardown for a finished agent."""
    al = _al()
    al._lazy_imports()
    db = al.db
    _validation = al._validation
    _learnings = al._learnings
    _plan_cleanup = al._plan_cleanup
    _task_chains = al._task_chains
    _regressions = al._regressions
    _project_registry = al._project_registry

    from swarm.agent_recovery import (
        _handle_task_failure,
        _validate_project_plan_subtasks,
        _project_plan_subtasks,
    )
    from swarm.agent_auto_tasks import (
        auto_spawn_integration_task,
        auto_handle_sprint_qa,
        auto_spawn_qa_task,
        auto_spawn_audit_task,
    )

    # Release any file locks held by this agent so other agents aren't blocked.
    # This is a safety net -- the agent should unlock files itself, but if it was
    # killed, timed out, or crashed the locks would otherwise linger until restart.
    if project and _project_registry is not None:
        try:
            freed = _project_registry.unlock_all_for_agent(project, agent_id)
            if freed:
                print(f"[Swarm] Released {len(freed)} file lock(s) for agent {agent_id[:8]}: {freed}")
        except Exception as _ul_err:
            print(f"[Swarm] WARNING: could not release file locks for agent {agent_id[:8]}: {_ul_err}")

    exit_code = _resolve_agent_exit_code(script_path, exit_code)
    log_snapshot = _read_agent_log(log_path)
    full_output = log_snapshot.full_output
    output = log_snapshot.tail_output
    success = _classify_agent_success(agent_id, exit_code, full_output)

    wt_path_str, wt_branch = _active_worktree_handle(agent_id)
    worktree_result = _finish_worktree_phase(
        agent_id,
        success,
        project,
        task_id,
        wt_path_str,
        wt_branch,
    )
    success = worktree_result.success
    _validation_failed_in_worktree = worktree_result.validation_failed
    _validation_error_output = worktree_result.validation_error

    diff_stat = _capture_project_diff_stat(project)
    input_tokens, output_tokens = _read_agent_token_usage(task_id, agent_id)
    _mark_agent_finished(agent_id, success, exit_code, output, diff_stat, input_tokens, output_tokens)
    _task_snapshot_pre_complete: Optional[dict] = None

    if task_id:
        # Snapshot the task now, before any status mutations or prune_history()
        # races can delete it from the DB.
        _task_snapshot_early = db.task_get(task_id)
        _spawned_continuation = False

        if success:
            # Reparent dependents if a continuation task was spawned.
            # The agent logs "Continuation task created: <id>" -- parse it and swap
            # the original task ID out of all downstream dependencies so the chain
            # doesn't unblock prematurely before the continuation finishes.
            _cont_id_match = re.search(r"Continuation task created: ([^\s(]+)", full_output)
            _spawned_continuation = bool(_cont_id_match)
            if _cont_id_match:
                _cont_id = _cont_id_match.group(1)
                _reparented = 0
                for _t in db.task_get_all():
                    _deps = _t.get("dependencies") or []
                    if task_id in _deps:
                        _new_deps = [_cont_id if d == task_id else d for d in _deps]
                        db.task_update(_t["id"], {"dependencies": _new_deps})
                        _reparented += 1
                if _reparented:
                    print(f"[Swarm] Reparented {_reparented} dependent(s) from {task_id} \u2192 continuation {_cont_id}")

            _task_snapshot_pre_complete = db.task_get(task_id)
            db.task_update_status(
                task_id, "completed",
                completed=datetime.now().isoformat(),
            )
            db.task_record_completed(task_id, project or "")
            # Cancel any stale recovery tasks that were spawned for this task
            # while it was retrying -- now that it succeeded they're redundant.
            _all = db.task_get_all()
            for _rt in _all:
                _meta = _rt.get("metadata") or {}
                if (_meta.get("is_recovery_task") and
                        _meta.get("failed_task_id") == task_id and
                        _rt.get("status") == "pending"):
                    db.task_update_status(_rt["id"], "failed")
                    print(f"[Swarm] Cancelled stale recovery task {_rt['id'][:8]} "
                          f"(original {task_id[:8]} completed successfully)")
            task = db.task_get(task_id)
            if task:
                # Create plan record when a project_plan task completes successfully
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
                        except Exception as _head_err:
                            print(f"[Swarm] WARNING: could not advance project head after planner {task_id}: {_head_err}")
                    print(f"[Swarm] Created plan {plan_id} for {project} "
                          f"({len(tasks)} task(s) assigned)")

                al._fire_task_webhook(
                    "task_completed",
                    project=project or "",
                    task_id=task_id,
                    task_type=task.get("type", ""),
                    description=task.get("description", "").split("\n")[0][:100],
                    diff_stat=diff_stat,
                )

            # After any successful task completion, re-sync open_regression_count
            # from the actual regressions table. This unblocks expansion when a bug
            # fix resolves an issue that had only ever seen timeout-based validation
            # (so resolve_regressions_for_passing_run never fired).
            if project and _regressions is not None:
                try:
                    _proj_row = db.project_get(project)
                    if _proj_row and (_proj_row.get("open_regression_count") or 0) > 0:
                        # If this was a bug/repair task, close any regression it was linked to.
                        _task_type_for_reg = (_task_snapshot_pre_complete or {}).get("type", "")
                        if _task_type_for_reg in {"bug", "triage"} or (
                            (_task_snapshot_pre_complete or {}).get("metadata", {}).get("is_recovery_task")
                            or (_task_snapshot_pre_complete or {}).get("metadata", {}).get("is_closure_repair_task")
                        ):
                            _resolved = _regressions.resolve_regressions_for_linked_task(task_id, project)
                            if _resolved:
                                print(f"[Swarm] Auto-resolved {len(_resolved)} regression(s) for {project} after task {task_id[:8]} completed")
                        # Always refresh the counters in case something else cleared regressions
                        _regressions.refresh_project_recurrence_state(project)
                        _refreshed_row = db.project_get(project)
                        _new_count = (_refreshed_row or {}).get("open_regression_count", 0)
                        _new_status = (_refreshed_row or {}).get("closure_status", "")
                        print(f"[Swarm] Refreshed regression state for {project}: open_regression_count={_new_count} closure_status={_new_status}")
                except Exception as _reg_err:
                    print(f"[Swarm] WARNING: regression state refresh failed for {project}: {_reg_err}")
        else:
            # If validation failed in-worktree, spawn bug task with worktree metadata
            # before calling _handle_task_failure (which handles retry/recovery logic)
            if _validation_failed_in_worktree and project and wt_path_str and wt_branch:
                # Cross-check main before spawning a worktree bug task.
                # If main is already clean the worktree error is stale (another agent
                # already fixed it in main). Spawning a bug task in the diverged
                # worktree would create an infinite chain chasing a ghost -- skip it.
                _main_failed, _main_err = _validation._post_task_validation_in_worktree(
                    project, task_id, worktree_path=None
                )
                if _validation.is_controller_config_blocker(_main_err):
                    print(f"[Swarm] Validation blocked by controller configuration for {project}: {_main_err}")
                    _handle_task_failure(task_id, project, _main_err, _task_snapshot=_task_snapshot_early)
                    return
                if not _main_failed:
                    print(f"[Swarm] Worktree validation failed for {task_id} but main is clean "
                          f"-- skipping bug task (stale worktree error)")
                    db.task_update_status(task_id, "completed", completed=datetime.now().isoformat())
                    db.task_record_completed(task_id, project or "")
                else:
                    # Main also has errors -- this is a real regression, spawn the bug task.
                    _current_task = db.task_get(task_id)
                    _task_meta = (_current_task.get("metadata") or {}) if _current_task else {}
                    _existing_notes = _task_meta.get("fix_notes", [])
                    _error_before = _task_meta.get("error_log", "")
                    _summary = _validation._llm_summarise_fix_attempt(
                        _error_before, _validation_error_output, Path(wt_path_str)
                    )
                    _fix_notes = _existing_notes + ([_summary] if _summary else [])
                    _validation._spawn_validation_bug_task(
                        project, task_id, _validation_error_output,
                        worktree_path=Path(wt_path_str),
                        worktree_branch=wt_branch,
                        fix_notes=_fix_notes,
                        original_task=_current_task,
                    )
                    # The bug task now owns the remaining work -- mark the original as
                    # completed so it doesn't retry and race against the bug task.
                    db.task_update_status(task_id, "completed", completed=datetime.now().isoformat())
                    db.task_record_completed(task_id, project or "")
                    print(f"[Swarm] Marked {task_id} completed (validation bug task takes over)")
            else:
                _handle_task_failure(task_id, project, full_output or output,
                                     _task_snapshot=_task_snapshot_early)

    if project and al.LOCK_PROJECT:
        db.project_set_locked(project, False)

    if script_path and Path(script_path).exists():
        try:
            Path(script_path).unlink()
        except Exception:
            pass

    print(f"[Swarm] Agent {agent_id[:8]} finished (exit {exit_code})"
          + (f" diff: {diff_stat.splitlines()[-1]}" if diff_stat else ""))

    # Extract learnings async (fire and forget -- doesn't block reaping)
    _task_for_learnings = db.task_get(task_id) if task_id else None
    _task_type_for_learnings = _task_for_learnings.get("type", "") if _task_for_learnings else ""
    if log_path and project and _task_type_for_learnings:
        _learnings.extract_learnings_async(
            task_id, _task_type_for_learnings, project, log_path, exit_code, str(al.DATA_DIR)
        )

    if success and task_id and project:
        # Sanity-check project_plan: it must create tagged subtasks and all dependencies
        # must resolve to real task IDs (never file paths).
        _task_for_san = db.task_get(task_id)
        if _task_for_san and _task_for_san.get("type") == "project_plan":
            _plan_errors = _validate_project_plan_subtasks(project, task_id)
            if _plan_errors:
                print(f"[Swarm] project_plan {task_id} invalid \u2014 treating as failure")
                for _err in _plan_errors[:10]:
                    print(f"[Swarm]   plan error: {_err}")
                _subs = _project_plan_subtasks(project, task_id)
                for _sub in _subs:
                    if _sub.get("status") == "pending":
                        db.task_update_status(_sub["id"], "cancelled")
                _deleted_for_planner = _plan_cleanup.delete_plans_for_planner(db, project, task_id)
                _deleted = _plan_cleanup.delete_matching_plans(db, project, [t["id"] for t in _subs])
                _all_deleted = list(dict.fromkeys(_deleted_for_planner + _deleted))
                if _all_deleted:
                    print(f"[Swarm] Deleted stale plan snapshot(s): {_all_deleted}")
                attempts = (_task_for_san.get("attempts") or 0) + 1
                max_att = _task_for_san.get("max_attempts", 3)
                db.task_update_status(task_id, "pending" if attempts < max_att else "failed", attempts=attempts)
                success = False

        # Skip post-validation for task types that don't produce code artifacts.
        # Note: for worktree-based tasks, validation was already run pre-merge above.
        # For non-worktree tasks (e.g. USE_WORKTREES=False), run it synchronously
        # here so follow-on integration/QA tasks cannot be scheduled from broken work.
        _skip_types = {"manager", "project_create", "qa", "research", "harness_qa", "hybrid_qa",
                       "project_plan", "audit", "art_pass"}
        # Re-fetch the task, but fall back to the pre-completion snapshot if the
        # monitor thread has already pruned it from the DB (race condition).
        _task_for_val = db.task_get(task_id) or _task_snapshot_pre_complete
        task_type_finished = _task_for_val.get("type", "") if _task_for_val else ""
        validation_failed_after_completion = False
        if not (_task_for_val and task_type_finished in _skip_types) and not _spawned_continuation:
            if not wt_path_str:
                _task_snapshot_for_validation = dict(_task_for_val) if _task_for_val else None
                _main_failed, _main_err = _validation._post_task_validation_in_worktree(
                    project,
                    task_id,
                    worktree_path=None,
                )
                if _main_failed and _main_err:
                    validation_failed_after_completion = True
                    if _validation.is_controller_config_blocker(_main_err):
                        print(f"[Swarm] Post-validation blocked by controller configuration for {project}; not spawning validation bug")
                        _handle_task_failure(task_id, project, _main_err, _task_snapshot=_task_snapshot_early)
                    else:
                        print(f"[Swarm] Post-validation FAILED for {project} {task_id} -- spawning validation bug before follow-on work")
                        _validation._spawn_validation_bug_task(
                            project,
                            task_id,
                            _main_err,
                            original_task=_task_snapshot_for_validation,
                        )
                else:
                    try:
                        _validation.run_closure_verification(project, task_id)
                    except Exception as _closure_err:
                        print(f"[Swarm] Closure verification trigger failed for {project} {task_id}: {_closure_err}")

        _task_metadata_for_qa = (_task_for_val or {}).get("metadata") or {}
        _is_recovery_task = _task_metadata_for_qa.get("is_recovery_task", False)

        # Auto-integration: after a feature/polish on a Godot project, spawn a lightweight
        # task to wire the new system into the existing game (signals, autoloads, scene tree).
        auto_spawn_integration_task(
            project=project,
            task_id=task_id,
            task_type_finished=task_type_finished,
            task_for_val=_task_for_val,
            diff_stat=diff_stat,
            workspace=al.WORKSPACE,
            validation_failed=validation_failed_after_completion,
        )

        # Sprint QA: when a QA task completes for an auto_replan project, mark it ready for planner
        auto_handle_sprint_qa(
            project=project,
            task_type_finished=task_type_finished,
            auto_replan_projects=al.AUTO_REPLAN_PROJECTS,
            projects_sprint_qa_done=al._projects_sprint_qa_done,
            validation_failed=validation_failed_after_completion,
        )

        # Auto-QA: increment counter for Godot projects NOT on the sprint cycle
        auto_spawn_qa_task(
            project=project,
            task_id=task_id,
            task_type_finished=task_type_finished,
            task_for_val=_task_for_val,
            workspace=al.WORKSPACE,
            auto_replan_projects=al.AUTO_REPLAN_PROJECTS,
            qa_completion_counter=al._qa_completion_counter,
            qa_auto_threshold=al.QA_AUTO_THRESHOLD,
            validation_failed=validation_failed_after_completion,
            spawned_continuation=_spawned_continuation,
            is_recovery_task=_is_recovery_task,
        )

        # Auto-audit: fire for any project (not just Godot) to catch false completions
        auto_spawn_audit_task(
            project=project,
            task_id=task_id,
            task_type_finished=task_type_finished,
            audit_completion_counter=al._audit_completion_counter,
            audit_auto_threshold=al.AUDIT_AUTO_THRESHOLD,
            validation_failed=validation_failed_after_completion,
            spawned_continuation=_spawned_continuation,
            is_recovery_task=_is_recovery_task,
        )
