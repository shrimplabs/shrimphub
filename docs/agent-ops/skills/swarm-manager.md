---
name: swarm-manager
description: Manage the swarm controller - start, stop, monitor agents, configure settings, switch LLM providers, and handle troubleshooting.
---

# Swarm Manager Skill

Manage the swarm controller: start/stop the API server, monitor agents, configure settings, and handle common issues.

## When to Use

- Start or restart the swarm API server
- Monitor active agents and their progress
- Adjust configuration (max agents, strategy, provider, quota limit)
- Kill stuck or hung agents
- Check project status, task queues, and health metrics
- Enable/disable auto mode
- Switch LLM provider (Minimax, Claude, OpenRouter, custom)
- Rescan projects

## Starting the Swarm

```bash
cd /path/to/swarm-controller
python swarm_runner.py api
# Dashboard: http://localhost:5001
```

## config.json Options

All fields optional. Create in the project root.

```json
{
  "workspace": "~/path/to/projects",
  "managed_projects": ["my-project"],
  "paused_projects": [],
  "max_active_agents": 6,
  "lock_project": false,
  "max_lines": 5000,
  "agent_timeout": 600,
  "quota_limit_percent": 90,
  "llm_provider": "minimax",
  "task_selection_strategy": "priority"
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `workspace` | `~/workspace` | Root directory for projects |
| `managed_projects` | `[]` | Projects to assign work (empty = all) |
| `max_active_agents` | `3` | Max concurrent agents |
| `lock_project` | `false` | `true` = one agent per project at a time |
| `max_lines` | `5000` | Line count that triggers auto-refactor task |
| `quota_limit_percent` | `90` | Stop spawning at this API usage % |
| `llm_provider` | `"minimax"` | Active LLM provider |

## LLM Providers

Built-in providers — set `llm_provider` in config.json or via the dashboard provider bar.

| Provider | Env var needed | Notes |
|----------|----------------|-------|
| `minimax` | `MINIMAX_API_KEY` | Default |
| `claude` | `ANTHROPIC_API_KEY` | Direct Anthropic API |
| `openrouter` | `OPENROUTER_API_KEY` | Access any model |
| `kimi` | `KIMI_API_KEY` | Anthropic-compatible coding endpoint |

```bash
# Switch provider via API
curl -X POST http://localhost:5001/api/provider \
  -H "Content-Type: application/json" \
  -d '{"provider": "claude"}'

# Override model
curl -X POST http://localhost:5001/api/provider \
  -H "Content-Type: application/json" \
  -d '{"provider": "openrouter", "model": "google/gemini-2.0-flash-exp"}'

# Check all providers + key status
curl http://localhost:5001/api/providers
```

## Monitoring

```bash
# Agents
curl http://localhost:5001/api/agents

# Tasks
curl http://localhost:5001/api/tasks

# Projects
curl http://localhost:5001/api/projects

# Per-project health (score, task counts, last commit age)
curl http://localhost:5001/api/projects/my-project/health

# Quota usage
curl http://localhost:5001/api/quota-limit
```

## Spawning & Auto Mode

```bash
# Spawn N agents
curl -X POST http://localhost:5001/api/spawn-batch \
  -H "Content-Type: application/json" \
  -d '{"count": 3}'

# Enable auto mode (runs until queue empty or quota exceeded)
curl -X POST http://localhost:5001/api/auto-mode \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# Disable auto mode
curl -X POST http://localhost:5001/api/auto-mode \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

## Agent Output

```bash
# Static snapshot
curl http://localhost:5001/api/agents/<id>/output

# Live stream (SSE)
curl -N http://localhost:5001/api/agents/<id>/stream

# Kill agent
curl -X POST http://localhost:5001/api/agents/<id>/kill
```

## Task Selection Strategies

| Strategy | Description |
|----------|-------------|
| `refactor_first` | Refactor tasks always first, then by priority |
| `priority` | Strict priority order |
| `round_robin` | Cycle through projects evenly |
| `dependency_aware` | Prioritise tasks whose dependencies are complete |
| `least_recently_worked` | Pick project not touched longest |

```bash
curl -X POST http://localhost:5001/api/strategy \
  -H "Content-Type: application/json" \
  -d '{"strategy": "round_robin"}'
```

## Configuration via API

```bash
# Set max agents
curl -X POST http://localhost:5001/api/max-agents \
  -H "Content-Type: application/json" \
  -d '{"max_active_agents": 5}'

# Set quota stop threshold
curl -X POST http://localhost:5001/api/quota-limit \
  -H "Content-Type: application/json" \
  -d '{"limit_percent": 85}'
```

## Project Management

```bash
# Rescan project file sizes
curl -X POST http://localhost:5001/api/rescan

# Unlock a project
curl -X PUT http://localhost:5001/api/projects/my-project \
  -H "Content-Type: application/json" \
  -d '{"locked": false}'

# Add/update managed_projects at runtime (no restart needed)
curl -X POST http://localhost:5001/api/managed-projects \
  -H "Content-Type: application/json" \
  -d '{"managed_projects": ["project-a", "project-b", "new-project"]}'

# Update paused projects
curl -X POST http://localhost:5001/api/managed-projects \
  -H "Content-Type: application/json" \
  -d '{"paused_projects": ["project-on-hold"]}'

# View current managed/paused lists
curl http://localhost:5001/api/managed-projects
```

Only the fields you include in the POST body are updated — sending `managed_projects` does not clear `paused_projects` and vice versa.

## Killing Agents

```bash
# Kill a specific agent by ID
curl -X POST http://localhost:5001/api/agents/<id>/kill
```

The kill endpoint works even after a server restart — if the in-memory process handle is gone, it falls back to killing by PID stored in the database and resets the task to `pending` so it can retry.

From the dashboard: click an agent card to open the log panel, then click **✕ Kill Agent** inside the panel.

## Auto Mode & Quota

Auto mode continuously fills agent slots. When the API quota limit is hit, auto mode suspends automatically (shown as "Auto: Paused (quota)" in the dashboard). It resumes on its own once the quota resets — you don't need to re-enable it manually.

```bash
# Check auto mode state (includes suspended_for_quota field)
curl http://localhost:5001/api/auto-mode

# Enable
curl -X POST http://localhost:5001/api/auto-mode \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'
```

## Agent History

```bash
# View archived completed/failed agents
curl http://localhost:5001/api/history
```

Completed tasks are also archived to `data/task-history.jsonl` when pruned from the database.

## Task Retry

Failed tasks automatically retry up to `max_attempts` times (default 3). Each retry includes the previous failure reason in the prompt. To change the limit per task:

```bash
curl -X PUT http://localhost:5001/api/tasks/<id> \
  -H "Content-Type: application/json" \
  -d '{"max_attempts": 5}'
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| API not responding | Start: `python swarm_runner.py api` |
| No tasks spawning | Check managed_projects includes your project: `GET /api/managed-projects` |
| Run button does nothing | Project not in managed_projects — add it via `POST /api/managed-projects` |
| Agent stuck | Click agent card → Kill Agent in log panel; or `POST /api/agents/<id>/kill` |
| Kill says "failed" | Falls back to PID kill automatically; if still failing, process may have already exited |
| Quota exceeded | Auto mode suspends and resumes automatically; check `/api/quota-limit` |
| Wrong LLM key | Check `/api/providers` for key status; update `.env` |
| Auto mode won't resume | If you manually disabled it, it won't auto-resume — only quota-suspension auto-resumes |

Current default notes:

- The effective scheduler default is `priority`.
- The loop limit is `200`.
- The API is unauthenticated by default unless `login_required` is set.
