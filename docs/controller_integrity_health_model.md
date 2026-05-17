# Controller Integrity Health Model

This document explains how to read the controller integrity signals now exposed in the API and dashboard, and how operators should respond when the system drifts into invalid state.

## Surfaces

- API: `GET /api/dependencies/integrity`
- Dashboard: `Task Dependencies -> Integrity` panel
- Repair endpoints:
  - `POST /api/projects/<project>/reconcile-head`
  - `POST /api/plans/<project>/cleanup`
  - `POST /api/projects/<project>/cleanup-recovery`
  - `POST /api/agents/reconcile`

## Signal Model

The integrity API now separates live-state problems from archival-only problems.

### Summary

`summary.active`
- `task_count`: active tasks scanned in the requested scope
- `missing_dependencies`: tasks whose live dependency list points at missing targets
- `dependency_violations`: tasks whose dependencies are structurally invalid
- `ready_tasks`: tasks with all live blockers satisfied
- `blocked_tasks`: tasks currently blocked for any reason

`summary.live`
- `problem_count`: total live integrity findings
- `stale_heads`: projects whose `head_task_id` is missing or invalid
- `orphaned_agents`: active agent records whose task/runtime ownership is broken
- `dead_blockers`: tasks blocked only by failed/missing dependencies
- `recursive_recovery`: recovery tasks incorrectly hanging off recovery tasks
- `continuity_gaps`: branches with no canonical continuation path

`summary.archival`
- `problem_count`: total archival-only findings
- `stale_plans`: saved planner snapshots that no longer describe valid live planner output
- `history_missing_dependencies`: historical records whose deps no longer resolve
- `history_task_count`: archived task count in the sampled scope

### Live Findings

These are actionable controller problems.

- `stale_heads`
  The project has no valid canonical attachment point. New work may float off-chain or attach to bad ancestors.

- `orphaned_agents`
  Agent/task ownership drifted across restart or failure. Runtime state and DB state disagree.

- `dead_blockers`
  Tasks are blocked by invalid state, not by normal dependency sequencing. These should not be treated like ordinary pending work.

- `recursive_recovery`
  Recovery containment failed and the branch is self-amplifying instead of converging.

- `continuity_gaps`
  A failed branch no longer has a canonical replacement/recovery/override path.

### Archival Findings

These affect operator visibility and graph hygiene, but are not directly runnable blockers.

- `stale_plans`
  Ghost planner snapshots that can pollute graph rendering or operator understanding if left in place.

- history issues
  Old data that may still be useful for forensics, but must not be treated as live runnability.

## Operator Workflow

### 1. Start with scope

- Select a project in the sidebar to diagnose a local chain problem.
- Leave no project selected to inspect global orphaned agents or system-wide drift.

### 2. Read the integrity panel

Use the panel in this order:

1. `Live issues`
2. `Invalid blockers`
3. `Archival issues`
4. `Blocked tasks`

The important distinction is:
- normal blocked tasks are expected DAG behavior
- invalid blockers mean the controller state needs repair before more work should be attached

### 3. Apply deterministic repairs

Use the panel buttons, not ad hoc DB edits:

- `Repair head`
  Use when `stale_heads > 0`

- `Clean stale plans`
  Use when `stale_plans > 0`

- `Clean recovery chain`
  Use when `recursive_recovery > 0` or `continuity_gaps > 0`

- `Reconcile agents`
  Use when `orphaned_agents > 0`

Each action is designed to be deterministic and idempotent relative to the current state.

### 4. Re-check before continuing work

After repair:
- live issue counts should drop
- invalid blocker counts should drop or reach zero
- the project head should point at a valid continuation node

If the counts do not improve, stop and inspect the specific branch rather than repeatedly clicking repairs.

## Intended Meanings

### Healthy project

- `summary.live.problem_count == 0`
- `Invalid blockers == 0`
- graph may still be long or heavily branched, but continuity is intact

### Needs operator attention

Any of the following should be treated as operational problems:

- `stale_heads > 0`
- `orphaned_agents > 0`
- `dead_blockers > 0`
- `recursive_recovery > 0`
- `continuity_gaps > 0`

### Archival cleanup only

If only `summary.archival.problem_count > 0`:
- the live graph may still be runnable
- cleanup improves graph readability and prevents ghost-node confusion

## Blind Spots

These are still not fully automatic:

- a sufficiently indirect external script can still mutate protected vendor/generated paths
- historical data may still require human judgment when deciding what to preserve versus prune
- some multi-step operator decisions remain policy choices, not automatic repairs
- the dashboard now surfaces the integrity state, but it does not yet explain every individual finding inline at node-level detail

## Guidance For Future Extensions

- New integrity signals should be classified as `live` or `archival` at the API level first.
- Every new repair path should have:
  - one deterministic endpoint
  - one dashboard action
  - one before/after operator-visible signal
  - regression coverage
- Avoid mixing forensic history views with live runnability logic.
