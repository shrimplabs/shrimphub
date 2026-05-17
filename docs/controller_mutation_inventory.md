# Controller Mutation Inventory

This inventory maps the primary controller paths that create or mutate live task state and project head state. It is the handoff artifact for the `rs7.2.x` audit beads and the enforcement/reconciliation work that follows.

## Canonical State

- `tasks` table in [`swarm/db.py`](../swarm/db.py)
  - live task graph, status, dependencies, `agent_id`, metadata
- `projects.head_task_id` in [`swarm/db.py`](../swarm/db.py)
  - canonical attachment point for new project work
- `agents` table in [`swarm/db.py`](../swarm/db.py)
  - runtime process ownership and task linkage

## Shared Integrity Helpers

- [`swarm/integrity.py`](../swarm/integrity.py)
  - task/head/lifecycle predicates
- [`swarm/task_chains.py`](../swarm/task_chains.py)
  - head repair, tail inference, root-task chaining helpers
  - batch-root anchoring helpers for project creation and wizard seeding
- [`swarm/task_mutations.py`](../swarm/task_mutations.py)
  - normalized dependency rewrites
  - canonical pending resets
  - shared reparenting/cancel-with-metadata helpers

These should become the only approved entry points for head resolution, root-task attachment, and common post-create task rewrites.

## Task Creation and Mutation Paths

### API task routes

- [`swarm/api_tasks.py`](../swarm/api_tasks.py)
  - `POST /api/tasks`
    - creates single tasks
    - already uses `chain_to_project_head(..., ensure_head=True)` when no deps are supplied
  - `POST /api/tasks/batch`
    - creates multi-task batches
    - now uses `ensure_project_head()` for `chain_to_head`
  - `PATCH /api/tasks/<id>`
    - updates task fields directly
    - invariant gap: dependency/status edits are still largely ad hoc after creation
  - dependency repair endpoints at the bottom of the file
    - mutate dependencies directly
    - invariant gap: should eventually share one dependency-normalization path

### Planner/reset routes

- [`swarm/api_plans.py`](../swarm/api_plans.py)
  - `POST /api/plans/<project>/reset`
    - cancels/deletes planner-generated tasks
    - creates replacement planner
    - now uses `ensure_project_head()` for anchor repair before replacement

### Project and wizard routes

- [`swarm/api_projects.py`](../swarm/api_projects.py)
  - project creation creates genesis directly and sets head through project registry
  - restart/requeue endpoints mutate task status directly
- [`swarm/api_wizard.py`](../swarm/api_wizard.py)
  - project/task creation path
  - now preserves explicit intra-batch dependencies and only anchors true roots through task-chain helpers

### Spawn/chat/history routes

- [`swarm/api_spawn.py`](../swarm/api_spawn.py)
  - `POST /api/create-project`
    - creates `_swarm` project tasks through `chain_to_project_head`
- [`swarm/api_chat.py`](../swarm/api_chat.py)
  - chat-created project/task batches now preserve explicit intra-batch dependencies and only anchor true roots to project head/genesis
  - also mutates managed-project config state
- [`swarm/api_history.py`](../swarm/api_history.py)
  - resurrects archived tasks and mutates live status
  - invariant gap: archival resurrection should be reviewed against continuity/head rules

### Runtime/controller-generated tasks

- [`swarm/agent_lifecycle.py`](../swarm/agent_lifecycle.py)
  - auto integration bug spawn
  - auto QA spawn
  - auto audit spawn
  - recovery task spawn
  - task completion/failure status transitions
  - current gap: some auto QA/audit paths still read raw `proj.get("head_task_id")`
- [`swarm/orchestrator.py`](../swarm/orchestrator.py)
  - auto planner/project task creation
  - project bootstrap creation

### Validation/worktree helpers

- [`swarm/validation.py`](../swarm/validation.py)
  - bug task creation and dependency rewrites after validation
- [`swarm/worktree.py`](../swarm/worktree.py)
  - merge-conflict bug creation and metadata updates
- [`swarm/agent_runtime.py`](../swarm/agent_runtime.py)
  - file-lock claim enforcement before first shared write
  - same-file lock conflict handoff creation with upstream dependency preservation

## Project Head Mutation Paths

### Canonical paths

- [`swarm/task_chains.py`](../swarm/task_chains.py)
  - `ensure_project_head()`
  - genesis creation and repaired head persistence
- [`swarm/agent_lifecycle.py`](../swarm/agent_lifecycle.py)
  - recovery spawn advances head when failed branch head is superseded
  - prune-history repair updates heads from continuity-eligible completed tails

### Legacy/direct paths still present

- [`swarm/projects.py`](../swarm/projects.py)
  - `set_head_task_id()` writes directly to project state
- [`swarm/api_projects.py`](../swarm/api_projects.py)
  - initial project creation sets genesis as head directly
- [`swarm/api.py`](../swarm/api.py)
  - startup registration mirrors repaired head back into registry
- [`swarm/agent_lifecycle.py`](../swarm/agent_lifecycle.py)
  - auto QA/audit still read raw head in some branches instead of `get_project_head()`

## Current Gaps To Carry Into Enforcement

1. Head access is not fully centralized yet.
   - Some lifecycle paths still read `proj.get("head_task_id")` directly.
2. Task mutation after creation is not uniformly guarded.
   - direct `task_update(...)` calls can still rewrite dependencies/status/metadata without invariant checks.
3. Archival resurrection needs continuity review.
   - history/plan state can still be promoted back to live state through separate code paths.
4. Project registry and DB head writes are still split.
   - `projects.py` and `db.project_upsert(...)` both mutate head state.
5. Lock-conflict continuation is runtime-driven, but broader post-create dependency rewrites are still spread across lifecycle and API paths.

## Next Enforcement Targets

1. Route all head reads/writes through `swarm.task_chains` or a dedicated integrity helper.
2. Add one normalized dependency/status mutation guard for post-create updates.
3. Audit history resurrection and auto-generated lifecycle tasks against the new head/continuity rules.
4. Unify project-registry head mirroring so the DB remains the canonical source of truth.

## Recovery, Agent, and Restart Mutation Paths

### Recovery creation and branch continuation

- [`swarm/agent_lifecycle.py`](../swarm/agent_lifecycle.py)
  - `_handle_task_failure(...)`
    - decides retry vs terminal failure
    - currently blocks recovery-from-recovery by checking `metadata.is_recovery_task`
  - `_spawn_review_task(...)`
    - creates canonical recovery task
    - reparents downstream dependents from failed task to recovery task
    - now advances project head when the failed task was the branch head

### Agent/task transition paths

- [`swarm/agent_lifecycle.py`](../swarm/agent_lifecycle.py)
  - `spawn_agent(...)`
    - writes `agents` row
    - transitions task `pending -> in_progress`
    - now rejects duplicate/ambiguous ownership before spawning
  - `_finish_agent(...)`
    - writes final `agents` status
    - transitions tasks to `completed`, `pending`, or `failed`
    - creates continuation/recovery/integration/QA/audit tasks as follow-on work
    - can reparent dependents to continuation tasks on success
  - `check_dep_violations()`
    - kills active agents whose dependencies became unmet
    - resets task status to `pending`

### Restart and orphan reconciliation

- [`swarm/agent_lifecycle.py`](../swarm/agent_lifecycle.py)
  - `check_agent_status()`
    - reconciles finished/timed-out in-memory handles
    - reconciles DB-tracked active agents from previous server runs by PID
    - resets `in_progress` tasks whose active agent disappeared
  - `prune_history()`
    - repairs `head_task_id` from continuity-eligible completions
    - currently doubles as a head repair pass during lifecycle monitoring
- [`swarm/api_agents.py`](../swarm/api_agents.py)
  - can force-stop agents and reset tasks back to `pending`
- [`swarm/api.py`](../swarm/api.py)
  - startup registration/repair mirrors repaired head state into the project registry

### Observed lifecycle risk points

1. Auto-generated QA and audit follow-ons in [`swarm/agent_lifecycle.py`](../swarm/agent_lifecycle.py) still read raw `head_task_id` in a couple branches.
2. Restart reconciliation is split across:
   - DB-tracked active agents
   - in-memory `_active_handles`
   - watchdog reset of `in_progress` tasks
   This is effective, but the ownership model is still spread across multiple loops.
3. Recovery continuation and continuation-task reparenting both mutate downstream dependencies directly.
   - these should eventually share one normalized dependency rewrite path
4. `prune_history()` still mixes archival maintenance with live head repair.
   - likely needs separation once reconciliation work begins

## Follow-on Targets From Lifecycle Audit

1. Centralize follow-on task chaining in lifecycle code on `get_project_head()` / `append_project_head()`.
2. Consolidate task/agent reconciliation into an explicit runtime ownership repair path rather than several partial loops.
3. Separate archival pruning from live head repair so reconciliation logic is easier to reason about and test.
4. Reuse one dependency rewrite helper for recovery and continuation reparenting.

## Planner, Plan Snapshot, and History Mutation Paths

### Planner output creation

- [`swarm/tools/core.py`](../swarm/tools/core.py)
  - `create_tasks_file_aware(...)`
    - canonical planner batch creation path
    - tags generated tasks with `metadata.parent_task_id`
- [`swarm/agent_lifecycle.py`](../swarm/agent_lifecycle.py)
  - successful `project_plan` completion snapshots generated subtasks into a `plans` record
  - invalid planner batches are cancelled and matching plan snapshots are deleted

### Plan snapshot lifecycle

- [`swarm/db.py`](../swarm/db.py)
  - `plan_upsert(...)`
  - `plan_get_by_project(...)`
  - `plan_delete(...)`
- [`swarm/plan_cleanup.py`](../swarm/plan_cleanup.py)
  - `project_plan_subtasks(...)`
  - `matching_plan_ids(...)`
  - `delete_matching_plans(...)`
- [`swarm/api_plans.py`](../swarm/api_plans.py)
  - reset route cancels planner output, deletes matching plan snapshots, and may queue a replacement planner

### History write and repair paths

- [`swarm/agent_lifecycle.py`](../swarm/agent_lifecycle.py)
  - `prune_history()`
    - archives finished agents to `agent-history.jsonl`
    - archives finished tasks to `task-history.jsonl`
    - truncates task history file
    - also repairs `projects.head_task_id` from continuity-eligible completions
- [`swarm/db.py`](../swarm/db.py)
  - `task_record_completed(...)`
    - durable completed-task ID set for dependency validation even after pruning

### History read and resurrection paths

- [`swarm/api_history.py`](../swarm/api_history.py)
  - reads `agent-history.jsonl`
  - requeues failed tasks from history
  - resurrects pruned tasks from `task-history.jsonl`
  - chains resurrected tasks through `chain_to_project_head(..., ensure_head=True)`
- [`swarm/api_deps.py`](../swarm/api_deps.py)
  - reads `task-history.jsonl`
  - reads `plans.task_graph`
  - renders archival ghost nodes in dependency view
  - can inspect missing deps in historical samples
- [`swarm/task_chains.py`](../swarm/task_chains.py)
  - infers project tails from continuity-eligible history when no valid live head exists

### Planner/history risk points

1. `prune_history()` currently mixes:
   - archival write
   - live-task deletion
   - head repair
   This is operationally useful but conflates ownership domains.
2. `plans.task_graph` remains a rendering/audit artifact, but it is still rich enough to create ghost-node confusion if invalidation misses a reset path.
3. History resurrection restores old tasks into the live graph.
   - this is necessary, but it means archival state is not purely passive and must keep respecting head/continuity rules.
4. Completed-task ID durability in [`swarm/db.py`](../swarm/db.py) is a separate continuity aid from `task-history.jsonl`.
   - enforcement work should treat these as different roles, not redundant copies.

## Follow-on Targets From Planner/History Audit

1. Separate archival maintenance from live-head repair where practical.
2. Ensure every planner invalidation path deletes or invalidates matching `plans` snapshots.
3. Keep history resurrection on the same chaining and dependency-validation helpers as normal task creation.
4. Treat `completed_task_ids`, `task-history.jsonl`, and `plans.task_graph` as three distinct sources with different authority levels in the ownership map.

## Scheduler, Managed-Project, and Graph-Rendering State Paths

### Managed/unmanaged project state

- [`swarm/api.py`](../swarm/api.py)
  - loads `managed_projects` and `paused_projects` from config into orchestrator globals
  - exposes `/api/managed-projects`
  - persists config changes back to `config.json`
- [`swarm/api_chat.py`](../swarm/api_chat.py)
  - auto-adds projects to `managed_projects`
  - persists config state independently
- [`swarm/api_wizard.py`](../swarm/api_wizard.py)
  - also auto-adds projects to `managed_projects`
- [`dashboard.js`](../dashboard.js)
  - reads `/api/managed-projects`
  - maintains local `_pausedProjects` state for operator toggles

### Project registry vs DB project state

- [`swarm/projects.py`](../swarm/projects.py)
  - registry-backed project metadata and locks
- [`swarm/db.py`](../swarm/db.py)
  - persistent project records including `head_task_id`
- [`swarm/api_projects.py`](../swarm/api_projects.py)
  - project CRUD through the registry
- [`swarm/api.py`](../swarm/api.py)
  - startup repair mirrors managed projects into the registry and mirrors repaired heads back into registry state

### Graph-rendering sources

- [`swarm/api_deps.py`](../swarm/api_deps.py)
  - live tasks from `tasks`
  - historical ghost nodes from `task-history.jsonl`
  - plan ghosts from `plans.task_graph`
  - styling cue for canonical head via `get_project_head()`
- [`dashboard.js`](../dashboard.js)
  - renders dependency graph from `/api/deps/dot`
  - exposes `history_limit` operator control
  - keeps local view transform state separate from graph data

### Scheduler/graph risk points

1. Managed-project eligibility is config/orchestrator state, not the same source as the sidebar project registry.
   - this is the root of the “visible but not auto-managed” confusion
2. Project registry, DB project rows, and orchestrator managed sets are three different representations of “known project”.
3. The dependency graph intentionally mixes live and archival data for operator context.
   - this is useful, but it means the UI can imply live structure from archival ghosts unless invalidation is disciplined.
4. `history_limit` is presentation state, not integrity state.
   - ownership mapping should keep that separate from actual graph authority.

## Follow-on Targets From Scheduler/Graph Audit

1. Unify project-management semantics around one canonical managed/unmanaged flag or a clearly synchronized model.
2. Make the DB the authoritative source for live project/chain state, with the registry as a mirror/cache.
3. Keep graph rendering explicitly layered: live tasks, archival history, plan ghosts.
4. Surface the managed/unmanaged distinction and archival/live distinction more clearly in operator UX.
