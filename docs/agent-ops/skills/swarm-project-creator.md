---
name: swarm-project
description: Create and onboard a new project into the swarm controller. Handles git init, Gitea repo, managed_projects registration, genesis anchor, and initial task batch. Supports Godot, Python, TypeScript, Rust, Go, and general programming projects. Invoke with /swarm-project [project name and description].
argument-hint: "[project-name] [description of what the project does and what to build first]"
allowed-tools: Read, Grep, Glob, Bash
---

# Swarm Project Creation

You are creating a new project in the swarm controller. Follow these steps exactly to avoid the common mistakes that cause projects to not appear in the dashboard or have broken dependency chains.

The user said: $ARGUMENTS

## Step 1 — Gather information

If any of the following are unclear from the user's message, ask before proceeding:
- **Project name**: must be kebab-case, e.g. `aquarium-monitor`
- **Project type**: see type guide below
- **Overview**: one paragraph describing what the project does
- **Initial tasks**: what should agents build first? (list 3–10 concrete deliverables)

### Project type guide

| Type | Use when | Validation | Scaffold task? |
|------|----------|------------|----------------|
| `godot` | Godot 4 game | `check_scripts.gd` + GUT tests | ❌ Never — bootstrap is automatic |
| `python` | Python service, CLI, library | `py_compile` + pytest | ✅ Yes — setup venv/deps/pyproject |
| `typescript` | Node/Bun/Deno TS project | `tsc --noEmit` | ✅ Yes — init package.json/tsconfig |
| `rust` | Rust crate or binary | `cargo check` | ✅ Yes — init Cargo.toml |
| `expo` | React Native / Expo app | `tsc --noEmit` | ✅ Yes — init Expo project |
| `python` | General programming (Go, C#, Swift, etc.) | language-specific | ✅ Yes — scaffold task sets up structure |

For **general programming** (Go, C#, Swift, etc.): use `type="python"` as the closest match and put language/framework context in every task description. Agents use generic prompts — the task description carries the language.

## Step 2 — Check current state

!`curl -s http://localhost:5001/api/health 2>/dev/null | python3 -c "import json,sys; h=json.load(sys.stdin); print('Swarm:', 'OK lag=' + str(h.get('monitor_lag_seconds','?')) + 's' if h.get('monitor_alive') else 'DOWN')" 2>/dev/null || echo "Swarm: not responding — start with: .venv/bin/python swarm_runner.py api"`

Check if project already exists:
!`curl -s http://localhost:5001/api/projects 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); ps=d.get('projects',{}); found=[n for n in ps if any(w in n.lower() for w in '$ARGUMENTS'.lower().split()[:2])]; print('EXISTS: ' + str(found) if found else 'Not found — safe to create')" 2>/dev/null || true`

Check auto mode:
!`curl -s http://localhost:5001/api/auto-mode 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print('Auto mode:', 'ON' if d.get('enabled') else 'OFF')" 2>/dev/null || echo "Auto mode: unknown"`

## Step 3 — Create the project

### Option A — New project (git init + Gitea repo + bootstrap + task seed)

Use `swarm_create_project`. Pass all tasks upfront — it seeds them atomically with correct dep wiring:

```
swarm_create_project(
  name="project-name",
  type="godot",   # or "python", "typescript", "rust", "expo"
  overview="One paragraph describing the project goal and target audience.",
  tasks=[
    {"type": "feature", "description": "...", "priority": 50},
    {"type": "feature", "description": "...", "priority": 50, "depends_on": [0]},
  ]
)
```

### Option B — Existing repo (already cloned/initialised)

```
swarm_register_project(name="project-name", managed=True)
```

Then create tasks separately (Step 4). For Godot: also run the wizard bootstrap to install state_server.gd, check_scripts.gd, GUT, etc.:
```bash
curl -s -X POST http://localhost:5001/api/wizard/create \
  -H "Content-Type: application/json" \
  -d '{"project": "project-name", "bootstrap_only": true}'
```

---

## Per-type task list rules

### Godot projects
- `swarm_create_project` auto-runs `_bootstrap_godot_project_support()`: installs GUT, state_server.gd, test_harness.gd, check_scripts.gd, project.godot, main.tscn
- **NEVER add a scaffold/setup task** — it creates a phantom root and wastes an agent slot
- First task = first real gameplay feature (e.g. "Player ship with basic movement and shooting")
- End the DAG with a `harness_qa` or `qa` task as the final gate
- Good task granularity: one system per task (player, enemies, scoring, HUD — not "build the whole game")

Example Godot task list:
```python
tasks = [
  {"type": "feature", "description": "Single tower that auto-shoots nearest enemy. Scene: scenes/tower.tscn, script: scripts/tower.gd. Exposes shoot_range and damage properties.", "priority": 50},
  {"type": "feature", "description": "Enemy wave system: spawn enemies on a path toward the tower. scripts/wave_manager.gd. Emit wave_complete signal when all enemies dead.", "priority": 50, "depends_on": [0]},
  {"type": "feature", "description": "Score + lives HUD. scenes/hud.tscn. Wire to tower kills and enemy reach-base events.", "priority": 50, "depends_on": [1]},
  {"type": "polish", "description": "Visual polish: add particle effects on enemy death, tower shoot flash, screen shake on life loss.", "priority": 50, "depends_on": [2]},
  {"type": "harness_qa", "description": "Sprint gate QA: verify wave spawning, tower shooting, score incrementing, game-over on 0 lives.", "priority": 75, "depends_on": [3]},
]
```

### Python projects
- Scaffold task IS correct as the root — sets up `pyproject.toml`, `.venv`, dependencies, `src/` layout
- Each subsequent task builds one module or feature
- End with a `harness_qa` or `feature` task that adds pytest coverage

Example Python task list:
```python
tasks = [
  {"type": "feature", "description": "Scaffold: create pyproject.toml with FastAPI + uvicorn + pytest deps. Create src/myapp/__init__.py and src/myapp/main.py with a /health endpoint. Add .venv setup instructions to README.", "priority": 50},
  {"type": "feature", "description": "Add SQLAlchemy models for User and Session in src/myapp/models.py. Use SQLite for dev. Alembic migrations.", "priority": 50, "depends_on": [0]},
  {"type": "feature", "description": "Add JWT auth middleware in src/myapp/auth.py. POST /auth/login returns token, GET /auth/me returns current user.", "priority": 50, "depends_on": [1]},
  {"type": "feature", "description": "Add pytest test suite in tests/. Cover: health endpoint, user creation, login flow, JWT validation.", "priority": 50, "depends_on": [2]},
]
```

### TypeScript projects
- Scaffold task sets up `package.json`, `tsconfig.json`, `src/index.ts`, linter config
- Agents run `tsc --noEmit` for validation
- Describe the runtime in the scaffold task: Node 20 / Bun / Deno, ESM vs CJS

### Rust projects
- Scaffold task: `cargo init`, add dependencies to `Cargo.toml`, create module structure
- Agents run `cargo check` for validation
- Specify edition (2021) and async runtime (tokio/async-std) in scaffold description

### General programming (Go, C#, Swift, etc.)
- Use `type="python"` (closest validation match)
- Put the language, version, and framework in every task description
- Scaffold task creates the project structure (go.mod, Package.swift, .csproj, etc.)
- Agents won't have language-specific prompts but will follow the task description accurately

---

## Step 4 — Create tasks (if not passed to swarm_create_project)

**Rules:**
1. One root task — the one nothing else depends on
2. Use integer `depends_on` indices — never hardcoded IDs
3. Use `POST /api/tasks/batch` — never N individual POSTs

```python
import requests, json

tasks = [
  {"type": "feature", "description": "...", "priority": 50},
  {"type": "feature", "description": "...", "priority": 50, "depends_on": [0]},
  {"type": "feature", "description": "...", "priority": 50, "depends_on": [0]},
  {"type": "feature", "description": "...", "priority": 50, "depends_on": [1, 2]},
]

resp = requests.post("http://localhost:5001/api/tasks/batch", json={
    "project": "project-name",
    "tasks": tasks,
})
data = resp.json()
print("Created:", data.get("created"), "tasks")
print("ID map:", json.dumps(data.get("id_map", {}), indent=2))
```

Verify after creation:
```bash
curl -s "http://localhost:5001/api/tasks?project=project-name" | python3 -c "
import json, sys
tasks = json.load(sys.stdin)
tasks = tasks if isinstance(tasks, list) else tasks.get('tasks', [])
mine = [t for t in tasks if t.get('project') == 'project-name']
by_status = {}
for t in mine:
    by_status[t['status']] = by_status.get(t['status'], 0) + 1
print(f'{len(mine)} tasks:', by_status)
"
```

---

## Step 5 — Verify genesis anchor

```bash
curl -s http://localhost:5001/api/projects | python3 -c "
import json, sys
data = json.load(sys.stdin)
p = data.get('projects', {}).get('project-name', {})
print('head_task_id:', p.get('head_task_id', 'MISSING'))
print('managed:', p.get('managed'))
"
```

If `head_task_id` is null, force reconcile:
```bash
curl -s -X POST http://localhost:5001/api/dependencies/integrity \
  -H "Content-Type: application/json" \
  -d '{"action": "reconcile_heads"}'
```

---

## Step 6 — Add to managed_projects

`swarm_create_project` handles this automatically. For `swarm_register_project`, verify and add manually:

```python
import requests, json
d = requests.get("http://localhost:5001/api/managed-projects").json()
current = d.get("managed_projects", [])
if "project-name" not in current:
    current.append("project-name")
    resp = requests.post("http://localhost:5001/api/managed-projects", json={"managed_projects": current})
    print("Updated:", resp.json())
else:
    print("Already managed")
```

---

## Step 7 — Confirm and spawn

```bash
curl -s "http://localhost:5001/api/tasks?project=project-name" | python3 -c "
import json, sys
tasks = json.load(sys.stdin)
tasks = tasks if isinstance(tasks, list) else tasks.get('tasks', [])
mine = [t for t in tasks if t.get('project') == 'project-name']
pending = sum(1 for t in mine if t['status'] in ('pending', 'in_progress'))
print(f'{len(mine)} total tasks, {pending} actionable')
"
```

If auto mode is ON, agents spawn automatically. If OFF:
```
swarm_spawn(project="project-name")
```

---

## Writing good task descriptions

Include:
- **What** to build (specific system or feature, not "add stuff")
- **Where** — file paths if known (`scripts/player.gd`, `src/auth/jwt.py`)
- **Acceptance criteria** — what does done look like?
- **Interfaces** — signals, function signatures, endpoints the system exposes
- **Dependencies** — what existing code does it integrate with?

Bad: `"Add authentication"`
Good: `"Add JWT authentication in src/auth/jwt.py. POST /auth/login accepts {email, password}, validates against User model, returns {token, expires_at}. Middleware in src/auth/middleware.py validates Bearer token on protected routes. Add tests in tests/test_auth.py covering login, invalid credentials, expired token."`

---

## Common mistakes to avoid

- **Godot: never add a scaffold task** — bootstrap is automatic, it creates a phantom root
- **Never delete the batch root task** — breaks all downstream deps
- **Never hardcode task IDs in depends_on** — use integer indices, always
- **Never mix curl shell vars and JSON** — use Python requests
- **Never use `POST /api/projects/{name}/head`** — that endpoint does not exist
- **Never manually create a genesis task** — `ensure_project_head` handles it; manual ones break chains
- **Check agents aren't already running** before resetting statuses: `curl -s http://localhost:5001/api/agents`

---

## After completion

Tell the user:
1. Project name, type, and how many tasks were created
2. Auto mode status (ON = tasks start automatically, OFF = need to enable or spawn)
3. Dashboard URL: `http://localhost:5001` → select project from sidebar
4. Any caveats (parallel roots, external tools needed, language-specific setup required)
5. Suggest `/swarm-delegate` to watch individual tasks as they run
