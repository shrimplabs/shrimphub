# Event-Driven Phase 1 — Concrete Execution Plan

*Fable review, 2026-07-13. Makes Phase 1 of `03-event-driven-design.md` executable.
Line references are against the tree at commit `a719d14f`; verify before editing.*

## 0. Scope statement

Phase 1 from doc 03: **waiter threads replace `poll()`**. Each spawned agent gets a
dedicated thread blocked in `proc.wait()`; when the process exits, the finish pipeline
starts immediately instead of waiting up to 5s (idle: 30s) for the next monitor scan.

This document extends that with the *minimal* event bus skeleton (doc 03 defers the
queue to Phase 3, but building the 100-line bus now gives Phase 3 its spine, gives us
a feature flag seam, and costs nothing — the bus in Phase 1 carries exactly two event
types and has exactly two subscribers).

**Hard constraints honored throughout:**
- The monitor thread keeps running, unchanged in structure. Events are additive.
- Events are notifications, never authority. Every handler re-verifies against
  shared state (`_active_handles`, `_finishing_agents`, the DB) before acting.
  A lost event degrades to today's poll behavior, never to wrong behavior.
- Scheduling authority does NOT move. `fill_slots` still runs only in the monitor
  thread. Phase 1 only (a) starts finishes immediately and (b) wakes the monitor
  early. Task claiming is untouched (that's Phase 0/Phase 3 territory).
- Default-off feature flag. With `event_bus_enabled: false` the system is
  byte-for-byte today's behavior. All 1502 existing tests pass untouched at every
  intermediate commit.

**Explicitly out of Phase 1 scope** (do not let an agent "improve" its way into these):
- Per-agent timeout via `threading.Timer` (doc 03 mentions it; deferred — see §7,
  the `freeze_started` interaction makes it non-trivial and the 2h timeout gains
  nothing from sub-second latency)
- `TASK_CREATED` emission from API/task_mutations (too many call sites; Phase 3)
- Worker pools, jobs table, ready-set, reverse-dep index (Phase 2/3)
- Any change to `fill_slots`, `_get_next_task`, or spawn-time claiming (Phase 0/3)
- Atomic claim UPDATE / per-project merge lock (Phase 0 — independent workstream;
  Phase 1 does not depend on it and does not conflict with it)

---

## 1. What Phase 1 actually changes

### Files changed

| File | Change | Risk |
|---|---|---|
| `swarm/events.py` | **NEW** — the event bus (~120 lines) | none (additive) |
| `swarm/agent_lifecycle.py` | Extract `claim_finish()` + `start_finish_thread()` from `check_agent_status()`; route all four kill/finish paths through them; start waiter thread in `spawn_agent()`; add `EVENT_BUS_ENABLED` global + `configure()` param; add `wait_for_all_finishes()` test helper | **hot path** |
| `swarm/api.py` | Wire `event_bus_enabled` config into `agent_lifecycle.configure()`; replace monitor `time.sleep(sleep_secs)` with `Event.wait(sleep_secs)`; register the wake subscriber; `/api/event-bus` GET/POST endpoint | moderate |
| `swarm/constants.py` | `EVENT_BUS_ENABLED_DEFAULT = False` | none |
| `tests/test_event_bus.py` | **NEW** — bus unit tests | none |
| `tests/test_lifecycle.py` | New waiter-thread test class; harden `_wait_for_subprocess` and `_check_agent_status_sync` helpers (flag-on safe) | none |
| `CLAUDE.md` | Config table row for `event_bus_enabled` | none |

### Behavior preserved exactly (flag on or off)

- The monitor loop body: dep-violation check → `check_agent_status()` →
  `fill_slots()` and all sweeps, same order, same cadence.
- `check_agent_status()` remains the sweep-side fallback and still handles:
  timeouts, DB-tracked agents from previous server runs (they have no waiter —
  restart durability is Phase 2's jobs table), and any agent whose waiter thread
  died. Its signature and return type (`List[threading.Thread]`) are unchanged.
- The entire finish pipeline (`agent_finish._finish_agent`) — invoked identically,
  same daemon-thread pattern, same `_finishing_agents` guard.
- `check_dep_violations` semantics: a dep-violator kill still resets the task to
  `pending` and does NOT run `_finish_agent` (see §2.4 for the ordering fix that
  keeps this true under waiters).
- Manual kill (`POST /api/agents/<id>/kill`): today it kills and leaves the handle
  for the monitor to reap through the normal finish path. With the flag on, the
  waiter reaps it immediately — same pipeline, lower latency, no code change needed
  in `api_agents.py`.

### Behavior that changes (flag on)

1. Agent process exits → `_finish_agent` starts within milliseconds (was: ≤5s
   active, ≤30s idle).
2. `_finish_agent` completes → monitor wakes immediately and runs its full cycle,
   including `fill_slots` (was: remainder of the 5s/30s sleep).
3. New log lines: `[EventBus] ...` and per-exit latency stamps.

---

## 2. The in-process event bus

### 2.1 Library choice: stdlib `queue.Queue` + one dispatcher thread. No blinker.

Rationale:
- **No new dependency.** `requirements.txt` stays untouched; agents and tests
  import nothing new.
- **blinker is synchronous** — handlers would run on the *emitter's* thread. The
  emitter here is a waiter thread holding nothing, which would be fine today, but
  Phase 3 needs a single-threaded scheduler draining a queue. Build that shape now:
  the Phase 1 dispatcher thread *is* the embryo of the Phase 3 scheduler thread.
- `threading.Event` alone can't carry payloads or fan out by type.

### 2.2 `swarm/events.py` — full spec

```python
"""In-process event bus. Phase 1 of the event-driven migration (roadmap #9).

Rules (enforced in review, stated here for agents):
- Events are NOTIFICATIONS, never authority. Handlers must re-verify state.
- Handlers must not block: SQLite + memory + starting daemon threads only.
  Anything slower is a job (Phase 2).
- Do not add event types without updating docs/fable-review/13-*.md §2.3.
"""
import queue, threading, time, traceback
from collections import defaultdict
from dataclasses import dataclass, field

# Event type constants — the COMPLETE Phase 1 set. Resist growing it.
AGENT_EXITED   = "agent_exited"    # payload: agent_id, exit_code, task_id, project
AGENT_FINISHED = "agent_finished"  # payload: agent_id, task_id, project, final_status

@dataclass
class Event:
    type: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

class EventBus:
    def __init__(self):
        self._q: queue.Queue[Event] = queue.Queue()
        self._subs: dict[str, list] = defaultdict(list)
        self._lock = threading.Lock()
        self._enabled = False
        self._dispatcher: threading.Thread | None = None
        self.stats = {"published": 0, "dropped_disabled": 0, "handled": 0,
                      "handler_errors": 0, "by_type": defaultdict(int)}

    def set_enabled(self, on: bool) -> None: ...          # flips flag, lazily starts dispatcher
    def enabled(self) -> bool: ...
    def subscribe(self, event_type: str, handler) -> None: ...
    def publish(self, event_type: str, **payload) -> bool:
        # If disabled: increment dropped_disabled, return False. Never blocks.
        # If enabled: build Event, put on queue, update stats, return True.
        ...
    def drain(self, timeout: float = 5.0) -> bool:
        # Test helper: block until queue empty AND in-flight handler returned
        # (implemented with q.task_done()/q.join() plus a deadline). Returns
        # False on timeout instead of raising.
        ...
    def reset_for_tests(self) -> None:
        # Clear subs, stats, queue. Dispatcher thread survives (daemon, idles).
        ...

    def _dispatch_loop(self):
        while True:
            ev = self._q.get()
            try:
                for h in list(self._subs.get(ev.type, ())):
                    try:
                        h(ev)
                        self.stats["handled"] += 1
                    except Exception:
                        self.stats["handler_errors"] += 1
                        traceback.print_exc()   # never let a handler kill the loop
            finally:
                self._q.task_done()

bus = EventBus()   # module singleton, mirrors the db/orchestrator module pattern
```

Implementation notes:
- Dispatcher is a single daemon thread named `event-bus-dispatch`, started lazily
  on first `set_enabled(True)`. If it dies (it can't — the loop is fully wrapped),
  the system degrades to pure polling. Nothing joins it at shutdown.
- `publish()` when disabled returns `False` — waiter threads use this to no-op.
- Handler exceptions are printed and counted, never raised. The `handler_errors`
  counter is the health signal (exposed via `/api/event-bus`).
- **No coalescing, no priorities, no persistence.** Two event types, two
  subscribers, dozens of events per hour. Phase 3 adds coalescing when
  `TASK_CREATED` storms become possible.

### 2.3 Event types — the complete Phase 1 taxonomy (two)

| Event | Emitted by | Payload | Subscribers |
|---|---|---|---|
| `AGENT_EXITED` | waiter thread, after `proc.wait()` returns | `agent_id, exit_code, task_id, project` | lifecycle handler: `claim_finish()` → `start_finish_thread()` |
| `AGENT_FINISHED` | the shared finish-thread wrapper, in its `finally`, after `_finish_agent` returns (regardless of which path started it) | `agent_id, task_id, project, final_status` (read via `db.task_get`) | monitor-wake subscriber in `api.py` |

Deliberately absent: `TASK_CREATED`, `TASK_COMPLETED`, `TASK_FAILED`,
`QUOTA_*`, `SWEEP_TICK`. Those arrive with the scheduler in Phase 3, when
something can actually *do* incremental work with them. `AGENT_FINISHED` is the
Phase 1 stand-in for "state changed, re-evaluate" — coarse, but the monitor's
full-cycle recompute is exactly what runs on wake, so coarseness is free.

### 2.4 The lifecycle refactor: `claim_finish()` — the load-bearing piece

Today `check_agent_status()` (`agent_lifecycle.py:480-571`) has the pop-handle +
add-to-`_finishing_agents` + spawn-finish-thread logic inlined twice (finished
path at 534-565, timeout path at 503-532), and `_kill_dep_violator`
(`agent_lifecycle.py:425-450`) pops the handle *after* killing. With a second
observer (the waiter) watching every process, all finish/kill paths must claim
ownership atomically **before** acting. Extract:

```python
def claim_finish(agent_id: str) -> Optional[Dict]:
    """Atomically claim the right to run teardown for this agent.

    Returns the handle dict if this caller won the claim; None if another
    path (waiter / sweep / dep-violator) already owns it. MUST be called
    before killing a process or starting _finish_agent.
    """
    with _finishing_lock:
        if agent_id in _finishing_agents:
            return None
        with _handle_lock:
            data = _active_handles.pop(agent_id, None)
        if data is None:
            return None            # handle gone: someone else owns teardown
        _finishing_agents.add(agent_id)
        return data

def start_finish_thread(agent_id: str, exit_code: int, data: Dict) -> threading.Thread:
    """Run _finish_agent on a daemon thread; discard from _finishing_agents in
    finally; publish AGENT_FINISHED in finally; register thread in
    _live_finish_threads (a module-level list, pruned of dead threads on each
    append) so tests can join stragglers."""
```

Lock-ordering note: `claim_finish` takes `_finishing_lock` then `_handle_lock`.
Audit the two existing sites that take these locks back-to-back
(`check_agent_status` lines 512-515 and 545-548 take `_finishing_lock` then
`_handle_lock` — same order, safe; keep that order everywhere).

All five paths route through it:

1. **Waiter thread** (new): `proc.wait()` returns → if `bus.publish(AGENT_EXITED, ...)`
   returned False (disabled), do nothing (sweep handles it). The *handler* (not the
   waiter) calls `claim_finish`; if None, another path won — no-op.
2. **Sweep, finished path** (`check_agent_status`): `poll()` non-None →
   `claim_finish()`; None means the waiter beat us — skip silently.
3. **Sweep, timeout path**: `claim_finish()` FIRST, *then* kill the process, then
   `start_finish_thread(aid, -1, data)`. (Today's order — kill, then mark finishing —
   would let the waiter race in between; claiming first closes that.)
4. **Dep-violator kill** (`_kill_dep_violator`): `claim_finish()` FIRST, then kill,
   then its own task-reset/agent-failed DB writes, then discard from
   `_finishing_agents` in a `finally` **without** calling `_finish_agent`. This
   preserves today's semantics (dep violations bypass the finish pipeline) and
   guarantees the waiter's handler finds the claim taken and does nothing.
   The DB-tracked branch (pid-only, no handle) is unchanged — no waiter exists
   for those.
5. **Manual kill API**: unchanged code. It kills without claiming; whichever of
   waiter/sweep observes the exit first claims and runs the normal finish path,
   exactly as the sweep alone does today.

This refactor is valuable even if the event bus is deleted: it removes duplicated
teardown logic and fixes the (currently unreachable, soon reachable) timeout-path
ordering. It ships as its own commit with the flag not yet in existence.

### 2.5 Publisher/subscriber wiring

**Waiter thread** — created at the end of `spawn_agent()` right after the handle is
registered (`agent_lifecycle.py:349-359`):

```python
def _waiter(aid=agent_id, p=proc, tid=task.get("id"), proj=project):
    exit_code = p.wait()                     # blocks; zero CPU
    if not bus.enabled():
        return                               # flag off/flipped off: sweep reaps as today
    bus.publish(AGENT_EXITED, agent_id=aid, exit_code=exit_code,
                task_id=tid, project=proj)

threading.Thread(target=_waiter, daemon=True,
                 name=f"waiter-{agent_id[:8]}").start()
```

Waiters are started **unconditionally** (they cost one blocked thread each, ≤25
at peak) and gate on the flag at *emit* time. This makes the flag safe to flip
live in either direction with agents in flight.

**Lifecycle handler** — registered once in `agent_lifecycle.configure()`:

```python
def _on_agent_exited(ev):
    aid = ev.payload["agent_id"]
    data = claim_finish(aid)
    if data is None:
        return                               # sweep/dep-check won the race
    try:
        kill_godot_children(data["process"].pid)
    except Exception:
        pass
    print(f"[EventBus] agent {aid[:8]} exited -> finish "
          f"(latency {time.time()-ev.ts:.3f}s)")
    start_finish_thread(aid, ev.payload["exit_code"], data)
```

Note the handler starts a thread and returns — it never runs `_finish_agent`
inline (that can block minutes on Godot validation, and the dispatcher thread
must stay hot).

**Monitor-wake subscriber** — registered in `create_app()` next to the monitor
setup (`api.py:457-460`):

```python
_monitor_wake = threading.Event()
events.bus.subscribe(events.AGENT_FINISHED, lambda ev: _monitor_wake.set())
```

and in `_monitor()` (`api.py:603-604`):

```python
# was: time.sleep(sleep_secs)
_monitor_wake.wait(timeout=sleep_secs)
_monitor_wake.clear()
```

Semantics: identical to `time.sleep` when no event fires (same timeout), wakes
early when a finish lands. The monitor body is idempotent and already tolerates
back-to-back cycles (all its checks re-read the DB), so early wake is safe.
`_last_monitor_tick` behavior is unchanged — it updates after the wait as today,
so health/lag reporting needs no adjustment.

### 2.6 Config + wiring

- `config.json` key: **`event_bus_enabled`** (bool, default `false`).
  Documented in the CLAUDE.md config table.
- `swarm/constants.py`: `EVENT_BUS_ENABLED_DEFAULT: bool = False`.
- `agent_lifecycle.configure(..., event_bus_enabled: bool = False)` sets the
  module global and calls `events.bus.set_enabled(flag)` + registers
  `_on_agent_exited` (idempotent: `subscribe` de-dupes by identity, or configure
  guards with a `_bus_wired` flag — pick the configure-guard, it's simpler).
- `api.py create_app()` passes `config.get("event_bus_enabled", False)` into
  `agent_lifecycle.configure(...)` (call site at `api.py:128-141`).
- **`GET/POST /api/event-bus`** (in `api_config.py`, matching the
  `/api/qa-max-cycles` pattern): GET returns `{enabled, stats}`; POST
  `{enabled: bool}` flips `events.bus.set_enabled()`, updates the lifecycle
  global, persists to config.json. This is the live rollback switch.

### 2.7 Coexistence with the monitor — the double-coverage matrix

| Scenario | Flag off | Flag on |
|---|---|---|
| Agent exits normally | sweep reaps ≤5s | waiter reaps ~0s; sweep finds handle gone, no-ops |
| Waiter thread dies (impossible-ish) | — | sweep reaps ≤5s (fallback intact) |
| Agent from previous server run (no handle, no waiter) | reconciliation/sweep | same — unchanged |
| Timeout (2h) | sweep kills+finishes | sweep claims first; waiter's later emit finds claim taken, no-ops |
| Dep-violation kill | dep-check resets task, no finish | dep-check claims first; waiter emit no-ops |
| Manual API kill | sweep reaps ≤5s | waiter reaps ~0s, same finish path |
| Bus queue backs up (can't at this volume) | — | worst case: finish latency reverts toward poll latency; sweep still guards |

Every row's failure mode is "today's behavior", which is the invariant that makes
Phase 1 shippable.

---

## 3. What breaks and how to fix it

### 3.1 With the default (`event_bus_enabled: false`): **nothing**

This is by construction, and it is the acceptance gate for tasks P1-1 through
P1-4: full suite green with zero test edits. The only existing-code changes that
execute with the flag off are the `claim_finish` refactor (behavior-preserving —
same locks, same order of effects) and `Event.wait` replacing `time.sleep`
(equivalent when the event never fires; tests run with `disable_monitor` anyway
via the `PYTEST_CURRENT_TEST` guard at `api.py:253`).

Watch items for the refactor commit (P1-1) — these exercise the extracted paths
and must stay green, run them explicitly before the full suite:
- `tests/test_lifecycle.py` (spawn → exit → `check_agent_status` lifecycle tests,
  ~lines 342-471, 609-637, 1136; timeout tests; `_finishing_agents` guard tests)
- `tests/test_prune.py`, `tests/test_orchestrator.py` (dep-violation kill tests
  around `orchestrator._active_handles` injection at test_orchestrator.py:513-523)
- `tests/test_fill_slots.py`, `tests/test_improvements.py`, `tests/test_api.py`,
  `tests/test_chat_actions.py` (fake-handle injectors — unaffected: handles
  injected directly into `_active_handles` never get waiters, so their reap path
  is the sweep, same as today)

### 3.2 When the flag is ON (P1-6 flips it for a second CI pass): specific breakage

Only tests that call **`spawn_agent()` with a real subprocess** race with waiters.
Tests that inject fake handles are immune (no waiter is ever created for them).

**`tests/test_lifecycle.py`** — the whole real-subprocess section:

1. `_wait_for_subprocess()` (lines 133-142) returns True only if the handle is
   still in `_active_handles` with a non-None `poll()`. With waiters, the handle
   may already be claimed+popped → helper times out → test fails.
   **Fix:** treat "handle absent" as done:
   ```python
   if handle is None: return True          # waiter already claimed it
   if handle["process"].poll() is not None: return True
   ```
2. `_check_agent_status_sync()` (lines 145-154) joins only the threads returned
   by `check_agent_status()`. With waiters, the finish thread was started by the
   bus handler; `check_agent_status()` returns `[]`; assertions on task/agent DB
   state race with the in-flight finish.
   **Fix:** the helper becomes:
   ```python
   def _check_agent_status_sync():
       threads = orc.check_agent_status()
       for t in threads: t.join(timeout=10)
       events.bus.drain(timeout=5)               # AGENT_EXITED handled
       lifecycle.wait_for_all_finishes(timeout=10)  # joins _live_finish_threads
   ```
   `wait_for_all_finishes()` is new production-side test plumbing in
   `agent_lifecycle.py` (joins the `_live_finish_threads` registry, then waits
   until `_finishing_agents` is empty or deadline). With the flag off both extra
   calls are no-ops, so this fix lands **preemptively in P1-4** and the helper is
   correct under both flag states.
3. The `isolated_orc` teardown (lines ~100-102) clears `_active_handles` while
   waiter threads may still be blocked on `wait()` for procs it just killed —
   the waiter then emits for an agent whose handle is gone; `claim_finish`
   returns None; harmless. But add `events.bus.reset_for_tests()` +
   `set_enabled(False)` to the fixture teardown anyway so no event leaks across
   tests.

**`tests/test_script_generation.py`, `tests/test_api.py` spawn-path tests** —
audit for `spawn_agent` calls with real scripts; apply the same two helper fixes
if they assert on post-exit state. (Grep: `spawn_agent(` in tests/ — as of
`a719d14f` the real-subprocess spawns are confined to test_lifecycle.py; the
audit is a checklist item in P1-6, not an expected diff.)

**Monitor-dependent tests** (`test_api.py` health/lag): unaffected —
`_last_monitor_tick` semantics unchanged, monitor disabled under pytest.

### 3.3 New tests required

**`tests/test_event_bus.py`** (P1-2, ~15 tests):
- publish while disabled → returns False, handler never fires, `dropped_disabled`
  increments
- publish while enabled → handler receives Event with payload + ts
- multiple subscribers on one type all fire, in registration order
- handler raising → other handlers still fire, `handler_errors` increments,
  dispatcher survives (publish again, second event handled)
- `drain()` returns True when queue empties, False on timeout with a stuck
  handler (use a `threading.Event`-blocked handler)
- `reset_for_tests()` clears subs/stats/queue
- `set_enabled(False)` mid-stream: already-queued events still drain (documented
  choice: disable gates *emit*, not the queue), new publishes drop
- stats counters accurate across all of the above

**`tests/test_lifecycle.py::TestWaiterThreads`** (P1-3/P1-4, ~10 tests, all with
the bus enabled via fixture):
- spawn real `exit 0` subprocess, **never call `check_agent_status`** → within
  timeout, task is `completed`, agent record finalized (proves the waiter path
  alone finishes agents)
- same with `exit 1` → retry/failure path fires
- waiter + sweep both active: spawn, wait for exit, call
  `check_agent_status()` in a tight loop concurrently → exactly one
  `_finish_agent` execution (assert via monkeypatched counter on
  `agent_finish._finish_agent`) — the claim-race test, run it 20× in-process
- dep-violation kill with flag on → task ends `pending`, `_finish_agent` never
  called (counter == 0), no `handler_errors`
- timeout path with flag on (set `AGENT_TIMEOUT=0.1`, spawn `sleep 5` script) →
  exactly one finish, exit_code -1
- flag flipped off between spawn and exit → waiter no-ops, sweep reaps (proves
  live-toggle safety)
- `AGENT_FINISHED` published exactly once per finish, with correct
  `final_status`
- monitor wake: subscribe a probe, `_monitor_wake.set()` observed after a finish
  (unit-level: call the wake subscriber directly; integration-level wake timing
  is covered by the claim-race test's timeout bounds)

**`tests/test_api.py` or `test_config_endpoints`** (P1-5):
- `GET /api/event-bus` shape; `POST` flips `events.bus.enabled()` and persists
  to the temp config file (use the existing `config_file=tmp_path` pattern —
  mandatory per CLAUDE.md).

---

## 4. Rollback plan

Three nested levels, cheapest first:

1. **Live toggle (seconds, no restart):** `POST /api/event-bus {"enabled": false}`
   or edit `config.json` → `"event_bus_enabled": false` + restart. Waiters gate at
   emit time, so in-flight agents are unaffected; the sweep resumes sole ownership
   on the next 5s tick. The dispatcher thread idles empty. This is the response to
   "run-13 is behaving weirdly, get the variable out NOW".
2. **Config default stays false (the whole soak period):** Phase 1 merges with the
   flag off. Production runs enable it explicitly. If soak fails, nobody else ever
   sees it. The flag default only flips to `true` (P1-8) after one full clean
   experiment run with drift/latency metrics reviewed.
3. **Code revert (if the refactor itself is implicated):** the changes are five
   commits on isolated seams (`events.py` is a new file; `claim_finish` is a
   pure extraction; the monitor diff is 3 lines). `git revert` of P1-3/P1-4
   leaves P1-1's refactor in place (it's independently correct); reverting P1-1
   restores the exact `a719d14f` teardown code. No schema changes, no data
   migrations, nothing persisted except the config key — rollback cannot strand
   state.

**Rollback triggers** (decide now, not during the incident): any of —
- double-finish observed (two `_finish_agent` executions for one agent id in logs)
- `handler_errors > 0` sustained across a run
- monitor lag or test-suite flakiness measurably worse than baseline
- any unexplained task stuck `in_progress` with a dead pid (waiter lost + sweep
  somehow blind — should be impossible, treat as P0 if seen)

---

## 5. Observable improvement

### Primary metric: **agent-exit → finish-start latency**

Today: uniform 0–5s (mean ~2.5s) while active, 0–30s idle. After: <50ms.
Measured directly — the `_on_agent_exited` handler logs
`latency {time.time()-ev.ts:.3f}s`, and the sweep path logs its equivalent
(`poll()`-detected exits stamp `data["exit_detected_at"] - proc exit` is not
knowable, so for the baseline use the existing agent DB timestamps, below).

### Secondary metric: **dep-chain hop latency** (dep completed → dependent's agent started)

Both timestamps already exist with microsecond precision
(`datetime.now().isoformat()` in `task_update_status` calls). Baseline and
post-Phase-1 are computed from the DB with the same query — run it against run-12
(baseline) and the first flag-on run:

```sql
-- hop latency: for each started task with deps, time from its last dep's
-- completion to its own start. Requires tasks.started / tasks.completed columns
-- (already populated by task_update_status).
SELECT t.id,
       (julianday(t.started) - MAX(julianday(d.completed))) * 86400.0 AS hop_secs
FROM tasks t
JOIN json_each(t.dependencies) je
JOIN tasks d ON d.id = je.value
WHERE t.started IS NOT NULL AND d.completed IS NOT NULL
GROUP BY t.id
HAVING hop_secs >= 0;
```

Report p50/p90. Expected: baseline p50 ≈ 2.5–7s (poll + fill serialization);
after ≈ dominated by finish-pipeline duration only. **Success bar: p50 hop
latency drops by ≥2s and no metric in §4's rollback-trigger list regresses.**
Phase 1 does *not* target the <1s doc-03 goal — that needs Phase 3's claim
rework; Phase 1 should remove the poll-wait component and prove the seam.

### Tertiary: bus health counters

`GET /api/event-bus` → `{published, handled, handler_errors, dropped_disabled,
by_type}`. Across a clean run: `handler_errors == 0`, and
`published[agent_exited] == handled[agent_exited]`. Also count sweep-claimed
finishes while the flag is on (log-greppable: finish start lines minus
`[EventBus]` finish lines) — this is the Phase 1 analogue of doc 03's drift
counter and should be ~0 (only restart-orphans and timeout kills).

Write the before/after comparison into
`docs/experiment-designs/` alongside the run analyses so it feeds the roadmap #7
analytics judgment.

---

## 6. Phase 1 task breakdown (swarm tasks)

Create via `POST /api/tasks/batch` on project `swarm-controller` with
`depends_on` indices exactly as numbered. All tasks: `max_attempts: 3` (default).
Every task's acceptance criteria include: **full suite green
(`.venv/bin/pytest`), no edits to unrelated tests, no new deps in
requirements.txt.** Each is sized for one agent session (≤200 loops).

| # | type | prio | depends_on | risk | description (abridged — full text below) |
|---|---|---|---|---|---|
| P1-1 | refactor | 100 | — | **HOT PATH** | Extract `claim_finish()` + `start_finish_thread()` in `agent_lifecycle.py`; route finished/timeout/dep-violator paths through them; claim-before-kill ordering |
| P1-2 | feature | 50 | — | safe | Create `swarm/events.py` bus + `tests/test_event_bus.py` |
| P1-3 | feature | 50 | [0, 1] | **HOT PATH** | Waiter threads in `spawn_agent` + `AGENT_EXITED` handler + `event_bus_enabled` flag plumbing (default off) + `TestWaiterThreads` |
| P1-4 | feature | 50 | [2] | moderate | `AGENT_FINISHED` emit in finish wrapper; monitor `Event.wait` wake; `wait_for_all_finishes()`; harden the two test_lifecycle helpers |
| P1-5 | feature | 50 | [3] | safe | `GET/POST /api/event-bus` endpoint + stats exposure + CLAUDE.md config-table row |
| P1-6 | qa | 75 | [3] | moderate | Flag-on suite pass: run full pytest with `event_bus_enabled` forced true (env `SWARM_EVENT_BUS=1` honored by `configure`); fix any racy test using the §3.2 patterns; audit tests/ for other real-subprocess `spawn_agent` callers |
| P1-7 | research | 50 | [4, 5] | safe | Baseline metrics: run the §5 SQL against run-12 DB data, record p50/p90 hop latency + methodology into `docs/fable-review/13a-phase1-baseline.md` (research type = read-only; the doc is the deliverable via scratchpad→report, or downgrade to feature if write access needed) |
| P1-8 | feature | 50 | [5, 6] | **DO NOT AUTO-RUN** | After one clean flag-on production run reviewed by operator: flip `EVENT_BUS_ENABLED_DEFAULT` to true, update docs. Gate: create with `run_after` far-future or leave for `/swarm-task` manual pickup |

Batch call sketch:

```json
{
  "project": "swarm-controller",
  "tasks": [
    {"type": "refactor", "priority": 100, "description": "P1-1 ..."},
    {"type": "feature",  "priority": 50,  "description": "P1-2 ..."},
    {"type": "feature",  "priority": 50,  "description": "P1-3 ...", "depends_on": [0, 1]},
    {"type": "feature",  "priority": 50,  "description": "P1-4 ...", "depends_on": [2]},
    {"type": "feature",  "priority": 50,  "description": "P1-5 ...", "depends_on": [3]},
    {"type": "qa",       "priority": 75,  "description": "P1-6 ...", "depends_on": [3]},
    {"type": "research", "priority": 50,  "description": "P1-7 ...", "depends_on": [4, 5]},
    {"type": "feature",  "priority": 50,  "description": "P1-8 ...", "depends_on": [5, 6]}
  ]
}
```

(`chain_to_head` will additionally anchor P1-1/P1-2 to the project HEAD — expected
and fine.)

### Full task descriptions (paste into the batch call)

**P1-1 (refactor, HOT PATH).** In `swarm/agent_lifecycle.py`, extract two module
functions from `check_agent_status()`: (a) `claim_finish(agent_id) -> Optional[Dict]`
— atomically (take `_finishing_lock`, then `_handle_lock`, in that order) return
None if agent_id is in `_finishing_agents` or absent from `_active_handles`;
otherwise pop the handle, add to `_finishing_agents`, return the handle dict.
(b) `start_finish_thread(agent_id, exit_code, data) -> threading.Thread` — the
existing `_run_finish` daemon-thread body, plus: append the thread to a new
module list `_live_finish_threads` (prune dead entries on append). Rewrite the
finished-agent loop and the timed-out loop in `check_agent_status` to use them;
in the timeout path, claim BEFORE killing the process. Rewrite
`_kill_dep_violator` (in-memory-handle branch only) to call `claim_finish` first,
then kill, then its existing DB writes, then `finally: _finishing_agents.discard`
— it must NOT call `_finish_agent`. Add
`wait_for_all_finishes(timeout: float = 10) -> bool` joining
`_live_finish_threads` then polling `_finishing_agents` empty. Pure refactor: no
new behavior, no new config. Acceptance: full pytest green with zero test-file
edits; `check_agent_status` return type unchanged.

**P1-2 (feature, safe).** Create `swarm/events.py` per the spec in
`docs/fable-review/13-event-driven-phase1.md` §2.2 (module docstring rules,
Event dataclass, EventBus with set_enabled/subscribe/publish/drain/
reset_for_tests/stats, module singleton `bus`, dispatcher daemon thread named
`event-bus-dispatch`, lazy start, handler exception isolation). Exactly two
event-type constants: `AGENT_EXITED`, `AGENT_FINISHED`. Create
`tests/test_event_bus.py` covering the §3.3 bus list. No imports of swarm.db or
Flask — the module must be dependency-free (stdlib only). Do not wire it into
anything else yet.

**P1-3 (feature, HOT PATH).** Wire the bus into the agent lifecycle behind a
default-off flag. (1) `swarm/constants.py`: `EVENT_BUS_ENABLED_DEFAULT = False`.
(2) `agent_lifecycle.configure()`: new kwarg `event_bus_enabled=False`; set
module global `EVENT_BUS_ENABLED`; call `events.bus.set_enabled(flag)`; register
`_on_agent_exited` once (guard with module `_bus_wired` flag). Env override: if
`os.environ.get("SWARM_EVENT_BUS") == "1"`, force enabled (for CI flag-on pass).
(3) `_on_agent_exited(ev)`: `claim_finish`; if None return; kill_godot_children
(best-effort); log `[EventBus] agent {id[:8]} exited -> finish (latency {..}s)`;
`start_finish_thread`. Handler must never block or raise. (4) `spawn_agent()`:
after the handle registration block (~line 359), start a daemon waiter thread
(`name=f"waiter-{agent_id[:8]}"`) that does `proc.wait()` then, only if
`bus.enabled()`, publishes AGENT_EXITED with agent_id/exit_code/task_id/project.
Waiters always start; flag gates emit. (5) `swarm/api.py` `create_app()`: pass
`event_bus_enabled=config.get("event_bus_enabled", False)` into
`agent_lifecycle.configure`. (6) Add `TestWaiterThreads` to
`tests/test_lifecycle.py` per §3.3 (flag-on fixture with
`events.bus.reset_for_tests()` + `set_enabled(False)` teardown; include the
20-iteration claim-race test and the dep-violation no-finish test). Acceptance:
full suite green with flag off; new class green.

**P1-4 (feature, moderate).** (1) In `start_finish_thread`'s wrapper `finally`,
publish `AGENT_FINISHED` (agent_id, task_id, project, final_status from
`db.task_get(task_id)["status"]` if task_id else None) — single emit site, fires
for both waiter- and sweep-initiated finishes. (2) `swarm/api.py` monitor: create
module-scoped `_monitor_wake = threading.Event()` near `_last_monitor_tick`
(~line 459); subscribe `AGENT_FINISHED -> _monitor_wake.set()`; replace
`time.sleep(sleep_secs)` at line 604 with `_monitor_wake.wait(timeout=sleep_secs);
_monitor_wake.clear()`. Nothing else in the loop changes. (3) Harden
`tests/test_lifecycle.py` helpers per §3.2: `_wait_for_subprocess` returns True
when handle is absent; `_check_agent_status_sync` additionally calls
`events.bus.drain(5)` and `lifecycle.wait_for_all_finishes(10)`. (4) Test:
AGENT_FINISHED emitted exactly once per finish with correct final_status; wake
event set by subscriber. Acceptance: full suite green flag-off AND the
TestWaiterThreads class green.

**P1-5 (feature, safe).** Add `GET/POST /api/event-bus` in `swarm/api_config.py`
following the `/api/qa-max-cycles` pattern: GET → `{enabled, stats}` (stats from
`events.bus.stats`, json-safe copy); POST `{enabled: bool}` → flips
`events.bus.set_enabled`, sets `agent_lifecycle.EVENT_BUS_ENABLED`, persists
`event_bus_enabled` to config.json. Tests use `config_file=tmp_path/"config.json"`
per CLAUDE.md (mandatory). Add the config-table row for `event_bus_enabled` to
CLAUDE.md and the endpoint to the API list. Acceptance: full suite green.

**P1-6 (qa, moderate).** Verification task, no production code changes expected.
Run the full suite twice: (a) normally, (b) with `SWARM_EVENT_BUS=1` exported.
For (b), triage every failure: it must be a test-plumbing race of the §3.2 kind
(assertions racing an event-initiated finish), never a product bug — if a
double-finish or lost-finish appears, STOP and file a bug task instead of
patching the test. Fix racy tests using only the sanctioned patterns:
handle-absent-is-done, `bus.drain()`, `wait_for_all_finishes()`. Also grep
tests/ for any other real-subprocess `spawn_agent` callers beyond
test_lifecycle.py and apply the same patterns. Deliverable: both suite runs
green; list of tests touched in the completion notes.

**P1-7 (research, safe).** Read-only measurement baseline. Using the §5 SQL
(adapt to actual schema — verify `tasks.started`/`tasks.completed` column names
in `swarm/db.py` first), compute p50/p90 dep-chain hop latency for the most
recent completed experiment run and for the last 7 days of swarm-controller's
own tasks. Record: the query used, the numbers, the run identifiers, and the
success bar (p50 −2s post-enablement). Report findings in the task completion
notes for the operator to paste into `docs/fable-review/13a-phase1-baseline.md`.

**P1-8 (feature, gated — manual pickup only).** Preconditions (verify, do not
assume): P1-6 complete; at least one full production run completed with
`event_bus_enabled: true`; `GET /api/event-bus` shows `handler_errors == 0` and
published==handled for agent_exited; operator sign-off recorded in the task
metadata. Then: flip `EVENT_BUS_ENABLED_DEFAULT = True` in constants.py, update
CLAUDE.md default, add a line to `docs/fable-review/13-event-driven-phase1.md`
marking Phase 1 complete with the measured before/after numbers. If any
precondition fails, mark the task failed with a note — do not flip the default.

### Risk annotation summary

- **HOT PATH (P1-1, P1-3):** touch the teardown seam every agent crosses. Both
  are guarded by the strongest invariant available — flag-off equals current
  behavior — but review the lock ordering and the dep-violator path by hand.
  These two are the only tasks where an agent mistake can lose or double-run a
  finish. Suggest `thinking_task_types` include their types for these runs, and
  review the diffs personally before merge.
- **Moderate (P1-4, P1-6):** monitor loop 3-line diff (easy to eyeball); test
  triage requires judgment (the STOP rule in P1-6 is the safety valve).
- **Safe (P1-2, P1-5, P1-7):** additive file, config endpoint, read-only analysis.

---

## 7. Deferred with reasons (do not resurrect inside Phase 1)

- **Per-agent timeout `threading.Timer`:** the sweep's timeout check reads
  `data.get("freeze_started")` live (`agent_lifecycle.py:498`) — an infra-freeze
  can start *after* a Timer is armed, so a naive Timer kills frozen agents the
  sweep would have spared. A correct Timer must re-check freeze state and
  re-arm, which is more machinery than the 2h safety net justifies while the
  sweep still runs every 5s. Revisit in Phase 3 when the sweep slows to 30s.
- **Emitting from `task_mutations.py` / `db.task_update_status`:** right
  location (doc 03 problem #3), wrong phase — nothing consumes task-level events
  until the ready-set exists.
- **Bus-driven `fill_slots`:** calling fill from a handler would create a second
  scheduling entry point — exactly the multi-owner disease Phase 3 exists to
  cure. The wake-event indirection keeps the monitor the sole scheduler.
