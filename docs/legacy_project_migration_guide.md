# Legacy Project Migration Guide

This guide describes how to normalize older projects that were created before the current controller invariants existed.

Use it when a project shows symptoms like:

- stale heads
- stale plan snapshots
- floating chains in the dependency graph
- blocked auto scheduling despite apparently ready work
- old recovery branches or failed branches still acting as blockers
- corrupted project lock state

This is an operator migration process for bringing a legacy project forward to the current live-state model.

## What "Legacy" Means

A project is "legacy" when some of its historical or live state was written before the controller enforced the current rules around:

- canonical project heads
- planner ownership and plan snapshots
- branch continuity after failures
- lock cleanup
- separation of live state from archival state

The project may still be functional. "Legacy" here means its stored state may not satisfy the current controller invariants.

## Migration Goals

After migration, the project should satisfy all of these:

- the project has one valid `head_task_id`
- live tasks form a coherent dependency graph
- no live task depends only on dead/missing blockers without a continuation path
- saved plan snapshots are either valid under current rules or removed
- project locks are not stale or corrupted
- old archival state is not interfering with live scheduling

## Core Principle

Do not try to make every old historical artifact fully live-compatible.

Instead:

1. normalize the current live chain
2. preserve useful history as archival evidence
3. remove or quarantine archival records that interfere with live scheduling or graph rendering

## Migration Order

Always migrate a legacy project in this order:

1. inspect live integrity findings
2. repair lock state
3. repair the project head
4. inspect live task continuity
5. inspect planner snapshots
6. clean stale plan snapshots
7. validate graph rendering against the repaired live state
8. only then consider pruning or archiving old historical branches

Do not start by deleting history. That can hide the real cause.

## Step 1: Inspect Integrity

Use the integrity view or API to collect:

- stale heads
- stale plans
- dead blockers
- continuity gaps
- recursive recovery
- orphaned agents

Interpretation:

- `stale_heads` means `projects.head_task_id` does not point at the real live continuation
- `stale_plans` means a saved planner snapshot no longer matches valid planner/live state
- `dead_blockers` means a live branch is still blocked by a dead dependency
- `continuity_gaps` means branch repair is missing

If the project has zero live findings but still looks strange in the graph, the issue is often archival rendering rather than live scheduling.

## Step 2: Repair Project Lock State

Before doing anything else, verify:

- `project.locked == false` unless an active agent actually owns the project
- `locked_at` is a real timestamp or `null`
- `unlocked_at` is a real timestamp or `null`

Known legacy symptom:

- `locked_at` or `unlocked_at` set to `"0"` or `"1"`

If present:

- run the lock repair path
- clear stale project locks

Why first:

- stale locks can make a healthy project appear unschedulable
- this can be mistaken for dependency or head issues

## Step 3: Repair the Project Head

The project head is the canonical live attachment point.

Migration rule:

- if the stored head is invalid, replace it with the newest continuity-eligible live tail
- if no valid live tail exists, infer from continuity-eligible history
- if no usable history exists, fall back to `<project>-genesis`

Do not leave a head pointing at:

- a missing planner node
- a failed/cancelled task with no continuation
- a cross-project task
- a pruned/deleted node

Important:

- a branching DAG is still allowed
- this step only establishes the canonical continuation point for future root attachment

## Step 4: Repair Live Branch Continuity

Legacy projects often contain branches where:

- a feature failed
- a bug/recovery node was inserted
- dependents were not repointed cleanly

Migration rule:

- no live branch may remain blocked solely by a dead dependency without a canonical continuation decision

Every dead blocker must resolve to one of:

- replacement task
- canonical recovery continuation
- descendant cancellation/obsolescence
- explicit operator override

Do not just delete the failed blocker if live descendants still depend on it.

That breaks branch continuity.

## Step 5: Inspect Plan Snapshots

Saved plans are archival snapshots, not live authority.

For each saved plan, determine whether it is:

- valid and still useful
- stale but harmless
- stale and actively interfering with rendering/operator understanding

Current-valid plan snapshot conditions:

- `planner_task_id` is present
- the planner belongs to the same project
- the planner is either still live as a `project_plan` task or exists in `completed_task_ids`
- the snapshot's `task_ids` still correspond to the intended generated tasks
- the tasks are not reassigned to a different plan

Legacy symptom:

- planner task was pruned from the live task table, but the generated tasks are still the active live plan output

That is not automatically stale.

## Step 6: Clean Stale Plan Snapshots

Delete a saved plan snapshot when it no longer represents current planner-owned work.

Examples:

- `planner_task_id` missing
- planner task truly missing and not present in completed IDs
- planner belongs to another project
- no live plan tasks remain
- tasks were reassigned to another plan

Do not delete a plan snapshot merely because its planner task is no longer in the live task table, if:

- that planner exists in completed-task history
- and its generated tasks are still the correct active live plan output

## Step 7: Validate the Dependency Graph View

After the live state is repaired:

- render the project graph
- confirm live nodes are connected to the repaired head/tail path
- confirm historical nodes are not falsely floating due to clipped ancestry

If the graph still appears floating but integrity is clean, check:

- history-limit ancestor closure
- stale archival plan snapshots
- old archived nodes being rendered as context only

Do not assume a visually floating chain is a live dependency bug.

## Step 8: Archive or Prune Legacy History

Only after live state is correct should you consider reducing historical clutter.

Safe policy:

- keep the current live chain
- keep the genesis ancestry needed to explain the active branch
- archive or prune disconnected historical detours that no longer matter operationally

Do not prune historical records first.

That can remove the evidence needed to infer the correct continuation tail.

## Recommended Repair Sequence Per Project

For a single project:

1. run integrity inspection
2. repair project locks
3. reconcile the project head
4. repair dead blockers and branch continuity
5. inspect saved plan snapshots
6. clean stale plans
7. verify graph rendering
8. optionally prune legacy historical branches

## Common Legacy Failure Patterns

### Pattern A: Stale Head After Planner Completion

Symptoms:

- `head_task_id` points at a planner task
- planner is no longer in the live task table
- generated subtasks are still present

Fix:

- advance head to the generated continuation tail
- do not leave head on the planner node

### Pattern B: Stale Plan Because Planner Was Pruned

Symptoms:

- plan snapshot flagged `missing_planner_task`
- generated tasks are still live
- planner exists in completed-task history only

Fix:

- treat completed-task presence as valid planner continuity
- do not delete the snapshot if it still describes the live generated tasks

### Pattern C: Auto Mode Not Picking Up Ready Tasks

Symptoms:

- tasks appear ready
- scheduler says nothing is runnable

Check:

- stale project locks
- project managed/unmanaged state
- orphaned agents
- dead blockers masked as missing deps

### Pattern D: Floating Chain In Graph But Integrity Is Clean

Symptoms:

- nodes look detached
- no live stale head or continuity issue

Likely causes:

- history limit clipped an ancestor
- stale archival plan snapshot
- graph showing historical context rather than live truth

### Pattern E: Recovery Tree Thrash

Symptoms:

- repeated recovery-on-recovery chains
- many pending recovery descendants
- branch no longer has one clear continuation path

Fix:

- choose one canonical recovery tail
- cancel stale duplicates
- reparent dependents
- if recovery failed terminally, create a normal continuation task

## What To Preserve

Keep:

- current live tasks
- completed task IDs
- valid plan snapshots
- historical genesis ancestry
- bug/recovery continuity that still explains the current branch

Do not preserve as live-operational state:

- stale planner roots
- ghost plan snapshots
- dead recovery duplicates
- corrupted lock metadata
- orphaned task branches with no active continuation role

## Verification Checklist

The migration is complete when all of these are true:

- integrity shows no stale head
- integrity shows no stale plan that should still be live
- project lock state is clean
- auto scheduling can pick ready tasks
- the graph shows a coherent live chain
- no live task depends only on a dead blocker without continuation

## When To Escalate Instead Of Auto-Repairing

Escalate or handle manually when:

- multiple competing live branches could plausibly be the canonical continuation
- the project contains significant manual/operator-only branch surgery
- archived history and live tasks disagree in a way that cannot be inferred safely
- pruning or deletion would destroy the only evidence of branch continuity

In those cases:

- preserve the evidence
- choose the canonical continuation explicitly
- then perform the migration steps from that decision

## Future Expectation

As current controller invariants get stricter, fewer projects should require this guide.

This document exists for:

- older projects written before the hardening work
- projects affected by historical controller bugs
- manual repair sessions where live and archival state drifted apart
