# Hardening Roadmap: Truth Layer, Recovery Fixes, Analytics

**Written 2026-07-07. ALL PHASES COMPLETE as of 2026-07-08.** This is a handoff
document. It assumes no conversational context — everything needed to resume is
here. Companion docs: `docs/handoff-playthrough-bot-2026-07-05.md`
(the contractor's lane, now landed) and `docs/experiment-designs/run11-analysis.md`.

## STATUS: all 5 phases done + 2 preconditions resolved

| Phase | What | Commit |
|-------|------|--------|
| Precondition 1 | Fix silently-broken signal extraction (missing `import os`) | `b4139f37` |
| Precondition 2 | Land contractor's playthrough completion work | `5440ac18` |
| D | Stale-code detection in /api/health + dashboard chip | `a55f0fc6` |
| C | Analytics layer (5 endpoints + dashboard panel) | `e9e6c52f` |
| E | Machine-checkable victory condition in planning prompts | `e8524b50` |
| B | Parse-retry budget + recovery-mechanism instrumentation | `569567d3` |
| A | Completion-evidence truth layer + soft enforcement | `d6e75681` |

Full suite green at 1488 passed. **The one remaining decision is operational,
not code**: after ~1 week of `metadata.completion_evidence.unverified` data
(surfaced in `/api/analytics/overview` + ship-candidates), decide whether to
flip the soft no-commit flag to hard failure — see "Phase A" below for the
exact one-branch change. **Restart the API server** to load all of this
(the new stale-code chip will nag if you forget).

--- Everything below is the original plan, kept for reference/rationale. ---


## ⚠ START HERE: two things are in-flight

### 1. A git stash contains an unfinished refactor + a REAL bug fix
`git stash list` → "WIP: signals helper refactor - db binding unresolved".
It contains two things for `swarm/agent_finish.py`:
- **A real bug fix**: `import os` was missing. The Phase-6c signal-extraction
  block uses `os.path.getsize()`, so **every agent's signal extraction has
  failed since 2026-07-04** (`grep '\[Signals\]' data/swarm.log` → "name 'os'
  is not defined" ×12). The `agent_signals` table is empty because of this.
  **This fix must land first** — analytics (Phase C) is useless without data.
- **An unfinished refactor**: the inline extraction block was being moved into
  a testable `_extract_and_store_signals()` helper. Problem discovered
  mid-edit: `agent_finish.py` has **no module-level `db` import** — the inline
  block appears to work only because `db` gets resolved some other way
  (`_finish_worktree_phase` at :112 uses `db = al.db`, i.e. via the
  `agent_lifecycle` lazy accessor `_al()`). The helper as stashed references a
  bare `db` name that will NOT resolve at module level.
  **Fix**: in the helper, use `al = _al(); al._lazy_imports(); db = al.db`
  (same pattern as `_finish_worktree_phase`, agent_finish.py:108-112). Then
  add a unit test that CALLS the helper against a synthetic log + isolated DB
  (fixture pattern in `tests/test_log_rotation.py`) so a missing import can
  never fail silently again. Simplest path: `git stash pop`, fix the db
  binding, test, commit.

### 2. The contractor has UNCOMMITTED work — do not collide
`git status` shows their uncommitted lane: `swarm/agent_runtime.py`,
`prompts/playthrough_bot.yaml`, `swarm/tools/playthrough_kit.py`,
`tests/test_agent_runtime.py`, `AGENT_KNOWLEDGE.md`, plus untracked
`AGENTS.md`, `.codex/`, `tests/test_playthrough_kit.py`. Their job: playthrough
bot completion (items 1-3 in the playthrough handoff doc). **Never commit their
files as a side effect** (`git add <specific paths>` only — a swarm agent
already once swept unrelated files into commit `b6f4d188`; don't repeat that).
Phases B and A below both modify `agent_runtime.py` — **wait until the
contractor commits, or coordinate explicitly**.

## Why this roadmap exists (context)

A 2026-07-06 review concluded: the swarm optimizes for "tasks completed," not
"games that work." Evidence, all from one week:
- Two showstopper bugs (dead `.tscn` signal connections; a HUD Control
  swallowing every click on screen) survived every verification layer and were
  found by a human trying to click Start.
- A playthrough_bot task was marked `completed` while the agent's own log
  claimed failure (and the log was wrong too — the work WAS committed by a
  prior attempt). Task DB, agent self-report, and git all disagreed.
- The truncation-retry mechanism burned ~15 loops without recovering.
- The API server ran 2 days on stale code with no warning (now fixed, Phase D).

Code exploration (2026-07-07) confirmed systemic holes, with exact locations
in "Key findings" below. User decisions already made: **enforcement = hard for
loop-limit, soft-flag for no-commit** (flip to hard after ~1 week of data);
**analytics = full (API + dashboard panel)**.

## Key findings from exploration (file:line refs verified 2026-07-07)

**Completion truth holes:**
- `swarm/agent_runtime.py:1388-1392` — hitting MAX_TOOL_LOOPS (200) sets
  `task_complete_hit = TASK_TYPE != "playthrough_bot" or ...` → every
  non-playthrough type gets FREE SUCCESS at loop limit.
- `swarm/agent_finish.py:79-92` (`_classify_agent_success`) — success iff
  exit 0 OR a standalone `TASK_COMPLETE` / `[Agent] Task complete!` log
  marker. No evidence required. Spoofable by an agent printing the line.
- `swarm/agent_runtime.py:1361-1375` — "TASK_COMPLETE blocked" validation
  check clears `_last_run_outputs` after ONE rejection (:1373), so a second
  bare TASK_COMPLETE passes. Twin branch at :1188-1204.
- `swarm/agent_finish.py:182-196` (`_capture_project_diff_stat`) — diff
  evidence is `git diff --stat HEAD~1` in the main project dir: NOT
  agent-attributed; a no-op agent inherits the previous commit's diff.
- No canonical write-vs-readonly task-type map exists anywhere. `READONLY`
  (agent_runtime.py:108) comes from `metadata.readonly` which nothing sets.

**Recovery machinery (all in `swarm/agent_runtime.py` unless noted):**
- Truncation retry (:1148-1167): cap of 3 counts only CONSECUTIVE
  truncations; counter resets on any parseable response (:1227). Alternating
  truncated/valid never trips it → the observed 15-loop burn. Log string says
  `({n}/2)` while code allows 3 (:1159) — cosmetic mismatch.
- Invalid-JSON retry branch (:1168-1176): **no cap at all**.
- Contractor's `[/TOOL_CALL` suffix repair (`swarm/llm_utils.py:154-158`)
  makes one truncation class parse → also resets the consecutive counter,
  further defeating the cap. Their lane; be aware, don't revert.
- Wrap-up warning (:897-929): fires once, **logs nothing** — invisible to
  analytics. Plan-budget variant (:881-895) also logs nothing.
- Meta-investigation (:862-879): logs `[Meta] Repeated error…` — NOT matched
  by any `extract_signals()` regex. Vision cap logs `[VisionCap] Blocked…` —
  also unmatched. Hint injection `[Hint]` (:854) — unmatched.
- Coordination: only `_wrap_up_injected` gates stall/meta/plan-budget.
  Everything else independent.

**Analytics surface (what exists to build on):**
- `agent_signals` table + `agent_signals_query/get/upsert` (`swarm/db.py`,
  end of file); `get_signals_summary` in `swarm/log_rotation.py`; endpoint
  `GET /api/log-rotation/signals` (swarm/api_config.py). EMPTY until the
  stash's os-fix lands.
- `estimated_cost_usd` computed at `swarm/agent_finish.py:811`, stored in
  agents table; agents rows are pruned to `data/agent-history.jsonl` after
  finish, so cost queries need table + JSONL fallback (see the pattern in
  `swarm/api_metrics.py` agent-history fallback).
- Dashboard pattern to copy: `loadMetrics()` at `dashboard-config.js:925` —
  fetch `/api/metrics` every 30s (interval registered in `dashboard.js:46`),
  render into `#metrics-grid` cells (`dashboard.html:59-64`). Plain HTML, no
  chart library.
- Value/repair ratio (run-11's hand-computed 2.8x) is computable from the
  tasks table alone: value = completed feature/polish/art_pass; repair = bug
  with `metadata.is_validation_bug` or attempts > 1. Tasks are permanent in
  the DB (8,364 pre-migration rows were backfilled 2026-07-04, tagged
  `metadata.backfilled_from_jsonl`).

## The phases

**Order: (stash fix) → C → E → B → A.** Phase D is DONE (commit `a55f0fc6`:
`/api/health` now reports `running_commit`/`repo_commit`/`code_stale`;
dashboard shows a stale-code chip). B and A wait on contractor sync.

### Phase C — Analytics layer (roadmap #7) — IN PROGRESS, barely started
New `swarm/analytics.py` (queries) + `swarm/api_analytics.py` (routes),
registered in `swarm/api.py` like the other `api_*` modules. Endpoints:
1. `GET /api/analytics/overview` — global + per-project completed/failed,
   total & avg cost per completed task, token totals, avg loops. Source:
   tasks table + agents table + agent-history.jsonl fallback.
2. `GET /api/analytics/value-repair?project=` — live value/repair ratio (see
   formula above). **Acceptance test: reproduces run-11 art arm's ~2.8x**
   against `docs/experiment-designs/run11-analysis.md`.
3. `GET /api/analytics/deaths` — from agent_signals: terminal_status
   breakdown, avg loop_count at death by task_type, top error_snippets.
4. `GET /api/analytics/mechanisms` — joins `mechanism_fires` (Phase B) with
   terminal_status: per-mechanism fire count + completion rate with/without.
   Answers "do our recovery reflexes actually help?"
5. `GET /api/analytics/ship-candidates` — per Godot project: closure_status,
   playthrough smoke-check presence/result, validation-bug rate (last 50
   tasks), unverified-completion count (Phase A) → ranked "closest to
   shippable." This picks the first game to actually release.
Dashboard: new collapsible Analytics section; overview + value-repair on the
30s poll, the rest on-demand. Tests: `tests/test_analytics.py`, seeded
isolated DB (copy `tests/test_log_rotation.py`'s fixture).

### Phase E — Victory-condition requirement (prompts only, no code)
`prompts/project_plan.yaml` + `prompts/project_create.yaml`: require
GAME_DESIGN.md to contain a `## Victory Condition` section stating a
machine-checkable end state, and require `get_game_state()` to expose the
fields to detect it. Mirror the existing "MUST contain a '## Controls'
section" rule (project_plan.yaml:40). **Do NOT touch
prompts/playthrough_bot.yaml** (contractor lane). Verify with a prompt render
test (no undefined template vars — see `swarm_runner._load_prompt`, `<<var>>`
Jinja2 delimiters).

### Phase B — Recovery machinery fixes + instrumentation (contractor sync first)
Fixes in `agent_runtime.py`:
1. Add per-run TOTAL truncation budget (suggest 8) alongside the consecutive
   cap; exceeding either fails the task with reason "llm truncation budget
   exhausted". Total counter never resets.
2. Invalid-JSON retry branch shares the same total budget.
3. Fix the `({n}/2)` log mismatch (:1159).
4. Sticky validation block: don't clear `_last_run_outputs` on rejection
   (:1373 and the :1188-1204 twin); clear only when a later run_command
   output has no `_FAILURE_PATTERNS` match.
Instrumentation:
5. Add log lines: `[WrapUp] injected at loop N`, `[PlanBudget] warning at
   loop N`.
6. `swarm/log_rotation.py` `extract_signals()`: add regexes for `[Meta]`,
   `[VisionCap]`, `[WrapUp]`, `[PlanBudget]`, `[Hint]`, stall redirect,
   truncation/invalid-JSON retries → accumulate into ONE new column
   `mechanism_fires TEXT` (JSON dict of counts).
7. `swarm/db.py`: add `mechanism_fires` column via `_evolve_schema()`
   PRAGMA-guard pattern; add to `agent_signals_upsert()` column list.
Tests in `tests/test_log_rotation.py` + `tests/test_agent_runtime.py`
(coordinate — contractor has uncommitted tests in the latter).

### Phase A — Completion truth layer (contractor sync first)
1. `swarm/constants.py`: `WRITE_TASK_TYPES = {"feature","bug","refactor",
   "polish","art_pass","integration","playthrough_bot"}`.
2. At spawn (`swarm/agent_lifecycle.py`): record project HEAD into agent
   metadata as `head_at_spawn`.
3. At finish (`agent_finish.py`, near the diff capture at :798): build
   `completion_evidence = {new_commits (rev-list head_at_spawn..HEAD),
   commit_hash, diff_stat (attributed: diff head_at_spawn..HEAD),
   validation_passed, exit_code, log_marker_present}` → store in task
   `metadata.completion_evidence` AND agent row metadata. Keep `HEAD~1` diff
   only as legacy fallback when `head_at_spawn` is absent.
4. **HARD now**: at `agent_runtime.py:1388-1392`, loop-limit exit sets
   `task_complete_hit = False` for ALL types (delete the playthrough-only
   carve-out). 200 loops without declaring completion = failure → normal
   retry/escalation.
5. **SOFT now, flip in ~1 week**: write-type task about to be completed with
   `new_commits == 0` → log `[Evidence] WARNING: write-type task completed
   with zero commits since spawn`, set
   `metadata.completion_evidence.unverified = true`, still complete. The
   analytics ship-candidates/overview endpoints surface the unverified count
   per project; when the false-positive rate is understood, flip this branch
   to route into the existing failure path.

## Verification checklist (per phase)
- `pytest -q` green after every phase. Baseline was 1460; 1471 with
  contractor's uncommitted tests present. Known pre-existing failures are
  listed in CLAUDE.md "Known Test Failures".
- Stash fix: after landing, run one real task and confirm
  `SELECT COUNT(*) FROM agent_signals` grows and `[Signals]` errors stop.
- Phase C: value-repair endpoint reproduces run-11's ~2.8x; dashboard panel
  renders live.
- Phase B: synthetic alternating truncated/valid log trips the total budget;
  `extract_signals` returns correct `mechanism_fires`.
- Phase A: real low-risk task → `completion_evidence.new_commits >= 1`;
  unit test: loop-limit exit fails a feature task; no-op completion sets
  `unverified: true`.

## Explicitly out of scope (do not scope-creep into these)
- Playthrough bot completion itself — contractor lane, run-12 stays paused
  until their clean run (see the playthrough handoff doc).
- Auto-seeding playthrough closure gates across all projects — after the
  contractor's clean run proves the pattern.
- Event-driven scheduling core (roadmap #9) — deliberately deferred.
- Actually shipping a game — operational follow-up once
  `/api/analytics/ship-candidates` exists; that endpoint is the enabler.

## Operational notes for whoever resumes
- The API server does NOT hot-reload. After merging, restart
  `swarm_runner.py api` — the new stale-code chip (Phase D) will nag on the
  dashboard if you forget.
- `log_extract_signals: true` is already set in config.json. `log_retention_days`
  is 0 (rotation off) — deliberate until analytics is consuming signals.
- Full original plan with user Q&A context lives at
  `~/.claude/plans/curried-prancing-russell.md` on the original machine; this
  document supersedes it and is self-contained.
