# RFC: Immutable Task History Refactor

**Status:** Under review  
**Authors:** SHRIMP contributors
**Date:** 2026-05-25

---

## Problem

The swarm controller currently treats the SQLite `tasks` table as a mutable working set. When a task completes, it is:

1. Appended to `data/task-history.jsonl` (flat file, pruned at 20,000 entries)
2. **Deleted** from the `tasks` table

This causes a fundamental dependency resolution bug. Other tasks' `dependencies` arrays contain IDs of completed tasks. Once those tasks are deleted from the DB, the dep resolution code in `strategies.py` cannot find them and either:

- **Blocks forever** — dep ID not in `completed_ids` (not in active table), so task never becomes ready
- **Self-heals incorrectly** — "absent from DB = met" rule conflates "completed and pruned" with "failed and pruned" — two very different states

This has caused repeated manual intervention to unblock tasks across multiple projects (gravity-golf, resonance-architect, anti-grav-rush, others).

---

## Principle

> "The future is mutable, the past is not."

Like git — a parent commit SHA stays valid forever. You don't delete parent commits when you make new ones; you follow the chain back through history. Completed tasks should be an immutable historical record, never deleted. The dep chain should be permanently traceable.

---

## Proposed Fix

**Never delete completed or failed tasks from the `tasks` table.**

They stay in the DB permanently with `status = "completed"` or `status = "failed"`. Dep resolution becomes a simple status lookup:

| Dep state | Resolution |
|-----------|-----------|
| `status = "completed"` in DB | **Met** |
| Absent from DB entirely | **Met** (escape hatch — manual deletion, data corruption) |
| `status = "failed"` in DB | **Blocked** (task ran on broken foundations) |
| `status = "pending"` or `"in_progress"` | **Blocked** (not done yet) |

This matches how git, beads, and every serious issue tracker handle historical references.

---

## Implementation Plan

### Phase 1 — Core Change (required)

**`swarm/agent_lifecycle.py` — `prune_history()` (line 628-697)**

Remove the task DELETE:
```python
# REMOVE:
conn.execute("DELETE FROM tasks WHERE status IN ('completed', 'failed', 'cancelled')")
conn.commit()
```

Keep the agent DELETE (agents are heavier, less useful long-term). Keep JSONL archival writes as a write-only export log. Remove the 20k prune limit — JSONL is no longer the source of truth so pruning it is dangerous.

Add `metadata.archived = true` flag after writing to JSONL, so repeated prune cycles don't double-write:
```python
finished_tasks = [
    t for t in db.task_get_all()
    if t.get("status") in ("completed", "failed", "cancelled")
    and not t.get("metadata", {}).get("archived")
]
# ... write to JSONL ...
for task in finished_tasks:
    db.task_update(task["id"], {"metadata": {**task.get("metadata", {}), "archived": True}})
```

**`swarm/strategies.py` — dep resolution (line 43-61)**

No change needed. The existing code:
```python
completed_ids = {t.id for t in task_source.get_all_tasks() if t.status == "completed"}
```
...already works correctly once completed tasks stay in the table. The set will include historical completed tasks and dep resolution "just works."

**`swarm/orchestrator.py` — `_get_next_task()` dep resolution**

Simplify by removing the `task_get_completed_ids()` call — it's redundant once completed tasks are in the main table:
```python
# Before:
completed_ids = db.task_get_completed_ids()
completed_ids |= {t["id"] for t in all_tasks if t["status"] == "completed"}

# After:
completed_ids = {t["id"] for t in all_tasks if t["status"] == "completed"}
```

The dep check at the bottom stays identical:
```python
if all(d in completed_ids or d not in active_ids for d in deps):
```
Now `d not in active_ids` means truly manually deleted, not "completed and pruned." This is the key semantic fix.

### Phase 2 — API Changes (required)

**`swarm/api_tasks.py` — `GET /api/tasks`**

Add status filtering so the dashboard doesn't load the full historical record by default:
```python
@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    include_completed = request.args.get("include_completed", "").lower() in ("true", "1")
    status_filter = request.args.get("status")
    all_tasks = task_source.get_all_tasks()
    if status_filter:
        tasks = [t for t in all_tasks if t.status == status_filter]
    elif include_completed:
        tasks = all_tasks
    else:
        tasks = [t for t in all_tasks if t.status in ("pending", "in_progress")]
    return jsonify({"tasks": [t.to_dict() for t in tasks]})
```

**`swarm/api_chat.py` — `_build_state_snapshot()`**

Filter completed tasks from the chat state snapshot — otherwise the context window fills up as completed tasks accumulate over time. Show counts only for completed tasks, full details for active ones.

### Phase 3 — Deprecate Shadow Infrastructure (cleanup)

**`completed_task_ids` table**

This table exists solely because completed tasks were being deleted. Post-refactor it's redundant. Keep it temporarily as a union fallback for tasks completed before migration:

```python
def task_get_completed_ids() -> set:
    conn = _connect()
    rows = conn.execute("SELECT id FROM tasks WHERE status='completed'").fetchall()
    ids = {r["id"] for r in rows}
    # Legacy fallback for pre-migration completed tasks
    rows2 = conn.execute("SELECT id FROM completed_task_ids").fetchall()
    ids |= {r["id"] for r in rows2}
    return ids
```

Remove the shadow table in a follow-up once the DB has been running for a few weeks.

**`task-history.jsonl`**

Demote to write-only export log. All code that reads from it should first check `db.task_get()` and fall back to JSONL only for pre-migration data. Affected: `api_history.py`, `api_projects.py`, `api_deps.py`, `agent_recovery.py`.

### Phase 4 — Migration

No schema changes required. The `tasks` table already has all needed columns.

**Existing DBs:** have no completed tasks (all pruned). After deploy, new completions stay. Old completed tasks are in `task-history.jsonl` and `completed_task_ids`. The union fallback in Phase 3 handles the transition period transparently.

**Optional backfill:** re-import completed tasks from `task-history.jsonl` into the `tasks` table at startup, gated by `"backfill_task_history": true` in `config.json`. Not required for correctness.

### Phase 5 — Test Impact

**Tests that will break (~3-5):**

Tests that assert a task is absent from DB after `prune_history()`:
```python
# Before:
assert db.task_get("t1") is None

# After:
task = db.task_get("t1")
assert task is not None
assert task["status"] == "completed"
assert task["metadata"].get("archived") is True
```

**Tests that may start passing:**
- `test_ready_tasks_honor_task_history_completed_dependencies` — currently a known failure, tests exactly the bug this fixes.

**Unaffected:** ~1,230 other tests.

---

## Questions for Reviewer

1. **Failed task blocking behavior:** A failed task (exhausted retries, recovery task spawned) stays in the DB as `status = "failed"`. Its dependents were already reparented to the recovery task, so they don't reference the failed task. But edge cases exist — is permanent blocking via `status = "failed"` the right default, or should failed tasks also be treated as "met" for dep purposes?

2. **Separate `task_archive` table vs keeping in `tasks`:** Opus considered a separate archive table. We prefer keeping everything in `tasks` for simplicity — one table, one query. Any objection?

3. **`metadata.archived` flag vs a separate `archived_at` column:** The flag approach requires no schema change. A column would be more queryable. Is the schema change worth it?

4. **`completed_task_ids` shadow table:** Remove immediately (since it's now redundant), or keep as union fallback through a migration period? We lean toward keeping it for 2-4 weeks then removing.

5. **JSONL write without prune:** Disk is cheap, but is there a principled reason to keep the 20k entry cap even as a write-only log?

---

## Files to Review

| File | Change |
|------|--------|
| `swarm/agent_lifecycle.py` | Remove task DELETE from `prune_history()`, add archived flag |
| `swarm/strategies.py` | No change needed |
| `swarm/orchestrator.py` | Simplify `completed_ids` construction |
| `swarm/api_spawn.py` | Same simplification |
| `swarm/api_tasks.py` | Add status filter to `GET /api/tasks` |
| `swarm/api_chat.py` | Filter completed from state snapshot |
| `swarm/agent_recovery.py` | Check `db.task_get()` before JSONL fallback |
| `swarm/db.py` | Update `task_get_completed_ids()` to union both sources |
| `tests/test_prune.py` | Fix 2-3 broken assertions |
| `tests/test_api.py` | Fix any tests assuming task deletion after prune |
| `CLAUDE.md` | Update dep resolution documentation |

---

## What We Are NOT Doing

- No separate `task_archive` table — unnecessary complexity
- No changes to agent archival — agents still move to `agent-history.jsonl`
- No changes to manual task deletion via `DELETE /api/tasks/<id>` — still works, still the escape hatch
- No immediate removal of `completed_task_ids` table — kept as migration fallback
