# Agent Profile Plugin System

Swarm Controller's plugin system lets you add new task types — or override how existing ones behave — without touching the core codebase. Everything is declared in a single YAML file.

## What a plugin can do

- Register a **new task type** (e.g. `"lore_pass"`, `"accessibility_audit"`)
- Override the **prompt** used for that task type
- Set a **permission profile** that is enforced at the tool-dispatch layer (not just prompt text)
- **Block or allow specific tools** with a fine-grained list
- Inject **runtime context** into the agent's task description via file reads, shell commands, or HTTP calls

## Creating a plugin

1. Create a `.yaml` file in the `plugins/` directory at the project root.
2. Restart the server — plugins are loaded once at startup.

That's it. The new task type is immediately available via `swarm_create_task(type="your_type")`.

---

## Plugin YAML schema

```yaml
# Required
plugin_id: "lore_pass"           # unique identifier; used in logs
task_type: "lore_pass"           # the task type string agents are spawned with

# Optional metadata
display_name: "Lore Pass"        # shown in logs and the dashboard

# Role family — used by the scheduler for capacity planning
# One of: implementation | qa | research | planning
role: "implementation"

# Permission profile — enforced in the tool-dispatch layer
# See "Permission profiles" below
permission_profile: "full"

# Path to a Jinja2+YAML prompt file (relative to swarm root)
# Must have 'system' and 'user' keys; uses << var >> syntax
# If omitted, the built-in prompt for this task_type is used (if any)
prompt_file: "plugins/lore_pass.yaml"

# Tool allowlist — if non-empty, ONLY these tools are available
# (subject to permission_profile and tools_blocked)
tools_allowed:
  - "read_file"
  - "list_files"
  - "write_file"
  - "git_commit"
  - "git_push"

# Tool blocklist — always denied, even if listed in tools_allowed
tools_blocked:
  - "run_command"
  - "web_search"

# Context providers — injected into the task description before the agent runs
# Evaluated in order; each section appears under a labelled header
context_providers:
  - type: file
    path: "GAME_DESIGN.md"       # relative to the project root
    max_chars: 8000

  - type: command
    command: "git log --oneline -10 2>&1"
    timeout_seconds: 5           # hard cap: 30 seconds
    max_chars: 1000

  - type: http
    url: "http://localhost:8080/api/lore-index"
    max_chars: 4000
```

---

## Fields reference

### Required fields

| Field | Type | Description |
|-------|------|-------------|
| `plugin_id` | string | Unique identifier for this plugin. Used in logs. |
| `task_type` | string | The task type string. Must be unique across all plugins. |

### Optional fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `display_name` | string | same as `plugin_id` | Human-readable name shown in logs and dashboard. |
| `role` | string | `"implementation"` | Role family for scheduler capacity planning. See below. |
| `permission_profile` | string | `"full"` | Pre-defined permission set enforced at dispatch. See below. |
| `prompt_file` | string | _(none)_ | Path to a prompt YAML file, relative to swarm root. |
| `tools_allowed` | list | _(all)_ | If non-empty, only these tools are available (allowlist mode). |
| `tools_blocked` | list | _(none)_ | These tools are always denied. Always wins over `tools_allowed`. |
| `context_providers` | list | _(none)_ | List of data sources to inject into the task description. |

---

## Role families

The `role` field tells the scheduler what kind of agent this is. It is used for capacity planning (e.g. limiting concurrent QA agents) and may gate certain system behaviours.

| Value | Description |
|-------|-------------|
| `implementation` | Writes and commits code. The default. |
| `qa` | Tests the project; generally read-only with limited write access for reports. |
| `research` | Investigates problems; read-only, writes findings to files. |
| `planning` | Creates task graphs; cannot write repo files directly. |

---

## Permission profiles

Permission profiles are enforced in the tool-dispatch layer — the agent cannot call a blocked tool even if the prompt tells it to.

| Profile | Blocked tools |
|---------|---------------|
| `full` | Nothing blocked (beyond `tools_blocked` list) |
| `read_write` | `run_command` |
| `read_only` | `write_file`, `patch_file`, `append_file`, `git_commit`, `git_push`, `run_command`, `create_task`, `create_tasks`, `create_tasks_file_aware` |
| `qa_write` | `patch_file`, `append_file`, `git_commit`, `git_push`, `run_command`, `create_task`, `create_tasks`, `create_tasks_file_aware` |

`tools_blocked` in your plugin spec is applied **on top of** the profile. It always wins.

If `tools_allowed` is non-empty, any tool not in the list is blocked (allowlist mode). The profile and `tools_blocked` still apply.

**Priority order:** profile blocks → `tools_blocked` → `tools_allowed` allowlist → allowed.

---

## Context providers

Context providers fetch data from external sources and inject it into the agent's task description before the agent starts. Each provider's output is logged with a truncated preview.

### `type: file`

Reads a file from the project directory.

```yaml
- type: file
  path: "GAME_DESIGN.md"    # relative to the project root
  max_chars: 8000           # truncated if longer
```

If the file does not exist, a `(file not found)` note is injected instead.

### `type: command`

Runs a shell command in the project directory.

```yaml
- type: command
  command: "python -m pytest --tb=no -q 2>&1 | tail -20"
  timeout_seconds: 10       # default 10, hard cap 30
  max_chars: 4000
```

- Runs in the **project directory**, not the swarm root
- Inherits the server's environment variables
- stdout and stderr are combined
- Hard cap on timeout: 30 seconds (requests for longer are silently capped)

### `type: http`

Makes a GET request to a URL.

```yaml
- type: http
  url: "http://localhost:8080/api/status"
  max_chars: 2000
```

- GET only
- Uses the server process's network access
- Times out after `timeout_seconds` (default 10, hard cap 30)

---

## Writing a plugin prompt

If you set `prompt_file`, the file must be a YAML with `system` and `user` keys. It uses Jinja2 templating with `<< variable >>` syntax (not `{{ }}`).

```yaml
# plugins/lore_pass.yaml

system: |
  You are a lore consistency agent working on the game project << project >>.
  Your job is to review all narrative text files and ensure they are consistent
  with the canonical lore established in GAME_DESIGN.md.

  You have full write access. Commit and push your changes when done.

user: |
  << description >>
```

Available template variables (same as built-in prompts):

| Variable | Description |
|----------|-------------|
| `project` | Project name |
| `description` | Full task description (includes context providers, retry context, etc.) |
| `project_path` | Absolute path to the project directory |
| `project_path_arg` | Shell-quoted project path |
| `godot_bin` | Path to Godot binary (empty if not configured) |
| `godot_command` | Shell-quoted Godot command |
| `godot_status` | Human-readable Godot availability status |
| `prompt_intent_variant` | Current intent prompt variant |

Prompt files can use `<% include 'common/tools.md' %>` to pull in shared blocks from `prompts/common/`.

---

## Worked example: a lore consistency agent

This example creates a `lore_pass` task type that checks narrative text for lore inconsistencies.

**`plugins/lore_pass.yaml`:**

```yaml
plugin_id: "lore_pass"
task_type: "lore_pass"
display_name: "Lore Consistency Pass"
role: "implementation"
permission_profile: "read_write"   # no run_command; it's a text editing task
prompt_file: "plugins/lore_pass_prompt.yaml"

tools_blocked:
  - "run_command"
  - "web_search"
  - "create_task"

context_providers:
  - type: file
    path: "GAME_DESIGN.md"
    max_chars: 12000

  - type: command
    command: "find . -name '*.md' -not -path './.git/*' | head -30"
    timeout_seconds: 5
    max_chars: 1000
```

**`plugins/lore_pass_prompt.yaml`:**

```yaml
system: |
  You are a lore consistency editor for the game << project >>.
  Review all narrative text against GAME_DESIGN.md. Fix inconsistencies in place.
  Commit and push when done.

user: |
  << description >>
```

**Creating a lore_pass task:**

```python
swarm_create_task(
    project="my-game",
    task_type="lore_pass",
    description="Check all dialogue files in dialogue/ for consistency with the canonical lore.",
    priority=50
)
```

---

## Constraints and limitations

- **Startup-only load**: plugins are loaded once when the server starts. Restart the server to pick up new or changed plugin files.
- **One plugin per task_type**: if two `.yaml` files declare the same `task_type`, the second is ignored with a warning.
- **No hot-reload**: intentional. Hot-reload introduces race conditions with running agents.
- **Command providers run in project dir**: the `command` field runs relative to the project being worked on, not the swarm root. This is intentional — most useful context comes from the project.
- **Command timeout hard cap**: 30 seconds. Longer timeouts are silently capped to prevent blocking agent startup.
- **HTTP providers**: GET only. POST is not supported.
- **`tools_blocked` always wins**: even if a tool appears in `tools_allowed`, adding it to `tools_blocked` blocks it.

---

## How plugins interact with built-in task types

Plugins can declare `task_type` values that match built-in types (e.g. `"feature"`, `"bug"`). This is supported but use it carefully — it overrides the built-in prompt and permission logic for that type system-wide.

Generally, use plugins for **new** task types. To adjust behaviour for existing types, prefer editing the prompt YAML files in `prompts/` directly.

---

## Troubleshooting

**Plugin not loading:**
- Check the server log for `[Plugins]` lines on startup.
- Ensure the file is named `*.yaml` (not `.yml`) and is in the `plugins/` directory at the swarm root.
- Validate the YAML is well-formed: `python -c "import yaml; yaml.safe_load(open('plugins/my_plugin.yaml'))"`.

**Context provider not running:**
- Check agent logs for `[Plugins:plugin_id] provider N ...` lines.
- For `command` providers, ensure the command works when run manually from the project directory.
- For `file` providers, the path must be relative to the project root (not the swarm root).

**Tools still accessible despite `tools_blocked`:**
- Restart the server — plugins are loaded at startup only.
- Check the agent log for `[Plugins]` authority denial messages.

**Plugin prompt not rendering:**
- Ensure `prompt_file` path is relative to the swarm root (e.g. `"plugins/my_prompt.yaml"`).
- The file must have `system` and `user` keys.
- Template variables use `<< var >>` syntax, not `{{ var }}`.
