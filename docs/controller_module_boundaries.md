# Controller Module Boundaries

This document records the intended post-hardening module layout for controller integrity work. It is the handoff artifact for `rs7.9.4` and the canonical reference for where future mutation, reconciliation, diagnostics, and dashboard changes should land.

## Canonical Modules

### Invariants and mutation guards

- [`swarm/integrity.py`](../swarm/integrity.py)
  - task, dependency, head, and lifecycle predicates
  - use this first when deciding whether a write is valid
- [`swarm/task_mutations.py`](../swarm/task_mutations.py)
  - normalized dependency rewrites
  - shared task reset/cancel helpers
  - use this for post-create task graph edits instead of ad hoc `task_update(...)`

### Domain maintenance and reconciliation

- [`swarm/maintenance/project_heads.py`](../swarm/maintenance/project_heads.py)
  - canonical head lookup
  - head repair
  - genesis fallback
  - tail inference from live/history state
- [`swarm/maintenance/plans.py`](../swarm/maintenance/plans.py)
  - plan snapshot ownership
  - stale plan detection
  - planner-linked cleanup
- [`swarm/maintenance/agents.py`](../swarm/maintenance/agents.py)
  - agent/runtime drift repair
  - orphaned task reset
  - stale active-agent reconciliation
- [`swarm/maintenance/recovery.py`](../swarm/maintenance/recovery.py)
  - recovery tree collapse
  - canonical recovery/continuation selection
  - downstream dependency reparenting for broken recovery branches

### Compatibility and composition layers

- [`swarm/task_chains.py`](../swarm/task_chains.py)
  - lightweight chaining helpers only
  - composes project-head maintenance into task attachment rules
- [`swarm/plan_cleanup.py`](../swarm/plan_cleanup.py)
  - compatibility import surface for plan maintenance helpers
- [`swarm/agent_lifecycle.py`](../swarm/agent_lifecycle.py)
  - runtime orchestration and lifecycle transitions
  - should call domain maintenance modules, not reimplement them

### Diagnostics and operator surfaces

- [`swarm/api_deps.py`](../swarm/api_deps.py)
  - integrity findings
  - dependency graph rendering
  - plan ghost filtering
- [`dashboard_deps_integrity.js`](../dashboard_deps_integrity.js)
  - dependency graph interaction
  - integrity panel rendering
  - repair action wiring
- [`dashboard.js`](../dashboard.js)
  - dashboard bootstrap and shared UI glue
  - should delegate dependency/integrity concerns to `dashboard_deps_integrity.js`

## Where New Mutation Paths Must Hook In

When adding a new controller path that creates or mutates live task state:

1. Validate invariants through [`swarm/integrity.py`](../swarm/integrity.py) or an existing guarded API path.
2. Use [`swarm/task_mutations.py`](../swarm/task_mutations.py) for dependency rewrites, resets, and reparenting.
3. If the path touches `projects.head_task_id`, route through [`swarm/maintenance/project_heads.py`](../swarm/maintenance/project_heads.py).
4. If the path touches planner snapshots, route through [`swarm/maintenance/plans.py`](../swarm/maintenance/plans.py).
5. If the path repairs runtime drift or restart state, route through [`swarm/maintenance/agents.py`](../swarm/maintenance/agents.py).
6. If the path repairs recovery chains, route through [`swarm/maintenance/recovery.py`](../swarm/maintenance/recovery.py).

Do not introduce new direct writes to:

- `task.dependencies`
- `task.status` during recovery/repair flows
- `projects.head_task_id`
- `plans` ownership/cleanup state

unless the write is going through one of the modules above or there is a documented reason to add a new shared primitive first.

## Transitional Seams That Still Need Care

- [`swarm/plan_cleanup.py`](../swarm/plan_cleanup.py) still exists as a compatibility shim.
  - New code should import [`swarm/maintenance/plans.py`](../swarm/maintenance/plans.py) directly.
- [`swarm/task_chains.py`](../swarm/task_chains.py) still re-exports head behavior indirectly.
  - New head-specific code should use [`swarm/maintenance/project_heads.py`](../swarm/maintenance/project_heads.py).
- [`swarm/agent_lifecycle.py`](../swarm/agent_lifecycle.py) remains large because orchestration and task-transition behavior are still co-located.
  - Keep reconciliation helpers out of it unless they truly depend on lifecycle internals.
- [`dashboard.js`](../dashboard.js) still owns general dashboard bootstrap.
  - Future dependency graph or integrity UX work should go in [`dashboard_deps_integrity.js`](../dashboard_deps_integrity.js), not back into the monolith.

## Practical Rule Of Thumb

If a change can be described as one of these, it already has a canonical home:

- "Is this task/head valid?" -> [`swarm/integrity.py`](../swarm/integrity.py)
- "Rewrite a dependency/reset/cancel task safely" -> [`swarm/task_mutations.py`](../swarm/task_mutations.py)
- "Repair or choose the project head" -> [`swarm/maintenance/project_heads.py`](../swarm/maintenance/project_heads.py)
- "Clean or validate plan snapshots" -> [`swarm/maintenance/plans.py`](../swarm/maintenance/plans.py)
- "Repair orphaned agents/runtime drift" -> [`swarm/maintenance/agents.py`](../swarm/maintenance/agents.py)
- "Collapse or continue broken recovery chains" -> [`swarm/maintenance/recovery.py`](../swarm/maintenance/recovery.py)
- "Render or repair dependency/integrity UI" -> [`dashboard_deps_integrity.js`](../dashboard_deps_integrity.js)

If a change does not fit one of those buckets, add the bucket before adding the new mutation path.
