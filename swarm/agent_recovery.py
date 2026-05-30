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

import threading
from collections import defaultdict

from swarm.branch_intent import branch_intent_metadata, format_branch_intent

# Per-branch-root lock: serialises the check-then-create in _spawn_review_task
# so concurrent daemon threads can't all race past the canonical_recovery guard.
# Keyed by branch_root_id (more precise than project — unrelated failures in the
# same project don't block each other).
_recovery_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_recovery_locks_guard = threading.Lock()

# Hard cap: max recovery+research tasks allowed per branch root before we stop
# creating new ones entirely.  Safety net if the lock somehow slips.
_MAX_RECOVERY_TASKS_PER_BRANCH = 3

# ---------------------------------------------------------------------------
# Escalation policy
# ---------------------------------------------------------------------------
# Defines how each task type behaves when attempts are exhausted.
# on_exhaust: "research" → spawn a research feeder task (future Phase B)
#             "cancel"   → mark task cancelled, unblock dependents
# This dict is the source of truth; config.json can override individual entries.

_DEFAULT_ESCALATION_POLICY: dict[str, dict] = {
    "bug":         {"max_attempts": 3, "on_exhaust": "research", "research_max_attempts": 2},
    "feature":     {"max_attempts": 3, "on_exhaust": "research", "research_max_attempts": 2},
    "refactor":    {"max_attempts": 3, "on_exhaust": "research", "research_max_attempts": 2},
    "polish":      {"max_attempts": 3, "on_exhaust": "cancel"},
    "qa":          {"max_attempts": 2, "on_exhaust": "cancel"},
    "harness_qa":  {"max_attempts": 2, "on_exhaust": "cancel"},
    "hybrid_qa":   {"max_attempts": 2, "on_exhaust": "cancel"},
    "scenario_qa": {"max_attempts": 2, "on_exhaust": "cancel"},
    "art_pass":    {"max_attempts": 2, "on_exhaust": "cancel"},
    "audit":       {"max_attempts": 2, "on_exhaust": "cancel"},
    "research":    {"max_attempts": 2, "on_exhaust": "cancel"},
    "plan":        {"max_attempts": 2, "on_exhaust": "cancel"},
    "project_plan":{"max_attempts": 2, "on_exhaust": "cancel"},
}


def get_escalation_policy(task_type: str, config: dict | None = None) -> dict:
    """Return the escalation policy for a task type, merging config overrides."""
    base = _DEFAULT_ESCALATION_POLICY.get(task_type, {"max_attempts": 3, "on_exhaust": "cancel"})
    if config:
        overrides = config.get("escalation_policy", {}).get(task_type, {})
        if overrides:
            base = {**base, **overrides}
    return base


def _get_recovery_lock(branch_root_id: str) -> threading.Lock:
    with _recovery_locks_guard:
        return _recovery_locks[branch_root_id]


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
    # Check live DB first (completed/failed tasks now stay in the tasks table)
    live = _db().task_get(task_id)
    if live:
        return live
    # DEPRECATED: fall back to JSONL for pre-migration tasks only.
    # Remove this fallback after 2025-07-01.
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
# Branch failure history collector
# ---------------------------------------------------------------------------

def _collect_branch_failure_history(branch_root_id: str, project: str, current_failed_id: str) -> str:
    """Return a digest of all previous failure excerpts for this branch root.

    Walks every task in the DB that shares the same recovery_root_task_id,
    collects their error_log_excerpt / last_failure, and returns a combined
    string capped at ~4000 chars.  Used to give deep-recovery agents full
    context across the whole chain instead of just the immediate parent.
    """
    db = _db()
    all_tasks = db.task_get_all()
    siblings = [
        t for t in all_tasks
        if t.get("id") != current_failed_id
        and (
            (t.get("metadata") or {}).get("recovery_root_task_id") == branch_root_id
            or t.get("id") == branch_root_id
        )
        and t.get("project") == project
        and t.get("status") in ("failed", "completed")
    ]
    # Sort oldest first
    siblings.sort(key=lambda t: t.get("created") or "")

    parts = []
    for t in siblings:
        tid = t.get("id", "?")
        meta = t.get("metadata") or {}
        excerpt = (
            meta.get("error_log_excerpt")
            or meta.get("last_failure")
            or t.get("metadata", {}).get("error_log_excerpt")
            or ""
        )
        if excerpt:
            parts.append(f"--- Attempt {tid[:40]} ---\n{excerpt[:600]}")

    if not parts:
        return ""
    combined = "\n\n".join(parts)
    if len(combined) > 4000:
        combined = combined[-4000:]
    return combined


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

    # Compute branch_root_id before acquiring the lock (only needs failed_task metadata).
    failed_meta = failed_task.get("metadata") or {}
    branch_root_id = failed_meta.get("recovery_root_task_id") or failed_meta.get("failed_task_id") or failed_id

    # Serialise the check-then-create for this branch root.  Without this lock,
    # concurrent daemon threads all read the same stale task snapshot, see no
    # existing recovery tasks, and each independently create one — causing an
    # exponential cascade of parallel recovery tasks.
    with _get_recovery_lock(branch_root_id):
        # Re-read inside the lock so we see any recovery tasks created by threads
        # that acquired the lock before us.
        all_tasks = db.task_get_all()
        dependents = [t for t in all_tasks if failed_id in (t.get("dependencies") or [])]

        # Count ALL recovery+research tasks on this branch (failed, cancelled, pending, in_progress).
        # Used for depth tracking and the hard cap.  Counts both is_recovery_task and
        # escalated_to_research tasks so research escalations also dedupe correctly.
        all_branch_recovery_tasks = [
            t for t in all_tasks
            if (t.get("metadata") or {}).get("recovery_root_task_id") == branch_root_id
        ]
        _branch_failures = sum(
            1 for t in all_branch_recovery_tasks
            if t.get("status") in ("failed", "cancelled")
        )
        _recovery_depth = max(int(failed_meta.get("recovery_depth") or 0), _branch_failures)
        orig_priority = max(50, orig_priority - 5 * _recovery_depth)

        # Hard cap: if we already have too many recovery attempts on this branch,
        # force escalation to research rather than silently stopping.
        total_branch_tasks = len(all_branch_recovery_tasks)
        if total_branch_tasks >= _MAX_RECOVERY_TASKS_PER_BRANCH and orig_type not in {"qa", "harness_qa", "hybrid_qa"}:
            print(f"[Swarm] Recovery cap reached ({total_branch_tasks}/{_MAX_RECOVERY_TASKS_PER_BRANCH}) "
                  f"for branch {branch_root_id[:12]} — forcing research escalation")
            _recovery_depth = max(_recovery_depth, 1)  # ensure escalation triggers below

        # Find live (pending/in_progress) recovery OR research-escalation tasks for this branch.
        # Include escalated_to_research so we don't duplicate research tasks either.
        live_branch_recoveries = [
            t for t in all_branch_recovery_tasks
            if t.get("status") in ("pending", "in_progress")
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

        # Compute linear chain deps: new recovery depends on the most recent previous
        # recovery task on this branch (not on the failed task's upstream deps).
        # This ensures recoveries run sequentially, each seeing the previous attempt's outcome.
        previous_branch_tasks = [
            t for t in all_branch_recovery_tasks
            if t.get("id") != recovery_id
        ]
        if previous_branch_tasks:
            previous_branch_tasks.sort(key=lambda t: t.get("created") or "")
            latest_previous = previous_branch_tasks[-1]
            _linear_chain_deps = [latest_previous["id"]]
        else:
            _linear_chain_deps = None  # first recovery — use normal dep logic below

        note = (
            f"NOTE: {len(dependents)} other task(s) are waiting on this work to complete."
            if dependents
            else "NOTE: No tasks depend on this one, but the feature is still missing from the project."
        )

        failure_excerpt, output_chars = _bounded_failure_excerpt(last_output)
        orig_desc_capped = orig_desc[:2000] + ("\n[... description truncated ...]" if len(orig_desc) > 2000 else "")

        # Collect the full failure chain across all sibling recovery tasks
        chain_history = _collect_branch_failure_history(branch_root_id, project, failed_id)
        chain_section = (
            f"\nFULL CHAIN FAILURE HISTORY (all previous attempts on this branch):\n{chain_history}\n"
            if chain_history else ""
        )

        escalate_to_research = _recovery_depth >= 1 and orig_type not in {"qa", "harness_qa", "hybrid_qa"}

        if escalate_to_research:
            recovery_desc = (
                f"RESEARCH ESCALATION: Bug branch failed {_recovery_depth + 1} recovery generations "
                f"({attempts} attempts each) without resolving.\n\n"
                f"ORIGINAL TASK ({branch_root_id}):\n{orig_desc_capped}\n\n"
                f"LATEST FAILURE ({failed_id}):\n{failure_excerpt}\n"
                f"{chain_section}\n"
                f"YOUR JOB (read-only diagnosis + queue a fix):\n"
                f"1. Read every file mentioned in the failure history above.\n"
                f"2. Reproduce the failure locally with a minimal run_command to confirm the root cause.\n"
                f"3. Identify exactly what is wrong and why previous attempts failed to fix it.\n"
                f"4. Queue ONE well-scoped bug task with a precise description of the fix needed.\n"
                f"   Include in that task: exact files to change, what to change, and why.\n"
                f"5. Do NOT commit code yourself — diagnosis and task creation only.\n\n"
                f"{note}"
            )
        elif orig_type in {"qa", "harness_qa", "hybrid_qa"}:
            recovery_desc = (
                f"RECOVERY TASK: Complete the QA run that failed {attempts} times.\n\n"
                f"ORIGINAL TASK ({failed_id}):\n{orig_desc_capped}\n\n"
                f"LATEST FAILURE:\n{failure_excerpt}\n"
                f"{chain_section}\n"
                f"YOUR JOB:\n"
                f"1. Read the failure history above and understand what went wrong.\n"
                f"2. Re-run the QA investigation using the available QA tools.\n"
                f"3. If you confirm project bugs, create bug tasks with clear reproduction steps and evidence.\n"
                f"4. If bug-task creation is unavailable, write QA_REPORT.md with the confirmed findings and finish.\n"
                f"5. Do NOT implement project code fixes, patch gameplay files, or commit code from this QA recovery task.\n"
                f"6. Kill any launched game process before finishing.\n\n"
                f"{note}"
            )
        else:
            recovery_desc = (
                f"RECOVERY TASK: Complete the work that failed {attempts} times.\n\n"
                f"ORIGINAL TASK ({failed_id}):\n{orig_desc_capped}\n\n"
                f"LATEST FAILURE:\n{failure_excerpt}\n"
                f"{chain_section}\n"
                f"YOUR JOB:\n"
                f"1. Read ALL failure history above — understand what was tried and why it didn't work.\n"
                f"2. Inspect the codebase to understand the current state.\n"
                f"3. ACTUALLY COMPLETE the original task — implement the feature/fix.\n"
                f"   Do NOT just document the failure. The project is incomplete without this work.\n"
                f"4. Avoid the mistakes from previous attempts (described in failure history above).\n"
                f"5. Validate, commit, and push when done.\n\n"
                f"{note}"
            )

        # Research escalation tasks are NOT marked is_recovery_task so they can
        # call create_task to queue the diagnosed fix.  They do carry the root ID
        # so the dep-graph stays connected.
        recovery_meta: dict = {
            "is_review_task": not escalate_to_research,
            "is_recovery_task": not escalate_to_research,
            "escalated_to_research": escalate_to_research,
            "recovery_depth": _recovery_depth + 1,
            "error_log_excerpt": failure_excerpt,
            "error_log_chars": output_chars,
            "failed_task_id": failed_id,
            "recovery_root_task_id": branch_root_id,
            "dependent_count": len(dependents),
            **branch_intent_metadata(failed_task),
        }
        if not escalate_to_research and failed_meta.get("worktree_path") and failed_meta.get("worktree_branch"):
            wt_p = Path(failed_meta["worktree_path"])
            if wt_p.exists():
                recovery_meta["worktree_path"] = str(wt_p)
                recovery_meta["worktree_branch"] = failed_meta["worktree_branch"]
                recovery_meta["worktree_inherited"] = True
                print(f"[Swarm] Recovery task will inherit worktree {wt_p.name} from {failed_id}")

        # Use linear chain deps if we have a previous recovery to chain to,
        # otherwise fall back to normal dependency wiring.
        task_deps = (
            _linear_chain_deps
            if _linear_chain_deps is not None
            else _replacement_task_dependencies(failed_task, new_task_id=recovery_id)
        )

        recovery_task = {
            "id": recovery_id,
            "project": project,
            "type": "research" if escalate_to_research else orig_type,
            "description": recovery_desc,
            "priority": 80 if escalate_to_research else orig_priority,
            "status": "pending",
            "attempts": 0,
            "max_attempts": 2 if escalate_to_research else 3,
            "dependencies": task_deps,
            "metadata": recovery_meta,
            "created": datetime.now().isoformat(),
        }
        db.task_upsert(recovery_task)
        if escalate_to_research:
            print(f"[Swarm] ESCALATED to research {recovery_id} for {project} — branch {branch_root_id[:12]} depth {_recovery_depth + 1}")
        else:
            print(f"[Swarm] Created recovery task {recovery_id} for failed task {failed_id} (depth {_recovery_depth + 1})")

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
# Research-feeder: spawn research that feeds back into the original task
# ---------------------------------------------------------------------------

def _spawn_research_feeder(failed_task: dict, attempts: int, last_output: str) -> Optional[str]:
    """Spawn a research task that feeds findings back into the original failed task.

    This is the NEW escalation path (replaces _spawn_review_task for non-QA tasks).

    Key properties:
    - Original task stays as the authoritative dep-graph node. Dependents NEVER move.
    - Research task depends on nothing (runs immediately), carries feeds_into_task_id.
    - Original task gets a temporary dependency on the research task (blocks it until
      research completes), then is reset to pending with enriched context.
    - On research completion, _apply_research_feeder_result() in agent_finish.py
      injects findings, resets attempts to 0, removes the research dep.
    - Dedupe guard: only one pending/in_progress research feeder per original task ID.
    """
    import uuid as _uuid
    al = _lc()
    al._lazy_imports()
    db = al.db

    failed_id = failed_task.get("id", "unknown")
    project = failed_task.get("project", "")
    orig_type = failed_task.get("type", "bug")
    orig_desc = (failed_task.get("description") or "")[:2000]
    failed_meta = failed_task.get("metadata") or {}
    branch_root_id = failed_meta.get("research_root_task_id") or failed_id

    if project in al.PAUSED_PROJECTS:
        print(f"[Swarm] Skipping research feeder — {project} is paused")
        return None

    # Dedupe: only one live research feeder per original task
    all_tasks = db.task_get_all()
    live_feeders = [
        t for t in all_tasks
        if (t.get("metadata") or {}).get("feeds_into_task_id") == failed_id
        and t.get("status") in ("pending", "in_progress")
    ]
    if live_feeders:
        existing_id = live_feeders[0]["id"]
        print(f"[Swarm] Research feeder {existing_id} already live for {failed_id[:8]} — skipping duplicate")
        return existing_id

    # Accumulate attempt history in metadata (survives the attempts=0 reset)
    attempt_history = list(failed_meta.get("attempt_history") or [])
    failure_excerpt, output_chars = _bounded_failure_excerpt(last_output)
    attempt_history.append({
        "attempt": attempts,
        "excerpt": failure_excerpt[:600],
        "chars": output_chars,
    })

    # Keep history bounded: last 5 entries + rolling summary of earlier ones
    if len(attempt_history) > 5:
        attempt_history = attempt_history[-5:]

    chain_history = _collect_branch_failure_history(branch_root_id, project, failed_id)
    chain_section = (
        f"\nFULL FAILURE HISTORY (all previous attempts):\n{chain_history}\n"
        if chain_history else ""
    )

    research_id = f"research-feeder-{_uuid.uuid4().hex[:8]}"
    research_desc = (
        f"RESEARCH FEEDER: Diagnose why '{orig_type}' task {failed_id[:12]} has failed "
        f"{attempts} time(s) without resolving.\n\n"
        f"ORIGINAL TASK:\n{orig_desc}\n\n"
        f"LATEST FAILURE:\n{failure_excerpt}\n"
        f"{chain_section}\n"
        f"YOUR JOB (read-only diagnosis only):\n"
        f"1. Read every file mentioned in the failure history above.\n"
        f"2. Understand exactly why previous attempts failed.\n"
        f"3. Identify the root cause and what a correct fix looks like.\n"
        f"4. Write your findings to the scratchpad — be specific: exact files, exact lines, exact fix.\n"
        f"5. Do NOT commit code. Do NOT create new tasks. Your output feeds directly back into the "
        f"   original task so it can retry with your diagnosis as context.\n"
        f"6. End with TASK_COMPLETE.\n"
    )

    research_meta = {
        "feeds_into_task_id": failed_id,
        "research_root_task_id": branch_root_id,
        "is_research_feeder": True,
        "failure_attempts": attempts,
        "error_log_excerpt": failure_excerpt,
    }

    db.task_upsert({
        "id": research_id,
        "project": project,
        "type": "research",
        "description": research_desc,
        "priority": 85,  # high — unblocks the original task
        "status": "pending",
        "attempts": 0,
        "max_attempts": 2,
        "dependencies": [],  # runs immediately
        "metadata": research_meta,
        "created": datetime.now().isoformat(),
    })

    # Block the original task on research completion: add research_id as a dependency.
    # Also store the updated attempt_history so future agents see the full picture.
    current_task = db.task_get(failed_id)
    if current_task:
        current_deps = list(current_task.get("dependencies") or [])
        if research_id not in current_deps:
            current_deps.append(research_id)
        updated_meta = dict(current_task.get("metadata") or {})
        updated_meta["attempt_history"] = attempt_history
        updated_meta["awaiting_research_feeder"] = research_id
        # Reset to pending (unblocked when research completes via _apply_research_feeder_result)
        db.task_update(failed_id, {
            "status": "pending",
            "attempts": 0,
            "dependencies": current_deps,
            "metadata": updated_meta,
        })

    print(f"[Swarm] Spawned research feeder {research_id} for {failed_id[:8]} (attempt {attempts}) — original task reset to pending, blocked on research")
    return research_id


def _apply_research_feeder_result(research_task_id: str, research_output: str, db_conn=None,
                                   needs_human_review: bool = False):
    """Called when a research feeder task completes (successfully or exhausted).

    Injects research findings into the original task's metadata, removes the
    research dependency, and resets the original task to pending so it can
    retry with enriched context.

    If needs_human_review=True (research feeder exhausted without resolving):
    - Sets metadata.needs_human_review = True
    - Sets run_after = 24h from now so the task doesn't immediately re-enter
      the queue and burn more agent cycles while waiting for human attention
    """
    from datetime import timedelta
    al = _lc()
    al._lazy_imports()
    db_ref = db_conn or al.db

    research_task = db_ref.task_get(research_task_id)
    if not research_task:
        return
    research_meta = research_task.get("metadata") or {}
    original_task_id = research_meta.get("feeds_into_task_id")
    if not original_task_id:
        return  # not a feeder task

    original_task = db_ref.task_get(original_task_id)
    if not original_task:
        print(f"[Swarm] Research feeder {research_task_id[:8]} complete but original task {original_task_id[:12]} not found (may already be completed)")
        return

    original_status = original_task.get("status")
    if original_status == "completed":
        print(f"[Swarm] Research feeder {research_task_id[:8]} complete but original task {original_task_id[:12]} already completed — self-cancelling")
        return

    # Summarise research output (bounded)
    research_summary = research_output[-3000:] if research_output else "(no output)"

    # Inject findings into original task metadata
    orig_meta = dict(original_task.get("metadata") or {})
    orig_meta["research_context"] = research_summary
    orig_meta["research_feeder_id"] = research_task_id
    orig_meta.pop("awaiting_research_feeder", None)

    # Remove the research dep from the original task's dependency list
    orig_deps = list(original_task.get("dependencies") or [])
    orig_deps = [d for d in orig_deps if d != research_task_id]

    # Cycle cap: track how many times this task has gone through the research feeder
    # path. If it exceeds the cap, cancel instead of resetting — prevents infinite
    # research→retry→fail→research loops on tasks with unfixable root causes.
    MAX_RESEARCH_CYCLES = 2
    cycles = orig_meta.get("research_feeder_cycles", 0) + 1
    orig_meta["research_feeder_cycles"] = cycles

    if not needs_human_review and cycles > MAX_RESEARCH_CYCLES:
        orig_meta["needs_human_review"] = True
        orig_meta["human_review_reason"] = (
            f"Research feeder cycle cap reached ({cycles} cycles). "
            f"Task has gone through research→retry {cycles} times without resolving. "
            f"Needs human diagnosis."
        )
        run_after = (datetime.now() + timedelta(hours=24)).isoformat()
        db_ref.task_update(original_task_id, {
            "status": "failed",
            "dependencies": orig_deps,
            "metadata": orig_meta,
            "run_after": run_after,
        })
        print(
            f"[Swarm] Research feeder cycle cap ({MAX_RESEARCH_CYCLES}) reached for "
            f"{original_task_id[:12]} — cancelling instead of resetting (cycle {cycles})"
        )
        return

    update: dict = {
        "status": "pending",
        "attempts": 0,
        "dependencies": orig_deps,
        "metadata": orig_meta,
    }

    if needs_human_review:
        orig_meta["needs_human_review"] = True
        orig_meta["human_review_reason"] = (
            f"Research feeder {research_task_id[:12]} exhausted all attempts without resolving. "
            f"Agent and research both failed — needs human diagnosis."
        )
        # Snooze for 24h so it doesn't immediately re-enter the queue
        run_after = (datetime.now() + timedelta(hours=24)).isoformat()
        update["run_after"] = run_after
        print(
            f"[Swarm] Research feeder {research_task_id[:8]} exhausted — "
            f"flagged {original_task_id[:12]} needs_human_review, snoozed 24h"
        )
    else:
        print(f"[Swarm] Research feeder {research_task_id[:8]} result injected into {original_task_id[:12]} — task reset to pending with research context")

    db_ref.task_update(original_task_id, update)


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
        task_type = task.get("type", "")
        task_meta = task.get("metadata") or {}
        escalation = get_escalation_policy(task_type)
        on_exhaust = escalation.get("on_exhaust", "cancel")

        if task_meta.get("is_recovery_task") and task_type == "bug" and project:
            # Legacy recovery task (pre-feeder): redirect to research feeder instead of
            # spawning another terminal continuation. These have already gone through
            # multiple recovery generations — research is the right next step.
            print(f"[Swarm] Legacy recovery task {task_id} exhausted — escalating to research feeder")
            _spawn_research_feeder(task, attempts, agent_output)
        elif task_meta.get("is_recovery_task"):
            # Legacy recovery task of a non-bug type — terminal continuation as before
            _spawn_terminal_recovery_continuation(task, attempts, agent_output)
        elif task_meta.get("is_research_feeder"):
            # Research feeder exhausted — mark cancelled, flag original for human review
            print(f"[Swarm] Research feeder {task_id} exhausted {attempts} attempts — flagging original task for human review")
            db.task_update(task_id, {"status": "cancelled"})
            # Inject partial findings + human review flag into original
            _apply_research_feeder_result(task_id, agent_output, needs_human_review=True)
        elif project and on_exhaust == "research":
            # New path: spawn research feeder, original task stays in place
            _spawn_research_feeder(task, attempts, agent_output)
        elif project and on_exhaust == "cancel":
            # QA/research/plan types: cancel cleanly, don't recurse
            print(f"[Swarm] Task {task_id} ({task_type}) exhausted — cancelling per escalation policy")
        elif project:
            # Fallback to legacy recovery for unknown types
            _spawn_review_task(task, attempts, agent_output)
