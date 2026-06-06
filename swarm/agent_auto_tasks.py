"""
swarm.agent_auto_tasks -- auto-spawned follow-on task logic.

Extracted from agent_lifecycle._finish_agent. Contains three post-completion
auto-spawn behaviours:

1. auto_spawn_integration_task  -- wire newly added Godot systems into the game
2. auto_handle_sprint_qa        -- mark sprint QA complete for auto-replan projects
3. auto_spawn_qa_task           -- periodic QA trigger for non-sprint Godot projects

Each function accepts the values it needs as arguments so it can be called
from _finish_agent without the extracted module needing to hold global state.
All access to db / _task_chains is done via the agent_lifecycle lazy-import
helpers at call-time.
"""

from __future__ import annotations

import time
from pathlib import Path

from swarm.experiment_metadata import stamp_experiment_metadata


def _al():
    """Return the agent_lifecycle module (lazy, to avoid circular imports)."""
    import swarm.agent_lifecycle as _lifecycle
    return _lifecycle


def _db():
    import swarm.agent_lifecycle as _lifecycle
    _lifecycle._lazy_imports()
    return _lifecycle.db


def _task_chains():
    import swarm.agent_lifecycle as _lifecycle
    _lifecycle._lazy_imports()
    return _lifecycle._task_chains


# ---------------------------------------------------------------------------
# 1. Auto-integration
# ---------------------------------------------------------------------------

def auto_spawn_integration_task(
    project: str,
    task_id: str,
    task_type_finished: str,
    task_for_val: dict | None,
    diff_stat: str,
    workspace: Path,
    validation_failed: bool,
) -> None:
    """Spawn an integration task after a feature/polish completes on a Godot project."""
    if validation_failed:
        return
    if task_type_finished not in ("feature", "polish"):
        return
    if not task_for_val:
        return
    if task_for_val.get("metadata", {}).get("is_integration_task"):
        return

    project_path = workspace / project
    if not (project_path / "project.godot").exists():
        return

    db = _db()
    existing_tasks = db.task_get_by_project(project)
    has_integration = any(
        t.get("metadata", {}).get("is_integration_task")
        and t.get("status") in ("pending", "in_progress", "failed")
        for t in existing_tasks
    )
    if has_integration:
        return

    orig_desc = task_for_val.get("description", "")[:300]
    integ_id = f"integration-{project}-{int(time.time())}"
    integ_desc = (
        f"Integration: wire up recently completed features into the game.\n\n"
        f"Triggering task: {orig_desc}\n\n"
        f"Files changed by triggering task:\n{diff_stat[:400]}\n\n"
        f"IMPORTANT: Other features may have completed while a previous integration "
        f"task was running. Before doing anything, run:\n"
        f"  git log --oneline -15\n"
        f"  git diff HEAD~10..HEAD --stat\n"
        f"to find ALL recently committed work, not just the triggering task. "
        f"Read every changed file. Identify any new systems, signals, nodes, or "
        f"autoloads that were added across all recent commits. Ensure they are "
        f"properly connected to existing game systems (SignalBus, GameManager, "
        f"scene tree, UI). Fix any disconnections. "
        f"Do NOT rewrite existing working systems \u2014 only add the missing wiring."
    )
    metadata = stamp_experiment_metadata(project, {"is_integration_task": True})
    db.task_upsert({
        "id": integ_id,
        "project": project,
        "type": "bug",
        "description": integ_desc,
        "priority": 85,
        "status": "pending",
        "dependencies": [task_id],
        "metadata": metadata,
        "attempts": 0,
        "max_attempts": 2,
    })
    print(f"[Swarm] Auto-spawned integration task {integ_id} for {project} after feature completion")


# ---------------------------------------------------------------------------
# 2. Sprint QA marker
# ---------------------------------------------------------------------------

def auto_handle_sprint_qa(
    project: str,
    task_type_finished: str,
    auto_replan_projects: list,
    projects_sprint_qa_done: set,
    validation_failed: bool,
) -> None:
    """Mark a project's sprint QA as done when a QA task completes for an auto-replan project."""
    if validation_failed:
        return
    if task_type_finished not in ("qa", "harness_qa", "hybrid_qa"):
        return
    if project not in auto_replan_projects:
        return
    projects_sprint_qa_done.add(project)
    print(f"[Swarm] Sprint QA complete for {project} \u2014 planner fires on next empty queue")


# ---------------------------------------------------------------------------
# 3. Auto-QA
# ---------------------------------------------------------------------------

def auto_spawn_qa_task(
    project: str,
    task_id: str,
    task_type_finished: str,
    task_for_val: dict | None,
    workspace: Path,
    auto_replan_projects: list,
    qa_completion_counter: dict,
    qa_auto_threshold: int,
    validation_failed: bool,
    spawned_continuation: bool,
    is_recovery_task: bool,
) -> None:
    """Increment QA counter and auto-spawn a QA task when the threshold is reached."""
    _skip = {"qa", "harness_qa", "hybrid_qa", "scenario_qa", "audit", "manager", "project_create", "project_plan"}
    if validation_failed or spawned_continuation or is_recovery_task:
        return
    if task_type_finished in _skip:
        return

    project_path = workspace / project
    if not (project_path / "project.godot").exists():
        return
    if project in auto_replan_projects:
        return

    db = _db()
    qa_completion_counter[project] = qa_completion_counter.get(project, 0) + 1
    existing = db.task_get_by_project(project)
    _qa_types = {"qa", "harness_qa", "hybrid_qa", "scenario_qa"}
    # Suppress if the full sprint-close sequence is already in the pipeline.
    # Check QA specifically — if QA is queued, art_pass and polish must already be there too.
    has_qa = any(
        t.get("type") in _qa_types
        and t.get("status") in ("pending", "in_progress", "failed")
        for t in existing
    )
    open_nonqa = [
        t for t in existing
        if t.get("status") in ("pending", "in_progress")
        and t.get("id") != task_id
        and t.get("type") not in (_qa_types | {"audit", "manager"})
    ]
    # Don't spawn QA while bug tasks are still active — QA will race with fixes
    # and get killed by the dep violation checker. Wait for a clean project state.
    has_active_bugs = any(
        t.get("type") == "bug"
        and t.get("status") in ("pending", "in_progress")
        for t in existing
    )
    threshold_due = qa_completion_counter[project] >= qa_auto_threshold
    empty_queue_due = not open_nonqa
    if not (threshold_due or empty_queue_due):
        return

    qa_completion_counter[project] = 0
    if has_qa:
        return
    if has_active_bugs:
        print(f"[Swarm] Deferring auto-QA for {project}: {sum(1 for t in existing if t.get('type')=='bug' and t.get('status') in ('pending','in_progress'))} bug task(s) still active")
        # Don't zero the counter — recheck next completion
        qa_completion_counter[project] = qa_auto_threshold
        return

    tc = _task_chains()
    has_harness = (project_path / "autoload" / "test_harness.gd").exists()
    qa_type = "harness_qa" if has_harness else "qa"
    now = int(time.time())
    qa_reason = (
        f"after {qa_auto_threshold} completions"
        if threshold_due else
        "because the Godot project queue is empty"
    )

    # If no art_pass is already queued, insert one before the polish + QA tasks.
    # Art pass must always precede polish and QA so visual gaps are fixed first.
    has_art_pass = any(
        t.get("type") == "art_pass" and t.get("status") in ("pending", "in_progress", "failed")
        for t in existing
    )
    has_polish = any(
        t.get("type") == "polish" and t.get("status") in ("pending", "in_progress", "failed")
        for t in existing
    )
    upstream_id = task_id  # chain builds: triggering task → art_pass → polish → qa
    if not has_art_pass:
        art_id = f"art-auto-{project}-{now}"
        art_deps = tc.append_project_head(
            db, project, [task_id], task_id=art_id, ensure_head=True
        )
        db.task_upsert({
            "id": art_id,
            "project": project,
            "type": "art_pass",
            "description": f"Auto art pass: ensure game entities are visible on screen, sprites/textures are attached, and no placeholder geometry remains before QA ({qa_reason})",
            "priority": 60,
            "status": "pending",
            "dependencies": art_deps,
            "metadata": stamp_experiment_metadata(project, {"auto_spawned": True}),
            "attempts": 0,
            "max_attempts": 2,
        })
        upstream_id = art_id
        print(f"[Swarm] Auto-spawned art_pass task {art_id} for {project} before QA")
    else:
        # Art pass already queued — find its ID so polish chains to it, not to the triggering task
        art_task = next(
            (t for t in existing if t.get("type") == "art_pass" and t.get("status") in ("pending", "in_progress", "failed")),
            None,
        )
        if art_task:
            upstream_id = art_task["id"]

    if not has_polish:
        pol_id = f"pol-auto-{project}-{now}"
        pol_deps = tc.append_project_head(
            db, project, [upstream_id], task_id=pol_id, ensure_head=True
        )
        db.task_upsert({
            "id": pol_id,
            "project": project,
            "type": "polish",
            "description": f"Auto UI/UX polish: ensure screen transitions, button feedback, menu flow, HUD clarity, and game feel are correct before QA ({qa_reason})",
            "priority": 60,
            "status": "pending",
            "dependencies": pol_deps,
            "metadata": stamp_experiment_metadata(project, {"auto_spawned": True}),
            "attempts": 0,
            "max_attempts": 2,
        })
        upstream_id = pol_id
        print(f"[Swarm] Auto-spawned polish task {pol_id} for {project} before QA")
    else:
        # Polish already queued — find its ID so QA chains to it, not to the triggering task
        pol_task = next(
            (t for t in existing if t.get("type") == "polish" and t.get("status") in ("pending", "in_progress", "failed")),
            None,
        )
        if pol_task:
            upstream_id = pol_task["id"]

    qa_id = f"qa-auto-{project}-{now}"
    qa_desc = (
        f"Auto harness QA: run synchronous checkpoint tests against the game logic ({qa_reason})"
        if has_harness else
        f"Auto QA: playtest and verify core game loop is functional after recent changes ({qa_reason})"
    )
    qa_deps = tc.append_project_head(
        db, project, [upstream_id], task_id=qa_id, ensure_head=True
    )
    db.task_upsert({
        "id": qa_id,
        "project": project,
        "type": qa_type,
        "description": qa_desc,
        "priority": 75,
        "status": "pending",
        "dependencies": qa_deps,
        "metadata": stamp_experiment_metadata(project, {}),
        "attempts": 0,
        "max_attempts": 2,
    })
    print(f"[Swarm] Auto-spawned {qa_type} task {qa_id} for {project} {qa_reason} (deps: {len(qa_deps)} task(s))")

