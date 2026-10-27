# Controller Delegation Model

This document defines the intended model for structured subagent delegation in the swarm controller. It is the contract for the `delegate_helper` and `delegate_task_batch` work tracks and the reference for runtime, lifecycle, validation, and dashboard behavior.

## Goals

- Let a parent task break broad work into smaller units without losing graph integrity.
- Keep durable implementation work visible in the project DAG.
- Prevent delegated child work from causing overlapping parallel writes and merge-conflict churn.
- Distinguish short-lived exploratory helpers from durable child tasks.
- Preserve branch continuity, retries, recovery, and operator visibility.

## Delegation Types

### `delegate_helper`

Read-only helper delegation for bounded analysis.

Characteristics:
- transient helper agent, not a durable project task by default
- no file edits
- no git mutations
- no task creation unless explicitly promoted by the parent/controller
- returns findings to the parent task

Use cases:
- codebase inspection
- tracing symbol usage
- diagnosing a flaky validation failure
- proposing dependency edges for an existing task list
- gathering reproduction evidence

Helper delegation is advisory. It should inform the parent task's next move, not silently become the source of project truth.

### `delegate_task_batch`

Structured child-task delegation for durable work.

Characteristics:
- creates visible child tasks in the project graph
- each child has its own lifecycle, retries, bugs, and recovery
- each child declares write ownership up front
- the controller validates child overlap and dependency safety before creation
- parent/child traceability is recorded in task metadata

Use cases:
- broad feature tasks with separable implementation slices
- refactors that can be partitioned by module or write set
- integration work that should fan out and converge
- large bug fixes with clear subcomponents

Task delegation is durable. If the child work matters to project truth, it belongs in the graph.

## Selection Rule

- If the work is durable and affects repo truth, use `delegate_task_batch`.
- If the work is exploratory and informs the parent only, use `delegate_helper`.

Do not:
- use helpers for durable repo edits
- use child tasks for trivial one-shot lookups

## Parent Eligibility

Write-capable delegation should be limited to task types that already own implementation work, such as:
- `feature`
- `bug`
- `refactor`
- `polish`

Read-only helper delegation may be allowed more broadly, including:
- `feature`
- `bug`
- `refactor`
- `audit`
- `research`
- `triage`

Planner and QA-family task types should stay constrained:
- `project_plan` / `plan` / `python_plan`
  - already decompose via planner-specific task creation flows
  - should not gain arbitrary helper/task delegation without a separate policy decision
- `qa` / `harness_qa` / `hybrid_qa`
  - may file bug tasks through existing QA flows
  - should not spawn general-purpose implementation child batches

## File Ownership Rules

Every delegated child task must declare intended ownership:
- file paths
- modules
- or controller-recognized write scopes

The controller must validate the child batch before creation.

### Allowed Parallelism

Parallel child tasks are allowed only when their write scopes are disjoint.

Example:

```text
child-a owns scripts/tower_data.gd
child-b owns scripts/fusion_lookup.gd
child-c owns ui/tower_panel.gd
=> parallel is allowed
```

### Required Sequencing

If child write scopes overlap, the parent must either:
- explicitly chain them in order
- or keep the work local instead of delegating

Example:

```text
child-a owns scripts/tower_data.gd
child-b also owns scripts/tower_data.gd
=> must be sequential or rejected
```

### Reject Cases

Reject delegated child batches when:
- overlapping writes are declared without ordering
- child scopes are empty for write-capable tasks
- child dependencies are cyclic or invalid
- the parent attempts to parallelize obviously conflicting tasks in the same file area

## Parent Lifecycle Modes

Delegation should be explicit about what happens to the parent.

### `wait`

Parent delegates children and spawns a durable resume successor that depends on them.

Use when:
- the parent must resume with child outputs
- there is genuine follow-on reasoning or integration to do in the same task

Current controller behavior:
- child root tasks depend on the parent task id
- the parent records delegation metadata
- the parent can complete after delegation
- a resume successor task becomes the durable continuation node that waits on child completion

### `integrate`

Parent delegates implementation children plus a final integration successor task.

Use when:
- broad work should fan out, then converge into a known integration phase
- the parent should not remain a vague implementation owner after delegation

Current controller behavior:
- child root tasks depend on the parent task id
- the parent records delegation metadata
- the parent can complete after delegation
- the integration successor depends on all child tasks and becomes the convergence point

### `replace`

Parent is superseded by the child graph.

Use when:
- the original task was too broad
- child tasks fully replace the parent's implementation responsibility

Current controller behavior:
- child root tasks depend on the parent task id
- the parent records delegation metadata
- no successor task is created
- the child batch itself becomes the continuation of the branch

### Forbidden Parent State

The parent must not:
- delegate all meaningful work away
- remain runnable as if it still owns the same implementation
- duplicate responsibility with its children

That creates ambiguous ownership and bug-chain noise.

## Metadata and Traceability

Delegated child tasks should record:
- `parent_task_id`
- `delegation_batch_id`
- `delegation_mode`
- declared write scope
- child ordering/group metadata

Helper delegations should record lightweight runtime metadata:
- parent task id
- helper purpose
- scope
- helper result summary

This preserves auditability without forcing helpers into the durable task DAG.

## Failure and Continuity Rules

### Helper failure

Helper failure should not alter project graph continuity directly.

The parent may:
- retry locally
- request a new helper
- promote the problem into a durable child task if needed

### Child task failure

Delegated child tasks follow normal task lifecycle:
- retry
- recovery continuation
- replacement/continuation bug insertion
- descendant continuity rules

### Same-file lock conflict during ordinary parallel work

When two non-delegated live tasks converge on the same file:
- the first writer claims the file lock
- the losing task must not overlap that write
- the controller should create a canonical continuation task behind the lock owner
- that continuation should inherit the blocked task's other upstream dependencies
- downstream dependents of the blocked task should be reparented onto the continuation

This is not a separate delegation mechanism, but it follows the same continuity principle:
- serialize durable work in the graph
- keep ownership visible
- do not hide overlap resolution in transient runtime state alone

Parent lifecycle must remain consistent:
- `wait` parents stay blocked on the canonical continuation path
- `integrate` parents wait on repaired children or replacement branches
- `replace` parents are already out of the live ownership path

## Observability Requirements

Operators must be able to see:
- which parent delegated what
- helper vs child-task delegation
- child write ownership
- blocked/waiting parent state
- current delegation batch topology

Delegation that matters must not be invisible.

## Initial Rollout Constraints

- structured child-task delegation is limited to at most 6 children per batch
- delegated child tasks may not create further structured child-task batches by default
- helper delegation remains available to delegated children because it is read-only
- operators should treat delegation metadata and successor tasks as the source of truth for parent continuation during rollout

First implementation should be intentionally narrow:

- support `delegate_helper` only for read-only work
- support `delegate_task_batch` only for structured child task creation with explicit file scopes
- cap child count per delegation batch
- reject overlapping parallel writes instead of trying to be clever
- do not introduce arbitrary recursive delegation without depth limits

## Relationship To Existing Features

Existing pieces that already align with this model:
- `parent_task_id` metadata
- `list_subtasks(...)`
- planner file-aware task creation
- QA bug-task file-ownership chaining

Delegation should build on those, not bypass them.

## Summary

The swarm should support two delegation modes:

- `delegate_helper`
  - transient, read-only, advisory
- `delegate_task_batch`
  - durable, graph-visible, file-aware, overlap-safe

The controller must enforce:
- explicit child ownership
- no unsafe overlapping parallel writes
- explicit parent lifecycle semantics
- full traceability of delegated work

If a delegated unit affects project truth, it belongs in the graph.
