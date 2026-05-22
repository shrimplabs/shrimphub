"""
swarm.agent_recovery -- task failure handling, recovery task creation, and
                        continuation task dependency management.

Extracted from agent_lifecycle.py. All functions access shared state (db,
PAUSED_PROJECTS, etc.) from agent_lifecycle at call-time via lazy imports to
avoid circular dependencies.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Set

from swarm.branch_intent import branch_intent_metadata, format_branch_intent


# ---------------------------------------------------------------------------
# Lazy accessor helpers
# ---------------------------------------------------------------------------

def _lc():
    """Return the agent_lifecycle module (lazy)."""
    import swarm.agent_lifecycle as _al
    return _al


def _db():
    """Return the live db module (lazy, via agent_lifecycle)."""
    import swarm.agent_lifecycle as _al
    _al._lazy_imports()
    return _al.db


# ---------------------------------------------------------------------------
# Path / text helpers
# ---------------------------------------------------------------------------

def _looks_like_file_path(value: str) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if "/" in text or "\\" in text:
        return True
    return text.lower().endswith((".gd", ".tscn", ".tres", ".py", ".json", ".md", ".png", ".wav"))


def _bounded_failure_excerpt(last_output: str) -> tuple[str, int]:
    _SIGNAL_WORDS = ("ERROR", "Error", "Exception", "Traceback", "FAIL", "assert", "WARNING: response truncated")
    signal_lines = [
        ln for ln in (last_output or "").splitlines()
        if any(sw in ln for sw in _SIGNAL_WORDS)
    ]
    signal_excerpt = "\n".join(signal_lines[-60:])
    tail_excerpt = (last_output or "")[-3000:]
    if signal_excerpt and signal_excerpt not in tail_excerpt:
        failure_excerpt = f"[Key error lines]\n{signal_excerpt}\n\n[Last ~3000 chars of log]\n{tail_excerpt}"
    else:
        failure_excerpt = tail_excerpt
    max_excerpt = 6000
    if len(failure_excerpt) > max_excerpt:
        failure_excerpt = failure_excerpt[-max_excerpt:]
    return failure_excerpt, len(last_output or "")


# ---------------------------------------------------------------------------
# Plan hint helpers (used by _validate_project_plan_subtasks)
# ---------------------------------------------------------------------------

def _project_plan_subtasks(project: str, planner_task_id: str) -> list[dict]:
    al = _lc()
    al._lazy_imports()
    return al._plan_cleanup.project_plan_subtasks(al.db, project, planner_task_id)


def _normalize_plan_hint(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _extract_dependency_hint_names(description: str) -> list[str]:
    desc = description or ""
    match = re.search(r"\[SEQUENTIAL:\s*after\s+([^\]]+)\]", desc, re.IGNORECASE)
    if not match:
        return []
    raw_hint = match.group(1)
    pieces = re.split(r"\s*\+\s*|\s*,\s*|\s+\band\b\s+", raw_hint, flags=re.IGNORECASE)
    return [piece.strip() for piece in pieces if piece and piece.strip()]


def _match_plan_hint_to_task_ids(
    hint_names: list[str],
    subtasks: list[dict],
    current_task_id: str,
) -> tuple[dict[str, str], list[str]]:
    matched: dict[str, str] = {}
    missing: list[str] = []
    candidates = [
        task for task in subtasks
        if task.get("id") != current_task_id
    ]
    normalized_candidates = [
        (
            task.get("id", ""),
            _normalize_plan_hint(task.get("description", "")),
        )
        for task in candidates
    ]
    for hint in hint_names:
        normalized_hint = _normalize_plan_hint(hint)
        if not normalized_hint:
            continue
        found_id = None
        for candidate_id, candidate_desc in normalized_candidates:
            if normalized_hint and normalized_hint in candidate_desc:
                found_id = candidate_id
                break
        if found_id:
            matched[hint] = found_id
        else:
            missing.append(hint)
    return matched, missing


def _validate_project_plan_subtasks(project: str, planner_task_id: str) -> list[str]:
    db = _db()
    subtasks = _project_plan_subtasks(project, planner_task_id)
    if not subtasks:
        return ["project_plan created no tagged subtasks"]

    all_task_ids = {t["id"] for t in db.task_get_all()}
    completed_ids = db.task_get_completed_ids()
    errors: list[str] = []
    for task in subtasks:
        tid = task.get("id", "")
        deps = task.get("dependencies") or []
        for dep in deps:
            if dep == tid:
                errors.append(f"{tid}: self dependency")
            elif _looks_like_file_path(dep):
                errors.append(f"{tid}: file path used as dependency ({dep})")
            elif dep not in all_task_ids and dep not in completed_ids:
                errors.append(f"{tid}: unknown dependency ({dep})")

        hint_names = _extract_dependency_hint_names(task.get("description", ""))
        if hint_names:
            matched_hints, missing_hints = _match_plan_hint_to_task_ids(hint_names, subtasks, tid)
            for hint_name, expected_dep_id in matched_hints.items():
                if expected_dep_id not in deps:
                    errors.append(
                        f"{tid}: sequential hint '{hint_name}' missing dependency on {expected_dep_id}"
                    )
            if missing_hints:
                errors.append(
                    f"{tid}: sequential hint references unknown sibling task(s): {', '.join(missing_hints)}"
                )

        desc_prefix = (task.get("description", "") or "").strip().split("]", 1)[0].upper()
        sibling_subtask_ids = {subtask.get("id", "") for subtask in subtasks}
        sibling_deps = [dep for dep in deps if dep in sibling_subtask_ids]
        file_aware_auto_dep_indices = (task.get("metadata") or {}).get("file_aware_auto_dep_indices") or []
        if "[PARALLEL" in desc_prefix and sibling_deps and not file_aware_auto_dep_indices:
            errors.append(
                f"{tid}: marked parallel but depends on sibling task(s): {', '.join(sibling_deps)}"
            )
    return errors


# ---------------------------------------------------------------------------
# Task history lookup
# ---------------------------------------------------------------------------

def _task_history_lookup(task_id: str) -> Optional[dict]:
    if not task_id:
        return None
    history_file = _lc()._get_data_dir() / "task-history.jsonl"
    if not history_file.exists():
        return None
    latest_match: Optional[dict] = None
    try:
        with history_file.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if record.get("id") != task_id:
                    continue
                latest_match = record
    except Exception:
        return None
    return latest_match


# ---------------------------------------------------------------------------
# Dependency helpers for recovery/continuation tasks
# ---------------------------------------------------------------------------

def _replacement_task_dependencies(
    failed_task: dict,
    *,
    new_task_id: str,
) -> list[str]:
    """Preserve branch continuity for recovery/continuation tasks.

    Prefer the failed task's own upstream deps. If those were already lost,
    fall back to the branch root's historical deps. As a last resort, attach
    the replacement to the current project head/genesis chain.
    """
    al = _lc()
    al._lazy_imports()
    db = al.db

    project = failed_task.get("project", "")
    failed_id = failed_task.get("id", "")
    failed_meta = failed_task.get("metadata") or {}
    is_recovery = bool(failed_meta.get("is_recovery_task"))
    is_review = bool(failed_meta.get("is_review_task"))

    def _normalized_deps(task: Optional[dict]) -> list[str]:
        deps = []
        seen: Set[str] = set()
        for dep in (task or {}).get("dependencies") or []:
            if not isinstance(dep, str):
                continue
            dep_id = dep.strip()
            if not dep_id or dep_id in {failed_id, new_task_id} or dep_id in seen:
                continue
            seen.add(dep_id)
            deps.append(dep_id)
        return deps

    completed_ids = db.task_get_completed_ids()

    def _filter_completed(deps: list[str]) -> list[str]:
        """Drop deps that are already completed/archived -- they can't block anything."""
        active_ids = {t["id"] for t in db.task_get_all()}
        return [
            d for d in deps
            if d not in completed_ids and d in active_ids
        ]

    direct_deps = _filter_completed(_normalized_deps(failed_task))
    if direct_deps:
        return direct_deps
    if _normalized_deps(failed_task) and not is_recovery:
        return []

    candidate_ids = [
        failed_meta.get("recovery_root_task_id"),
        failed_meta.get("failed_task_id"),
        failed_meta.get("branch_intent_root_task_id"),
    ]
    for candidate_id in candidate_ids:
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            continue
        if candidate_id in {failed_id, new_task_id}:
            continue
        candidate_task = (
            db.task_get(candidate_id)
            or db.task_get_completed_record(candidate_id)
            or _task_history_lookup(candidate_id)
        )
        candidate_deps = _filter_completed(_normalized_deps(candidate_task))
        if candidate_deps:
            return candidate_deps

    if not project and not is_recovery and not is_review:
        return []

    genesis_id = f"{project}-genesis"
    genesis_task = db.task_get(genesis_id) or db.task_get_completed_record(genesis_id)
    if genesis_task is None:
        now = datetime.now().isoformat()
        db.task_upsert({
            "id": genesis_id,
            "project": project,
            "type": "feature",
            "description": "Project registered - genesis anchor",
            "status": "completed",
            "created": now,
            "completed": now,
            "priority": 50,
        })
        try:
            db.task_record_completed(genesis_id, project=project)
        except Exception:
            pass
    return [genesis_id]


# ---------------------------------------------------------------------------
# Terminal recovery continuation (for exhausted recovery tasks)
# ---------------------------------------------------------------------------

def _spawn_terminal_recovery_continuation(failed_task: dict, attempts: int, last_output: str) -> str | None:
    al = _lc()
    al._lazy_imports()
    db = al.db

    failed_id = failed_task.get("id", "unknown")
    project = failed_task.get("project", "")
    if not project:
        return None

    failed_meta = failed_task.get("metadata") or {}
    branch_root_id = failed_meta.get("recovery_root_task_id") or failed_meta.get("failed_task_id") or failed_id
    continuation_id = f"bug-{failed_id}"
    existing = db.task_get(continuation_id)
    excerpt, output_chars = _bounded_failure_excerpt(last_output)
    dependents = [
        t for t in db.task_get_all()
        if failed_id in (t.get("dependencies") or [])
    ]
    orig_desc = (failed_task.get("description") or "")[:2000]
    continuation_desc = (
        "TERMINAL RECOVERY CONTINUATION:\n\n"
        f"Recovery task {failed_id} exhausted its allowed attempts after {attempts} failure(s).\n"
        "Continue the branch as a normal bug-fix task so downstream dependencies stay valid.\n\n"
        f"{format_branch_intent(failed_task)}\n\n"
        f"FAILED RECOVERY TASK:\n{orig_desc}\n\n"
        f"LATEST FAILURE SUMMARY:\n{excerpt}\n\n"
        "YOUR JOB:\n"
        "1. Diagnose the remaining blocker.\n"
        "2. Complete the missing work for the original task objective above.\n"
        "3. Keep downstream tasks unblocked by preserving branch continuity.\n"
        "4. Validate and close the branch cleanly.\n"
    )
    metadata = {
        "continuation_for_failed_task": failed_id,
        "continuation_reason": "terminal_recovery_failure",
        "branch_continuation": True,
        "recovery_root_task_id": branch_root_id,
        "dropped_terminal_dependency": failed_id,
        "failure_attempts": attempts,
        "error_log_excerpt": excerpt,
        "error_log_chars": output_chars,
        **branch_intent_metadata(failed_task),
    }
    continuation_deps = _replacement_task_dependencies(
        failed_task,
        new_task_id=continuation_id,
    )
    if existing:
        db.task_update(continuation_id, {
            "description": continuation_desc,
            "metadata": metadata,
            "status": "pending",
            "priority": 100,
            "dependencies": continuation_deps,
        })
    else:
        db.task_upsert({
            "id": continuation_id,
            "project": project,
            "type": "bug",
            "priority": 100,
            "description": continuation_desc,
            "status": "pending",
            "dependencies": continuation_deps,
            "metadata": metadata,
            "attempts": 0,
            "max_attempts": 3,
            "created": datetime.now().isoformat(),
        })

    proj = db.project_get(project)
    if proj and proj.get("head_task_id") == failed_id:
        db.project_update(project, {"head_task_id": continuation_id})
        print(f"[Swarm] Advanced head to continuation {continuation_id} for {project}")

    for dep_task in dependents:
        new_deps = al._task_mutations.replace_task_dependencies(
            db,
            dep_task["id"],
            {failed_id: continuation_id},
        )
        if new_deps is not None:
            print(f"[Swarm] Reparented {dep_task['id']}: dependency {failed_id} → continuation {continuation_id}")
    return continuation_id


# ---------------------------------------------------------------------------
# Main recovery task spawner
# ---------------------------------------------------------------------------

def _spawn_review_task(failed_task: dict, attempts: int, last_output: str):
    """Create a recovery task that diagnoses failures and completes the original work.

    The recovery task MUST actually do the work -- not just document it -- because
    other tasks may depend on the output. If the recovery task also fails, a
    replacement task is created and all dependents are reparented to it.
    """
    import uuid as _uuid
    al = _lc()
    al._lazy_imports()
    db = al.db

    failed_id = failed_task.get("id", "unknown")
    project = failed_task.get("project", "")
    if project in al.PAUSED_PROJECTS:
        print(f"[Swarm] Skipping recovery task — {project} is paused")
        return
    orig_type = failed_task.get("type", "bug")
    orig_desc = failed_task.get("description", "")
    orig_priority = failed_task.get("priority", 70)

    all_tasks = db.task_get_all()
    dependents = [t for t in all_tasks if failed_id in (t.get("dependencies") or [])]
    failed_meta = failed_task.get("metadata") or {}
    branch_root_id = failed_meta.get("recovery_root_task_id") or failed_meta.get("failed_task_id") or failed_id
    _recovery_depth = int(failed_meta.get("recovery_depth") or 0)
    orig_priority = max(50, orig_priority - 5 * _recovery_depth)

    live_branch_recoveries = [
        t for t in all_tasks
        if (t.get("metadata") or {}).get("is_recovery_task")
        and (t.get("metadata") or {}).get("recovery_root_task_id") == branch_root_id
        and t.get("status") in ("pending", "in_progress")
    ]
    canonical_recovery = None
    if live_branch_recoveries:
        live_branch_recoveries.sort(
            key=lambda t: (
                0 if t.get("status") == "in_progress" else 1,
                t.get("created") or "",
                t.get("id") or "",
            )
        )
        canonical_recovery = live_branch_recoveries[0]
        for stale in live_branch_recoveries[1:]:
            if stale.get("status") == "pending":
                db.task_update_status(stale["id"], "cancelled")
                print(f"[Swarm] Cancelled stale recovery task {stale['id'][:8]} for branch {branch_root_id[:8]}")

    recovery_id = f"recovery-{_uuid.uuid4().hex[:8]}"

    if canonical_recovery:
        canonical_id = canonical_recovery["id"]
        proj = db.project_get(project) if project else None
        if proj and proj.get("head_task_id") == failed_id:
            db.project_update(project, {"head_task_id": canonical_id})
            print(f"[Swarm] Advanced head to reused recovery {canonical_id} for {project}")
        for dep_task in dependents:
            new_deps = al._task_mutations.replace_task_dependencies(
                db,
                dep_task["id"],
                {failed_id: canonical_id},
            )
            if new_deps is not None:
                print(f"[Swarm] Reparented {dep_task['id']}: dependency {failed_id} → recovery {canonical_id}")
        print(f"[Swarm] Reusing existing recovery task {canonical_id} for branch {branch_root_id[:8]}")
        return

    note = (
        f"NOTE: {len(dependents)} other task(s) are waiting on this work to complete."
        if dependents
        else "NOTE: No tasks depend on this one, but the feature is still missing from the project."
    )

    failure_excerpt, output_chars = _bounded_failure_excerpt(last_output)
    orig_desc_capped = orig_desc[:2000] + ("\n[... description truncated ...]" if len(orig_desc) > 2000 else "")

    recovery_desc = (
        f"RECOVERY TASK: Complete the work that failed {attempts} times.\n\n"
        f"ORIGINAL TASK ({failed_id}):\n{orig_desc_capped}\n\n"
        f"FAILURE HISTORY (excerpt -- full log in metadata.error_log):\n{failure_excerpt}\n\n"
        f"YOUR JOB:\n"
        f"1. Read the failure history above and understand what went wrong.\n"
        f"2. Inspect the codebase to understand the current state.\n"
        f"3. ACTUALLY COMPLETE the original task — implement the feature/fix.\n"
        f"   Do NOT just document the failure. The project is incomplete without this work.\n"
        f"4. Avoid the mistakes from previous attempts (described in failure history above).\n"
        f"5. Validate, commit, and push when done.\n\n"
        f"{note}"
    )

    recovery_meta: dict = {
        "is_review_task": True,
        "is_recovery_task": True,
        "recovery_depth": _recovery_depth + 1,
        "error_log_excerpt": failure_excerpt,
        "error_log_chars": output_chars,
        "failed_task_id": failed_id,
        "recovery_root_task_id": branch_root_id,
        "dependent_count": len(dependents),
        **branch_intent_metadata(failed_task),
    }
    if failed_meta.get("worktree_path") and failed_meta.get("worktree_branch"):
        wt_p = Path(failed_meta["worktree_path"])
        if wt_p.exists():
            recovery_meta["worktree_path"] = str(wt_p)
            recovery_meta["worktree_branch"] = failed_meta["worktree_branch"]
            recovery_meta["worktree_inherited"] = True
            print(f"[Swarm] Recovery task will inherit worktree {wt_p.name} from {failed_id}")

    recovery_task = {
        "id": recovery_id,
        "project": project,
        "type": orig_type,
        "description": recovery_desc,
        "priority": orig_priority,
        "status": "pending",
        "attempts": 0,
        "max_attempts": 3,
        "dependencies": _replacement_task_dependencies(
            failed_task,
            new_task_id=recovery_id,
        ),
        "metadata": recovery_meta,
        "created": datetime.now().isoformat(),
    }
    db.task_upsert(recovery_task)
    print(f"[Swarm] Created recovery task {recovery_id} for failed task {failed_id}")

    proj = db.project_get(project) if project else None
    if proj and proj.get("head_task_id") == failed_id:
        db.project_update(project, {"head_task_id": recovery_id})
        print(f"[Swarm] Advanced head to recovery {recovery_id} for {project}")

    for dep_task in dependents:
        new_deps = al._task_mutations.replace_task_dependencies(
            db,
            dep_task["id"],
            {failed_id: recovery_id},
        )
        if new_deps is not None:
            print(f"[Swarm] Reparented {dep_task['id']}: dependency {failed_id} → {recovery_id}")


# ---------------------------------------------------------------------------
# Task failure handler (orchestrates retry vs. recovery)
# ---------------------------------------------------------------------------

def _handle_task_failure(task_id: str, project: Optional[str], agent_output: str,
                         _task_snapshot: Optional[dict] = None):
    """On task failure, retry if attempts remain, else mark failed."""
    al = _lc()
    al._lazy_imports()
    db = al.db

    task = db.task_get(task_id) or _task_snapshot
    if not task:
        return

    attempts = task.get("attempts", 0) + 1
    max_attempts = task.get("max_attempts", 3)

    if attempts < max_attempts:
        failure_snippet = ""
        for line in reversed(agent_output.splitlines()):
            line = line.strip()
            if line and not line.startswith("["):
                failure_snippet = line[:300]
                break

        meta = task.get("metadata", {})
        meta["last_failure"] = failure_snippet or agent_output[-300:]
        meta["failure_attempt"] = attempts

        db.task_upsert({
            **task,
            "status": "pending",
            "attempts": attempts,
            "started": None,
            "completed": None,
            "agent_id": None,
            "metadata": meta,
        })
        print(f"[Swarm] Task {task_id} failed (attempt {attempts}/{max_attempts}) — retrying")
    else:
        db.task_update_status(
            task_id, "failed",
            completed=datetime.now().isoformat(),
        )
        print(f"[Swarm] Task {task_id} failed after {attempts} attempts — giving up")
        al._fire_task_webhook(
            "task_failed",
            project=project or "",
            task_id=task_id,
            task_type=task.get("type", ""),
            description=task.get("description", "").split("\n")[0][:100],
            attempts=attempts,
            max_attempts=max_attempts,
        )
        if project and (task.get("metadata") or {}).get("is_recovery_task"):
            _spawn_terminal_recovery_continuation(task, attempts, agent_output)
        elif project:
            _spawn_review_task(task, attempts, agent_output)
