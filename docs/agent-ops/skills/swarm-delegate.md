---
name: swarm-delegate
description: Delegate a specific task to a swarm agent and watch it run. Creates the task, waits for an agent to pick it up, polls for progress, and reports the result. Use when you want to offload multi-file/commit work and stay informed. Invoke with /swarm-delegate project=<name> [type=feature|bug|...] "<description>"
argument-hint: "project=<name> [type=feature|bug|refactor|polish|qa|research] \"<task description>\""
allowed-tools: Read, Grep, Glob, Bash
---

# Swarm Delegate

You are delegating a task to the swarm controller and watching it through to completion.

User said: $ARGUMENTS

## Step 1 — Parse arguments

Extract from `$ARGUMENTS`:
- `project=<name>` — required
- `type=<type>` — optional, default `feature`; valid: `feature`, `bug`, `refactor`, `polish`, `qa`, `harness_qa`, `research`, `plan`
- `priority=<n>` — optional, default 80 for bug, 50 for everything else
- Everything else = the task description

If project is missing, ask the user before proceeding.

## Step 2 — Pre-flight checks

!`curl -s http://localhost:5001/api/health 2>/dev/null | python3 -c "import json,sys; h=json.load(sys.stdin); print('Swarm: OK lag=' + str(h.get('monitor_lag_seconds','?')) + 's agents=' + str(h.get('active_agents','?')) + '/' + str(h.get('max_agents','?')))" 2>/dev/null || echo "Swarm: NOT RUNNING — start with: .venv/bin/python swarm_runner.py api"`

Verify the project exists and is managed:
```bash
curl -s http://localhost:5001/api/projects | python3 -c "
import json, sys, os
data = json.load(sys.stdin)
projects = data.get('projects', {})
name = next((k for k in projects if k == os.environ.get('PROJECT','') or '$ARGUMENTS'.split('project=')[1].split()[0] if 'project=' in '$ARGUMENTS' else ''), None)
if name:
    p = projects[name]
    print(f'Found: {name} managed={p.get(\"managed\")} head={p.get(\"head_task_id\",\"none\")[:12] if p.get(\"head_task_id\") else \"none\"}')
else:
    print('NOT FOUND — use /swarm-project to create it first')
"
```

If the project is not found or not managed, stop and tell the user.

## Step 3 — Create the task

Use the batch endpoint so it auto-chains to the project HEAD:

```python
import requests, json, sys

PROJECT = ""   # fill in from args
TYPE    = "feature"  # fill in from args
DESC    = ""   # fill in from args
PRIORITY = 50  # 80 for bug, 100 for critical

resp = requests.post("http://localhost:5001/api/tasks/batch", json={
    "project": PROJECT,
    "tasks": [{"type": TYPE, "description": DESC, "priority": PRIORITY}],
})
data = resp.json()
if resp.status_code != 200 or not data.get("id_map"):
    print("ERROR creating task:", data)
    sys.exit(1)

task_id = data["id_map"]["0"]
print(f"Created task: {task_id}")
print(f"View at: http://localhost:5001 → {PROJECT}")
```

Tell the user the task ID and that you're now watching for it to be picked up.

## Step 4 — Wait for pickup

Poll until the task moves to `in_progress` (agent picked it up) or timeout (5 minutes):

```bash
python3 -c "
import requests, time, sys
task_id = sys.argv[1]
deadline = time.time() + 300  # 5 min
while time.time() < deadline:
    r = requests.get(f'http://localhost:5001/api/tasks/{task_id}').json()
    status = r.get('status','?')
    agent_id = r.get('agent_id','')
    if status == 'in_progress' and agent_id:
        print(f'PICKED_UP agent={agent_id}')
        break
    elif status in ('completed','failed','cancelled'):
        print(f'TERMINAL status={status}')
        break
    print(f'Waiting... status={status}')
    time.sleep(15)
else:
    print('TIMEOUT — task not picked up within 5 minutes')
    print('Check auto mode: curl -s http://localhost:5001/api/auto-mode')
" TASK_ID
```

If auto mode is off, warn the user and suggest: `curl -s -X POST http://localhost:5001/api/spawn-batch -H "Content-Type: application/json" -d '{}'`

## Step 5 — Poll for progress

Once the agent is running, poll every 60 seconds and print a progress summary. Do NOT try to stream SSE directly — poll instead:

```bash
python3 -c "
import requests, time, sys
task_id, agent_id = sys.argv[1], sys.argv[2]
deadline = time.time() + 7200  # 2 hour max
last_phase = ''
while time.time() < deadline:
    # Check task status
    t = requests.get(f'http://localhost:5001/api/tasks/{task_id}').json()
    status = t.get('status','?')
    if status in ('completed','failed','cancelled'):
        print(f'DONE status={status}')
        meta = t.get('metadata',{})
        print(f'diff={meta.get(\"diff_stat\",\"none\")}')
        print(f'last_failure={meta.get(\"last_failure\",\"\")[:300]}')
        break
    # Check agent log for phase transitions
    try:
        log = requests.get(f'http://localhost:5001/api/agents/{agent_id}/log?tail=50').json()
        lines = log.get('lines', []) if isinstance(log, dict) else []
        for line in reversed(lines):
            if 'PHASE:' in line or 'Pipeline' in line or 'complete' in line.lower():
                if line != last_phase:
                    print(f'[agent] {line.strip()}')
                    last_phase = line
                break
    except Exception:
        pass
    time.sleep(60)
else:
    print('TIMEOUT — agent ran for 2 hours without completing')
" TASK_ID AGENT_ID
```

Print human-readable updates as they come in, e.g.:
- "Agent picked up the task — running PLAN phase"
- "SCOUT complete — inspected 8 files"
- "WORK complete — running validation"
- "Task complete! Diff: 3 files changed, 142 insertions"

## Step 6 — Report result

After the task reaches a terminal state:

```bash
curl -s "http://localhost:5001/api/tasks/TASK_ID" | python3 -c "
import json, sys
t = json.load(sys.stdin)
meta = t.get('metadata', {})
status = t['status']
print(f'Status: {status}')
print(f'Attempts: {t.get(\"attempts\",0)}/{t.get(\"max_attempts\",3)}')
if meta.get('diff_stat'):
    print(f'Diff: {meta[\"diff_stat\"].splitlines()[-1] if meta[\"diff_stat\"] else \"none\"}')
if status == 'failed':
    print(f'Failure: {meta.get(\"last_failure\",\"\")[:400]}')
    print()
    print('Options:')
    print(f'  Reset: curl -s -X POST http://localhost:5001/api/tasks/{t[\"id\"]}/reset -H \"Content-Type: application/json\" -d \"{{}}\"')
    print(f'  Log: curl -s http://localhost:5001/api/agents/<agent_id>/log')
"
```

**On success:** Tell the user what was built, what files changed, and that changes are committed and pushed.

**On failure:** 
- Show the last_failure excerpt
- Tell the user the swarm will auto-retry (up to max_attempts)
- If exhausted: suggest `/swarm-delegate` again with a more specific description, or `/swarm` to investigate

**Send a push notification on completion** using the PushNotification tool:
- Success: `"✅ <project>: <task type> done — <diff summary>"`
- Failure: `"❌ <project>: <task type> failed — <brief reason>"`

## Task type guidance

| Type | When to use | Priority |
|------|-------------|----------|
| `feature` | New functionality, new files | 50 |
| `bug` | Fix a specific defect — include repro steps | 80 |
| `refactor` | Restructure without changing behaviour | 100 |
| `polish` | Visual / UX improvements | 50 |
| `research` | Read-only investigation, produces a report | 50 |
| `qa` | Vision-based exploratory QA (Godot, needs StateServer) | 75 |
| `harness_qa` | Deterministic checkpoint QA (needs TestHarness) | 75 |
| `plan` | Read-only planner — creates tasks as output | 50 |

## Writing good task descriptions

A good description includes:
- **What** to build/fix (specific, not vague)
- **Where** — file names if known (`scripts/player.gd`, `api/auth.py`)
- **Acceptance criteria** — what does done look like?
- **Context** — any constraints, existing patterns to follow

Bad: `"Add health system"`
Good: `"Add a health regeneration system to scripts/dragon/dragon_character.gd. Dragon should regenerate 5 HP/sec when out of combat for 3 seconds. Integrate with existing HealthComponent. Add signal health_regenerated(amount). Validate with check_scripts.gd."`

## If the task is too large

Signs the task is too large for one agent:
- Description has more than 3–4 distinct deliverables
- It touches more than 5–6 files
- It would take a human more than a few hours

In that case, use `/swarm` to create a multi-task plan instead, or ask the user if they want to break it down.

## Troubleshooting

**Task not picked up after 5 minutes:**
```bash
curl -s http://localhost:5001/api/auto-mode  # is auto mode on?
curl -s http://localhost:5001/api/dependencies/ready | python3 -c "import json,sys; tasks=json.load(sys.stdin); tasks=tasks if isinstance(tasks,list) else tasks.get('tasks',tasks.get('ready',[])); print([t['id'] for t in tasks if t.get('project')=='PROJECT'])"
```

**Task keeps failing:**
- Check `last_failure` in task metadata
- Read the agent log: `swarm_agent_log(agent_id, tail=200)`
- Check if it's a validation environment issue (Godot path, missing deps)
- Use `swarm_agent_hint()` to inject a correction into a running agent

**Ghost dep blocking the task:**
```bash
python3 -c "
import sqlite3, json
conn = sqlite3.connect('data/swarm.db')
conn.row_factory = sqlite3.Row
t = conn.execute('SELECT dependencies FROM tasks WHERE id=?', ('TASK_ID',)).fetchone()
deps = json.loads(t['dependencies'] or '[]')
for d in deps:
    row = conn.execute('SELECT id, status FROM tasks WHERE id=?', (d,)).fetchone()
    print(d, row['status'] if row else 'MISSING')
"
```
Remove any cancelled/failed/missing deps via `swarm_remove_dependency(task_id, dep_id)`.
