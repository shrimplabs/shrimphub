# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Swarm Controller is a modular agent orchestration system. It spawns LLM-powered subprocesses to build, refactor, and maintain multiple code projects (Godot, Python, TypeScript, Swift/iOS, Unity, Rust, Go, C++, C#). Key properties:

- SQLite-backed state (WAL mode, thread-safe)
- Multiple LLM providers: Minimax, Claude, OpenRouter, Kimi, custom
- Automatic task retry with failure context fed back into prompts
- Recovery tasks spawned when retries exhausted; dependents reparented automatically
- Dependency self-healing: chains unblock when pruned-failed deps are detected
- Post-task validation with auto-spawned bug tasks on failure (runs synchronously in monitor thread -- can block up to ~5 min for GUT tests)
- Real-time log streaming via SSE, per-project health metrics
- Loop stall detection: injects a redirect prompt when the same tool call repeats 3× identically
- Token tracking: input/output tokens recorded per agent, visible in dashboard
- Auto-QA: Godot projects automatically receive a QA task every 8 completions
- Auto-audit: Godot projects receive an audit task every 20 completions
- Auto-replan: per-project opt-in toggle that spawns `project_plan` when a project's task queue empties
- Context compaction: long agent conversations are summarised mid-run to reduce token usage; threshold is 120k estimated tokens (~80k buffer before MiniMax's 200k window)
- Jitter: random 0.5-3 s sleep before every LLM call to spread RPM load
- Project registry persistence: all managed projects are registered on startup so they always appear in the dashboard
- QA cycle cap: QA agents stop requeuing themselves after `qa_max_cycles` (default 3, configurable); prevents infinite QA loops
- Dep violation checker: monitor kills any agent whose task has unmet dependencies (catches both in-memory and DB-tracked agents after restart)
- Worktree `.godot` cache seeding: new worktrees copy the `.godot/` directory from the main project so Godot class_name resolution works correctly during validation
- Integrity diagnostics: real-time task authority validation, orphan detection, live repair in dashboard
- Manager Chat: conversational interface (`/api/chat`) to control tasks and agents from the dashboard
- Project Creation Wizard: chat-based new project scaffolding (`/api/wizard/plan`, `/api/wizard/create`)
- RAG backend: optional ChromaDB vector search for agent code context
- Task auto-chaining: tasks created within a project are chained to the project HEAD to prevent floating chains

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

cp config.example.json config.json   # then edit workspace path

# .env in project root
echo "MINIMAX_API_KEY=your_key" > .env
# or ANTHROPIC_API_KEY / OPENROUTER_API_KEY / KIMI_API_KEY
```

## Running

```bash
.venv/bin/python swarm_runner.py api        # API server + dashboard at http://localhost:5001
.venv/bin/pytest                            # full test suite (excludes dashboard -- see below)
.venv/bin/pytest tests/test_api.py          # single file
.venv/bin/pytest tests/test_api.py::TestClass::test_name  # single test

# Dashboard tests require Playwright browsers (one-time install):
.venv/bin/playwright install chromium
.venv/bin/pytest tests/test_dashboard.py
```

## Issue Tracking

This repo uses **bd** (beads) for issue tracking -- not TodoWrite or markdown TODO lists.

```bash
bd prime                    # full workflow context + session-close protocol
bd ready                    # find available work
bd show <id>                # view issue details
bd update <id> --claim      # claim work
bd close <id>               # complete work
bd dolt push                # sync beads data (only if a Dolt remote is configured)
```

## Shell Commands in Agents

Always use non-interactive flags to avoid agents hanging on confirmation prompts (some systems alias `cp`/`mv`/`rm` to interactive mode):

```bash
cp -f src dst      # not: cp src dst
mv -f src dst      # not: mv src dst
rm -f file         # not: rm file
rm -rf dir         # not: rm -r dir
```

## Architecture

```
swarm_runner.py              thin entry point + generate_task_script()
swarm_mcp_server.py          MCP server exposing swarm API as tools (register in ~/.claude/settings.json)
sync_templates.py            sync state_server.gd + test_harness.gd to all managed Godot projects
swarm/api.py                 Flask app factory, registers all api_*.py route modules, monitor thread
swarm/orchestrator.py        high-level scheduling & coordination (598 lines)
swarm/agent_runtime.py       agent tool functions + LLM call loop (1721 lines)
swarm/agent_lifecycle.py     agent process management & lifecycle hooks (1529 lines)
swarm/db.py                  SQLite layer (WAL, thread-local connections, schema evolution)
swarm/tasks.py               Task dataclass + SQLiteTaskSource
swarm/projects.py            Project dataclass + SQLiteProjectRegistry
swarm/agents.py              Agent dataclass + SQLiteAgentTracker
swarm/strategies.py          Task selection strategies
swarm/dependencies.py        DAG + cycle detection
swarm/constants.py           Module-level constants (MAX_TOOL_LOOPS, AGENT_TIMEOUT, etc.)
swarm/provider_utils.py      LLM provider configs + resolution helpers
swarm/llm_utils.py           LLM call helpers shared across modules
swarm/integrity.py           Integrity health checks (task authority, orphan detection)
swarm/learnings.py           Learning/feedback management
swarm/task_chains.py         Auto-chain tasks to project HEAD
swarm/task_mutations.py      Centralized task mutation helpers
swarm/validation.py          Post-task validation (Godot + Python)
swarm/worktree.py            Git worktree isolation for validation runs
swarm/vision.py              Vision model dispatch (MCP, local mlx-vlm, REST)
swarm/mcp_client.py          MCP server subprocess management
swarm/qa_tools.py            QA/vision tools: StateServer, click_element, vision_query (1423 lines)
swarm/prompts.py             Prompt loading and template rendering
swarm/plan_cleanup.py        Sprint plan snapshot cleanup
swarm/login.py               Auth helpers
swarm/rag/                   RAG backend (ChromaDB)
swarm/tools/core.py          Agent tool implementations: file I/O, git, web, task creation, MCP
swarm/tools/knowledge.py     Shared knowledge base + scratchpad tools
swarm/maintenance/
  agents.py                  Agent maintenance (restart, reconciliation)
  plans.py                   Sprint plan snapshot management
  project_heads.py           Project HEAD reconciliation & repair
  recovery.py                Recovery branch cleanup

# Route modules (registered into api.py)
swarm/api_agents.py          Agent lifecycle endpoints
swarm/api_auth.py            Authentication
swarm/api_broadcast.py       Broadcast read/write (shared knowledge)
swarm/api_chat.py            Manager chat + project creation wizard (1292 lines)
swarm/api_config.py          Configuration management
swarm/api_deps.py            Dependency graph + integrity diagnostics (766 lines)
swarm/api_history.py         Task/agent history
swarm/api_metrics.py         Health metrics
swarm/api_plans.py           Sprint planner management
swarm/api_projects.py        Project management
swarm/api_spawn.py           Agent spawning
swarm/api_tasks.py           Task CRUD & batch creation (800 lines)
swarm/api_webhook.py         Webhook firing
swarm/api_wizard.py          Project creation wizard routes
```

### Key flow

1. `swarm/api.py` `create_app()` initialises SQLite, syncs `swarm_runner` module globals, starts monitor thread, registers all `api_*.py` route modules
2. Monitor calls `orchestrator.check_dep_violations()` → `orchestrator.check_agent_status()` (reap finished processes) → `orchestrator.fill_slots(generate_task_script)` when auto mode is on
3. `generate_task_script(task)` in `swarm_runner.py` builds a thin Python wrapper that sets config vars then calls `swarm.agent_runtime.main()`
4. `agent_runtime.main()` runs the tool loop: call LLM → parse `[TOOL_CALL]` → execute tool → repeat until `TASK_COMPLETE` (max 200 loops)
5. `agent_lifecycle._finish_agent()` captures diff stat + token usage, handles retry logic, runs post-validation synchronously in monitor thread

### Important: module globals sync

`swarm/api.py` imports `generate_task_script` from `swarm_runner` **and** syncs `swarm_runner`'s module-level globals before doing so:

```python
import swarm_runner as _runner_mod
_runner_mod.WORKSPACE = workspace
_runner_mod.LLM_PROVIDER = config.get("llm_provider", "minimax")
# ...
from swarm_runner import generate_task_script
```

This ensures the wrapper scripts get the resolved config, not the defaults baked in at import time.

### Important: config file isolation

`create_app()` accepts a `config_file` parameter. **Tests must pass `config_file=tmp_path/"config.json"`** to avoid writing to the real `config.json`. File config provides defaults; explicit config passed to `create_app()` always takes priority (merged with file first, then explicit values overwrite).

```python
# In tests:
flask_app = create_app(
    config={...},
    data_dir=tmp_path / "data",
    config_file=tmp_path / "config.json",  # REQUIRED to prevent real config corruption
)
```

### Timeout design

Two independent limits apply to agents:
- **Loop limit** (`MAX_TOOL_LOOPS = 200` in `swarm/constants.py`) -- the primary governor; agents exit cleanly at loop 200
- **Wall-clock watchdog** (`AGENT_TIMEOUT = 7200s` default in `constants.py`, overridable via `config.json`) -- safety net for truly hung processes only; should not fire on working agents

### Recovery and self-healing

- When a task exhausts `max_attempts`, `_spawn_review_task()` creates a recovery task with full failure history and reparents all dependents to it
- Recovery tasks have `is_recovery_task: True` in metadata -- they never spawn further recovery tasks (prevents infinite chains)
- `_get_next_task()` treats a dep as "met" if it's completed OR if it no longer exists in the active tasks table -- chains self-heal when deps are pruned as failed
- Recovery task creation is NOT gated on MANAGED_PROJECTS (only PAUSED_PROJECTS skip) -- recovery always fires

### Continuation task dependency reparenting

When an agent hits the loop or context limit and spawns a continuation task, `_finish_agent()` parses `"Continuation task created: <id>"` from the agent log and reparents all downstream tasks that depended on the original task to depend on the continuation instead. This prevents the chain from fragmenting.

Lock conflicts also serialize via continuation nodes -- the second agent creates a continuation task pointing to the lock holder rather than running in parallel.

### Validation bug task reparenting

`_spawn_validation_bug_task()` similarly reparents all tasks that depended on the original completed task to depend on the new validation bug task. The original task is marked `completed` (not retried) so the chain is correctly: original → validation_bug → dependents.

**Important**: when updating an existing validation bug task, use `db.task_update()` (not `db.task_upsert()`) to avoid wiping dependencies that were set externally.

### Dependency violation checker

`orchestrator.check_dep_violations()` is called by the monitor thread before `check_agent_status()`. It kills and resets to `pending` any agent whose task has unmet dependencies. Checks two sources:
1. `_active_handles` -- in-memory handles for agents spawned in the current server session
2. `db.agent_get_active()` -- DB-tracked agents (survive server restarts as separate PIDs)

This catches the "two agents running the same chain" scenario that can arise after restarts or dep reparenting races.

### Worktree `.godot` cache seeding

Godot requires `.godot/global_script_class_cache.cfg` to resolve `class_name` types. This file is gitignored, so fresh worktrees don't have it -- causing validation runs to fail with "Could not find type X", spawning false-positive bug tasks.

Fix: `_create_worktree()` and `_post_task_validation_in_worktree()` both copy `.godot/` from the main project path into the worktree if it exists and isn't already there.

### Integrity diagnostics

`swarm/integrity.py` provides real-time task authority validation. The monitor thread periodically checks:
- Tasks running without an active agent (orphaned)
- Agents running tasks with unmet dependencies
- Controller state ownership violations

Results are surfaced in the dashboard and via `GET /api/dependencies/integrity`. Repair actions are available via `POST /api/dependencies/integrity`.

## Configuration

`config.json` (gitignored) -- all optional:

| Key | Default | Description |
|-----|---------|-------------|
| `workspace` | `~/workspace` | Root directory for projects |
| `managed_projects` | `[]` | Projects to assign work |
| `paused_projects` | `[]` | Projects to skip |
| `max_active_agents` | `3` | Concurrent agent limit |
| `max_lines` | `5000` | Refactor threshold |
| `lock_project` | `false` | `true` = one agent/project |
| `agent_timeout` | `7200` | Seconds before watchdog kills agent (safety net only; the loop limit is the primary governor) |
| `quota_limit_percent` | `90` | Stop spawning at this API usage % |
| `llm_provider` | `"minimax"` | Active provider |
| `llm_providers` | `{}` | Per-provider overrides (model, base_url, etc.) |
| `mcp_servers` | `{}` | MCP server definitions |
| `task_selection_strategy` | `"refactor_first"` | Task picker |
| `auto_replan_projects` | `[]` | Projects that auto-spawn `project_plan` when task queue empties |
| `qa_max_cycles` | `3` | Max times a QA agent may requeue itself before stopping |
| `meta_investigation` | `true` | **EXPERIMENT** -- fires an out-of-band LLM investigator when the same error repeats 3+ times; injects a hint into the agent context. Set `false` to disable. |
| `meta_investigation_provider` | `""` | LLM provider used for investigation calls (defaults to the main provider; set to `"claude"` etc. to override) |
| `thinking_task_types` | `[]` | Task types that get thinking enabled (e.g. `["bug", "qa"]`) |
| `thinking_task_budget` | `10000` | Thinking budget tokens for task types listed in `thinking_task_types` |
| `vision_provider` | `"minimax-mcp"` | Vision model backend for QA agents |
| `vision_provider_fast` | `"local"` | Fast vision backend (local mlx-vlm) |
| `vision_providers` | `{}` | Per-vision-provider config overrides |
| `rag` | `{}` | RAG config (`enabled`, `index_path`, `backend`, `top_k`) -- see RAG section below |
| `completion_webhook_url` | `""` | URL to POST when an agent completes |
| `fallback_providers` | `[]` | Provider names to try if primary fails |
| `godot_path` | `""` | Absolute path to Godot binary; inherited by agents as `GODOT_PATH` |
| `minimax_base_url` | MiniMax default | Override MiniMax API endpoint |
| `disable_remote_repo` | `true` | Set `false` to enable Gitea repo provisioning in the wizard |
| `login_required` | `false` | Set `true` to enable session auth (API open to network by default) |

## Security / Authentication

Authentication is **off by default** -- the API is open to anyone on the network. To enable it, add `"login_required": true` to `config.json`. Default credentials are then `admin` / `admin`.

To set a strong password before enabling:

```bash
python - <<'PY'
from swarm.login import hash_password_for_storage
pw, salt = hash_password_for_storage(input("Password: "))
print(f'"login_password_hash": "{pw}",\n"login_salt": "{salt}"')
PY
```

Add the printed values plus `"login_username": "admin"` and `"login_required": true` to `config.json`.

## LLM Providers

Built-in providers in `swarm/provider_utils.py`:

| Name | Format | Env var | Model |
|------|--------|---------|-------|
| `minimax` | `anthropic` (Bearer) | `MINIMAX_API_KEY` | MiniMax-M2.7 |
| `claude` | `anthropic_native` (x-api-key) | `ANTHROPIC_API_KEY` | claude-sonnet-4-6 |
| `openrouter` | `openai` | `OPENROUTER_API_KEY` | claude-3.5-sonnet |
| `kimi` | `anthropic` (Bearer) | `KIMI_API_KEY` | k2p5 |

`format` values:
- `anthropic` -- `Authorization: Bearer`, `/messages` endpoint, Anthropic body
- `anthropic_native` -- `x-api-key` + `anthropic-version: 2023-06-01`, `/messages` endpoint
- `openai` -- `Authorization: Bearer`, `/chat/completions`, OpenAI body

Custom providers can be registered at runtime via `POST /api/provider`.

## Task Model

```python
@dataclass
class Task:
    id: str
    project: str
    type: str            # feature | bug | refactor | polish | qa | harness_qa | art_pass | audit | research | plan | python_plan | project_plan
    description: str
    priority: int        # 100=refactor, 80=bug, 50=feature/polish
    status: str          # pending | in_progress | completed | failed
    attempts: int        # incremented on each failure
    max_attempts: int    # default 3; recovery task spawned after this many failures
    metadata: dict       # last_failure, failure_attempt, diff_stat, is_recovery_task, qa_cycle, etc.
    dependencies: list   # task IDs that must complete first (or be absent from DB)
    run_after: str       # optional ISO datetime; task skipped until this time
```

On failure: if `attempts < max_attempts`, task resets to `pending` with `metadata.last_failure` set. On exhausted attempts: recovery task spawned, dependents reparented.

## Data Files

| Path | Description |
|------|-------------|
| `data/swarm.db` | Primary SQLite DB (tasks, projects, agents tables) |
| `data/agent-history.jsonl` | Archived agents (pruned from DB after completion) |
| `data/task-history.jsonl` | Archived completed/failed tasks (used by repair/requeue) |
| `data/agent_<id>.log` | Per-agent stdout/stderr |
| `data/agent_<id>.py` | Generated wrapper script (deleted on completion) |
| `data/agent_<task_id>_tokens.json` | Transient token counts written by agent, read by orchestrator on finish |

SQLite schema evolution: `swarm/db.py:_evolve_schema()` adds new columns to existing DBs via `ALTER TABLE` on startup -- safe to run on any DB version.

## Prompt Types

Prompt YAML files live in `prompts/`. Each maps to a task type:

| File | Task type | Notes |
|------|-----------|-------|
| `feature.yaml` | `feature` | Godot feature work |
| `bug.yaml` | `bug` | Godot bug fix |
| `refactor.yaml` | `refactor` | Code quality / size reduction |
| `polish.yaml` | `polish` | UX/visual polish |
| `qa.yaml` | `qa` | Vision-capable QA agent |
| `harness_qa.yaml` | `harness_qa` | Deterministic checkpoint QA |
| `hybrid_qa.yaml` | `hybrid_qa` | Hybrid vision QA mode |
| `art_pass.yaml` | `art_pass` | Asset integration + visual improvement |
| `audit.yaml` | `audit` | Project code audit |
| `audit_learnings.yaml` | `audit_learnings` | Audit learning accumulation |
| `research.yaml` | `research` | Read-only research / analysis |
| `plan.yaml` | `plan` | Universal read-only planner (write-blocked) |
| `project_plan.yaml` | `project_plan` | Godot sprint planner |
| `project_create.yaml` | `project_create` | New project scaffolding |
| `manager.yaml` | `manager` | Dashboard chat manager |
| `triage.yaml` | `triage` | Issue triage |
| `python/feature.yaml` | `feature` (Python) | Python feature work |
| `python/bug.yaml` | `bug` (Python) | Python bug fix |
| `python/plan.yaml` | `plan` (Python) | Python read-only planner |
| `python/refactor.yaml` | `refactor` (Python) | Python refactoring |

`prompts/common/` contains shared fragments included across multiple prompts (acceptance criteria, tool lists, scratchpad, etc.).

`plan` task type: write tools are hard-blocked; creating tasks via the API IS the deliverable.

## API Endpoints

### Core
- `GET /api/projects` `/api/tasks` `/api/agents`
- `GET /api/health` -- monitor alive, lag seconds, uptime
- `POST /api/spawn` -- spawn for a specific project
- `POST /api/spawn-batch` -- fill all slots (checks quota first)
- `GET/POST /api/auto-mode` -- returns `{enabled, suspended_for_quota}`
- `GET /api/history`

### Projects
- `GET /api/projects/<name>/health` -- score, task counts, avg lines, last commit age
- `POST /api/projects/<name>/repair` -- reset failed/orphaned tasks, resurrect missing dep tasks
- `POST /api/projects/<name>/restart` -- reset ALL tasks to pending, attempts=0
- `POST /api/projects/<name>/scan`
- `GET/POST /api/projects/<name>/lock` `/unlock` `/locks`

### Tasks
- `GET /api/tasks` -- list all tasks
- `POST /api/tasks` -- create one task `{project, type, description, priority, dependencies, metadata}`
- `GET /api/tasks/<id>` -- get task
- `PUT/PATCH /api/tasks/<id>` -- update task (merge: only fields present in body are touched)
- `DELETE /api/tasks/<id>` -- delete task
- `GET /api/tasks/<id>/dependencies` -- read dep list
- `PUT /api/tasks/<id>/dependencies` -- replace full dep list `{dependencies: [...]}`
- `POST /api/tasks/<id>/dependencies` -- add deps without replacing `{dependency: "<id>"}` or `{dependencies: [...]}`
- `DELETE /api/tasks/<id>/dependencies/<dep_id>` -- remove single dep edge
- `POST /api/tasks/<id>/reset` -- reset failed task to pending (clear attempts, add note)
- `POST /api/history/<agent_id>/requeue` -- resurrect task from agent/task history

#### Batch task creation (`POST /api/tasks/batch`)

Creates multiple tasks in one call with reliable dep wiring. Use this instead of N individual POSTs.

```json
{
  "project": "swarm-controller",
  "tasks": [
    {"type": "bug",     "description": "Fix X",        "priority": 80},
    {"type": "bug",     "description": "Fix Y",        "priority": 80},
    {"type": "feature", "description": "Fix Z",        "priority": 50, "depends_on": [0, 1]},
    {"type": "feature", "description": "Fix W (after Z)", "priority": 50, "depends_on": [2]}
  ]
}
```

- **`depends_on`** -- list of integer indices into the `tasks` array; resolved to actual IDs before any task is created (no chicken-and-egg, no hardcoded IDs)
- **`dependencies`** -- explicit task ID strings; merged with `depends_on` results
- **`project`** -- top-level default; per-item `project` overrides it
- **`chain: true`** -- each task automatically depends on the previous one (linear sequence)
- IDs are generated upfront with an index suffix so same-millisecond creation never collides
- Response includes `id_map: {"0": "<id>", "1": "<id>", ...}` for referencing generated IDs afterward

### Agents
- `GET /api/agents/<id>/stream` -- SSE live log stream
- `POST /api/agents/<id>/kill`

### Manager Chat
- `POST /api/chat` -- conversational control of tasks/agents (create, kill, query state, toggle auto-mode)
- `POST /api/project-chat` -- project-scoped conversational control
- `POST /api/create-project-tasks` -- create tasks from a project overview via LLM

### Project Creation Wizard
- `POST /api/wizard/plan` -- generate a project plan (git repo + task DAG) from a description
- `POST /api/wizard/create` -- execute a wizard plan (create repo, scaffold, seed tasks)

### LLM Providers
- `GET /api/providers` -- all providers + key status
- `GET/POST /api/provider` -- get/set active provider; POST can register new providers

### Config (all persist to config.json)
- `GET/POST /api/managed-projects` -- live update managed_projects and paused_projects
- `GET/POST /api/max-agents` `/api/quota-limit` `/api/pi-subagents` `/api/mcp-servers`
- `GET/POST /api/qa-max-cycles` -- get/set max QA requeue cycles (default 3)
- `GET/POST /api/strategy` `/api/strategies`
- `GET/POST /api/thinking` -- enable/disable thinking budget (MiniMax: budget controls whether thinking blocks appear in response)
- `GET /api/auto-replan` -- list projects with auto-replan enabled
- `POST /api/auto-replan/<name>` -- toggle auto-replan for a project (persists to config.json)

### Project Actions
- `POST /api/projects/<name>/replan` -- immediately spawn a `project_plan` task for a project

### Dependencies & Integrity
- `GET /api/dependencies` `/api/dependencies/dot` `/api/dependencies/ready` `/api/dependencies/execution-order`
- `GET /api/dependencies/integrity` -- live integrity report (orphans, dep violations, state ownership)
- `POST /api/dependencies/integrity` -- trigger integrity repair actions
- `GET /api/task-lookup/<task_id>` -- resolve task by partial ID or description match

## Known Test Failures

As of the open-source release these tests fail and are tracked as bugs -- do not introduce further regressions in these areas:

- `test_api.py::TestDependencies::test_ready_tasks_honor_task_history_completed_dependencies`
- `test_closure_status.py::test_closure_status_green_when_required_gates_pass`
- `test_closure_verification.py::test_resolve_godot_command_rewrites_legacy_gut`
- `test_orchestrator.py::TestPostTaskValidation::test_main_scene_startup_script_error_fails_validation`
- `test_script_generation.py::TestProjectTypeDetection::test_godot_path_is_embedded_in_generated_prompts`

These two are network-flaky and allowed to fail in CI:
- `test_web_search_fetch.py::TestWebSearch::test_web_search_returns_false_on_network_error_without_raising`
- `test_web_search_fetch.py::TestWebSearch::test_duckduckgo_fallback_when_no_api_keys`

## Key Constraints

- Never exceed `MAX_ACTIVE_AGENTS`
- `PAUSED_PROJECTS` get no work assignments (and no recovery tasks)
- `MANAGED_PROJECTS` (if non-empty) restricts which projects receive work
- With `lock_project: true`, `db.project_set_locked()` gates concurrent access
- Task dependencies must be acyclic; `swarm/dependencies.py` validates this
- Agents use `[TOOL_CALL]{...}[/TOOL_CALL]` format; the runtime also accepts `<tool_call>` tags
- Write one file at a time in prompts to avoid LLM truncation
- Never kill/restart the server process from within an agent -- use task tools only
- Shell variable interpolation fails in JSON curl -- hardcode task IDs in dependency arrays

## Post-task Validation

After a successful run, `_post_task_validation_in_worktree()` runs synchronously in the monitor thread (blocking). This can delay the monitor by up to ~5 minutes when GUT tests run. GUT tests only run when `addons/gut/` exists AND has a complete install (checked via `_gut_installation_complete()` -- requires `gut_loader.gd`):

Project type is auto-detected by `_detect_project_type()` in `validation.py` from file signatures:

| Type | Detection | Validation |
|------|-----------|------------|
| `godot` | `project.godot` | `godot --headless --script res://check_scripts.gd`; GUT tests if `addons/gut/` present |
| `python` | `*.py` / `pyproject.toml` / `requirements.txt` | `python -m py_compile *.py`; pytest if `.venv/bin/pytest` exists |
| `swift` | `Package.swift` or `*.xcodeproj` | `swiftc -parse` on all `.swift` files |
| `unity` | `Assets/` + `ProjectSettings/` | `mcs` (Unity bundled or system) on all `.cs` files |
| `rust` | `Cargo.toml` | `cargo check` |
| `csharp` | `*.csproj` / `*.sln` | `mcs` on all `.cs` files |
| `typescript` | `package.json` + `*.ts` | `tsc --noEmit` |

Agents use generic prompts (no language-specific prompt files for Swift/Unity/Rust/etc.) — the task description carries enough context. The Godot-specific parts of prompts are ignored for non-Godot projects.

Failure → `_spawn_validation_bug_task()` creates a priority-100 bug task.

## Auto-QA

After every `QA_AUTO_THRESHOLD` (default: 8) successful task completions on a Godot project, `_finish_agent()` automatically creates a `qa` task (priority 75). Detection uses `project.godot` presence -- Python/other projects are never auto-QA'd. Won't double-spawn if a QA task is already pending or in-progress for that project.

After every `AUDIT_AUTO_THRESHOLD` (default: 20) completions, an `audit` task is similarly auto-spawned.

### QA Cycle Cap

QA agents may requeue themselves after finding bugs (each requeue depends on the spawned bug tasks). To prevent infinite loops, `qa_requeue_self()` in `agent_runtime.py` tracks the current cycle (`QA_CYCLE`) and refuses to requeue once `QA_MAX_CYCLES` is reached (default: 3). On the final cycle, the agent writes `QA_REPORT.md` listing any remaining known issues instead of requeuing.

- `QA_CYCLE` is stored in task `metadata.qa_cycle` and incremented each requeue
- `QA_MAX_CYCLES` is read from `config.json` (`qa_max_cycles`, default 3) at wrapper generation time
- Configurable live via `POST /api/qa-max-cycles` or the **QA cycles max** input in the dashboard spawn bar
- The QA prompt displays the current cycle and warns the agent on its final cycle

### QA Bug Task Dependencies

When QA finds multiple bugs, it performs **file ownership analysis** before creating tasks:
1. Lists which files each bug fix will likely touch
2. Chains any two bugs that share a file (second depends on first) -- prevents merge conflicts from parallel agents
3. Applies system-level grouping as a secondary check (same-system bugs chained even if different files)

This mirrors the dependency analysis in `project_plan.yaml`.

## Loop Stall Detection

`agent_runtime.main()` tracks the last 3 single-tool-call loops in `_stall_deque`. If all 3 are identical (same tool + same args), a redirect message is injected into the conversation before the next LLM call and the deque is cleared. Only fires once per stall; suppressed if a wrap-up warning is already active.

## Token Tracking

`call_llm()` returns `(text: str, tokens: dict)`. `main()` accumulates `total_input_tokens` / `total_output_tokens` and writes them to `data/agent_<task_id>_tokens.json` on exit. `_finish_agent()` reads this file, stores the values in the `agents` DB table (`input_tokens`, `output_tokens` columns), and deletes the file. Dashboard displays per-agent token counts and a project-level total.

## QA Agent

QA tasks (`type: "qa"`) launch a vision-capable agent that:
1. Launches the game via `launch_game()`
2. Reads `GAME_DESIGN.md` to derive a test plan
3. Interacts with the game using screenshots + `vision_query` + `click_element`
4. Reads live state from the in-game `StateServer` (TCP port 11009) via `get_game_state()`
5. Creates bug tasks for every deviation from the design doc, then requeuing itself via `requeue_self()`

The `StateServer` autoload (`autoload/state_server.gd`) must be present in the Godot project and registered in `project.godot`. It listens on port 11009.

Vision tools live in `swarm/qa_tools.py` and `swarm/vision.py`. The vision backend is configured via `vision_provider` and `vision_providers` in `config.json`.

### StateServer commands

| Command | Response |
|---------|----------|
| `{"command":"state"}` | Full game state (see shape below) |
| `{"command":"screenshot_b64"}` | `{"image_base64":"<png>"}` |
| `{"command":"input","type":"click","x":N,"y":N}` | Inject mouse click |
| `{"command":"input","type":"action","action":"ui_accept"}` | Inject Godot action |
| `{"command":"press_button","id":"..."}` | Find button by node name **or `qa_label` metadata**, fire it |
| `{"command":"a11y_tree"}` | Flat list of all visible interactive UI elements |

### StateServer `state` response shape

```json
{
  "timestamp": 1712345678.0,
  "scene_tree": {
    "name": "Main", "type": "Node2D", "path": "/root/Main",
    "visible": true, "position": [0, 0],
    "children": [
      {
        "name": "StartButton", "type": "Button", "qa_label": "start",
        "visible": true, "position": [400, 300],
        "bounds": [375, 285, 150, 50]
      }
    ]
  },
  "game_state": {}
}
```

`scene_tree` is a **full node hierarchy** (not type counts). Every node has `name`, `type`, `path`. CanvasItem nodes add `visible`. Node2D adds `position` (global). Control nodes add `position` (local) and `bounds` (global rect `[x,y,w,h]`). Nodes tagged with `node.set_meta("qa_label", "start")` expose `qa_label`.

`game_state` is populated only if the root scene implements `get_game_state() -> Dictionary`.

### `a11y_tree` response shape

```json
{
  "a11y_tree": [
    {"role": "button", "label": "Start Game", "path": "/root/Main/StartButton", "bounds": [375, 285, 150, 50], "visible": true},
    {"role": "label",  "label": "Score: 0",   "path": "/root/Main/ScoreLabel",  "bounds": [10, 10, 100, 20],  "visible": true}
  ]
}
```

Roles: `button` (any BaseButton subclass), `label`, `input`, `progressbar`, `slider`, `listbox`, `widget`. Label priority: `qa_label` metadata → `node.text` → `node.name`.

### `click_element` grounding behaviour

`click_element(image_path, element_description)` uses a **grounding-first** approach:
1. Queries `a11y_tree` from StateServer
2. Finds element by case-insensitive partial label match (checks both `label` and `qa_label`)
3. Presses it via `press_button` using the element's scene path -- **no VLM coordinate prediction**
4. Falls back to color detection, then VLM coordinates only if StateServer is unavailable or element not found

### `vision_query` timeout

`vision_query(image_path, question, model_tier="fast", timeout=30)` -- the `timeout` parameter (default 30s) caps the VLM call. Returns `{"ok": false, "error": "vision_query timed out after 30s"}` on timeout.

### StateServer click injection

`StateServer` injects mouse clicks via `Input.parse_input_event` -- **not** via `DisplayServer.window_move_to_foreground()` or `Window.grab_focus()`. Those calls require macOS window focus and silently no-op when the screen is locked.

**Never add focus calls to the click path.** Action/keyboard events work regardless of screen lock.

### What game projects must implement for QA to work well

**Minimum (required for any structured state):**
- Register `StateServer` autoload in `project.godot`: `StateServer="res://autoload/state_server.gd"`
- Copy `state_server.gd` from `templates/godot/autoload/` -- never write from scratch

**Recommended (for meaningful state reads):**
- Implement `get_game_state() -> Dictionary` on the root scene node. Return the fields the QA agent needs to verify game logic: scores, lives, level, active scene, player state, etc.

**Optional (enables label-based button clicking without coordinates):**
- Tag interactive nodes: `$PlayButton.set_meta("qa_label", "play")` -- then `press_button{"id":"play"}` works regardless of internal node name. Use short lowercase labels.

**For `harness_qa` mode (deterministic checkpoint testing):**
- Also register `TestHarness` autoload: `TestHarness="res://autoload/test_harness.gd"`
- Call `await TestHarness.checkpoint(state_dict)` at each stable game state
- State dict should include all fields needed to validate correctness at that point

## Godot Project Templates

Canonical Godot support files live in `templates/godot/`. Every new Godot project created by `project_create` should copy from here -- never write these from scratch.

```
templates/godot/
  autoload/
    state_server.gd     -- TCP state/screenshot/input server for QA agents (port 11009)
    test_harness.gd     -- Automated test harness for harness_qa agents
  addons/gut/           -- GUT test framework (copy into new projects)
  check_scripts.gd      -- Headless script validator (run with --headless --quit)
  test/unit/            -- Example GUT test structure
```

### Keeping templates in sync

When a fix is applied to `state_server.gd` or `test_harness.gd` in any game project, **also update `templates/godot/autoload/`** so new projects get the correct version. The template is the source of truth.

When the template is updated, existing game projects need their local copies replaced. To sync all managed projects:
```bash
cp templates/godot/autoload/state_server.gd /path/to/project/autoload/state_server.gd
cp templates/godot/autoload/test_harness.gd /path/to/project/autoload/test_harness.gd
```

To check all game projects for stale click injection code:
```bash
grep -rl "window_move_to_foreground\|grab_focus" /path/to/workspace --include="*.gd"
```

## Art Pass Agent

`art_pass` tasks are vision-capable agents that improve game visuals by:
1. Assessing current visual state via screenshot + vision model
2. Browsing the configured asset library
3. Copying relevant assets into the project and wiring them up
4. Verifying improvements with screenshots before committing

Has full write access (unlike QA) -- commits and pushes changes. Uses vision tools from the QA pipeline. Focused on placeholder replacement, icon integration, UI polish, and asset wiring. Configure local asset paths outside this repo, for example in `config.json` or task-specific instructions.

Configure your asset library paths in `config.json` or task-specific instructions. See `prompts/art_pass.yaml` for the expected structure.

## RAG (Retrieval-Augmented Generation) -- Optional Experimental

RAG gives agents code-aware documentation context from an operator-supplied
local vector index. It is opt-in and disabled by default. See
`docs/rag.md` for current setup notes.

## MCP Integration


```json
{
  "mcp_servers": {
    "godot": {
      "command": "$(which godot-mcp)",
      "env": { "GODOT_PROJECT_PATH": "/path/to/project" }
    }
  }
}
```

`MCPClient` in `swarm/mcp_client.py` manages subprocess lifecycle for MCP servers.

## Godot Validation Command

```
godot --headless --path <project_path> --script res://check_scripts.gd --quit
```

Use the `godot_path` config key to set the exact binary path if `godot` is not on `PATH` (required on macOS where it is typically `/Applications/Godot.app/Contents/MacOS/Godot` or `/opt/homebrew/bin/godot`).

## Auto-Replan

When a project's task queue empties and it is listed in `auto_replan_projects` (config.json), `fill_slots()` automatically spawns a `project_plan` task. The planner reads `GAME_DESIGN.md` and the existing codebase, then creates a full dependency-ordered task set via the API.

- Only fires for Godot projects (requires `project.godot` + `GAME_DESIGN.md`)
- Only fires when auto mode is ON
- Toggle via dashboard **♻ Auto** button per project, or `POST /api/auto-replan/<name>`
- Read current state: `GET /api/auto-replan`
- Force immediate replan: `POST /api/projects/<name>/replan`
- Persisted in `config.json` as `auto_replan_projects: [...]`

## Rate Limit Mitigations (MiniMax)

MiniMax rate limits are RPM-based (~50 req/min). Measures in place:

- **Jitter** (`agent_runtime.py`): `time.sleep(random.uniform(0.5, 3.0))` before every LLM call -- spreads requests across the minute window
- **Retry backoff**: 7 attempts with `[10, 30, 60, 120, 240]` second delays
- **Context compaction**: when estimated conversation tokens exceed `COMPACT_TOKEN_THRESHOLD = 120_000` (~80k buffer before MiniMax's 200k window), the middle of the conversation is summarised via a separate LLM call, keeping only system prompt + 4-message tail. Prevents token bloat on long-running agents. Token estimate is `sum(len(content)) // 2` (2 chars/token approximation).
- **Prompt caching**: MiniMax caches automatically at 512+ token threshold (90% cost discount on cache hits). No code changes needed. Cache hits logged as `[LLM] cache read=N write=N`.
- **Thinking budget**: set to `0` by default -- MiniMax M2.7 reasons internally regardless; budget only controls whether thinking *blocks* appear in the response. Enabling it (5000+) massively increases output tokens and worsens rate limits.
- **Fallback providers**: `fallback_providers` config allows automatic failover if the primary provider rate-limits or errors

## Project Registry Persistence

The project registry is in-memory and resets on server restart. `create_app()` runs a startup scan loop:

```python
for proj in config.get("managed_projects", []):
    if proj_path.exists() and not project_registry.get(proj):
        project_registry.add_project(proj)
        files = project_registry.scan_project_files(proj, exts, ignore)
        project_registry.update_file_counts(proj, files)
```

This ensures all managed projects always appear in the dashboard after a restart, even if they were never explicitly added via the API.

## Manager Chat

`POST /api/chat` accepts a conversational message and returns an action response. The manager agent (`prompts/manager.yaml`) can:
- Create, kill, reset, and query tasks
- Toggle auto-mode and spawn agents
- Answer questions about current system state

`POST /api/project-chat` scopes the conversation to a single project.

`_build_state_snapshot()` in `api_chat.py` assembles a full snapshot of tasks, agents, and projects to give the chat LLM context.

## Project Creation Wizard

`POST /api/wizard/plan` -- given a project name, type, and description, returns a plan object containing:
- A proposed git repo structure
- A task DAG (list of tasks with `depends_on` indices)
- Bootstrap scaffold steps

`POST /api/wizard/create` -- executes a plan:
1. Creates the git repo and pushes to Gitea
2. Installs canonical Godot bootstrap scaffolding from templates
3. Seeds the task DAG via `/api/tasks/batch`
4. Registers the project with the swarm

DAG structure is preserved (not flattened to a genesis anchor) -- the full dependency graph is created as-is.
