# Punch List — Code Review 2026-07-03

Findings from a read-only review of the swarm-controller codebase. No changes made.
Ordered by severity within each section. File references are to current line numbers.

---

## P1 — Bugs / correctness

### 1. Expansion-block deadlock: comment says "allow through", code returns None
`swarm/orchestrator.py:906-913` — `_get_next_task()` comment says "If ALL ready tasks
are blocked, allow the top task through to avoid deadlock (the closure policy 'no
deadlock' rule)", but the code does `return None` in exactly that case. Meanwhile
`_sort_by_strategy()` (lines 1045-1052) deliberately leaves all-blocked tasks in the
list "to avoid deadlock" — and `_get_next_task` then throws that away. Net effect: a
frozen/stalled project whose entire ready set is expansion tasks makes zero progress,
which is the deadlock both comments claim to prevent. Pick one behavior and make both
sites agree.

### 2. config.json `escalation_policy` overrides are never applied
`swarm/agent_recovery.py:1420` — `_handle_task_failure` calls
`get_escalation_policy(task_type)` without the `config` argument, so the per-type
overrides documented in CLAUDE.md (`"escalation_policy": {...}` in config.json) are
silently ignored on the main failure path. Latent today (no override currently
configured) but the documented feature does not work.

### 3. Ghost-dep sweep still reads the 102MB task-history.jsonl — and discards it
`swarm/api.py:448-468` (`_sweep_ghost_deps`) — the code builds `history_ids` by
reading and JSON-parsing the full `data/task-history.jsonl` (currently **102MB**),
then the very next block says "Skip the 93MB JSONL read" and computes
`known_ids = active_ids | completed_ids` **without using `history_ids`**. The
expensive read still happens: once at startup and every 20 monitor cycles (~100s),
inside the monitor thread. Delete lines 448-463; the optimization was half-applied.

### 4. Cycle checker rejects unrelated writes if ANY cycle exists in the table
`swarm/db.py:87-120` — `_would_introduce_cycle` DFS-es from **every** node in the
graph, so it returns True if a pre-existing cycle exists anywhere among the 8,890
task rows, even when the write being validated doesn't touch it. One historical
cycle would brick all dependency writes with "Dependency cycle detected". It should
only search for cycles reachable from `task_id`. (Also: full-table load + DFS on
every task write, under `_task_write_lock` — see P2 perf theme.)

### 5. Blocking quota HTTP call inside the scheduling hot path
`swarm/orchestrator.py:211-257, 396, 487, 821` — `check_quota_limit()` is a network
request (10s timeout, `verify=False`) called: twice per `fill_slots()` cycle, plus
**once per `_get_next_task()` call** (up to SPAWN_PER_CYCLE+1 times per cycle), all
while holding `_fill_slots_lock`. A slow MiniMax endpoint stalls the monitor for tens
of seconds per tick. The `_quota_watcher` thread already polls every 10s — cache its
result (module-level value + timestamp) and have fill_slots/_get_next_task read the
cache instead of re-fetching.

### 6. Two competing auto-replan paths can bypass sprint QA
`swarm/api.py:693-727` (monitor auto-replan loop) vs `swarm/orchestrator.py:391-457`
(fill_slots sprint cycle). Both fire for `auto_replan_projects` on empty queue. In the
normal path fill_slots runs first and creates the sprint QA task, so the monitor loop
sees a non-empty queue and skips. But when fill_slots is skipped (rate-limit cooldown,
LLM connectivity check failure, over quota), the monitor loop still runs and creates a
`project_plan` directly — skipping the sprint QA gate entirely. The monitor loop looks
like a legacy leftover; consider removing it in favor of the fill_slots sprint cycle.

### 7. `import_tasks` bypasses all dependency validation and head-chaining
`swarm/api_tasks.py:899-941` — `/api/tasks/import` creates tasks without
`_validate_dependency_ids` (placeholder/ghost dep guard) and without
`chain_to_head`, contradicting "off-chain task creation is not allowed" enforced in
the other creation endpoints. Ghost/placeholder deps can enter through this door.

### 8. Batch creation can chain roots to the wrong project's head
`swarm/api_tasks.py:437-453` — in `/api/tasks/batch`, root tasks are chained to
`default_project`'s head even when the item has a per-item `project` override,
creating a cross-project dependency edge.

### 9. `_wire_runtime` swallows swarm_runner import failures silently
`swarm/api.py:133-151` — `except Exception: return None, None`. If `swarm_runner`
fails to import (syntax error, bad config), the server boots fine but auto-mode
silently never spawns (`generate_task_script is None`), and `_quota_watcher`
references `_runner_mod.LLM_PROVIDER` → AttributeError printed every 10s. Log the
traceback at minimum; arguably fail loudly.

### 10. Loop-limit vs context-limit continuation tasks are wired differently
`swarm/agent_runtime.py:1396-1404` (context limit: `"dependencies": [TASK_ID]`) vs
`:1640-1647` (loop limit: no dependencies → auto-chained to project head by the API).
Both then rely on `_phase_reparent_continuation`. The loop-limit continuation ends up
ordered after the *project head* rather than after the task it continues, which is
only correct by coincidence when the finished task is the head. Make both pass
`dependencies: [TASK_ID]`.

### 11. Auto-scale starts at ceiling, not floor
`swarm/api.py:81-84` — `_auto_scale_current` is initialized to the ceiling
(`max_active_agents`, currently 16) while `orchestrator.py:85` documents "starts at
floor, ramps up". After every restart the system jumps straight to max concurrency
and only backs off after 429s arrive. If starting hot is intentional, fix the comment;
otherwise start at the floor.

### 12. `insert_task_after` reparents dependents before the new task exists
`swarm/api_tasks.py:766-820` — dependents are rewired to `new_id` *before*
`task_source.add_task(new_task)` runs. In that window the deps point at a
nonexistent ID; a concurrently-running ghost-dep sweep or dep-violation check could
prune the edge or kill an agent. Low probability, but the create-then-rewire order
is free to fix.

---

## P2 — Performance (recurring theme: unscoped full-table scans)

The tasks table has ~8,900 rows and grows monotonically (completed tasks are
immutable history by design). Recent work scoped the monitor's main scans to managed
projects, but many per-event code paths still call unscoped `db.task_get_all()`:

- `agent_lifecycle.prune_history()` — **runs every monitor cycle (5s)** via
  `check_agent_status()`, and does two unscoped `task_get_all()` passes (archival
  scan + head-reconciliation scan) plus per-task metadata writes.
- `agent_finish._phase_reparent_continuation` / `_phase_complete_task` (stale
  recovery scan) — full scan per agent finish. Could be
  `WHERE dependencies LIKE '%"<id>"%'` (the pattern `task_delete` already uses).
- `agent_recovery._live_dependents`, `_collect_branch_failure_history`,
  `_spawn_review_task`, `_spawn_research_feeder` dedupe scan.
- `orchestrator._fire_idle_gardener/_librarian/_auditor/_scheduler/_archaeologist`
  — each does unscoped `task_get_all()` when enabled.
- `api_tasks.get_task_dependents`, `insert_task_after` — full scan per request.
- `db._would_introduce_cycle` — full-table load + whole-graph DFS **on every task
  write**, serialized under `_task_write_lock`.
- `db.task_get_completed_ids(projects=...)` — the project scoping is undermined by
  the unconditional legacy union `SELECT id FROM completed_task_ids` (line 818),
  which returns all-projects IDs anyway.

Other perf items:
- **`[Loop N/200]` prefix on the system prompt breaks prompt caching**
  (`agent_runtime.py:1020`). Prefix-based caches (Anthropic-style; MiniMax caches
  automatically at 512+ tokens) are invalidated when the first tokens change every
  loop. Moving the loop budget into the (already-appended) last user message would
  keep the system prompt byte-stable and could massively raise cache hit rates —
  directly relevant to the RPM pressure all the jitter/backoff machinery fights.
- `data/` is **12GB**; `agent-history.jsonl` 54MB, `task-history.jsonl` 102MB.
  No rotation/retention policy anywhere. Old agent logs likely dominate — add a
  retention sweep (e.g. delete `agent_*.log` older than N days, archive JSONLs).
- `update_project_registry` reads every matching file in every managed project to
  count lines, synchronously.

---

## P3 — Security / operational hardening

- **`verify=False`** on the MiniMax quota call (`orchestrator.py:226`). Fix the macOS
  cert chain (`pip install certifi` + `SSL_CERT_FILE`, or `python3 -m pip install
  --upgrade certifi`) rather than disabling TLS verification; also silences the
  urllib3 InsecureRequestWarning spam.
- API binds `0.0.0.0:5001` with `login_required` unset (auth off) and
  `Access-Control-Allow-Origin: *` on every response. The API can run arbitrary
  shell via agent task descriptions, and `allow_self_modification` is currently
  **true** in config.json. Anyone on the LAN owns the machine. Documented as a known
  default, but with self-modification on, consider binding to 127.0.0.1 or enabling
  auth.
- `agent_update_status` / `task_update` / `project_update` interpolate dict keys
  into SQL (`f"{k}=:{k}"`). All current callers pass fixed keys, but it's one
  careless route away from SQL injection. Cheap fix: whitelist column names in db.py.
- `.env` parsing in `agent_runtime.py:494-499` doesn't strip quotes/comments and
  overwrites existing env vars unconditionally.

---

## P4 — Dead code / cleanup / consistency

- `_task_history_lookup` legacy fallback in `agent_recovery.py` is past its own
  removal date ("Remove this fallback after 2025-07-01" — it is now 2026-07).
- `_fire_task_webhook` is defined twice, verbatim (orchestrator.py:158 and
  agent_lifecycle.py) — extract to one module.
- `orchestrator.py:363-374` calls `agent_lifecycle.configure(...)` at **import
  time** with the module-default values (WORKSPACE=".", etc.). `create_app` re-calls
  it with real config so the API path is fine, but any entry point that imports
  orchestrator without calling `_wire_runtime` runs against `Path(".")`. Remove the
  import-time call or make it lazy.
- Legacy `_spawn_review_task` path (pre-feeder recovery) still present; CLAUDE.md
  says it can go once no active legacy recovery rows remain — DB shows 0 active
  agents and 35 failed tasks; worth checking whether that day has arrived.
- `_read_agent_token_usage` returns a fixed 7-tuple, yet `_finish_agent` has
  defensive unpacking for a "legacy 3-tuple shape used by older test mocks" — fix
  the mocks and delete the shim.
- `agent_finish.py:836` uses `__import__("datetime")` inline; `datetime` is already
  imported at module top. Also `datetime.utcnow()` (line 679) is deprecated in 3.12.
- The auto-commit fallback (`agent_runtime.py:1571-1587`) labels every commit
  "Refactor: update ..." regardless of task type — misleading git history in game
  repos.
- Repo root clutter: `swarm_controller.db` (stray DB in root — canonical is
  `data/swarm.db`), three `config.json.bak-*` files, `void-patrol-variant-f-run4.gif`,
  `spawn-test-proj/`. All untracked/ignored, but worth deleting or relocating.
- Twelve near-identical Flask routes for dashboard JS files (`api.py:319-365`) —
  one `send_from_directory` route with a filename allowlist would do.
- POST `/api/tasks` and `/api/tasks/batch` don't accept `run_after` even though the
  task model and PATCH support it.
- PATCH `/api/tasks/<id>` sets `completed` for `completed|failed` but not
  `cancelled` (batch-status does handle cancelled) — minor inconsistency.

---

## P5 — Documentation drift (CLAUDE.md / memory vs reality)

- CLAUDE.md's architecture table is stale: line counts are far off (api_chat.py is
  now 2,399 lines not 1,292; agent_runtime 1,716 not 1,210; api.py 1,325 not
  "monitor thread" only), and these modules aren't documented at all:
  `api_snapshots.py` (895 lines), `pipeline.py`, `project_graph_policy.py`,
  `api_scheduler.py`, `api_gardener.py`, `api_meta.py`, `api_meta_auditor.py`,
  `api_cartographer.py`, `api_librarian.py`, `api_archaeologist.py`,
  `agent_loop_helpers.py`, `model_routing.py`, `branch_intent.py`,
  `experiment_metadata.py`, `closure/`, `tools/` split, `plugins.py`, `platform.py`,
  `godot_bootstrap.py`.
- The whole meta-agent system (Gardener, Librarian, Auditor, Cartographer,
  Archaeologist, Scheduler, `meta_mode_enabled`) is undocumented in CLAUDE.md.
- Closure/verification system (closure_status, regressions, verification_runs,
  `phase_gate` task type, expansion blocking) is undocumented — and it materially
  changes scheduling behavior.
- Undocumented config keys in active use: `auto_scale`, `spawn_per_cycle`,
  `use_worktrees`, `allow_self_modification`, `local_fallback_on_quota`,
  `human_review_flag_enabled`, `gardener_*`, `librarian_*`, `scheduler_*`,
  `archaeologist_*`, `cartographer_*`, `meta_auditor_*`, `project_pipelines`.
- CLAUDE.md still says "recovery tasks spawned when retries exhausted; dependents
  reparented automatically" in the header bullet list, which the escalation section
  below it explicitly says is no longer how it works.

---

## Test suite (run during review, 2026-07-03)

The full suite was killed after 39 minutes with **20 failed / 135 passed** —
it never got past `tests/test_agent_runtime.py`. None of the 20 failures are on
CLAUDE.md's known-failures list. Root causes, verified by isolated repro:

### T1. Stream parser regression breaks all list-based `iter_lines` mocks
`swarm/llm_utils.py:499-505` — the mid-stream-error rework iterates via
`next(line_iter)` where `line_iter = resp.iter_lines(chunk_size=8192)`. Real
`requests` returns a generator, so production is fine — but every test mock sets
`iter_lines.return_value = [list of lines]`, and `next(<list>)` raises
`TypeError: 'list' object is not an iterator`. The parser catches it as
"stream interrupted" and enters the 7-attempt retry loop. Verified:
`TestCallLlm::test_429_is_retried_and_succeeds` fails with exactly this error.
One-line fix: `line_iter = iter(resp.iter_lines(chunk_size=8192))` (also makes
the parser robust to any iterable); alternatively update the mocks.

### T2. Failing LLM tests sleep for real — suite appears hung
The retry loop's backoff (`llm_utils.py:639`, `[10,30,60,120,240]`, 7 attempts)
runs **unmocked `time.sleep`** in several TestCallLlm tests → up to ~15 minutes
of real wall-clock sleep per failing test (measured: 2 tests = 15:42). This is
why the suite looked hung at 39 minutes with only 155 tests executed. Fixes:
add `pytest-timeout` to requirements-dev + a global timeout in pytest.ini; or an
autouse fixture patching `swarm.llm_utils.time.sleep`; or make the backoff
schedule injectable so tests can zero it.

### T3. Cross-test state pollution in test_agent_runtime.py
`TestExecuteTool`/`TestReadFile`/`TestListFiles`/`TestWriteFile` (17 tests) fail
in the full-suite run but **pass in isolation** — ordering-dependent pollution
of `agent_runtime` module globals. e.g. `TestCallLlm::test_openai_format_returns_text`
sets `rt.LLM_PROVIDER = "openrouter"` and `test_loopback_openai_provider...`
mutates `rt.LLM_PROVIDERS` with no teardown. Add an autouse fixture that
snapshots/restores the `agent_runtime` config globals per test.

### T4. Update CLAUDE.md known-failures list once T1-T3 are fixed
The four documented known failures were never reached in this run (suite died in
test_agent_runtime.py first), so their status is unverified.
