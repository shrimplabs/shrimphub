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
                # Baseline diff: only fail if NEW errors were introduced by this agent.
                # Pre-existing errors (captured at spawn time) are inherited blockers,
                # not charged to the current agent's work.
                _baseline = (_task_for_val_early.get("metadata") or {}).get("validation_baseline")
                if _baseline is not None:
                    _project_path = al.WORKSPACE / project
                    _ptype = "godot" if (_project_path / "project.godot").exists() else "python"
                    _new_errs, _inherited = _validation.filter_new_errors(_val_err, _baseline, _ptype)
                    if not _new_errs:
                        # All errors are pre-existing -- agent did not make things worse.
                        # Report inherited blockers but do NOT block the merge.
                        if _inherited:
                            print(
                                f"[Swarm] Pre-merge validation: {len(_inherited)} inherited pre-existing error(s) "
                                f"for agent {agent_id[:8]} -- not charged to this agent, proceeding with merge"
                            )
                        else:
                            print(f"[Swarm] Pre-merge validation: no new errors for agent {agent_id[:8]} -- proceeding with merge")
                        _val_failed = False  # treat as pass
                    else:
                        print(
                            f"[Swarm] Pre-merge validation FAILED for agent {agent_id[:8]}: "
                            f"{len(_new_errs)} NEW error(s) introduced "
                            f"({len(_inherited)} inherited/pre-existing) -- skipping merge, preserving worktree"
                        )
                        _val_err = (
                            f"NEW errors introduced by this agent ({len(_new_errs)}):\n"
                            + "\n".join(_new_errs)
                            + (f"\n\nPre-existing errors NOT charged to this agent ({len(_inherited)}):\n"
                               + "\n".join(_inherited) if _inherited else "")
                        )
                else:
                    print(f"[Swarm] Pre-merge validation FAILED for agent {agent_id[:8]} -- no baseline available, treating all errors as new")

            if _val_failed:
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


def _read_agent_token_usage(task_id: Optional[str], agent_id: str) -> tuple[int, int, int, int, int, str, str]:
    """Returns (input, output, cache_read, cache_write, loop_count, provider, model)."""
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    loop_count = 0
    provider = ""
    model = ""
    token_key = task_id or agent_id
    token_file = Path(str(_al().DATA_DIR)) / f"agent_{token_key}_tokens.json"
    if token_file.exists():
        try:
            tok = json.loads(token_file.read_text())
            input_tokens = tok.get("input", 0)
            output_tokens = tok.get("output", 0)
            cache_read_tokens = tok.get("cache_read", 0)
            cache_write_tokens = tok.get("cache_write", 0)
            loop_count = tok.get("loop_count", 0)
            provider = tok.get("provider", "")
            model = tok.get("model", "")
            token_file.unlink()
        except Exception:
            pass
    return input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, loop_count, provider, model


def _mark_agent_finished(agent_id: str, success: bool, exit_code: int,
                         output: str, diff_stat: str, input_tokens: int,
                         output_tokens: int, loop_count: int = 0,
                         cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                         provider: str = "", model: str = "",
                         estimated_cost_usd: float = 0.0):  # noqa: PLR0913
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
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        provider=provider,
        model=model,
        estimated_cost_usd=estimated_cost_usd,
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

    # Cancel stale recovery/feeder tasks for this task (it succeeded so they're redundant).
    for rt in db.task_get_all():
        meta = rt.get("metadata") or {}
        is_stale_recovery = (
            meta.get("is_recovery_task") and
            meta.get("failed_task_id") == task_id and
            rt.get("status") == "pending"
        )
        is_stale_feeder = (
            meta.get("is_research_feeder") and
            meta.get("feeds_into_task_id") == task_id and
            rt.get("status") in ("pending", "in_progress")
        )
        if is_stale_recovery or is_stale_feeder:
            db.task_update_status(rt["id"], "cancelled")
            print(f"[Swarm] Cancelled stale {'feeder' if is_stale_feeder else 'recovery'} task {rt['id'][:8]} "
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
            # Mirror _handle_task_failure: always record last_failure so the API
            # shows a meaningful error message instead of empty last_failure.
            meta = dict(task_for_san.get("metadata") or {})
            meta["last_failure"] = "\n".join(plan_errors[:5])
            meta["failure_attempt"] = attempts
            db.task_update_status(task_id, "pending" if attempts < max_att else "failed", attempts=attempts, metadata=meta)
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

    # Librarian completion counter: increment after every successful non-meta task.
    # The orchestrator monitors LIBRARIAN_COMPLETION_COUNTER and fires the librarian
    # when the threshold is reached and the system is idle.
    # Skip librarian tasks themselves to avoid triggering a chain reaction.
    _skip_meta = {
        "gardener", "cartographer", "librarian", "archaeologist",
        "auditor", "scheduler", "audit_learnings",
    }
    if task_type_finished not in _skip_meta:
        try:
            from swarm import orchestrator as _orch
            _orch.LIBRARIAN_COMPLETION_COUNTER += 1
            # Also sync to the state file so the API can read it
            from swarm import api_librarian as _librarian
            _librarian.increment_librarian_counter(al.DATA_DIR)
        except Exception as _lb_err:
            print(f"[Swarm] WARNING: librarian counter increment failed: {_lb_err}")


# ---------------------------------------------------------------------------
# Experiment metrics
# ---------------------------------------------------------------------------

def _write_experiment_metrics(
    task_id: str,
    project: str,
    task_snapshot: Optional[dict],
    success: bool,
    loop_count: int,
    diff_stat: str,
) -> None:
    """Append one line to data/experiment_metrics.jsonl for A/B pipeline analysis.

    Only writes when a pipeline_variant is present in task metadata — i.e. only
    for tasks that ran under an explicit pipeline configuration. Tasks without a
    pipeline_variant are skipped to keep the metrics file clean.
    """
    al = _al()
    if not task_snapshot:
        return
    metadata = task_snapshot.get("metadata") or {}
    pipeline_variant = metadata.get("pipeline_variant")
    experiment_variant = metadata.get("experiment_variant", "")
    if pipeline_variant is None and not experiment_variant:
        return  # not an experiment task

    pipeline_state = {}
    pipeline_state_path = al.DATA_DIR / f"agent_{task_id}_pipeline.json"
    if pipeline_state_path.exists():
        try:
            pipeline_state = json.loads(pipeline_state_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            pipeline_state = {}

    # Parse diff stat for insertions/deletions.
    insertions = 0
    deletions = 0
    if diff_stat:
        ins_m = re.search(r"(\d+) insertion", diff_stat)
        del_m = re.search(r"(\d+) deletion", diff_stat)
        if ins_m:
            insertions = int(ins_m.group(1))
        if del_m:
            deletions = int(del_m.group(1))

    # Detect whether a validation bug task was spawned (written into log by _spawn_validation_bug_task).
    bug_spawned = False
    try:
        log_path_candidate = al.DATA_DIR / f"agent_{task_id}.log"
        if log_path_candidate.exists():
            log_text = log_path_candidate.read_text(encoding="utf-8", errors="replace")
            bug_spawned = "[Swarm] Spawning validation bug task" in log_text
    except Exception:
        pass

    completed_at = datetime.utcnow().isoformat() + "Z"
    record = {
        "timestamp": completed_at,
        "experiment_id": metadata.get("experiment_id", ""),
        "experiment_arm": metadata.get("experiment_arm", ""),
        "task_id": task_id,
        "source_task_id": metadata.get("source_task_id", ""),
        "source_project": metadata.get("source_project", ""),
        "project": project,
        "task_type": task_snapshot.get("type", ""),
        "experiment_variant": experiment_variant,
        "pipeline_variant": pipeline_variant,
        "phase_order": metadata.get("phase_order") or (pipeline_variant if isinstance(pipeline_variant, list) else None),
        "is_valid_order": metadata.get("is_valid_order"),
        "invalidity_reason": metadata.get("invalidity_reason", ""),
        "phase_random_seed": metadata.get("phase_random_seed"),
        "phases_completed": pipeline_state.get("phases_completed", []),
        "phase_timings": pipeline_state.get("phase_timings", {}),
        "work_loops": loop_count,
        "validation_passed": success,
        "pipeline_validation_passed": (pipeline_state.get("validation") or {}).get("passed"),
        "bug_spawned": bug_spawned,
        "attempts": task_snapshot.get("attempts", 1),
        "diff_insertions": insertions,
        "diff_deletions": deletions,
        "started_at": task_snapshot.get("started"),
        "completed_at": completed_at,
        "pipeline_state_path": str(pipeline_state_path) if pipeline_state else "",
        # Terminal task status — needed by source-task aggregator to detect failed source tasks
        "status": task_snapshot.get("status", ""),
        # WS4/WS6: failure classification and repair tracking
        "failure_kind": pipeline_state.get("failure_kind", ""),
        "failure_phase": pipeline_state.get("failure_phase", ""),
        "repair_attempts": pipeline_state.get("repair_attempts", 0),
        "infrastructure_failure": pipeline_state.get("failure_kind") == "infrastructure_exception",
        # WS6: research feeder cycle and recovery task counts (from task metadata)
        "research_feeder_cycles": int(metadata.get("research_feeder_cycles") or 0),
        "is_recovery_task": bool(metadata.get("is_recovery_task")),
        "is_research_feeder": bool(metadata.get("feeds_into_task_id")),
    }

    try:
        metrics_path = al.DATA_DIR / "experiment_metrics.jsonl"
        with open(metrics_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        experiment_id = record.get("experiment_id") or "unlabeled"
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", experiment_id)
        event_dir = al.DATA_DIR / "experiments" / safe_id
        event_dir.mkdir(parents=True, exist_ok=True)
        with open(event_dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "task_completed", **record}) + "\n")
    except Exception as e:
        print(f"[Swarm] WARNING: failed to write experiment metrics: {e}")


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
    # Defensive unpacking: handle both the full 7-tuple (input, output, cache_read,
    # cache_write, loop_count, provider, model) and the legacy 3-tuple shape used by
    # older test mocks. Cache fields default to zero / empty when absent.
    _tok_result = _read_agent_token_usage(task_id, agent_id)
    if len(_tok_result) >= 7:
        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, loop_count, provider, model = _tok_result[:7]
    elif len(_tok_result) >= 3:
        input_tokens, output_tokens, loop_count = _tok_result[:3]
        cache_read_tokens = 0
        cache_write_tokens = 0
        provider = ""
        model = ""
    else:
        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, loop_count, provider, model = \
            0, 0, 0, 0, 0, "", ""
    from swarm.provider_utils import estimate_cost_usd
    cost = estimate_cost_usd(model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
    _mark_agent_finished(
        agent_id, success, exit_code, output, diff_stat,
        input_tokens, output_tokens, loop_count,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        provider=provider,
        model=model,
        estimated_cost_usd=cost,
    )

    # Phase 5 -- task outcome.
    task_snapshot_pre_complete: Optional[dict] = None
    spawned_continuation = False

    if task_id:
        # Snapshot the task now, before status mutations or prune_history() races.
        task_snapshot_early = db.task_get(task_id)

        # Phase 5a -- reparent any continuation task the agent spawned, regardless of
        # success or failure. If this runs only on success, a failed task leaves the
        # continuation depending on the failed task itself -- permanent deadlock.
        spawned_continuation = _phase_reparent_continuation(task_id, full_output)

        if success:
            # Phase 5b -- success path.
            task_snapshot_pre_complete = _phase_complete_task(task_id, project, diff_stat)

            # Research feeder completion: if this task was a research feeder,
            # inject its findings into the original task and reset it to pending.
            if task_snapshot_early and (task_snapshot_early.get("metadata") or {}).get("is_research_feeder"):
                try:
                    from swarm.agent_recovery import _apply_research_feeder_result
                    _apply_research_feeder_result(task_id, full_output or output)
                except Exception as _rfe:
                    print(f"[Swarm] WARNING: research feeder result injection failed for {task_id[:8]}: {_rfe}")
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

    # Phase 6b -- experiment metrics collection.
    if task_id and project:
        _write_experiment_metrics(
            task_id=task_id,
            project=project,
            task_snapshot=task_snapshot_early,
            success=success,
            loop_count=loop_count,
            diff_stat=diff_stat,
        )

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
