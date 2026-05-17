# Controller State Ownership Map

This is the condensed ownership view derived from the mutation audits in [controller_mutation_inventory.md](controller_mutation_inventory.md). It identifies the canonical source of truth for each state domain and the controller paths that should be guarded or refactored first.

## Canonical Sources of Truth

### Live task graph

- Canonical source: `tasks` table in [db.py](../swarm/db.py)
- Includes:
  - task status
  - dependencies
  - `agent_id`
  - metadata
- Guard/refactor paths:
  - [api_tasks.py](../swarm/api_tasks.py)
  - [agent_lifecycle.py](../swarm/agent_lifecycle.py)
  - [validation.py](../swarm/validation.py)
  - [worktree.py](../swarm/worktree.py)

### Project chain attachment

- Canonical source: `projects.head_task_id` in [db.py](../swarm/db.py)
- Approved helper layer:
  - [task_chains.py](../swarm/task_chains.py)
- Mirror/cache:
  - [projects.py](../swarm/projects.py) registry state
- Guard/refactor paths:
  - any remaining direct `proj.get("head_task_id")`
  - registry writes that bypass `task_chains`

### Agent runtime ownership

- Canonical persistent source: `agents` table in [db.py](../swarm/db.py)
- Canonical transient source: `_active_handles` in [agent_lifecycle.py](../swarm/agent_lifecycle.py)
- Guard/refactor paths:
  - `spawn_agent(...)`
  - `_finish_agent(...)`
  - `check_agent_status()`
  - `check_dep_violations()`

### Planner snapshot state

- Canonical source: `plans` table in [db.py](../swarm/db.py)
- Ownership helpers:
  - [plan_cleanup.py](../swarm/plan_cleanup.py)
- Important constraint:
  - `plans` are archival/rendering state, not live runnability state

### Archival state

- Canonical sources:
  - `task-history.jsonl`
  - `agent-history.jsonl`
  - `completed_task_ids`
- Roles:
  - history files: operator context and repair input
  - completed-task IDs: dependency continuity after pruning
- Guard/refactor paths:
  - [api_history.py](../swarm/api_history.py)
  - [api_deps.py](../swarm/api_deps.py)
  - `prune_history()` in [agent_lifecycle.py](../swarm/agent_lifecycle.py)

### Managed/unmanaged scheduling state

- Canonical source today: config/orchestrator globals in [api.py](../swarm/api.py) and [orchestrator.py](../swarm/orchestrator.py)
- UI reflection:
  - [dashboard.js](../dashboard.js)
- Risk:
  - not the same source as the project registry/sidebar

## Guarded Mutation Surfaces

These are the paths that should be treated as high-priority invariant enforcement surfaces:

1. Task creation and dependency mutation
   - [api_tasks.py](../swarm/api_tasks.py)
   - planner batch creation in [core.py](../swarm/tools/core.py)
2. Lifecycle-driven task creation and rewiring
   - [agent_lifecycle.py](../swarm/agent_lifecycle.py)
3. Planner reset and snapshot invalidation
   - [api_plans.py](../swarm/api_plans.py)
   - [plan_cleanup.py](../swarm/plan_cleanup.py)
4. History resurrection and archival rendering
   - [api_history.py](../swarm/api_history.py)
   - [api_deps.py](../swarm/api_deps.py)
5. Project-head writes and repairs
   - [task_chains.py](../swarm/task_chains.py)
   - [projects.py](../swarm/projects.py)

## Intentional Internal Rewrite Paths

These direct dependency rewrites are still allowed because they are internal controller continuity operations, not generic operator-facing mutation paths:

- continuation-task reparenting in [agent_lifecycle.py](../swarm/agent_lifecycle.py)
- recovery-task reparenting in [agent_lifecycle.py](../swarm/agent_lifecycle.py)
- validation bug-task reparenting in [validation.py](../swarm/validation.py)
- dependency integrity repair removals in [api_deps.py](../swarm/api_deps.py)

These paths still go through `db.task_update(...)`, so they retain DB-level self-cycle validation, but they are intentionally narrower than the public task-mutation API surfaces.

## Refactor Priorities

1. Centralize all project-head reads/writes through `task_chains`.
2. Add one normalized dependency/status mutation guard for post-create task updates.
3. Separate archival maintenance from live reconciliation in lifecycle code.
4. Unify managed-project semantics so scheduler and sidebar stop using divergent project-state models.
