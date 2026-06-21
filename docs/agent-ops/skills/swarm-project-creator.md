---
name: swarm-project
description: Create and onboard a new project into the swarm controller. Handles git init, Gitea repo, managed_projects registration, genesis anchor, and initial task batch. Invoke with /swarm-project [project name and description].
argument-hint: "[project-name] [description of what the project does and what to build first]"
allowed-tools: Read, Grep, Glob, Bash
---

# Swarm Project Creation

You are creating a new project in the swarm controller. Follow these steps exactly to avoid the common mistakes that cause projects to not appear in the dashboard or have broken dependency chains.

The user said: $ARGUMENTS

## Step 1 — Gather information

If any of the following are unclear from the user's message, ask before proceeding:
- **Project name**: must be kebab-case, e.g. `aquarium-monitor`
- **Project type**: `godot` (Godot 4 game), `python` (Python service/tool), or `expo` (React Native / Expo app)
- **Overview**: one paragraph describing what the project does
- **Initial tasks**: what should agents build first? (list 3–10 concrete deliverables)

## Step 2 — Check current state

!`curl -s http://localhost:5001/api/health 2>/dev/null | python3 -c "import json,sys; h=json.load(sys.stdin); print('Swarm:', 'OK' if h.get('ok') else 'DOWN')" 2>/dev/null || echo "Swarm: not responding"`

Check if project already exists:
!`curl -s http://localhost:5001/api/projects 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); ps=d.get('projects',{}); [print(f'EXISTS: {n}') for n in ps if '$ARGUMENTS'.split()[0].lower() in n.lower()]" 2>/dev/null || true`

Check if auto mode is on:
!`curl -s http://localhost:5001/api/auto-mode 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print('Auto mode:', 'ON' if d.get('enabled') else 'OFF')" 2>/dev/null || echo "Auto mode: unknown"`

## Step 3 — Create the project

Use the `swarm_create_project` MCP tool if git init + Gitea setup is wanted:

```
swarm_create_project(
  name="project-name",
  type="python",   # or "godot" or "expo"
  overview="One paragraph describing the project goal.",
  tasks=[
    {"type": "feature", "description": "...", "priority": 50},
    {"type": "feature", "description": "...", "priority": 50, "depends_on": [0]},
  ]
)
```

**Godot projects**: `swarm_create_project` automatically runs `_bootstrap_godot_project_support()` which installs GUT, state_server.gd, test_harness.gd, check_scripts.gd, project.godot, and main.tscn. **Do NOT add a "scaffold" or "setup" task** — it's redundant and creates a phantom root. Start your task list with the first real gameplay feature (e.g. "Single Tower Battlefield" or "Player ship and basic movement").

**If the repo already exists** (e.g. you already cloned it), use:
```
swarm_register_project(name="project-name", managed=True)
```
Then run `POST /api/wizard/create` (or call `_bootstrap_godot_project_support()` via the wizard) to install the Godot scaffold on the existing repo before seeding tasks. Create tasks separately.

## Step 4 — Create the initial task batch correctly

This is where mistakes commonly happen. Follow these rules:

### Rules for task batches

1. **One root task per batch** — identify which task nothing else depends on. This becomes the chain anchor.
2. **Use `depends_on` indices** — always use integer indices into the task array, not hardcoded IDs.
3. **Use `POST /api/tasks/batch`** with the `chain: false` body format (indices handle sequencing):

```python
import requests, json

tasks = [
  # For Godot: start with first real gameplay feature — NO scaffold task (bootstrap is automatic)
  # For Python: a setup/scaffold task is fine here
  {"type": "feature", "description": "First real feature", "priority": 50},
  {"type": "feature", "description": "Core module A", "priority": 50, "depends_on": [0]},
  {"type": "feature", "description": "Core module B", "priority": 50, "depends_on": [0]},
  {"type": "feature", "description": "Integration", "priority": 50, "depends_on": [1, 2]},
]

resp = requests.post("http://localhost:5001/api/tasks/batch", json={
    "project": "project-name",
    "tasks": tasks,
})
data = resp.json()
print("Created:", data.get("created"), "tasks")
print("ID map:", json.dumps(data.get("id_map", {}), indent=2))
```

4. **Do NOT mix shell variable interpolation with JSON** — always use Python requests or a heredoc with literal IDs.
5. **After batch creation**, verify with:

```bash
curl -s "http://localhost:5001/api/tasks?project=project-name" | python3 -c "
import json, sys
data = json.load(sys.stdin)
tasks = data if isinstance(data, list) else data.get('tasks', [])
by_status = {}
for t in tasks:
    if t.get('project') == 'project-name':
        s = t.get('status','?')
        by_status[s] = by_status.get(s, 0) + 1
print(by_status)
"
```

## Step 5 — Verify the genesis anchor

After creating tasks, check the project head is set:

```bash
curl -s http://localhost:5001/api/projects | python3 -c "
import json, sys
data = json.load(sys.stdin)
p = data.get('projects', {}).get('project-name', {})
print('head_task_id:', p.get('head_task_id'))
print('managed:', p.get('managed'))
"
```

If `head_task_id` is null or missing, force a reconcile:

```bash
curl -s -X POST http://localhost:5001/api/dependencies/integrity \
  -H "Content-Type: application/json" \
  -d '{"action": "reconcile_heads"}'
```

## Step 6 — Add to managed_projects

```bash
curl -s http://localhost:5001/api/managed-projects | python3 -c "
import json, sys; d=json.load(sys.stdin)
print('Currently managed:', d.get('managed_projects', []))
"
```

If the project is not in the list, add it:

```bash
# Get current list first, then add
CURRENT=$(curl -s http://localhost:5001/api/managed-projects | python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get('managed_projects',[])))")
# Then POST the updated list
python3 -c "
import requests, json
current = $CURRENT
if 'project-name' not in current:
    current.append('project-name')
resp = requests.post('http://localhost:5001/api/managed-projects', json={'managed_projects': current})
print(resp.json())
"
```

## Step 7 — Confirm and spawn

```bash
# Confirm the project appears with tasks
curl -s http://localhost:5001/api/tasks | python3 -c "
import json, sys
tasks = json.load(sys.stdin)
tasks = tasks if isinstance(tasks, list) else tasks.get('tasks', [])
mine = [t for t in tasks if t.get('project') == 'project-name']
pending = sum(1 for t in mine if t['status'] in ('pending','in_progress'))
print(f'{len(mine)} total tasks, {pending} actionable')
"
```

If auto mode is ON, agents will spawn automatically. If OFF, tell the user they can re-enable auto mode or manually spawn with:

```
swarm_spawn(project="project-name")
```

## Common mistakes to avoid

- **Never delete the batch root task** — it breaks all downstream deps
- **Never use `POST /api/projects/{name}/head`** — that endpoint does not exist; use the integrity repair endpoint instead
- **Never mix curl shell vars and JSON** — use Python requests for complex JSON bodies
- **Do not create a separate "genesis" task manually** — `ensure_project_head` auto-creates one when needed; manual genesis tasks with wrong deps cause floating chains
- **Check agents are not already running** before resetting task statuses — `curl -s http://localhost:5001/api/agents`

## After completion

Tell the user:
1. The project name and how many tasks were created
2. Whether auto mode is on (tasks will start automatically) or off (they need to enable it)
3. The URL to view the project: `http://localhost:5001` → select project from sidebar
4. Any caveats about the task chain (e.g. parallel roots, external dependencies needed)
