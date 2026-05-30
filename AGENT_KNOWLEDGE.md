# Agent Knowledge

## Canonical Runtime Path

1. `swarm/api.py` bootstraps config, SQLite, route modules, and the monitor thread.
2. `swarm/orchestrator.py` decides what task can run next.
3. `swarm/agent_lifecycle.py` spawns and reaps subprocess agents.
4. `swarm_runner.py` generates the thin wrapper script for each task.
5. `swarm/agent_runtime.py` runs the LLM/tool loop inside the subprocess.
6. `swarm/validation.py` and `swarm/closure/*` decide whether finished work is healthy.

## Canonical State Ownership

- Live task graph: `swarm/db.py` `tasks` table
- Project head / branch continuity: `swarm/task_chains.py` and `swarm/maintenance/project_heads.py`
- Runtime agent records: `swarm/db.py` `agents` table
- Live process handles: `swarm/agent_lifecycle.py`
- Verification and regressions: `swarm/db.py` `verification_runs` and `regressions`

See: `docs/controller_module_boundaries.md`, `docs/controller_state_ownership_map.md`

## Current Defaults That Matter

- Scheduler default strategy: `priority`
- Agent loop limit: `200`
- Auto-QA threshold: `8` completed tasks
- Auto-audit threshold: `20` completed tasks
- QA requeue cap: `3`

## Documentation Authority

- Human-facing overview: `README.md`
- OSS work items: `docs/open_source_checklist.md`
- Release hygiene: `docs/release_checklist.md`
- Agent helper docs: `docs/agent-ops/`

If implementation changes any of the defaults or flows above, update the matching docs in the same change.

## tools/ Module Split (refactor/tools-split branch)

### Module structure
- `swarm/tools/core.py`: utility helpers (web, RAG, broadcast, delegate_helper) + config globals. Re-exports from `_shared` (log, _sanitize_text, _project_root) and `tasks.py` (all task tools). Does NOT re-export file tools.
- `swarm/tools/files.py`: all file tools. Uses `import swarm.tools.core as _core` (lazy) for globals at call time.
- `swarm/tools/tasks.py`: all task management tools.
- `swarm/tools/__init__.py`: re-exports ALL tools from submodules for backward compat. File tools come from files.py with F401 suppression. Tasks come from tasks.py with E402 suppression.
- `swarm/tools/_shared.py`: neutral hub for shared symbols (log, _sanitize_text, _project_root, _safe_cwd). Prevents circular import chains.

### Critical: Avoiding Circular Imports

`_shared.py` is the neutral hub. When core.py needs symbols from files.py AND files.py needs core helpers:
- core.py imports `log, _sanitize_text` from `_shared` (not from itself)
- files.py calls `_core._sanitize_text` — works because core re-exports from _shared
- NEVER put file-tool re-exports in core.py

### agent_runtime.py imports
- File tools: from `swarm.tools.files` (NOT from core)
- Shell/web/broadcast/delegate tools: from `swarm.tools.core`
- Task tools: from `swarm.tools.tasks`
- Knowledge tools: from `swarm.tools.knowledge`
- `run`, `_safe_cwd`: from `swarm.tools.shell`

### Ruff noqa codes
- F401: re-exported symbols
- E402: module-level imports not at top of file
- E741: ambiguous variable `l` → use `line`
- F821: undefined name → ensure import or local def
- F541: f-string without placeholders → remove `f` prefix

### Bug: _safe_cwd() in shell.py
`_project_root()` returns `str`, not `Path` — must call `Path(root).is_dir()` not `root.is_dir()`. `_core._safe_cwd()` does not exist in core.py — removed dead call. Both bugs from tools-split refactor.

### ARCH GOTCHA: Circular Import
Never do `import swarm.tools.core as _core` inside a function in shell.py or any file that core.py re-exports from. core.py re-exports from shell.py at module level — call-time import of core inside shell creates circular import. If shell.py needs config vars (WORKSPACE, etc.), import them from `swarm.tools._shared` instead.

### BROADCAST LOG UNRELIABLE
Always verify tool availability with a direct Python import test before trusting broadcast claims about which module owns a function.

## swarms/agents.py Template

This is a template, not a regular module — gets string-formatted at agent-spawn time. Ruff F401/F821 reports are template-time issues, not runtime bugs. Do NOT auto-fix them — they break the template substitution.

All ruff errors in `agent_runtime.py` and `agent_lifecycle.py` are safe to fix. In `agent_runtime.py`, all named qa_tools imports can be removed — only `from swarm import qa_tools` (module ref) is needed for `atexit.register(qa_tools.kill_game)` and `atexit.register(qa_tools.harness_kill_game)`.

## tool_dispatch.py Authority Gating

`swarm/tool_dispatch.py` `_tool_authority_denial()` is the central gate for all tool calls.
- `plan` and `python_plan` task types: blocks `mutating_tools | {run_command}` with "planning tasks are read-only"
- `project_plan`: special-case block for `{mutating_tools} | {run_command, create_task, create_tasks}` with "must use create_tasks_file_aware() only"
- Bug: `run_command` was missing from the `plan`/`python_plan` block set — agents bypassed write restrictions via shell.

## Gardener Knowledge Store

`swarm/gardener_knowledge.py`: standalone knowledge store. JSONL at `data/swarm_knowledge.jsonl`, markdown view at `data/SWARM_KNOWLEDGE.md`. Public API: `load()`, `append_entry()`, `update_confidence()`, `expire_stale()`, `render_markdown()`. Uses module-level Path constants — patch with monkeypatch in tests (pytest.fixture with autouse=True).

Entry schema: id, pattern_signature, confidence (confirmed/suspected/disputed), godot_version, first_seen, last_seen, ttl_days, affected_projects, evidence_task_ids, fix_summary, status (active/expired), created_by. Confidence defaults to "suspected" on append. TTL check uses datetime with timezone.utc.

## Gardener Dashboard UI (feature-62050470-0059)

**Backend** (`swarm/api_gardener.py`): 4 routes — GET/POST `/api/gardener/status`, `/api/gardener/config`, `/api/gardener/run`, GET `/api/gardener/knowledge`. Registered in `swarm/api.py:42`. All 20 tests pass.

**Frontend**: dashboard.html (762-778, 858-875), dashboard.css (.gk-badge, .gk-entry classes, lines 2693-2756), dashboard.js (bootstrap calls loadGardenerState()), dashboard-config.js (gardener functions 844-1012), dashboard-core.js (initTheme, applyTheme).

**Pattern for adding dashboard toggles**: implement backend route + frontend toggle + status row + modal panel + CSS classes. Use `showToast()` (from dashboard-core.js:312) + `fetch()` pattern.

## Daily Audit Top Patterns (from 253 learning files, 67 projects)

**BUG tasks**:
- Godot 4: `.has()` is Dictionary-only, `null > 0.0` always false, `get_viewport().set_input_as_handled()` only correct API
- GUT signals: Use `.bind()` method callbacks, never lambdas
- Lock conflict halt: Stop immediately on "locked by another task", don't retry
- Three-stage validation: script parse → scene load → game launch catches issues before wasted loops

**FEATURE tasks**:
- Scene nesting: 3-step pattern for adding child scenes (uid, ExtResource, node)
- Private method workaround: thin public wrapper
- `call_deferred` + `await get_tree().process_frame` for input/toggle test patterns

**REFACTOR tasks**:
- Missing re-exports after split are silent import hangs — always verify re-export blocks
- Ruff `--fix` silently breaks import chains — run tests after `--fix`
- `_shared.py` is the neutral hub for circular deps
- `write_file` requires `broadcast_write()` first — use `run_command` heredoc for concurrent edits
- Check `git remote get-url origin` before `git push`

**AUDIT tasks**:
- Use targeted `grep` before scanning all files
- `run_command` uses `command:` key (NOT `cmd:`)
- Read `project.godot` early to get autoloads list

**Highest-value patterns** (loop-count reduction):
1. Three-stage validation (script → scene → game launch)
2. Lock conflict halt immediately
3. `_shared.py` neutral hub eliminates circular import failures
4. GUT `.bind()` pattern eliminates lambda-signal test failures
5. Godot 4 API specificity (`null > 0.0`, `.has()`, `set_input_as_handled()`)

## scan_learnings.py

Canonical scanner for all audit_learnings tasks. Run via `python3 scan_learnings.py` (no flags). Output to `data/AUDIT_LEARNINGS_REPORT.md` (gitignored — never git commit). Processes 14 task types from `{task_type}.md` files under `data/learnings/{project}/`. Extracts patterns via DRE regex over `##` dated headers, buckets into typed clusters (BUG_CL, FEAT_CL, REF_CL, QA_CL, AUDIT_CL, CMAP). Report with summary table + per-type sections + cross-cutting observations.

## Scheduler Integration (fully committed, working)

**Task type**: `meta_scheduler` (NOT `scheduler`) — agent_runtime.py dispatches `TASK_TYPE=="meta_scheduler"` → SCHEDULER_SYSTEM/SCHEDULER_USER prompts (line 420). No branch for `"scheduler"` — it falls through to default feature prompt.

**Key fix (commit 57acb7e)**: `api_scheduler.py` creates type=`meta_scheduler` tasks (both `_run_scheduler_task` and `_is_scheduler_running` guard). If a scheduler task has type=`scheduler`, it gets the default feature prompt instead of scheduler.yaml.

**API endpoints for scheduler**:
- GET `/api/scheduler/status`: returns `{"last_run_ts": float, "scheduler_enabled": bool}`
- GET `/api/agents`: returns `{"agents": [...]}` (agents in `d['agents']` list)
- GET `/api/quota-limit`: returns `{"over_limit": bool, "remaining_percent": float, ...}`
- GET `/api/tasks?status=X&limit=N`: returns `{"tasks": [...]}`
- GET `/api/metrics`: aggregate stats
- GET `/api/config`: full config including max_active_agents

**Decision criteria** (from SCHEDULER_LOG.md patterns):
- Utilization < 75%: no ceiling change
- Quota < 75%: no throttling
- >50 failed tasks + archaeologist idle: recommend archaeologist triage
- Quota >82%: recommend `run_after` on harness_qa/qa tasks

**SCHEDULER_LOG.md is in .gitignore** — write but skip git add/commit.

**Test fix (commit 6915b7b)**: fixture must run timer+DB cleanup in PRE-yield block to prevent test-class bleed. Pattern: pre-yield cleanup block before `yield app` (pytest re-invokes fixture before each test), post-yield finally block after.

## Phantom Dependency Bug (SYSTEMIC — recurring)

**Root cause**: `chain_to_project_head()` returns a non-existent project head task ID, creating a phantom dependency that permanently blocks the task forever.

**Fix**: PATCH `/api/tasks/<id>` with `{"dependencies": []}` to remove phantom deps. `update_task` route in `swarm/api_tasks.py` already supports this. No code changes needed — data repair via API.

**Patterns found across 100+ failed tasks**:
- Self-referential deps: `deps=[self_id]` → remove all
- Non-existent IDs from `chain_to_project_head()`: IDs not in DB → clear
- Chain deps on in-progress tasks not yet in DB: → clear
- QA tasks depending on non-existent QA reruns: qa-pacman-chase-rerun-*, qa-fusion-foundry-3d-rerun-*
- Typo variants: `pol-auto-neon-breaker-1780120672` (pol vs qa prefix)

**Repair strategy**: Check if task has real (non-phantom) deps — if so, keep those and only remove phantom ones. Get full task ID by querying individual tasks (API may truncate in list responses).

**Verification**: After PATCH repairs, do FRESH fetch of all tasks — stale /tmp/all_tasks.json causes false "blocked" count.

**Confirmed effect**: Clearing phantom deps on in-progress tasks immediately triggers new agent spawning. Fastest way to unblock recovery pipelines.

## managed_projects Sync Bug (commits 2aa191e, 6ab296a)

**Bug**: Projects registered via POST /api/projects or /api/projects/<name>/scan added to in-memory registry but never synced to `orchestrator.MANAGED_PROJECTS` or persisted to `config.json`. Invisible in dashboard, don't survive restarts.

**Fix** (`swarm/api_projects.py`):
- `_sync_managed_projects(config, project_registry, orchestrator, config_file=None, config_write_lock=None)` helper. Reads registry state as canonical source, updates both `orchestrator.MANAGED_PROJECTS` and `config["managed_projects"]`, persists to config.json. Tolerates None gracefully.
- Called in: `add_project` (POST /api/projects), `update_project` (PUT /api/projects/<name>, when managed=True), `scan_project` (POST /api/projects/<name>/scan), `spawn_parallel` (POST /api/projects/<name>/spawn), `create_project_task` (api_spawn.py:133).

**Scan fix** (`swarm/api_projects.py` scan_project): Added `if not project_registry.get(project_name): project_registry.add_project(project_name, managed=True)` before `update_file_counts()` — projects scanned for the first time are now auto-managed.

**Bug also existed in**: `api_chat.py` and `api_wizard.py` — already fixed via broadcast log pattern. Only `api_projects.py` registration endpoints were missing the sync.

**Verified**: All 106 project-related tests pass. 7/7 managed-projects tests pass.

## _swarm_*.gd Bitrot Pattern (SYSTEMIC — 3rd+ occurrence)

**Bug**: `_swarm_*.gd` validation scaffolding files keep getting deleted between agent runs. Causes `_swarm_check.gd` to report script load failures → `boot_ok=false` in PROJECT_CLOSURE.md. Smoke validation always fails, completion signal never propagates.

**Fix**: Restore via `git checkout baa4409 -- _swarm_*.gd`.

**Affected projects**: echoes-of-the-unmade (24 failed), threshold-cartographer (4 failed), temporal-residue (18 failed). Root cause investigation needed: are agent runs doing `git checkout` or similar that wipes uncommitted changes?

## Phantom Recovery Loop Pattern

72 of 123 failed tasks are PHANTOM recovery shadow tasks (bug-bug-bug-recovery-* with 3/3 attempts), cycling on the same error without fixing root cause:
- echoes-of-the-unmade: `!d.has("speed")` — save/load dict bug
- negative-space: scene load failures (crosshair.tscn, pillar_puzzle.tscn, origin_chamber_zone.tscn)
- temporal-residue: `Identifier GameManager not declared` — Godot --script mode false positive

When recovery tasks hit the same error repeatedly, the recovery chain itself is broken. Fix at the source (the original bug), not downstream in recovery shadows.

## Archaeologist Task Type

Use `meta_auditor` (NOT `audit` — audit is per-project design audit).

**Files owned**: prompts/auditor.yaml (118-line meta-agent prompt), `swarm/api_meta_auditor.py` (305-line route module).

**Integration points**: api.py (registers after api_meta), swarm_runner.py (loads auditor prompts → AUDITOR_SYSTEM/USER globals), agent_runtime.py (dispatch + globals), orchestrator.py (META_AUDITOR_ENABLED/_INTERVAL_DAYS/_MAX_TASKS + `_fire_weekly_auditor()`), api_meta.py (reads enabled/intervals from config).

**Config keys** (with defaults): `meta_auditor_enabled: False`, `meta_auditor_interval_days: 7`, `meta_auditor_max_tasks: 20`.

**API routes**: GET/POST `/api/meta-auditor/status`, `/api/meta-auditor/run`, GET `/api/meta-auditor/config`, POST `/api/meta-auditor/config`.

**Weekly trigger**: `_fire_weekly_auditor()` called in `fill_slots()` when idle. Guards: META_AUDITOR_ENABLED, META_MODE_ENABLED, elapsed interval > meta_auditor_interval_days. Creates task type=`meta_auditor`, chained to swarm-controller project head. Skips if already pending/in_progress.

## Two-Workspace Gotcha (CRITICAL)

The service runs at **localhost:5001** from `~USER/workspace/swarm-controller/` (PID <pid>). The workspace at `~USER/workspace/swarm-controller/` is a DIFFERENT copy. All API calls to localhost:5001 hit workspace. DB operations and module imports from workspace fail with `ModuleNotFoundError`.

**Always**: API calls → localhost:5001 (workspace), file edits → workspace path.

## Cartographer Survey (cartographer-1780111710, commit a83f3f7)

**Results**: 84 managed projects, 82 healthy, 1 warning (test-project), 1 failing (ghost-circuit).

**Health score formula**: base=100 if verification=passed, 60 if no verification, 20 if failed; -20 per stall (max 40), -30 per regression (max 60). `closure_status='red'` is NOT a health indicator — it's "active feature work mode".

**Output files**: data/PROJECT_MAP.md (1001 lines), data/SWARM_SUMMARY.json (84 projects). Both committed to git.

**Known API field limitations**: git_branch='?' for all, git_dirty=False for all, active_agents=0 for all, known_patterns=[] for all.

## QA Task Tool Restrictions

QA-classified agents cannot use: `run_command`, `create_task`, `create_tasks`, `write_file`, `append_file`, `patch_file`. Only read-only tools available.

## test Fixture Cleanup Ordering Bug (test_api_scheduler.py)

**Root cause**: pytest fixture cleanup in `yield` block runs when fixture goes out of scope, not before next test's setup. With test-class-scoped fixtures, pytest may re-enter fixture for next class before previous class's yield-cleanup runs.

**Effect**: Scheduler task from `TestSchedulerRunCreates` bleeds into `TestSchedulerRunPrevents`, causing 409 instead of 200.

**Fix**: Two-part cleanup:
1. **Pre-yield block** (before `yield app`): runs before each test via pytest's fixture re-invocation
2. **Post-yield finally block**: runs cleanup after each test

```python
# Pre-yield: guaranteed before each test by pytest's fixture re-invocation
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
    # Post-test cleanup (same logic)
    ...
```

## Test Fixture Pattern for Module-Level Paths

`gardener_knowledge.py` uses module-level Path constants (JSONL_PATH, MARKDOWN_PATH, _DATA_DIR). Patch these with monkeypatch in tests:

```python
@pytest.fixture(autouse=True)
def patch_paths(self, tmp_path):
    from swarm import gardener_knowledge as gk
    gk.JSONL_PATH = tmp_path / "test.jsonl"
    gk.MARKDOWN_PATH = tmp_path / "test.md"
    gk._DATA_DIR = tmp_path
```

---
## Phantom Dep Fix Script (2026-05-30)

Phantom deps appear when a task depends on another task ID that no longer exists in the DB. All instances found:
1. Self-referential deps: `deps=[task-id-same-as-parent]` → clear all
2. Completed-task shadow deps: completed task creates recovery with dep on itself → clear
3. Re-run phantom refs: `qa-neon-breaker-rerun-*` pointing to `pol-auto-neon-*` prefix mismatch → clear
4. Chain continuation ghosts: `bug-bug-recovery-*` depending on `bug-recovery-*` (completed) → clear
5. Scheduler self-dep: `scheduler-*` depending on previous `scheduler-*` (completed) → clear
6. QA on-completed phantom: `qa-ghost-circuit-rerun` depending on already-completed QA tasks → clear

Pattern: ALWAYS verify dep target status before clearing. Completed deps are NOT phantom — only NOT_FOUND deps need clearing. For `qa-neon-breaker-rerun` case where the task itself exists but had wrong dep ID, keep the task and fix the dep to point to the valid completed task.

Fix: PATCH `/api/tasks/<id>` with `{"dependencies": []}` or updated valid list.
Diagnostic: `data/scheduler_check.py` (now tracked in git).

## data/scheduler_check.py (git-tracked)
Diagnostic script for scheduler runs. Run: `python3 data/scheduler_check.py`. Checks: agent count, quota, pending/in-progress/failed counts, phantom deps, pending dep status, failed backlog by project. Replace SCHEDULER_LOG.md with each run.
