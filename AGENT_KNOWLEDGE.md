# Swarm Controller Reference

## Architecture

### Runtime Path
1. `swarm/api.py` → `swarm/orchestrator.py` → `swarm/agent_lifecycle.py` → `swarm_runner.py` → `swarm/agent_runtime.py`
2. `swarm/validation.py` and `swarm/closure/*` decide task health

### State Ownership
- Task graph: `swarm/db.py` `tasks` table
- Branch continuity: `swarm/task_chains.py`, `swarm/maintenance/project_heads.py`
- Agent records: `swarm/db.py` `agents` table
- Live processes: `swarm/agent_lifecycle.py`
- Verification/regressions: `swarm/db.py` `verification_runs` and `regressions`

### Default Configs
- Scheduler strategy: `priority`
- Agent loop limit: `200`
- Auto-QA threshold: `8` tasks
- Auto-audit threshold: `20` tasks
- QA requeue cap: `3`
- API port: `5001` (not 8080)

## tools/ Module Split

### Structure
- `swarm/tools/core.py`: helpers (web, RAG, broadcast, delegate) + config globals. Re-exports from `_shared` and `tasks.py`. NOT file tools.
- `swarm/tools/files.py`: all file tools. Lazy-imports `swarm.tools.core as _core` for globals at call time.
- `swarm/tools/tasks.py`: all task management tools.
- `swarm/tools/__init__.py`: re-exports ALL tools. File tools: F401 suppression. Tasks: E402 suppression.
- `swarm/tools/_shared.py`: neutral hub (`log`, `_sanitize_text`, `_project_root`, `_safe_cwd`). Prevents circular imports.

### agent_runtime.py imports
- File tools: `from swarm.tools.files`
- Shell/web/broadcast/delegate: `from swarm.tools.core`
- Task tools: `from swarm.tools.tasks`
- Knowledge tools: `from swarm.tools.knowledge`
- `run`, `_safe_cwd`: `from swarm.tools.shell`

### ARCH GOTCHA: Circular Imports
Never `import swarm.tools.core as _core` inside a function in shell.py or any module core.py re-exports from. core.py re-exports from shell.py at module level — causes circular import. Shell.py must import config vars from `swarm.tools._shared`.

### Bug: _safe_cwd() in shell.py
`_project_root()` returns `str`, not `Path` — must use `Path(root).is_dir()` not `root.is_dir()`. `_core._safe_cwd()` does not exist in core.py — removed dead call.

### Ruff noqa codes
- F401: re-exported symbols
- E402: module-level imports not at top
- E741: ambiguous `l` → use `line`
- F821: undefined name
- F541: f-string without placeholders

## swarms/agents.py Template
Template gets string-formatted at agent-spawn time. Ruff F401/F821 reports are template-time issues. Do NOT auto-fix — breaks template substitution.

## tool_dispatch.py Authority Gating
`_tool_authority_denial()` gates all tool calls:
- `plan`/`python_plan`: blocks `mutating_tools | {run_command}` with "planning tasks are read-only"
- `project_plan`: blocks `mutating_tools | {run_command, create_task, create_tasks}` with "must use create_tasks_file_aware() only"
- **Bug fixed**: `run_command` was missing from `plan`/`python_plan` block set

## Phantom Dependency Bug (SYSTEMIC)

**Root cause**: `chain_to_project_head()` returns non-existent task ID → phantom dependency blocks task forever.

**Fix**: `PATCH /api/tasks/<id>` with `{"dependencies": []}`. `update_task` route in `swarm/api_tasks.py` supports this. No code changes needed.

**Patterns across 100+ failed tasks**:
- Self-referential: `deps=[self_id]`
- Non-existent IDs from `chain_to_project_head()`
- Chain deps on in-progress tasks not yet in DB
- QA tasks depending on non-existent QA reruns (`qa-pacman-chase-rerun-*`, `qa-fusion-foundry-3d-rerun-*`)
- Typo variants: `pol-auto-neon-breaker-1780120672` (pol vs qa prefix)

**2-Pass Repair Pattern**: Fresh agent spawns from repair introduce NEW phantom deps. Run `data/scheduler_check.py` twice — Pass 1 clears known phantoms, Pass 2 catches new ones. Repeat until "No phantom deps found".

**Verification**: After repairs, fetch fresh task list — stale `/tmp/all_tasks.json` causes false "blocked" count. Use `limit=2000` for full scan.

## _swarm_*.gd Bitrot (SYSTEMIC — recurring)

**Bug**: `_swarm_*.gd` validation scaffolding files deleted between agent runs → `_swarm_check.gd` reports script load failures → `boot_ok=false`.

**Fix**: `git checkout baa4409 -- _swarm_*.gd`

**Prevention**: Never let "Refactor" commits delete essential validation files. Always `git show HEAD:file` before committing changes to validation infrastructure.

## managed_projects Sync Bug

**Bug**: POST /api/projects and scan add projects to in-memory registry but never sync to `orchestrator.MANAGED_PROJECTS` or persist to `config.json`. Invisible in dashboard, don't survive restarts.

**Fix** (`swarm/api_projects.py`): `_sync_managed_projects(config, project_registry, orchestrator, config_file=None, config_write_lock=None)` reads registry as canonical, updates both orchestrator and config["managed_projects"], persists to config.json. Called in: `add_project`, `update_project` (managed=True), `scan_project`, `spawn_parallel`, `create_project_task`.

**Scan fix**: Added `if not project_registry.get(project_name): project_registry.add_project(project_name, managed=True)` before `update_file_counts()`.

## Gardener Knowledge Store

`swarm/gardener_knowledge.py`: JSONL at `data/swarm_knowledge.jsonl`, markdown at `data/SWARM_KNOWLEDGE.md`.

**Public API**: `load()`, `append_entry()`, `update_confidence()`, `expire_stale()`, `render_markdown()`

**Entry schema**: id, pattern_signature, confidence (confirmed/suspected/disputed), godot_version, first_seen, last_seen, ttl_days, affected_projects, evidence_task_ids, fix_summary, status (active/expired), created_by. Confidence defaults to "suspected".

**Test fixture for module-level paths**:
```python
@pytest.fixture(autouse=True)
def patch_paths(self, tmp_path):
    from swarm import gardener_knowledge as gk
    gk.JSONL_PATH = tmp_path / "test.jsonl"
    gk.MARKDOWN_PATH = tmp_path / "test.md"
    gk._DATA_DIR = tmp_path
```

## Gardener Dashboard UI

**Backend** (`swarm/api_gardener.py`): 4 routes — GET/POST `/api/gardener/status`, `/api/gardener/config`, `/api/gardener/run`, GET `/api/gardener/knowledge`

**Adding toggles**: backend route + frontend toggle + status row + modal panel + CSS classes. Use `showToast()` from dashboard-core.js:312 + `fetch()` pattern.

## Scheduler Integration

**Task type**: `meta_scheduler` (NOT `scheduler`). Falls through to default feature prompt.

**Key fix**: `api_scheduler.py` creates type=`meta_scheduler` tasks. If scheduler task has type=`scheduler`, gets default feature prompt instead of scheduler.yaml.

**API endpoints**:
- GET `/api/scheduler/status`: `{"last_run_ts": float, "scheduler_enabled": bool}`
- GET `/api/agents`: `{"agents": [...]}`
- GET `/api/quota-limit`: `{"over_limit": bool, "remaining_percent": float, ...}`
- GET `/api/tasks?status=X&limit=N`: `{"tasks": [...]}`
- GET `/api/metrics`, GET `/api/config`

**Decision criteria**: Utilization <75%: no ceiling change. Quota <75%: no throttle. >50 failed + archaeologist idle: recommend triage. Quota >82%: recommend `run_after` on qa tasks.

**SCHEDULER_LOG.md**: gitignored. Write but skip `git add/commit`. `data/scheduler_check.py` replaces it each run.

## Archaeologist Task Type

Use `meta_auditor` (NOT `audit` — audit is per-project design audit).

**Files**: `prompts/auditor.yaml`, `swarm/api_meta_auditor.py`

**Integration**: api.py, swarm_runner.py (→ AUDITOR_SYSTEM/USER globals), agent_runtime.py, orchestrator.py (META_AUDITOR_ENABLED/_INTERVAL_DAYS/_MAX_TASKS + `_fire_weekly_auditor()`), api_meta.py

**Config keys**: `meta_auditor_enabled: False`, `meta_auditor_interval_days: 7`, `meta_auditor_max_tasks: 20`

**Routes**: GET/POST `/api/meta-auditor/status`, `/api/meta-auditor/run`, GET/POST `/api/meta-auditor/config`

**Weekly trigger**: `_fire_weekly_auditor()` in `fill_slots()`. Guards: META_AUDITOR_ENABLED, META_MODE_ENABLED, elapsed interval. Creates task type=`meta_auditor`, chained to swarm-controller project head.

## Meta Mode

`META_MODE_ENABLED = False` in `swarm/orchestrator.py` (line 111). Master toggle for all meta-agents. When False, no meta-agent fires regardless of individual flags. Large injected PROJECT KNOWLEDGE + broadcast context (~1155 lines) causes meta agents to hit context limits at spawn → zombie agents with loop=None. Set True to enable.

## QA Task Tool Restrictions
QA-classified agents: NO `run_command`, `create_task`, `create_tasks`, `write_file`, `append_file`, `patch_file`. Read-only only.

## scan_learnings.py

Run: `python3 scan_learnings.py` from project root. Outputs to `data/AUDIT_LEARNINGS_REPORT.md` (gitignored — never commit). Processes 14 task types from `{task_type}.md` files under `data/learnings/{project}/`. Extracts patterns via DRE regex over `##` dated headers. No task creation.

## Daily Audit Patterns (253 learning files, 67 projects)

**BUG tasks**:
- Godot 4: `.has()` is Dictionary-only, `null > 0.0` always false, `get_viewport().set_input_as_handled()` only correct API
- GUT signals: Use `.bind()` method callbacks, never lambdas
- Lock conflict halt: Stop immediately on "locked by another task"
- Three-stage validation: script parse → scene load → game launch

**FEATURE tasks**:
- Scene nesting: uid + ExtResource + node pattern
- `call_deferred` + `await get_tree().process_frame` for input/toggle tests

**REFACTOR tasks**:
- Ruff `--fix` silently breaks import chains — run tests after `--fix`
- `_shared.py` neutral hub eliminates circular imports
- `write_file` requires `broadcast_write()` first for concurrent edits
- Check `git remote get-url origin` before `git push`

## Bug Fixes Applied

### Priority Parsing ValueError (commit e9bd3d2)
3 endpoints called `int()` directly on raw `priority` field without normalizing word values. Fixed with `_normalize_priority()` in:
1. `PATCH /api/tasks/<id>` (line ~573)
2. `POST /api/tasks/<gate_id>/insert-before-gate` (line ~817)
3. `POST /api/tasks/import` (line ~885)

### echoes-of-exile Validation Files (commit cf1b626)
Commit 5a1d529 DELETED `_swarm_check.gd`, `_swarm_scene_check.gd`, `_swarm_main_check.gd` from git history. Restored from commit b42de8e.

### _session_written_files UnboundLocalError (commit f2f69bc)
Moved initialization BEFORE the `while` loop (was inside `for` loop). Python local variable scope determined at compile time — any assignment makes name local throughout.

### test_fixture Cleanup (commit 6915b7b)
Fixture must run timer+DB cleanup in PRE-yield block to prevent test-class bleed:
```python
# Pre-yield: runs before each test via pytest's fixture re-invocation
with _lock:
    if _timer is not None:
        _timer.cancel()
try:
    for t in _db.task_get_all():
        if t.get("type") == "scheduler" and t.get("status") in ("pending", "in_progress"):
            _db.task_delete(t["id"])
except Exception:
    pass
app.config["TESTING"] = True
try:
    yield app
finally:
    # Post-test cleanup
    ...
```

### test_release_hygiene.py allowlist (commit 065b434)
Add `"data/scheduler_check.py"` to `allowed_in_data` set alongside `"data/PROJECT_MAP.md"`, `"data/SWARM_SUMMARY.json"`.

## Diagnostic Scripts

**data/scheduler_check.py** (git-tracked): Run `python3 data/scheduler_check.py`. Checks: agent count, quota, pending/in-progress/failed counts, phantom deps, pending dep status, failed backlog by project.

## Two-Workspace Gotcha (CRITICAL)
Service runs at **localhost:5001** from the active swarm-controller checkout. Second copy in workspace causes `ModuleNotFoundError` for DB operations and imports. API calls → localhost:5001, file edits → intended project workspace path.

## Phantom Recovery Loop Pattern
72 of 123 failed tasks are PHANTOM recovery shadow tasks (bug-bug-bug-recovery-* with 3/3 attempts), cycling on the same error. Fix at the source (original bug), not downstream.

## Stale orchestrator active_count (2026-05-31)
`get_active_count()` reads from `_active_handles` and DB. When all agents die, `_active_handles` empty but stale DB count persists because `_is_pid_running(pid)` returns True for stale PIDs. Monitor skips `fill_slots`.

**Recovery**: `POST /api/spawn` with `task_id` bypasses monitor. Or wait for reconciliation cycle.

## Agent loop=None Display Lag
`loop` field in GET /api/agents shows `None` even for actively-running agents. Loop counter updates at END of LLM call — timing lag. Check `/api/agents/<id>/output` for actual `(loop N/200)` markers.

## API Response Wrappers
Most endpoints return `{"task": {...}}` (singular) or `{"tasks": [...]}` (plural). GET /api/tasks returns `{"tasks": [...]}`, NOT a raw list. Always use `data['tasks']` not `data` directly.

## Ghost `\-` in YAML
Double-escaped dash `\-` in YAML list items means patch_file double-escaped. Correct form is plain `- item text` at line start. Always `git checkout` and re-apply cleanly.

## Working Tree Must Be Clean Before Completing
Always `git status --short` before finishing. Validate and commit if intentional, restore if accidental.

## ShaderMaterial Test Pattern (Godot 4)
Art pass replaces `StandardMaterial3D` with `ShaderMaterial` → unit tests checking BaseMaterial3D properties FAIL with "Invalid access to property". Fix: check `mat is ShaderMaterial` and use `mat.get_shader_parameter("uniform_name")`.

## Ruff JSON Behavior
`ruff check .` auto-excludes .json files. But `ruff check data/SWARM_SUMMARY.json` (passing JSON as CLI arg) treats as Python → F821 "Undefined name `null`". Expected. Do NOT add .ruffignore.

## Known Test Flakiness
- `test_list_tasks_includes_all`: non-deterministic test isolation issue — passes consistently on re-run. Not a code bug.
- `test_cleanup_recovery_creates_continuation_for_dead_recovery_branch`: same pattern — correct code, re-run passes reliably.

## spawn-test-proj (parallel-spawn-test-proj-0-1780271471)
- project.godot: SpawnService=*res://scripts/spawn_service.gd, ServiceManager=*res://scripts/service_manager.gd, Gut=*res://addons/gut/gut.gd
- spawn_service.gd: start() returns Error (coroutine, await), stop() void, is_running() bool, get_pid() int, spawn_entity() bool
- ServiceManager: is_service_ready(), get_service_uptime(), signals: service_ready, service_error
- main.gd: spawn_entity(name), spawn_entities_parallel(names), get_spawned_count(), process_request(path), get_game_state()
- service.py: port 18080, endpoints GET /ping, GET /health, POST /spawn
- Port 18080: kill stray processes with: lsof -ti:18080 | xargs kill
- GUT: 102/103 pass (1 pre-existing Door/CollisionShape2D failure from unrelated project -- signal-cartel)
- Godot 4.6.2: no class_name for autoloads (conflicts with singleton registration)
- PROJECT_CLOSURE.md required at project root
- _swarm_check.gd, _swarm_scene_check.gd, _swarm_main_check.gd validation scripts at project root

---
## test_cleanup_recovery_creates_continuation_for_dead_recovery_branch Flaky (Fixed)

### Bug Description
The test `test_cleanup_recovery_creates_continuation_for_dead_recovery_branch` in `tests/test_api.py` failed transiently in full suite runs with:
```
AssertionError: assert 'bug-recovery-dead-1' in []  # created_continuation_ids was empty
```

### Root Cause
Commit `2b3df0b` (fix: update stale recovery assertion) fixed a prior incorrect change that had added `'failed'` to the `live_recoveries`/`live_continuations` status filters in `swarm/maintenance/recovery.py`:
```python
# WRONG - 'failed' in live filters meant dead recoveries were treated as live
live_recoveries = [task for task in recoveries if task.get("status") in ("pending", "in_progress", "failed")]
```
This prevented the dead-branch continuation from ever being spawned, because failed recoveries were treated as the canonical live task instead of triggering the `else` branch that calls `spawn_terminal_recovery_continuation`.

### Fix (already in HEAD)
Commit `2b3df0b` reverted to the correct filters:
```python
live_recoveries = [task for task in recoveries if task.get("status") in ("pending", "in_progress")]
```
Now failed recoveries correctly fall through to spawn terminal continuations.

### Current Status
- 1315/1315 tests pass in full suite
- Working tree clean
- No remaining issue

---
## test_cleanup_recovery_creates_continuation_for_dead_recovery_branch Flaky (2026-06-01)

### Root Cause: Incorrect `live_recoveries` filter (8f3df0b)
Commit `8f3c28c` incorrectly added `"failed"` to `live_recoveries` and `live_continuations` filters in `swarm/maintenance/recovery.py`:
```python
# WRONG (8f3c28c):
live_recoveries = [task for task in recoveries if task.get("status") in ("pending", "in_progress", "failed")]
```
This treated failed recovery tasks as "live" (canonical), so the `else` branch that spawns terminal continuations was never reached.

### Fix (2b3df0b):
Reverted to original:
```python
# CORRECT:
live_recoveries = [task for task in recoveries if task.get("status") in ("pending", "in_progress")]
```
Failed tasks now correctly fall through to `else` → `failed_recoveries` → `spawn_terminal_recovery_continuation`.

### Test isolation note
Test passes in isolation and in single-worker mode. Failure only observed in full suite with xdist. The fix is correct regardless.

---
## audit_learnings run 2026-06-03 (audit-learnings-1780444996)
- 111 projects, 494 files scanned by `scan_learnings.py` at project root
- Output: `data/AUDIT_LEARNINGS_REPORT.md` (gitignored, ephemeral)
- Largest failure clusters: `hybrid_qa` (100% fail, 6/6), `qa` (17%, 27 failed), `feature` (14%, 50 failed), `project_plan` (11%, 3 failed), `bug` (10%, 36 failed)
- Pattern density: feature=307, bug=241, harness_qa=184, qa=155, polish=120, research=91, audit=67, art_pass=65
- Recommendations from report: Godot 4 API gotchas + refactor re-export breakage as pre-flight checklist items; consider daily cron
- Script `scan_learnings.py` is git-tracked; `data/AUDIT_LEARNINGS_REPORT.md` and `data/audit_learnings_last_run.txt` are .gitignored
- Mark task completed via `PATCH /api/tasks/<id>` with `{"status":"completed"}` after run

---
## audit_learnings run 2026-06-05 (audit-learnings-1780704236)
- 116 projects, 508 files scanned by `scan_learnings.py` at project root
- Output: `data/AUDIT_LEARNINGS_REPORT.md` (gitignored, 290 lines)
- Top failure clusters: `hybrid_qa` (100%, 6 failed), `qa` (17%, 27 failed), `feature` (14%, 50 failed), `project_plan` (11%, 3 failed), `bug` (9%, 36 failed), `audit` (8%, 6 failed), `plan` (6%, 1 failed), `refactor` (2%, 1 failed), `phase_gate` (50%, 1/2), `harness_qa` (1%, 2 failed), `polish` (0%, 1 failed), `art_pass` (1%, 2 failed), `research` (2%, 2 failed)
- Pattern density: feature=308, bug=242, harness_qa=185, qa=158, polish=121, research=92, audit=67, art_pass=65, refactor=45, project_plan=26, plan=19, hybrid_qa=2, audit_learnings=1
- Trends vs prior run (2026-06-04: 112 projects / 500 files): grew by 4 projects / 8 files. hybrid_qa still 100% failing (chronic). qa cluster down 1 fail (27 vs 28). feature +2 fails (50 vs 48). bug steady at 36. Stable signal.
- Marked completed via `PATCH /api/tasks/audit-learnings-1780704236` with `{"status":"completed"}`
- Working tree has 20+ unrelated modifications from sibling work (dashboard, swarm modules, tests) — NOT touched by this task; no commit required

## audit_learnings run 2026-06-04 (audit-learnings-1780617830)
- 112 projects, 500 learning files scanned by `scan_learnings.py` at project root
- Output: `data/AUDIT_LEARNINGS_REPORT.md` (gitignored, 290 lines)
- Top failure clusters: `hybrid_qa` (100%, 6 failed), `qa` (17%, 27 failed), `feature` (14%, 50 failed), `phase_gate` (50%, 1/2), `project_plan` (11%, 3 failed), `bug` (10%, 36 failed), `audit` (8%, 6 failed), `plan` (6%, 1 failed)
- Pattern density: feature (306), bug (241), harness_qa (185), qa (158), polish (120), research (94), audit (67), art_pass (65), refactor (45)
- Marked completed via `PATCH /api/tasks/audit-learnings-1780617830` with `{"status":"completed"}`

---
## audit_learnings run 2026-06-07 (audit-learnings-1780790637)
- 132 projects, 538 learning files scanned by `scan_learnings.py` at project root
- Output: `data/AUDIT_LEARNINGS_REPORT.md` (gitignored, 292 lines)
- Top failure clusters: `hybrid_qa` (100%, 6/6), `qa` (17%, 27 failed), `feature` (13%, 50 failed), `project_plan` (11%, 3 failed), `phase_gate` (50%, 1/2), `bug` (9%, 36 failed), `audit` (8%, 6 failed), `plan` (6%, 1 failed), `refactor` (2%, 1 failed), `polish` (0%, 1 failed), `art_pass` (1%, 2 failed), `research` (2%, 2 failed), `harness_qa` (1%, 2 failed), `audit_learnings` (0%, 0 failed)
- Pattern density: feature=307, bug=247, harness_qa=185, qa=158, polish=121, research=92, audit=67, art_pass=65, refactor=45, project_plan=26, plan=19, hybrid_qa=2, audit_learnings=0
- Trends vs 2026-06-05 (116 projects / 508 files): +16 projects / +30 files. hybrid_qa still 100% failing (chronic). qa stable at 27 failed. feature steady at 50. bug steady at 36. Stable signal across the board.
- Marked completed via `PATCH /api/tasks/audit-learnings-1780790637` with `{"status":"completed"}`
- Working tree has 20+ unrelated modifications from sibling work (dashboard, swarm modules, tests) — NOT touched by this task; no commit required

---
## Already-applied fix: drop task-history.jsonl read in _sweep_ghost_deps

**Bug**: `swarm/api.py` `_sweep_ghost_deps` previously read and JSON-parsed the full 102MB `data/task-history.jsonl` on startup and every 100s in the monitor thread, building an unused `history_ids` set.

**Fix (already in HEAD, commit 3a4b9215)**:
- File: `swarm/api.py` lines 444–448
- Removed the JSONL-read block that built `history_ids`.
- `known_ids = active_ids | completed_ids` retained unchanged.
- Added a comment: `# Skip the 102MB JSONL read of task-history.jsonl -- completed tasks stay in the DB now, so active_ids | completed_ids is sufficient.`

**Verification**: `.venv/bin/pytest tests/test_api.py -x -k ghost` passes 2/2.

**Duplicated-task warning**: Bug tasks requesting this fix (e.g., `bug-106400608-0182`, `bug-106264195-0141`) are duplicates. If a task description matches this exact fix, verify with `grep -n "history_ids\\|task-history.jsonl" swarm/api.py` — only docstring/comment hits should remain. If both hits are in comments, the fix is already applied and no source-code change is needed.

**Sibling**: `bug-106264195-0141` completed this fix on 2026-07-03 15:27 UTC.

---
## iter_lines mock regression — already-fixed pattern (2026-07-03)

### Pattern
Swarm stream-parser functions MUST wrap `resp.iter_lines(...)` with `iter(...)` before calling `next()`:
```python
# CORRECT
line_iter = iter(resp.iter_lines(chunk_size=8192))
raw_line = next(line_iter)

# WRONG — breaks every test that mocks iter_lines as a list
line_iter = resp.iter_lines(chunk_size=8192)
raw_line = next(line_iter)
```

### Why
Production `requests` returns a generator from `iter_lines`, so `next()` works directly. Test mocks usually return `iter_lines.return_value = [list]`, and `next(<list>)` raises `TypeError` (not `StopIteration`), which the parser catches as `stream interrupted` and triggers the 7-attempt retry loop → 20+ failing tests.

### Locations applied
- `swarm/llm_utils.py:499` — primary stream parser (commit 3a4b9215)

### Check before claiming
Always `grep -n 'iter_lines' <file>` and check whether the assignment uses `iter(...)` already — if yes, the bug is fixed and no commit is needed.

---
## orchestrator.py _get_next_task deadlock fix (already in HEAD e1801839)

Bug: At end of `_get_next_task`, when all ready tasks are expansion-blocked, the code returned `None` but the comment said "allow the top task through to avoid deadlock". Contradiction.

Fix: `return None  # All tasks are expansion-blocked -- no safe alternative` -> `return ready[0]  # All tasks are expansion-blocked -- allow top task through to avoid deadlock`.

Status as of 2026-07-03: **Already in HEAD** (commit e1801839 "Refactor: update orchestrator.py"). tests/test_orchestrator.py 29/29 pass.

Note: tests/test_fill_slots.py has 3 tests asserting OLD buggy behavior (e.g. test_stalled_project_blocks_expansion_when_no_repair_path_exists asserts `task is None`). These need updating to match correct behavior -- do NOT silently revert.

---
## _task_history_lookup legacy fallback — already removed in HEAD 966dedd4

**Bug**: `swarm/agent_recovery.py` had a `_task_history_lookup(task_id)` function with comment "Remove this fallback after 2025-07-01". It is now 2026-07, over a year past the removal date.

**Fix (already in HEAD, commit 966dedd4 dated 2026-07-03 16:14)**:
- Removed `def _task_history_lookup(...)` at line 77
- Removed the call site at line 115 (`or _task_history_lookup(candidate_id)`)
- Commit title: "Refactor: update equirements.txt, agent_lifecycle.py, agent_recovery.py, db.py (+2 more)"

**Verification**: `grep -rn _task_history_lookup swarm/ tests/` → no source matches (only stale `.pyc` bytecode matches).

**Duplicated-task warning**: Any future task requesting this fix is a duplicate. Verify with `grep -n _task_history_lookup swarm/agent_recovery.py swarm/agent_lifecycle.py` — should return zero hits before claiming.

**Sibling**: Completed by `bug-106264195-0616` on 2026-07-03 (this task and the `bug-106264195-0141` task-history.jsonl deletion are sibling no-op cleanup tasks).

---
## task_get_completed_ids project scoping fix (already in HEAD 966dedd4)

Bug: swarm/db.py task_get_completed_ids(projects=...) UNION'd legacy completed_task_ids without project filter.
Fix in HEAD at swarm/db.py:819-827 wraps legacy SELECT in if projects: with WHERE project IN (?,...).
Regression test added commit da630671 in tests/test_db.py.
39/39 db tests pass.

---
## bug-106264195-0616 -- _task_history_lookup removal (already done, duplicate task)

**Verified status (2026-07-03 17:15)**: The `_task_history_lookup` legacy fallback has already been removed in commit `966dedd4`.

**Verification commands**:
- `grep -rn "_task_history_lookup" swarm/ tests/ --include='*.py'` -> zero hits (only stale `.pyc` in `swarm/__pycache__/`)
- `get_file_outline swarm/agent_recovery.py` shows function order: `_get_recovery_lock` (77-79) -> `_live_dependents` (82-92) directly, no gap
- `_replacement_task_dependencies` now uses `db.task_get_completed_record(candidate_id)` for candidate lookups

**Note**: task description cites `tests/test_agent_recovery.py` but no such file exists in this repo. Equivalent coverage: `tests/test_lifecycle.py`, `tests/test_improvements.py`, `tests/test_agent_finish_phases.py`, `tests/test_pipeline_phase_artifacts.py`, `tests/test_db.py` (189/189 pass).

**Worktree gotcha**: pytest had 1 unrelated failure in `test_dispatches_list_files` due to a missing `.` in `/private/var/folders/.../pytest-of-costas/pytest-212/test_worktree_tasks_do_not_pul0/worktree/.` -- not related to this task. Skip with `--ignore=tests/test_agent_runtime.py` or pytest-xdist single worker if needed.

**Duplicated-task warning (updated)**: Future tasks requesting this fix should `grep -rn "_task_history_lookup" swarm/ tests/ --include='*.py'` first. Zero hits => no-op.

---
## SQL injection whitelist in swarm/db.py — already fixed in HEAD 20d9d239 (bug-107344794-0964)

Bug: `agent_update_status`, `task_update`, `task_update_status`, `project_update` interpolated dict keys directly into SQL.

Fix (already in HEAD commit 20d9d239):
- File: `swarm/db.py` lines 26-44
- Added module-level `ALLOWED_TASK_COLS`, `ALLOWED_PROJECT_COLS`, `ALLOWED_AGENT_COLS` sets.
- Added `bad = [k for k in fields if k not in ALLOWED_*]; if bad: raise ValueError(...)` at the top of each update function (lines 728, 766, 1061, 1403).
- Each `bad` block raises `ValueError("Invalid <entity> column(s): {bad}")` with the unknown key list.

Tests:
- `tests/test_db.py::test_task_update_rejects_unknown_column` — verifies `task_update` and `task_update_status` reject unknown columns and SQL-injection attempts (e.g., `{"status; DROP TABLE tasks; --": "x"}`).
- `tests/test_db.py::test_project_update_rejects_unknown_column` — verifies `project_update`.
- `tests/test_db.py::test_agent_update_status_rejects_unknown_column` — verifies `agent_update_status`.
- 42/42 tests in `tests/test_db.py` pass.

Duplicated-task warning: Future tasks requesting this fix should `grep -n "ALLOWED_.*_COLS\|Invalid.*column" swarm/db.py` first. Existing hits at lines 26-44 + update-function guards = already fixed, no-op.

Sibling: bug-107344794-0964 broadcast no-op on 2026-07-03 17:44.
