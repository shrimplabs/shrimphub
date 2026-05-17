# Swarm Controller

An agent orchestration system that spawns LLM-powered subprocesses to build, refactor, and maintain multiple code projects. Supports Godot, Python, and TypeScript projects out of the box. Configurable LLM providers include Minimax, Claude, OpenRouter, Kimi, and custom OpenAI-compatible or Anthropic-compatible endpoints.

## Features

- **Multi-provider LLM** — Minimax, Claude (direct), OpenRouter, Kimi, or any custom endpoint
- **SQLite state** — thread-safe storage with WAL mode; no more JSON race conditions
- **Task retry** — failed tasks automatically retry up to 3× with failure context in the prompt
- **Recovery tasks** — when retries are exhausted, a recovery agent is spawned with full failure history; dependents are reparented automatically
- **Dependency self-healing** — if a dep task is pruned as failed, dependent tasks unblock automatically rather than stalling forever
- **Continuation reparenting** — when an agent hits the loop/context limit and spawns a continuation, all downstream deps are automatically rerouted to the continuation task
- **Validation bug reparenting** — post-task validation failures spawn a bug task that slots into the dependency chain; downstream tasks wait for the bug fix, not the completed original
- **Dependency violation checker** — monitor kills any agent running a task whose deps aren't actually met (catches both live and pre-restart agents)
- **Real-time log streaming** — live agent output via Server-Sent Events in the dashboard
- **Per-project health metrics** — score, task history, avg file size, last commit age
- **Post-task validation** — Godot and Python projects validated after every agent run; failures spawn auto bug tasks
- **Agent result diffing** — `git diff --stat` captured after each agent and shown in the dashboard
- **Parallel execution** — multiple agents per project with file-level locking; task planner generates proper DAG (not just chains)
- **Project creation DAG preservation** — project-chat and wizard creation preserve explicit intra-batch dependencies and only attach true batch roots to the project head/genesis anchor
- **MCP integration** — connect any MCP server (e.g. godot-mcp) for extended tooling
- **Task dependencies** — DAG with cycle detection and parallel execution levels
- **Auto mode** — continuously fill agent slots, stop when quota exceeded or queue empty
- **Project repair** — one-click repair of broken projects from the dashboard (reset failed/orphaned tasks, resurrect missing deps from history)
- **Dashboard manager chat** — conversational interface that can create tasks, kill agents, toggle auto mode, and restart the server
- **QA cycle cap** — QA agents stop requeuing after a configurable max (default 3); prevents infinite QA loops; adjustable live from the dashboard
- **QA file ownership analysis** — QA bug tasks are dependency-chained by file ownership before creation, preventing merge conflicts from parallel bug agents
- **Context compaction meter** — agent cards show a live progress bar indicating conversation size vs. compaction threshold

## Prerequisites

- Python 3.10+
- At least one LLM API key (see [LLM Providers](#llm-providers))
- Projects in a directory of your choice (`workspace` in `config.json`)

## Quick Start

```bash
git clone <repo-url>
cd swarm-controller
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
python -m pip install -r requirements.txt

# Create a local config (workspace path is the main required setting)
cp config.example.json config.json

# Add your API key(s) to .env
cp .env.example .env
# then edit .env and fill in your key(s)

# Start the API server + dashboard
python swarm_runner.py api
```

Open **http://localhost:5001**

The controller works fine as a local-only checkout. Remote repo provisioning is
optional and disabled by default in the example config.

## Platform Support

- **macOS**: primary development platform; full controller and QA feature set
- **Linux**: core controller/runtime flows should work, but GUI-oriented QA paths are less exercised
- **Windows**: core controller/setup flows are being made platform-aware, but end-to-end support is not fully validated yet

Current Windows limitations:
- macOS-only QA fallbacks that rely on `osascript`, `screencapture`, or related tooling are unavailable
- Godot must be discoverable through `PATH`, `GODOT_PATH`, or the controller `config.json` field `"godot_path"`
- final validation still needs a real Windows machine or CI runner

For project agents that need Godot, prefer setting the controller-level path instead of letting agents infer it. Add the executable path to `config.json`, for example:

```json
{
  "godot_path": "C:\\Program Files\\Godot\\Godot_v4.6.2-stable_win64_console.exe"
}
```

On Windows, prefer the `_console.exe` binary for controller automation and headless validation. On macOS/Linux this can be an absolute path such as `/Applications/Godot.app/Contents/MacOS/Godot` or `/usr/local/bin/godot`. Spawned agents inherit the resolved path as `GODOT_PATH`; if it is missing, prompts instruct agents to report a controller configuration blocker.

## Architecture Notes

- [Controller Integrity Model](docs/controller_integrity_model.md) — controller invariants, branch continuity rules, and state ownership for live task orchestration
- [Controller Integrity Health Model](docs/controller_integrity_health_model.md) — how to read integrity diagnostics, dashboard signals, and repair actions
- [Controller Module Boundaries](docs/controller_module_boundaries.md) — canonical homes for invariants, maintenance domains, diagnostics, and dashboard concerns
- [Controller Delegation Model](docs/controller_delegation_model.md) — contract for helper delegation, child-task delegation, file-scope safety, and parent lifecycle semantics
- [Legacy Project Migration Guide](docs/legacy_project_migration_guide.md) — step-by-step process for normalizing older projects whose historical/live state predates current controller invariants
- [Optional RAG Integration](docs/rag.md) — configuration notes for the opt-in documentation retrieval tool
- [Agent-Ops Helper Docs](docs/agent-ops/SKILLS.md) — optional AI-agent operation recipes, separate from normal user-facing setup
- [Open-Source Checklist](docs/open_source_checklist.md) — concrete pre-release OSS work items and doc-audit targets
- [Release Checklist](docs/release_checklist.md) — pre-publish source hygiene and validation checklist

## Configuration

Create `config.json` in the project root (gitignored). All fields are optional.

```json
{
  "workspace": "~/path/to/your/projects",
  "managed_projects": ["project-a", "project-b"],
  "paused_projects": [],
  "max_active_agents": 3,
  "max_lines": 5000,
  "lock_project": false,
  "agent_timeout": 7200,
  "quota_limit_percent": 90,
  "llm_provider": "minimax",
  "task_selection_strategy": "priority",
  "mcp_servers": {}
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `workspace` | `~/workspace` | Root directory containing your projects |
| `managed_projects` | `[]` | Project folder names to assign work to |
| `paused_projects` | `[]` | Projects that receive no work |
| `max_active_agents` | `3` | Max concurrent agent subprocesses |
| `max_lines` | `5000` | Line count that triggers an auto-refactor task |
| `lock_project` | `false` | `true` = one agent per project; `false` = parallel |
| `agent_timeout` | `7200` | Wall-clock seconds before an agent is considered hung (safety net — the loop limit in agent_runtime is the primary governor) |
| `quota_limit_percent` | `90` | Stop spawning when API quota exceeds this % |
| `llm_provider` | `"minimax"` | Active LLM provider (see below) |
| `task_selection_strategy` | `"priority"` | How to pick the next task |
| `qa_max_cycles` | `3` | Max times a QA agent may requeue itself before writing a final report and stopping |

> **Note:** `managed_projects` and other settings can be updated live via `POST /api/managed-projects` without restarting the server. Changes persist to `config.json` automatically.
>
> **Optional remote repo provisioning:** the controller can create/push new project
> repos if you configure `gitea_host`, `gitea_org`, `gitea_user`, and
> `gitea_pass`, but these are not required for local use and are omitted from
> the default example config on purpose.

## Security

> ⚠️ **By default the API has no authentication.** It is accessible to anyone on your network. For local-only use this is fine; if you expose the server beyond localhost, enable auth.

To enable authentication, add `"login_required": true` to `config.json`. The default credentials are then `admin` / `admin`.

### Setting a strong password

Generate a salted password hash before enabling auth (never commit `config.json` — it is gitignored):

```bash
python - <<'PY'
from swarm.login import hash_password_for_storage

password = input("New password: ")
password_hash, salt = hash_password_for_storage(password)
print(f'"login_password_hash": "{password_hash}",')
print(f'"login_salt": "{salt}"')
PY
```

Add the output plus `"login_required": true` to `config.json`:

```json
{
  "login_required": true,
  "login_username": "admin",
  "login_password_hash": "paste-generated-hash-here",
  "login_salt": "paste-generated-salt-here"
}
```

On restart the server will require login and verify against the salted hash. Session tokens are stored in memory and transmitted over plain HTTP; TLS termination is the operator's responsibility.

## LLM Providers

The active provider is set via `llm_provider` in `config.json` or from the dashboard provider bar.

### Built-in providers

| Provider | Format | Env var | Default model |
|----------|--------|---------|---------------|
| `minimax` | Anthropic-compatible Bearer | `MINIMAX_API_KEY` | `MiniMax-M2.7` |
| `claude` | Native Anthropic API | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` |
| `openrouter` | OpenAI chat completions | `OPENROUTER_API_KEY` | `anthropic/claude-3.5-sonnet` |
| `kimi` | Anthropic-compatible Bearer | `KIMI_API_KEY` | `k2p5` |

### Overriding a model

```json
{
  "llm_provider": "openrouter",
  "llm_providers": {
    "openrouter": { "model": "google/gemini-2.0-flash-exp" }
  }
}
```

### Registering a custom provider

POST to `/api/provider`:

```json
{
  "provider": "my-local",
  "base_url": "http://localhost:11434/v1",
  "api_key_env": "OLLAMA_KEY",
  "format": "openai",
  "model": "llama3.2",
  "max_tokens": 4096
}
```

`format` is one of `anthropic` (Bearer + Anthropic body), `anthropic_native` (x-api-key header), or `openai` (chat completions).

## Task Types

| Type | Priority | Description |
|------|----------|-------------|
| `refactor` | 100 | Split oversized files to under `max_lines` |
| `bug` | 80 | Find and fix bugs |
| `feature` | 50 | Implement new features |
| `polish` | 50 | Improve existing UI/code |

### Task format

Tasks live in the SQLite database (`data/swarm.db`). Add them via the API or by posting to `/api/tasks`:

```json
{
  "id": "unique-task-id",
  "project": "project-name",
  "type": "feature",
  "description": "What to do",
  "priority": 80,
  "status": "pending",
  "dependencies": [],
  "max_attempts": 3
}
```

`max_attempts` defaults to 3. Failed tasks automatically reset to `pending` with the failure reason prepended to the prompt on retry. After all attempts are exhausted, a recovery task is created with full failure context and dependents are reparented to it.

### Dependency graphs

Dependencies form a DAG. The task planner generates parallel-friendly graphs — independent tasks (e.g. player system, enemy system, item system) fan out from a shared foundation and converge at an integration task. A dep is considered "met" if it is completed, or if it no longer exists in the DB (self-healing: chains don't stall when a dep is pruned as failed).

## API Reference

### Projects

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/projects` | List all projects |
| POST | `/api/projects` | Add a project |
| GET | `/api/projects/<name>` | Get project details |
| PUT | `/api/projects/<name>` | Update status/locked |
| POST | `/api/projects/<name>/scan` | Scan file sizes |
| GET | `/api/projects/<name>/health` | Health metrics |
| POST | `/api/projects/<name>/repair` | Surgical repair: reset failed/orphaned tasks, resurrect missing deps |
| POST | `/api/projects/<name>/restart` | Nuclear reset: all tasks → pending, attempts=0 |
| GET | `/api/projects/<name>/locks` | File-level locks |
| POST | `/api/projects/<name>/lock` | Lock a file |
| POST | `/api/projects/<name>/unlock` | Unlock a file |
| POST | `/api/projects/<name>/spawn` | Spawn parallel agents |

### Tasks

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/tasks` | List all tasks |
| POST | `/api/tasks` | Add a task |
| GET | `/api/tasks/<id>` | Get task |
| PUT/PATCH | `/api/tasks/<id>` | Update task — merge semantics, only fields present in body are touched |
| DELETE | `/api/tasks/<id>` | Delete task |
| GET | `/api/tasks/<id>/dependencies` | Read dependency list |
| PUT | `/api/tasks/<id>/dependencies` | Replace full dep list `{"dependencies": [...]}` |
| POST | `/api/tasks/<id>/dependencies` | Add deps without replacing existing — `{"dependency": "<id>"}` or `{"dependencies": [...]}` |
| DELETE | `/api/tasks/<id>/dependencies/<dep_id>` | Remove a single dep edge |
| POST | `/api/tasks/<id>/reset` | Reset failed task to pending (clear attempts, add note) |
| POST | `/api/tasks/batch` | Create multiple tasks in one call — see below |

#### Batch task creation

The batch endpoint is the recommended way to seed a project's task queue. It generates all IDs upfront so you can wire a full dependency DAG in a single request — no follow-up calls needed.

```json
POST /api/tasks/batch
{
  "project": "my-project",
  "tasks": [
    {"type": "bug",     "description": "Fix login crash",           "priority": 80},
    {"type": "bug",     "description": "Fix session expiry",        "priority": 80},
    {"type": "feature", "description": "Add OAuth (needs both fixes above)", "priority": 50, "depends_on": [0, 1]},
    {"type": "feature", "description": "Add profile page (after OAuth)",     "priority": 50, "depends_on": [2]}
  ]
}
```

Response:
```json
{
  "created": 4,
  "ids": ["bug-123-000...", "bug-123-001...", "feature-123-002...", "feature-123-003..."],
  "id_map": {"0": "bug-123-000...", "1": "bug-123-001...", "2": "feature-123-002...", "3": "feature-123-003..."}
}
```

Key fields:
- **`depends_on`** — list of integer indices into the `tasks` array; resolved to actual IDs before any task is created
- **`dependencies`** — explicit task ID strings (merged with `depends_on`)
- **`project`** — top-level default applied to all items; per-item `project` overrides it
- **`chain: true`** — automatically chains each task to depend on the previous one (linear sequence)
- `id_map` in the response lets you reference generated IDs for any follow-up calls

### Agents

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/agents` | List all agents |
| GET | `/api/agents/<id>` | Get agent |
| GET | `/api/agents/<id>/output` | Get log output |
| GET | `/api/agents/<id>/stream` | SSE stream of live output |
| POST | `/api/agents/<id>/kill` | Kill agent process |
| GET | `/api/agents/active` | Active count |
| GET | `/api/history` | Completed agent history |
| POST | `/api/history/<agent_id>/requeue` | Resurrect a task from agent/task history |

### Spawning & Control

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/spawn` | Spawn agent for a project |
| POST | `/api/spawn-batch` | Fill all available slots |
| GET/POST | `/api/auto-mode` | Get/set auto mode (`{enabled, suspended_for_quota}`) |
| POST | `/api/rescan` | Rescan all project file sizes |
| GET | `/api/health` | Server health: monitor alive, lag, uptime |

### Configuration

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET/POST | `/api/max-agents` | Agent cap |
| GET/POST | `/api/quota-limit` | Quota stop threshold |
| GET/POST | `/api/qa-max-cycles` | QA requeue cycle cap (default 3) |
| GET/POST | `/api/managed-projects` | Live update managed/paused project lists (persists to config.json) |
| GET/POST | `/api/pi-subagents` | Enable pi subagents (experimental) |
| GET/POST | `/api/mcp-servers` | MCP server config |
| GET | `/api/providers` | List LLM providers |
| GET/POST | `/api/provider` | Get/set active provider |
| GET/POST | `/api/strategy` | Task selection strategy |
| GET | `/api/strategies` | List available strategies |

### Quota & Dependencies

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/quota` | Raw API quota data |
| GET | `/api/quota-limit` | Quota limit status |
| GET | `/api/dependencies` | Task dependency graph stats |
| GET | `/api/dependencies/dot` | DOT graph for visualisation |
| GET | `/api/dependencies/ready` | Tasks ready to execute |
| GET | `/api/dependencies/execution-order` | Tasks in level order |

## Dashboard

Open **http://localhost:5001** — auto-refreshes every 5 seconds.

- **Provider bar** — switch LLM provider and model; shows API key status
- **Quota meter** — live API usage bar
- **Agent cards** — click any agent to open the log panel; active agents stream live via SSE
- **Kill Agent** — inside the log panel for running agents; harder to hit by accident
- **Kill All** — in the spawn bar with confirm dialog
- **Health bars** — per-project score, task pass/fail counts, last commit age
- **Diff stat** — shows `git diff --stat` summary for completed agents
- **Spawn bar** — set agent cap, spawn N agents, run all, enable Auto mode; **QA cycles max** input to adjust the QA requeue cap live
- **Auto mode** — turns amber and shows "Paused (quota)" when the API limit is hit; resumes automatically when quota resets
- **Loop counter** — active agent cards show current loop number (e.g. `Loop: 42/200`)
- **Context compaction meter** — thin progress bar on agent cards showing conversation size vs. the provider-specific compaction threshold; green → yellow → red
- **🔧 Repair** — on project cards with tasks; surgically fixes broken state (reset failed/orphaned tasks, resurrect missing dep tasks from history)
- **↺ Restart** — nuclear option; resets all tasks for a project to pending with attempts=0
- **↺ Reset** — on individual failed task cards; reset a single task to pending
- **↺ Re-queue** — on history cards with non-zero exit; resurrects the task from history
- **New Project** — conversational wizard that plans a full task graph with parallel dependencies
- **Manager Chat** — talk to the swarm manager; can create tasks, kill agents, toggle auto mode, restart the server
- **History** — collapsed by default; shows all archived agent runs

## Architecture

```
swarm_runner.py          — thin entry point + generate_task_script()
swarm/
  api.py                 — Flask app, all routes, monitor thread
  orchestrator.py        — spawn/monitor/fill/quota/validation/recovery logic
  agent_runtime.py       — all agent tool functions + LLM call loop (200-loop limit)
  db.py                  — SQLite storage layer (WAL, thread-local)
  tasks.py               — Task dataclass + SQLiteTaskSource
  projects.py            — Project dataclass + SQLiteProjectRegistry
  agents.py              — Agent dataclass + SQLiteAgentTracker
  strategies.py          — Task selection strategies
  dependencies.py        — DAG + cycle detection
data/
  swarm.db               — SQLite database (tasks, projects, agents)
  agent-history.jsonl    — Archived completed agent records
  task-history.jsonl     — Archived completed/failed task records
  agent_<id>.log         — Per-agent execution log
```

### Agent lifecycle

1. `orchestrator.fill_slots()` picks next task from SQLite (checks deps, paused/managed projects, locks)
2. `generate_task_script(task)` builds a thin Python wrapper with embedded config and prompts
3. Wrapper is written to `data/agent_<id>.py` and launched as a subprocess
4. Subprocess imports `swarm.agent_runtime`, sets config vars, calls `rt.main()`
5. `rt.main()` runs the tool loop: call LLM → parse `[TOOL_CALL]` → execute tool → repeat (max 200 loops)
6. On completion, orchestrator captures `git diff --stat`, updates DB, runs post-validation in background thread
7. On failure: retries up to `max_attempts` with failure context prepended to the prompt
8. On exhausted retries: recovery task created with full failure history; dependents reparented
9. Finished agent records pruned to `data/agent-history.jsonl`

### Timeout design

Two independent limits apply to every agent:

- **Loop limit** (`MAX_TOOL_LOOPS = 200` in `swarm/constants.py`) — the primary governor; an agent that reaches loop 200 exits cleanly
- **Wall-clock watchdog** (`agent_timeout = 7200s` in config) — safety net for truly hung processes (e.g. blocking system call); should never fire on a working agent

When an agent hits the loop limit it spawns a continuation task and logs `"Continuation task created: <id>"`. The orchestrator detects this and reparents all downstream deps to the continuation, keeping the chain intact.

### Context compaction

When a conversation's estimated token count exceeds the active provider's compaction threshold, `agent_runtime` summarises the middle of the conversation via a separate LLM call and keeps only the system prompt and the 4 most recent messages. This prevents runaway token growth on long tasks without cutting the loop limit.

### Post-task validation

After a successful agent run, the orchestrator validates the project (runs in a daemon thread, non-blocking):

- **Godot**: runs `godot --headless --script res://check_scripts.gd`; also scans output for `ERROR:` / `SCRIPT ERROR:` even on exit 0
- **Python**: prefers project-local `pytest` when tests exist, otherwise falls back to `python -m py_compile`

Validation failure auto-creates a priority-100 bug task for the next agent to fix.

## MCP Integration

```json
{
  "mcp_servers": {
    "godot": {
      "command": "godot-mcp",
      "env": { "GODOT_PROJECT_PATH": "/path/to/project" }
    }
  }
}
```

Agent tools: `mcp_list_tools(server)`, `mcp_call_tool(server, tool_name, args)`

## Task Selection Strategies

| Strategy | Description |
|----------|-------------|
| `refactor_first` | Refactor tasks always first, then by priority |
| `priority` | Strict priority order |
| `round_robin` | Cycle through projects evenly |
| `dependency_aware` | Prioritise tasks whose dependencies are complete |
| `least_recently_worked` | Pick the project not touched longest |

## Data Files

| Path | Description |
|------|-------------|
| `data/swarm.db` | Primary SQLite database |
| `data/agent-history.jsonl` | Archived completed agents (one JSON per line) |
| `data/task-history.jsonl` | Archived completed/failed tasks (used by repair/requeue) |
| `data/agent_<id>.log` | Live agent output log |
| `data/agent_<id>.py` | Generated agent wrapper script (deleted on completion) |
| `config.json` | Runtime configuration (gitignored) |
| `.env` | API keys (gitignored) |

## Godot Project Templates

Reusable Godot support files live in `templates/godot/`. The `project_create` agent copies these into every new project — they should never be written from scratch.

```
templates/godot/
  autoload/
    state_server.gd     — TCP server on port 11009: state snapshots, screenshots, input injection
    test_harness.gd     — Automated test harness used by harness_qa agents
  check_scripts.gd      — Headless GDScript validator
  icon.svg              — Portable default project icon
  test/unit/            — Example GUT test structure
```

GUT is treated as an external dependency, not vendored in this repository. The
controller installs pinned GUT releases into a local cache on demand, then
copies from that cache into Godot projects during bootstrap.

### StateServer

`state_server.gd` gives QA agents a live window into the running game. Register it as an autoload in `project.godot` and implement `get_game_state() -> Dictionary` in the parent scene for domain-specific state. Supported commands over TCP:

| Command | Response |
|---------|----------|
| `{"command":"state"}` | JSON game state snapshot |
| `{"command":"screenshot_b64"}` | `{"image_base64":"<png>"}` |
| `{"command":"input","type":"click","x":N,"y":N}` | Inject mouse click |
| `{"command":"input","type":"action","action":"ui_accept"}` | Inject Godot action |
| `{"command":"press_button","text":"Start"}` | Emit pressed signal on named button |

**Important:** Click injection uses `Input.parse_input_event` directly — never `DisplayServer.window_move_to_foreground()` or `Window.grab_focus()`. Those calls require macOS window focus and silently fail when the screen is locked, causing false-positive "button unresponsive" bug reports.

### Keeping templates in sync

When fixing `state_server.gd` or `test_harness.gd` in a game project, update `templates/godot/autoload/` as well so new projects get the fix automatically.

## Testing

```bash
# Install dev dependencies first
pip install -r requirements.txt -r requirements-dev.txt

python -m pytest                              # full suite (excluding dashboard)
python -m pytest tests/test_api.py            # API routes
python -m pytest tests/test_lifecycle.py      # real subprocess spawn/complete/kill
python -m pytest tests/test_fill_slots.py     # scheduling logic + run_after
python -m pytest tests/test_agent_runtime.py  # LLM loop + tools
python -m pytest tests/test_improvements.py   # robustness + recovery
python -m pytest tests/test_chat_actions.py   # manager chat actions

# Dashboard tests require a one-time Playwright browser install:
playwright install chromium
python -m pytest tests/test_dashboard.py
```
