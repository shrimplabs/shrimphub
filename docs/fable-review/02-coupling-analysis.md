# Coupling Analysis — Hidden Dependencies, Races, and Timing Accidents

*Fable deep review, 2026-07-13. Companion to `01-systems-diagram.md`.*

The system is correct-by-recomputation: almost nothing trusts cached state, everything
re-derives from the tasks table each tick. That design absorbs an enormous amount of
concurrency sloppiness — most races below self-heal within one or two ticks. The point
of this document is to name exactly *which* invariants are doing that absorbing, because
they are the things the event-driven redesign (doc 03) must preserve, and the things a
casual refactor of the monitor thread will silently break.

## A. The monitor thread's implicit contracts

If you touch `_monitor()` or its call order, these are the load-bearing assumptions:

### A1. Ordering inside the tick is semantic, not incidental
`check_ghost_merge_tasks → check_dep_violations → check_agent_status → fill_slots`.

- `check_dep_violations` runs **before** `check_agent_status` so a violating agent is
  killed before its exit could be classified as a legitimate finish. Reversed, a
  dep-violating agent that already exited would go through the full success pipeline —
  merge included — for a task whose deps were never met.
- `check_agent_status` runs **before** `fill_slots` so slots freed by exits this tick
  are refillable this tick. Reversed, spawn latency doubles (cosmetic), but worse:
  `get_active_count()` would count exited-but-unreaped processes, and after enough
  ticks the count model drifts.
- `check_dep_violations` calls `db.backfill_completed_task_ids()` **first** —
  explicitly documented as preventing the race where `_finish_agent` (daemon thread)
  has written `status=completed` but not yet `task_record_completed()`. This is a
  cross-thread ordering fix implemented as a call-order convention.

### A2. The prefetched-IDs staleness window
The tick prefetches `_cycle_completed_ids` / `_cycle_all_task_ids` once and hands them
to `check_dep_violations`. Any task completed by a finish daemon **after** the prefetch
is invisible to this tick's dep checker. Benign today only because the checker kills on
*unmet* deps (a stale set can only make a dep look unmet if the dep was completed
mid-cycle **and** an agent for the dependent was already running — impossible, since
`_get_next_task` wouldn't have spawned the dependent before the dep completed... except
via the manual-spawn API, see B1). The escape hatch that makes this "safe" is one
spawn-path invariant away from being a false-kill generator.

### A3. `get_active_count()` is a three-way sum with a documented race repair
`in_process (poll() is None) + _finishing + persisted DB-active`. The code comment at
agent_lifecycle.py:637 records the scar: filtering persisted agents by PID liveness
created a window where a dying agent counted as nothing → over-spawn. The current fix
counts possibly-dead DB rows as slot-holders until `reconcile_agent_runtime_state`
cleans them next tick. Consequence: **slot capacity is temporarily under-reported
after crashes** — the system deliberately trades throughput for never exceeding
MAX_ACTIVE_AGENTS. Any "optimization" that filters dead PIDs here reintroduces the bug.

### A4. `prune_history()` runs every tick and races the finish pipeline
`_finish_agent` snapshots the task early (`task_snapshot_early`, with the comment
"before status mutations or prune_history() races") and re-fetches with fallbacks
(`db.task_get(task_id) or task_snapshot_pre_complete`) throughout. The finish pipeline
is written defensively *against its own monitor*. Any new phase added to `_finish_agent`
that does `task_get` without a snapshot fallback inherits this race.

### A5. Sleep-first loop + `_last_monitor_tick` is the health signal
The tick timestamp is written *after* sleep, *before* work. `/api/health` computes
monitor lag from it. A tick blocked in `capture_validation_baseline` (see A6) shows as
lag — which is why `_quota_watcher` exists as a separate 10s thread: quota suspension
must fire even when the monitor is wedged in a Godot subprocess. That decoupling is an
admission that monitor-thread blocking is a known, worked-around failure class.

### A6. The monitor still blocks — at spawn, not finish
`fill_slots → spawn_agent → capture_validation_baseline` runs the full validation suite
(GUT inner timeout 300s) synchronously in the monitor thread, plus worktree `git
worktree add` + `.godot` copytree (unbounded by timeout; a large import cache = a long
copy), plus `run_closure_verification` on two paths (expansion-blocked, idle-cycle).
Worst case tick: connectivity check (5s) + baseline (~5 min) + closure verification
(minutes). During that: no dep-violation kills, no reaping, no rate-limit cooldown
updates, no auto-scale. Everything in the tick shares one fate.

## B. Races that exist today

### B1. Manual spawn bypasses the scheduler lock — TOCTOU on task claim
`api_spawn.py:88,123` call `orchestrator.spawn_agent()` **directly**, outside
`_fill_slots_lock`. The guards inside `spawn_agent` (`can_task_accept_agent` +
scan of `db.agent_get_active()`) are check-then-act with no lock spanning check and
`task_update_status(in_progress)`. Two concurrent spawns for the same task (dashboard
click + monitor tick) can both pass the guard. Line 84 even force-resets status to
pending to make spawn accept it. Defense-in-depth catches it later —
`check_dep_violations` / `reconcile_agent_runtime_state` will kill one — but only
after both agents burned LLM calls, and after both created worktrees. **This is the
single most concrete race in the codebase.** Fix is one line of scope: take
`_fill_slots_lock` (or a per-task claim via `UPDATE tasks SET status='in_progress'
WHERE id=? AND status='pending'` checking rowcount) in the API path.

### B2. Concurrent merges into the same project repo
`_finish_worktree_phase` → `_merge_worktree_branch` runs `git merge` in the **main
project directory** from a per-agent daemon finish thread. Two agents on the same
project finishing within the same window (common: QA spawns 3 bug tasks, they
serialize by deps, but sibling branches on *different* files run in parallel by
design) → two concurrent `git merge` processes in one repo. Git's `index.lock` makes
one fail; the failure is indistinguishable from a real conflict → spurious
`bug-merge-*` task, discarded work. There is **no per-project merge lock** anywhere
in worktree.py / agent_finish.py. `lock_project: false` is the default and the
file-ownership chaining in planning is the only thing keeping this rare. The same
window applies to `git rev-parse HEAD` at spawn vs. a merge in flight — `head_at_spawn`
can capture a mid-merge HEAD, corrupting completion-evidence attribution.

### B3. `.godot` copytree from a mutating source
Worktree seeding (`_create_worktree`, and again in validation) copies the main
project's `.godot/` while other processes (validation `--headless --import`, a
launched game under QA) may be writing it. `shutil.copytree` on a mutating tree throws
or produces a torn cache → downstream "Could not find type X" validation noise — the
exact false-positive class the seeding was built to eliminate.

### B4. Read-modify-write on task metadata is not atomic
`_task_write_lock` serializes individual writes, but the pattern
`task_get → mutate meta dict → task_update(metadata=meta)` appears in at least:
baseline capture (spawn), `head_at_spawn` capture (spawn), completion evidence
(finish), playthrough artifacts (finish), research-feeder injection, QA cycle
increments, chat/API PATCH handlers. Two writers interleaving = lost update. Today
the writers are mostly phase-ordered for a given task (spawn → run → finish), so
collisions need an API write landing mid-pipeline — rare, unlogged, and it would
silently drop e.g. `validation_baseline`, converting all pre-existing errors into
"new" ones on that task. A `task_update_metadata(task_id, patch_dict)` that does the
merge inside `_task_write_lock` would close the whole class.

### B5. "Absent dep = met" + creation ordering
`is_dependency_met`: a dep ID missing from the tasks table entirely counts as met.
Consequence: any code path that inserts a dependent row before its dependency row
makes the dependent *immediately schedulable* in the gap. The batch endpoint inserts
in index order (deps first) so it's safe; ad-hoc creators (agents via `create_task`,
recovery flows, wizard) must maintain the same discipline with zero enforcement.
The ghost-dep sweep (every ~100s) makes the escape hatch permanent: an edge to a
never-created ID is deleted. A typo'd dep ID doesn't block forever — it silently
vanishes. Correct for the "manually deleted task" case it was built for; a hazard
for every other case.

### B6. Auto-QA counters are process-local
`_qa_completion_counter` and `_projects_sprint_qa_done` are module dicts. Restart the
server at completion #7 and auto-QA's every-8 cadence resets. Harmless per-instance;
it means QA cadence is a function of server uptime, not project history — an
undocumented coupling between ops behavior (restarts) and QA coverage.

### B7. Auto-scale vs. configured max — two variables, one name
`auto_scale_step` mutates `orchestrator.MAX_ACTIVE_AGENTS`; `agent_lifecycle` has its
own `MAX_ACTIVE_AGENTS` set once at `configure()`. fill_slots reads the orchestrator
one (correct), but any future code in agent_lifecycle that consults its local copy
reads a stale ceiling. Same pattern as the `swarm_runner` module-globals sync noted in
CLAUDE.md — config lives in N module-global copies with hand-maintained sync points.

## C. Timing accidents the system currently relies on

These work because the numbers happen to be what they are:

1. **5s tick ≫ typical write burst.** Ghost sweeps, reconciliation, and backfill all
   assume mid-flight inconsistencies resolve before the next observer looks. E.g. a
   ghost-dep sweep running in the same window as a multi-call task-creation sequence
   (dependent inserted, dep insert still in flight from another request thread) would
   prune a real edge. The 20-tick (~100s) sweep cadence makes the coincidence unlikely,
   not impossible.
2. **`SPAWN_PER_CYCLE=1` caps monitor blocking.** Raise it to 5 and spawn-time baseline
   validation serializes 5 Godot runs inside one tick — up to ~25 min of monitor
   blindness. The knob looks like a throughput dial; it's actually a monitor-latency
   dial.
3. **Finish daemons are unbounded.** Every agent exit spawns a thread that runs Godot
   validation. 16 agents exiting together (quota suspension kill-wave) = 16 concurrent
   headless Godot processes + 16 merges. Nothing pools or queues this; it works because
   exits are usually staggered and the Mac is big.
4. **The 30s quota cache and 5s connectivity check** are the only things keeping
   fill_slots' network calls out of the hot path. Set `_QUOTA_CACHE_TTL=0` for
   debugging and every tick makes an HTTPS round-trip inside the scheduler.
5. **Kill-then-reset races the killed process's own writes.** `_kill_dep_violator`
   does `kill → task→pending → agent→failed`. SIGKILL means no cleanup in the agent,
   but its *already-buffered* file writes (tokens.json, log lines) land afterwards and
   are read by nobody or by the next attempt. Benign now; would corrupt any future
   design where agents write task state directly.
6. **The a11y/StateServer port sweep** (`lsof :11009-11209` every ~50s) assumes agents
   crash faster than ports are reused legitimately. A QA agent that launched a game
   milliseconds ago while the sweep's `pgrep -P` snapshot was taken can — in principle —
   have its game killed as an "orphan." The parent-child check narrows but doesn't
   close this.

## D. What breaks if you touch the monitor thread — summary table

| Change | What silently breaks |
|---|---|
| Reorder dep-check after status-check | Dep-violating exits get merged as successes |
| Parallelize the tick's checks | A2's prefetch staleness becomes false kills; backfill ordering contract broken |
| Filter dead PIDs in `get_active_count` | Over-spawn during agent death windows (regression of a fixed bug) |
| Remove `prune_history` from tick | Agents table grows unbounded; head_task_id stops advancing → task chaining (`chain_to_project_head`) anchors to stale heads |
| Raise `spawn_per_cycle` | Monitor blindness scales linearly with spawn-time validation |
| Move `fill_slots` to a request thread without `_fill_slots_lock` review | Double-claim races multiply (B1 is already this bug in miniature) |
| Skip the sleep-first pattern | Health lag metric reads wrong; tests that swap DBs under a winding-down monitor break (`_is_transient_monitor_db_error` guard exists for exactly this) |

## E. The deeper structural coupling

Everything above is a symptom of one fact: **the tasks table is simultaneously the
queue, the lock manager, the history ledger, and the message bus.** Status flips are
messages (`pending→in_progress` = claim, `completed` = dep-unblock event), but they're
delivered by polling scans, so every consumer re-derives the world and every producer
must be idempotent against being observed mid-write. The compensating machinery —
backfill, ghost sweeps, reconciler, integrity checker, dep-violation killer — is a
hand-rolled eventual-consistency layer. It works, and it's genuinely well-instrumented,
but each new feature pays the tax again (playthrough receipts, closure gates, and
evidence capture each added their own snapshot/fallback/re-check dance).

That is the case for doc 03: not that polling is slow, but that **every one of the
races above becomes a non-race when claims and completions are explicit events
processed by a single scheduler owner.**
