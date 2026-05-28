# Agent Knowledge

Current, high-signal notes for agent work in this repository.

## Canonical Runtime Path

1. `swarm/api.py` bootstraps config, SQLite, route modules, and the monitor thread.
2. `swarm/orchestrator.py` decides what task can run next.
3. `swarm/agent_lifecycle.py` spawns and reaps subprocess agents.
4. `swarm_runner.py` generates the thin wrapper script for each task.
5. `swarm/agent_runtime.py` runs the LLM/tool loop inside the subprocess.
6. `swarm/validation.py` and `swarm/closure/*` decide whether finished work is actually healthy.

## Canonical State Ownership

- Live task graph: `swarm/db.py` `tasks` table
- Project head / branch continuity: `swarm/task_chains.py` and `swarm/maintenance/project_heads.py`
- Runtime agent records: `swarm/db.py` `agents` table
- Live process handles: `swarm/agent_lifecycle.py`
- Verification and regressions: `swarm/db.py` `verification_runs` and `regressions`

See:
- `docs/controller_module_boundaries.md`
- `docs/controller_state_ownership_map.md`

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

---
swarm/tools/shell.py _safe_cwd(): _project_root() returns str, not Path — must call Path(root).is_dir() not root.is_dir(). _core._safe_cwd() does not exist in core.py — removed dead call. Both bugs were introduced during tools-split refactor.

---
ARCH GOTCHA: never do `import swarm.tools.core as _core` inside a function in swarm/tools/shell.py, or any other file that core.py re-exports from. core.py re-exports from shell.py at module level — any call-time import of core inside shell creates a module-load circular import. If shell.py needs config vars (WORKSPACE, etc.), import them from swarm.tools._shared instead (same source as _project_root). _shared is safe because neither core nor shell imports _shared at module level — they only reference the functions it defines.

---
## tools/ module split (refactor/tools-split branch)

### Module structure after split:
- swarm/tools/core.py: utility helpers (web, RAG, broadcast, delegate_helper) + config globals. Re-exports from _shared (log, _sanitize_text, _project_root) and tasks.py (all task tools). Does NOT re-export file tools.
- swarm/tools/files.py: read_file, list_files, search_code, get_file_stats, get_file_outline, read_file_range, patch_file, write_file, append_file. Uses `import swarm.tools.core as _core` (lazy) to read globals at call time.
- swarm/tools/tasks.py: all task management tools. Uses `urllib.request` locally.
- swarm/tools/__init__.py: re-exports ALL tools from submodules for backward compat. File tools come from swarm.tools.files with explicit `as X` re-export syntax (F401 suppression). Tasks come from tasks with E402 suppression.
- swarm/tools/_shared.py: neutral hub for shared symbols (log, _sanitize_text, _project_root, _safe_cwd). Prevents circular import chains.

### Critical pattern — avoiding circular imports:
`_shared.py` is the neutral hub. When core.py needs to use files.py symbols AND files.py needs to call core helpers (like _sanitize_text), BOTH import from _shared.py:
- core.py imports `log, _sanitize_text` from `_shared` (not from itself)
- files.py calls `_core._sanitize_text` where `_core = import swarm.tools.core as _core` — this works because core re-exports _sanitize_text from _shared
- NEVER put file-tool re-exports in core.py (causes circular dependency between core and files)

### Ruff noqa codes used:
- F401: re-exported symbols (explicit `as X` alias in __init__.py, or in core.py re-export block)
- E402: module-level imports not at top of file (tasks import in core.py, files import in __init__.py, agent_runtime.py)
- E741: ambiguous variable name `l` → use `line`
- F821: undefined name (run, _ur) → ensure import or local def
- F541: f-string without placeholders → remove `f` prefix

### agent_runtime.py imports:
- File tools: from swarm.tools.files (NOT from core)
- Shell/web/broadcast/delegate tools: from swarm.tools.core
- Task tools: from swarm.tools.tasks
- Knowledge tools: from swarm.tools.knowledge
- run, _safe_cwd: from swarm.tools.shell

### BROADCAST LOG:
The broadcast log (git_log_read from orchestrator or _agent_broadcast) may not be accurate — always verify tool availability with a direct Python import test before trusting any broadcast claim about which module owns a function.

---
## Daily Audit Learnings Report (2026-05-24)

Audit of 253 learning files across 67 projects — grouped by task type.
Report at: `data/AUDIT_LEARNINGS_REPORT.md`

### Top patterns by category:

**BUG tasks (highest volume)**
- Godot 4 API gotchas: `.has()` is Dictionary-only, `null > 0.0` always false, `get_viewport().set_input_as_handled()` only correct API
- GUT signal tests: Use `.bind()` method callbacks, never lambdas
- Lock conflict halt: Stop immediately on "locked by another task", don't retry
- Three-stage validation: script parse → scene load → game launch catches issues before wasted loops

**FEATURE tasks**
- Scene nesting: 3-step pattern for adding child scenes (uid, ExtResource, node)
- Private method workaround: thin public wrapper instead of making private public
- `call_deferred` + `await get_tree().process_frame` for input/toggle test patterns

**REFACTOR tasks (most failures)**
- Missing re-exports after split are a silent import hang — always verify re-export blocks after module extraction
- Ruff `--fix` silently breaks import chains — always run tests after `--fix`
- `_shared.py` is the neutral hub for circular deps
- `write_file` requires `broadcast_write()` first — use `run_command` heredoc for concurrent edits
- Check `git remote get-url origin` before `git push` to avoid silent failures

**AUDIT tasks**
- Use targeted `grep` searches before scanning all files
- `run_command` uses `command:` key (NOT `cmd:`)
- Read `project.godot` early to get autoloads list

### Highest-value patterns (loop-count reduction):
1. Three-stage validation (script → scene → game launch)
2. Lock conflict halt immediately
3. `_shared.py` neutral hub eliminates circular import failures
4. GUT `.bind()` pattern eliminates lambda-signal test failures
5. Godot 4 API specificity (`null > 0.0`, `.has()`, `set_input_as_handled()`)

---
## tool_dispatch.py authority gating (bug-667733530-513 fix)

`swarm/tool_dispatch.py` `_tool_authority_denial()` is the central gate for all tool calls.
- `plan` and `python_plan` task types: blocks `mutating_tools | {run_command}` with "planning tasks are read-only"
- `project_plan`: special-case block for `{mutating_tools} | {run_command, create_task, create_tasks}` with "must use create_tasks_file_aware() only"
- Bug was: `run_command` was missing from the `plan`/`python_plan` block set — agents bypassed write restrictions via shell

---
swarm/gardener_knowledge.py: standalone knowledge store. JSONL at data/swarm_knowledge.jsonl, markdown view at data/SWARM_KNOWLEDGE.md. Public API: load(), append_entry(), update_confidence(), expire_stale(), render_markdown(). Uses module-level Path constants (JSONL_PATH, MARKDOWN_PATH, _DATA_DIR) — patch these with monkeypatch in tests (pytest.fixture with autouse=True). Entry schema: id, pattern_signature, confidence (confirmed/suspected/disputed), godot_version, first_seen, last_seen, ttl_days, affected_projects, evidence_task_ids, fix_summary, status (active/expired), created_by. Confidence defaults to "suspected" on append. TTL check in _is_expired() uses datetime with timezone.utc.

---
## Bug fix: managed_projects not synced on project registration (commit 2aa191e)

**Problem**: When a project was registered via POST /api/projects or /api/projects/<name>/scan, it was added to the in-memory `project_registry` with `managed=True` but never synced to `orchestrator.MANAGED_PROJECTS` or persisted to `config.json`. The project was invisible in the dashboard sidebar and didn't survive restarts.

**Fix** (swarm/api_projects.py):
- Added `_sync_managed_projects(config, project_registry, orchestrator, config_file=None, config_write_lock=None)` helper. Reads registry state (managed flag) as canonical source of truth, updates both `orchestrator.MANAGED_PROJECTS` and `config["managed_projects"]`, and persists to config.json (if params provided). Tolerates None gracefully.
- Called in 4 places: `add_project` (POST /api/projects), `update_project` (PUT /api/projects/<name>, when managed=True), `scan_project` (POST /api/projects/<name>/scan), `spawn_parallel` (POST /api/projects/<name>/spawn).
- `register_routes` signature extended with `config_file=None, config_write_lock=None` params.

**Fix** (swarm/api.py):
- Passes `config_file=config_file, config_write_lock=_config_write_lock` to `api_projects.register_routes()` call.

**Bug also existed in** `swarm/api_chat.py` and `swarm/api_wizard.py` — but those were already fixed (see broadcast log: "Auto-add project to managed_projects so it will be picked up", multiple agents fixing). Only `api_projects.py` registration endpoints were missing the sync.

**Verified**: All 106 project-related tests pass. 7/7 managed-projects tests pass.
