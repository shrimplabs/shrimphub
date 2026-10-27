# Controller Integrity Model

This document defines the controller-side invariants for live task orchestration. It is the contract the API, scheduler, lifecycle code, and repair flows should enforce.

## Core Rules

### Task Graph

- Live task dependencies must point to existing task IDs.
- A task cannot depend on itself.
- Dependency values are task IDs only, not file paths or scene paths.
- Failed and cancelled tasks are blocking failures, but they are not allowed to strand a live branch.
- Delegated child-task batches must obey the same dependency invariants as any other live task graph mutation. Delegation does not create a separate graph model.

#### Allowable Root Cases

- Project genesis anchors such as `<project>-genesis`
- Explicit system/internal controller roots where detached creation is intentional
- Fresh project planners that are chained to a valid project head or genesis anchor

Detached project work without one of the above justifications is invalid and should be chained to the canonical project head instead.

#### Immediate Reject Cases

- Missing dependency target
- Self-dependency
- Duplicate dependency IDs in the same task
- File paths, scene paths, or other non-task identifiers in `dependencies`
- New floating project task with no explicit detached/system justification

#### Graph Shape Examples

Valid parallel branch:

```text
project-genesis -> planner
planner -> feature-a
planner -> feature-b
planner -> integration
integration depends on feature-a and feature-b
```

Valid chain attachment for new work:

```text
project head = qa-bug-123
new audit task with no explicit deps
=> dependencies = [qa-bug-123]
```

Invalid floating root:

```text
project head = feature-2
new feature task created with dependencies = []
=> invalid unless explicitly detached/system/internal
```

### Branch Continuity

- A live branch may not remain blocked solely by a dead dependency without a canonical continuation decision.
- A live branch blocked by a same-file lock conflict should serialize through a canonical continuation task rather than overlapping the lock owner's write scope.
- Terminal failures must resolve through exactly one of:
  - replacement task
  - canonical recovery continuation
  - descendant cancellation/obsolescence
  - explicit operator override

### Project Head

- Each project has one canonical live attachment point: `projects.head_task_id`.
- A valid project head must:
  - exist in the task table
  - belong to the same project
  - be continuity-eligible (`pending`, `in_progress`, or `completed`)
- New root work should attach to the valid head unless explicitly created as a detached/system root.
- Branching DAGs are allowed. The head is the canonical attachment point for new root work, not the only runnable task in the project.
- When `head_task_id` is missing, stale, cross-project, or points at a blocking failure, the controller must repair it by:
  - choosing the newest continuity-eligible live tail when one exists
  - otherwise inferring from continuity-eligible project history
  - otherwise creating or reusing `<project>-genesis`
- Recovery or replacement continuation tasks should advance the project head when they supersede the failed branch head.

### Planner Output

- Planner output is atomic from the controller's perspective: accepted as a valid batch or rejected.
- Planner dependencies may reference existing task IDs or task IDs created in the same batch.
- Project creation and wizard task seeding must preserve explicit intra-batch dependencies exactly as planned.
- Only true batch roots may be auto-anchored to the current project head or `<project>-genesis`.
- Plan snapshots only describe tasks created by their planner and must not outlive planner validity.
- Planner snapshots may inform operator views and audits, but they are never authoritative sources of live runnability.

### Recovery

- Recovery tasks do not spawn recovery tasks.
- A failed branch should have one canonical live recovery continuation at a time.
- Recovery metadata should summarize the current failure context rather than recursively accumulating old chains.
- Recovery metadata stores bounded excerpts and size summaries, not unbounded raw logs.
- Terminal failure handling must preserve branch continuity by producing exactly one of:
  - replacement task
  - canonical recovery continuation
  - descendant cancellation/obsolescence
  - explicit operator override
- Terminal recovery failure should transition into a normal continuation task that preserves the branch and re-parents dependents.
- Recovery continuation is a branch-level repair mechanism, not a second independent root.

### Agents

- One active agent maps to one `in_progress` task.
- One `in_progress` task should have at most one active agent.
- Restarts must reconcile orphaned agents and tasks deterministically.
- New agent spawns should only happen from `pending` tasks with no current `agent_id`.
- Duplicate active agents for the same task are invalid and should be rejected at spawn time rather than cleaned up later.

### Live vs Archival

- Live orchestration decisions should be based on live tasks first.
- History and plan snapshots are archival evidence and repair input, not authoritative live state.
- Archival state must not silently create runnable ghost dependencies.
- Dependency rendering may include archival ghosts for operator context, but those ghosts must never be treated as runnable blockers or valid live heads.

### Safety Boundaries

- Vendor code and generated framework code are read-only by default. This includes `addons/`, `.godot/`, `.import/`, and `*.uid`.
- Archival/task-rendering data is read-only with respect to live scheduling decisions.
- Controller-owned scaffolding such as canonical harness templates should be installed deterministically by the controller, not reauthored ad hoc by feature agents.
- Subagent delegation must preserve graph visibility for durable work and enforce file-scope safety. See [controller_delegation_model.md](controller_delegation_model.md).

### Task-Type Authority Boundaries

- `plan` / `python_plan`: read-only analysis plus task creation. They may inspect, search, and delegate, but must not edit repo files or commit.
- `project_plan`: read-only repo analysis plus one atomic `create_tasks_file_aware()` delegation step. No shell mutation, no direct file edits, no one-off child task spawning.
- `qa` / `harness_qa` / `hybrid_qa`: read-only testing. They may interact with the running game, write `QA_REPORT.md`, file bug tasks, and requeue. They must not mutate project code, shell-edit files, or commit.
- `triage`: read-only diagnosis. It may inspect, run validation commands, file bug tasks, and write `TRIAGE_REPORT.md`, but not edit or commit project code.
- `research`: evidence-gathering only. It may run experiments, write findings under `research/*.md`, and spawn follow-up implementation tasks, but not patch or directly implement code.
- recovery tasks: may repair the current branch, but must not spawn arbitrary child task graphs. If they fail terminally, the controller creates the canonical continuation path.

#### Intentional Exceptions

- `project_create` is allowed to install canonical scaffolding and vendor dependencies because bootstrapping a new project is controller-owned setup work, not normal feature development.
- QA-family tasks may write `QA_REPORT.md` because reporting is their deliverable; they still may not mutate project code.
- `triage` may write `TRIAGE_REPORT.md` and file bug tasks because diagnosis output is its deliverable.
- `research` may write `research/*.md` and create follow-up implementation tasks because findings are its deliverable.
- `audit` and `triage` may keep using `run_command` for validation/API inspection even though they are otherwise read-only.

#### Remaining Escape Hatches

- The runtime blocks direct file edits and obvious shell writes into vendor/generated paths, but a sufficiently indirect external script could still mutate them. That remaining class should be treated as a follow-up hardening target if agents start exploiting it in practice.
- New task types inherit the global dispatch table unless they are explicitly constrained. Adding a task type without updating the authority matrix is a controller bug.

#### Notes For Future Task Types

- Every new task type should declare:
  - whether it is read-only, report-writing, implementation, or planner-only
  - which output files, if any, it is allowed to write
  - whether it may shell out via `run_command`
  - whether it may spawn child tasks
  - whether it may commit or push
- Runtime enforcement should be added in `swarm.agent_runtime._tool_authority_denial(...)`, and the same behavior should be reflected in prompt docs and regression tests before the task type is considered complete.

## State Ownership

- `tasks` table: live runnable graph, task status, dependencies, agent ownership.
- `projects.head_task_id`: canonical attachment point for new project work.
- `plans`: planner snapshots used for graph rendering and audit, never the source of live runnability.
- `task-history.jsonl`: archival history used for repair/inference only.
- `agents`: runtime process tracking that must reconcile with `tasks`.
- config/project management state: scheduler eligibility and operator intent.

## Hard-Reject vs Soft-Repair

### Hard-Reject Invariants

- Reject missing dependency targets on new live tasks.
- Reject self-dependencies and duplicate dependency IDs.
- Reject file paths, scene paths, and other non-task identifiers in `dependencies`.
- Reject duplicate active agents for the same task at spawn time.
- Reject new floating project work unless it is explicitly detached/system/internal.

### Soft-Repair Invariants

- Repair stale or invalid `head_task_id` values before chaining new work.
- Repair missing canonical project heads by inferring a continuity-eligible tail or creating/reusing genesis.
- Repair branch continuity after terminal failures via replacement, recovery continuation, descendant cancellation, or explicit operator override.
- Repair same-file lock conflicts by sequencing a continuation behind the lock owner while preserving the blocked task's other upstream dependencies and downstream dependents.
- Repair stale `in_progress` tasks whose agent has disappeared.
- Remove or invalidate stale plan snapshots when their planner or generated task graph is reset.

## Implementation Checklist

1. Validate project heads before using them for chaining.
2. Preserve explicit intra-batch planner/project-creation dependencies and only anchor true roots to head/genesis.
3. Keep tail inference aligned with continuity-eligible statuses only.
4. Prevent branch continuity loss when a blocking task fails, is cleaned up, or is serialized behind a file lock.
5. Keep archival plan/history state from reintroducing ghost live nodes.
6. Reject duplicate agent ownership at spawn time and reconcile orphaned runtime state after restart.
7. Keep vendor/generated-code boundaries enforced in controller tooling, not just prompts.
