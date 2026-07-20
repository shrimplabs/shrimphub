# Event-Driven Scheduling Core — Design for Roadmap #9

*Fable deep review, 2026-07-13. Assumes the corrections in `01-systems-diagram.md`:
finish-side validation is already off the monitor thread; the remaining blockers are
spawn-time baseline validation, closure verification, and the polling scan itself.*

## Design goals, scoped to this system

1. **No tick ever blocks on a subprocess.** Monitor lag becomes structurally impossible,
   not just unlikely.
2. **One owner for scheduling decisions.** All task claims flow through a single
   scheduler thread — kills races B1/B2 from doc 02 by construction.
3. **Sub-second dep-chain latency.** Today a completed task's dependent waits up to 5s
   (idle: 30s) for the next scan. Chains of 10 tasks pay 50s+ of pure scheduler latency.
   At ~16 agents and multi-hundred-task runs this is real throughput.
4. **Keep the self-healing.** The recompute-from-DB sweeps are the system's immune
   system. They stay — demoted from primary mechanism to safety net.
5. **Single process, single machine, SQLite.** No Redis, no Celery, no message broker.
   `queue.Queue` + a jobs table is the right amount of infrastructure for 10–25 agents.

## Non-goals

- Multi-process federation (roadmap #17) — but nothing here precludes it: the events
  table is the natural federation seam later.
- Replacing the DB as source of truth. Events are *notifications*, never authority.
  Every event handler re-verifies against the DB before acting. This single rule is
  what makes the migration safe: a lost event degrades to today's behavior (the sweep
  catches it), never to wrong behavior.

## The architecture

```
                         ┌──────────────────────────────────────────┐
   Flask handlers ──────►│            EVENT BUS (in-process)         │
   task created/updated  │  queue.Queue, drained by scheduler thread │
                         └──────────────┬───────────────────────────┘
   agent waiter threads ────────────────┤ AGENT_EXITED(agent_id, code)
   (one blocking wait() per agent)      │ TASK_CREATED / TASK_COMPLETED
   quota watcher ───────────────────────┤ TASK_FAILED / DEPS_SATISFIED
   cooldown timers ─────────────────────┤ QUOTA_RESUMED / COOLDOWN_EXPIRED
   sweep timer (30s) ───────────────────┤ SWEEP_TICK
                                        ▼
                         ┌──────────────────────────────────────────┐
                         │        SCHEDULER THREAD (sole owner)      │
                         │  - claims tasks (atomic UPDATE…WHERE)     │
                         │  - enqueues jobs, never runs them         │
                         │  - maintains ready-set incrementally      │
                         └──────┬───────────────────┬───────────────┘
                     SPAWN_PREP │                   │ FINISH / VALIDATE
                                ▼                   ▼
                  ┌──────────────────┐   ┌─────────────────────────┐
                  │  SPAWN POOL (2)   │   │  HEAVY POOL (2–3)        │
                  │  worktree add     │   │  finish pipeline         │
                  │  .godot seed      │   │  validation (GUT etc.)   │
                  │  baseline valid.  │   │  closure verification    │
                  │  Popen + waiter   │   │  merge (per-project lock)│
                  └──────────────────┘   └─────────────────────────┘
                                │                   │
                                └───────► jobs table (SQLite) ◄──────
                                          durable, idempotent, replayed on restart
```

### Events (the complete initial set — resist growing it)

| Event | Producer | Scheduler reaction |
|---|---|---|
| `AGENT_EXITED(agent_id, exit_code)` | waiter thread (`proc.wait()`) | enqueue FINISH job to heavy pool |
| `FINISH_DONE(task_id, success)` | heavy pool worker | emit TASK_COMPLETED/FAILED |
| `TASK_COMPLETED(task_id)` | scheduler (post-finish) or API | recompute ready-set delta for its dependents only; try fill |
| `TASK_CREATED(task_id)` / `TASK_UPDATED` | API handlers, auto-task spawners | insert into ready-set if deps met; try fill |
| `TASK_FAILED(task_id)` | finish pipeline | retry/feeder logic (already exists in agent_recovery — becomes a handler) |
| `QUOTA_SUSPENDED` / `QUOTA_RESUMED` | quota watcher | gate/ungate fill |
| `COOLDOWN_EXPIRED` | `threading.Timer` set when cooldown starts | try fill |
| `SWEEP_TICK` | 30s timer | full recompute — today's monitor body, minus fill |

**What owns the queue:** the scheduler thread, exclusively. It is the only code that
may flip `pending → in_progress`. Flask handlers, finish workers, and meta-agents
*request* via events; they never claim. The claim itself is the atomic form that
SQLite gives us for free:

```sql
UPDATE tasks SET status='in_progress', agent_id=? WHERE id=? AND status='pending'
-- rowcount == 1 → claimed; == 0 → someone else got it / state changed, drop silently
```

This one statement replaces `_fill_slots_lock`, the spawn_agent duplicate guards, and
closes race B1 even for the manual-spawn API (the endpoint becomes: force status if
operator insists, then emit `TASK_CREATED`-equivalent and let the scheduler claim).

### The ready-set: incremental, with a reverse-dep index

Today `_get_next_task` rescans everything. Instead the scheduler keeps:

- `ready: dict[task_id, Task]` — pending, deps met, passes static filters
- `reverse_deps: dict[task_id, set[task_id]]` — who is waiting on me

On `TASK_COMPLETED(t)`: for each `d in reverse_deps[t]`, re-check *only d's* deps
against the DB; if met, add to `ready`. On `TASK_CREATED`: check that one task. The
per-tick full scan survives only inside `SWEEP_TICK`, which rebuilds both structures
from scratch and logs any divergence (`[Sweep] ready-set drift: +2 -0` — this drift
counter is the health metric for the whole migration; it should trend to zero).

Dynamic filters (paused, closure gates, QA concurrency cap, locked, `run_after`) stay
evaluated at claim time, not insertion time — they're cheap and time-varying.
`run_after` gets a `threading.Timer` per future task instead of being re-polled.

### Moving spawn-time validation off the scheduler

`spawn_agent` splits into:

1. **claim** (scheduler, microseconds): the atomic UPDATE above, task marked
   `in_progress` with `metadata.spawn_stage="preparing"`.
2. **SPAWN_PREP job** (spawn pool): worktree add, `.godot` copytree, baseline
   validation, `head_at_spawn`, wrapper script write, `Popen`, start waiter thread,
   `agent_upsert`. On failure: revert claim (`in_progress → pending`), emit event.
3. Concurrency slots are held from claim time — `get_active_count()` becomes
   `len(claimed ∪ running ∪ finishing)`, a scheduler-owned counter checked/adjusted
   only in the scheduler thread. No more three-way reconciliation sum.

Baseline validation thus overlaps with other work, and two spawns for different
projects prepare in parallel — today they serialize inside the monitor tick.

### Fixing the merge race while we're here (cheap, do it first)

Per-project `threading.Lock` in a `defaultdict(Lock)`, taken around
`_merge_worktree_branch` + `git rev-parse HEAD` at spawn-prep. This is Phase 0 —
it's a 20-line fix independent of the event work and closes doc 02's B2 today.

### What stays exactly as it is

- The DB schema, WAL mode, thread-local connections, `_task_write_lock`.
- The entire finish pipeline (`agent_finish.py`) — it just gets *invoked* by a heavy-pool
  worker off an event instead of a daemon thread off a poll. Its internal
  snapshot/fallback defenses stay (they're what makes handlers idempotent).
- All sweeps (ghost deps, orphan Godot, worktree cleanup, log rotation) — they move
  into `SWEEP_TICK` handlers, unchanged.
- The quota watcher thread — it already has the right shape; it gains an event emit.
- `agent_runtime.py` — agents notice nothing.

## Migration path (each phase ships independently, tests green between)

**Phase 0 — locks and claims (1–2 days).** Per-project merge lock; atomic
claim UPDATE inside `spawn_agent` replacing check-then-act; route `api_spawn` through
it. Pure hardening, no behavior change. *Deliverable: doc 02's B1/B2 closed.*

**Phase 1 — waiter threads replace `poll()` (2–3 days).** Each `Popen` gets a
`threading.Thread(target=lambda: (proc.wait(), on_exit(...)))`. `on_exit` initially
just calls the existing daemon-thread finish path immediately — agent-exit latency
drops from ≤5s to ~0 with no queue yet. `check_agent_status` keeps running as the
sweep-side fallback (double-invocation guarded by `_finishing_agents`, which already
exists for exactly this). Timeout watchdog becomes a `threading.Timer` per agent.

**Phase 2 — bounded worker pools + jobs table (3–5 days).** Introduce
`ThreadPoolExecutor` heavy pool (size 2–3: these run headless Godot; more than ~3
concurrent on one Mac contends with running QA games) and spawn pool (2). Finish and
spawn-prep become durable jobs: a `jobs(id, kind, payload, state, attempts,
created_at)` table written before enqueue, marked done after. On startup, `state IN
('queued','running')` jobs are re-enqueued — this replaces today's startup-orphan
handling for half-finished completions, which currently just loses the finish work
until reconciliation notices. *Deliverable: kill-wave of 16 exits → orderly queue,
not 16 concurrent Godot validations.*

**Phase 3 — the scheduler thread + event bus (1–2 weeks).** The structural piece.
Ready-set + reverse-dep index; monitor body split into event handlers + `SWEEP_TICK`.
The 5s poll drops to a 30s sweep. Run with `scheduler_drift` logging for at least one
full experiment run before…

**Phase 4 — retire the poll (after one clean run).** Sweep goes to 60–120s, purely
diagnostic. `disable_monitor` test plumbing is replaced by injecting events directly —
which is the hidden payoff: **tests stop sleeping.** Today's tests that wait for
monitor cycles become synchronous `scheduler.handle(event)` calls.

Roughly 3–4 weeks of part-time work, and Phases 0–2 are worth shipping even if
Phase 3 never lands.

## New problems this creates (and their mitigations)

1. **Lost wakeups.** An event dropped (bug, crash between DB write and enqueue) means
   a task sits ready but unscheduled. *Mitigation:* the sweep re-derives everything at
   30s cadence — worst case degrades to today's idle latency. This is why events must
   stay notifications, never authority.
2. **Event storms / re-entrancy.** A project_plan completing creates 30 tasks → 30
   `TASK_CREATED` events → 30 fill attempts. *Mitigation:* the scheduler coalesces —
   fill is a flag ("fill needed"), checked once per drain of the queue, not once per
   event. Handlers never emit synchronously into their own processing (enqueue only).
3. **Ready-set drift.** The incremental index can diverge from the DB (a handler
   forgets an edge case — e.g. `run_after` edits, dep-list PATCHes via API).
   *Mitigation:* every DB mutation path that touches `status`, `dependencies`, or
   `run_after` must emit; the API layer is the risk surface (deps endpoints,
   integrity repair, chat tools). Enforce by putting the emit *inside*
   `task_mutations.py` / `db.task_update_status` rather than at call sites — there are
   too many call sites to trust. The drift counter in SWEEP_TICK is the regression alarm.
4. **Priority inversion in the heavy pool.** A playthrough-bot finish (long) can queue
   behind three GUT validations. *Mitigation:* single priority queue keyed by
   (task priority, enqueue time); pool size 2–3 keeps head-of-line blocking bounded.
   Do NOT add per-kind pools — that's how you get 6 threads of Godot again.
5. **Shutdown ordering.** Today daemon threads die with the process and the sweeps
   repair on restart. With a jobs table, a SIGTERM mid-finish leaves `state='running'`
   jobs — replay must be idempotent. The finish pipeline already *is* mostly idempotent
   (task_update_status to completed twice is harmless; merge of an already-merged
   branch is a no-op ff; webhook double-fires are the one real duplicate — add a
   `webhook_fired` marker in job payload).
6. **The scheduler thread is now a single point of stall.** If a handler blocks (someone
   adds a subprocess call to one), the whole system freezes harder than today.
   *Mitigation:* hard rule with a lint/test: scheduler handlers may touch SQLite and
   memory only — anything else is a job. Add a watchdog: quota-watcher thread checks
   scheduler heartbeat (it already runs independently for exactly this class of reason).
7. **Test-suite churn.** ~1355 tests, many exercising `check_agent_status`/`fill_slots`
   directly. Phases 0–2 keep those entry points; Phase 3 keeps them as thin wrappers
   that emit-and-drain synchronously. Budget real time for this; it's the biggest cost
   of Phase 3 and the reason the roadmap correctly gated #9 behind #1 (green tests).

## What NOT to build

- **No async/await rewrite.** Flask, subprocess, and the tool ecosystem are thread-shaped;
  asyncio would be a second concurrency model layered on the first.
- **No external queue** (Redis/RQ/Celery). The jobs table + `queue.Queue` covers the
  durability and fan-out needs at this scale with zero new ops surface.
- **No event sourcing.** The tasks table remains the state; events are ephemeral pokes.
  Do not add an events *history* table "while we're at it" — the analytics layer (#7)
  already has `agent_signals` + experiment metrics for history.
- **No per-project scheduler shards.** Single scheduler thread is simpler and 25 agents
  is nowhere near its throughput ceiling (thousands of events/sec).

## Success criteria

- Monitor/scheduler max stall < 100ms sustained (today: minutes, observed).
- Dep-chain hop latency (dep completed → dependent spawned) < 1s (today: ≤5s + spawn
  serialization).
- Zero `bug-merge-*` tasks caused by `index.lock` collisions (measurable from
  merge-task descriptions in the DB).
- Sweep drift counter = 0 across a full experiment run.
- Test suite runtime unchanged or better (Phase 4 should *reduce* it).
