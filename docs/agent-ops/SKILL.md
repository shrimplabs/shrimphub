# Swarm Controller Helper

Quick reference for managing the current controller runtime via its API.

## Starting the API Server

```bash
cd /path/to/swarm-controller
python swarm_runner.py api
# Dashboard at http://localhost:5001
```

To run in the background:
```bash
nohup python swarm_runner.py api > /tmp/swarm.log 2>&1 &
```

## Spawning Agents

```bash
# Spawn up to N agents (respects max_active_agents and quota)
curl -s -X POST http://localhost:5001/api/spawn-batch \
  -H "Content-Type: application/json" -d '{"count": 3}'

# Enable auto mode (fills slots continuously until queue is empty)
curl -s -X POST http://localhost:5001/api/auto-mode \
  -H "Content-Type: application/json" -d '{"enabled": true}'

# Spawn for a specific project
curl -s -X POST http://localhost:5001/api/spawn \
  -H "Content-Type: application/json" \
  -d '{"project": "my-game", "type": "feature", "description": "Add combat"}'
```

## Checking Status

```bash
# Active agents
curl -s http://localhost:5001/api/agents | python3 -c "
import json,sys; d=json.load(sys.stdin)
for a in d.get('agents',[]): print(a['id'][:8], a['status'], a['project'])
"

# Task queue
curl -s http://localhost:5001/api/tasks | python3 -c "
import json,sys; d=json.load(sys.stdin)
for t in d.get('tasks',[]): print(t['id'], t['status'], t['project'])
"

# Project health
curl -s http://localhost:5001/api/projects/my-game/health

# Quota limit status
curl -s http://localhost:5001/api/quota-limit
```

## Adding Tasks

```bash
# Add a feature task
curl -s -X POST http://localhost:5001/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "project": "my-game",
    "type": "feature",
    "description": "Add player combat system",
    "priority": 50,
    "max_attempts": 3
  }'

# Add a bug task (high priority)
curl -s -X POST http://localhost:5001/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "project": "my-game",
    "type": "bug",
    "description": "Player falls through floor on jump",
    "priority": 80
  }'
```

## Configuration

```bash
# Set max concurrent agents
curl -s -X POST http://localhost:5001/api/max-agents \
  -H "Content-Type: application/json" -d '{"max_active_agents": 5}'

# Set task selection strategy
curl -s -X POST http://localhost:5001/api/strategy \
  -H "Content-Type: application/json" -d '{"strategy": "round_robin"}'

# Switch LLM provider
curl -s -X POST http://localhost:5001/api/provider \
  -H "Content-Type: application/json" -d '{"provider": "claude"}'

# Check provider status (shows which API keys are set)
curl -s http://localhost:5001/api/providers
```

## Viewing Agent Output

```bash
# Static snapshot
curl -s http://localhost:5001/api/agents/<id>/output

# Live stream (SSE — use curl -N to disable buffering)
curl -sN http://localhost:5001/api/agents/<id>/stream

# Kill a stuck agent
curl -s -X POST http://localhost:5001/api/agents/<id>/kill
```

## Project Management

```bash
# Rescan all project file sizes
curl -s -X POST http://localhost:5001/api/rescan

# Lock / unlock a project manually
curl -s -X PUT http://localhost:5001/api/projects/my-game \
  -H "Content-Type: application/json" -d '{"locked": false}'
```

Notes:

- The effective scheduler default is `priority` unless `config.json` overrides it.
- The API is unauthenticated by default. Enable `login_required` before exposing it beyond a trusted local environment.
