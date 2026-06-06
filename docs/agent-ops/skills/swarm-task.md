---
name: swarm-task
description: Pick up and complete a ready task from the swarm task graph, the same way a background agent would. Claims the task, does the work, then marks it completed. Invoke with /swarm-task [optional: project filter or task ID].
argument-hint: "[project name, task ID, or leave blank to pick the highest-priority ready task]"
allowed-tools: Read, Grep, Glob, Edit, Write, Bash
---

# Swarm Task Worker

You are going to pick up a task from the swarm controller's dependency graph, complete the work yourself, and mark it done — exactly as a background agent would, but interactively with the user watching.

The user said: $ARGUMENTS

## Step 1 — Find a task to claim

Check what is ready (all dependencies met, status=pending):

!`curl -s "http://localhost:5001/api/dependencies/ready" 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
tasks = data if isinstance(data, list) else data.get('tasks', data.get('ready', []))
if not tasks:
    print('No ready tasks.')
else:
    for t in tasks[:15]:
        print(f\"  {t['id']:<42} {t.get('project','?'):20} {t.get('type','?'):10} {t.get('description','')[:55]}\")
" 2>/dev/null || echo "  (swarm not responding)"`

If the user specified a project, filter to that project. If they specified a task ID, use that directly. Otherwise pick the highest-priority task (lowest numeric priority value = most urgent) that fits your capabilities — prefer `bug`, `feature`, `refactor` over `qa` or `harness_qa` (vision tasks need a running game).

## Step 2 — Read the full task description

Before claiming, fetch the full task to understand exactly what is required:

```bash
curl -s "http://localhost:5001/api/tasks/<TASK_ID>" | python3 -c "import json,sys; t=json.load(sys.stdin); t=t.get('task',t); print(t['description'])"
```

Also check what the project repo looks like:
```bash
ls ~/workspace/<project>/
```

If the task description is unclear or requires information you don't have, ask the user before claiming.

## Step 3 — Claim the task

Mark it `in_progress` so no swarm agent races you for it:

```bash
curl -s -X PATCH "http://localhost:5001/api/tasks/<TASK_ID>" \
  -H "Content-Type: application/json" \
  -d '{"status": "in_progress", "agent_id": "claude-code-interactive"}'
```

## Step 4 — Do the work

Read the relevant files, make the changes, run tests if applicable. Use the project's workspace path — managed projects live at `~/workspace/<project-name>/`.

Key rules (same as background agents follow):
- One file at a time to avoid truncation
- Run tests before marking complete — do not mark complete if tests fail
- Do not restart running services (shrimp-router, swarm, headroom) — edit files in place; services hot-reload or will be restarted by the user
- If you discover the task is blocked by something not in the dep graph, create a blocker task and stop

## Step 5 — Mark complete (or fail)

On success:
```bash
curl -s -X PATCH "http://localhost:5001/api/tasks/<TASK_ID>" \
  -H "Content-Type: application/json" \
  -d '{"status": "completed", "agent_id": null, "metadata": {"note": "<brief summary of what you did>"}}'
```

On failure (something blocked you):
```bash
curl -s -X PATCH "http://localhost:5001/api/tasks/<TASK_ID>" \
  -H "Content-Type: application/json" \
  -d '{"status": "failed", "agent_id": null, "metadata": {"last_failure": "<what blocked you and why"}}'
```

Then tell the user what happened and what the blocker is.
