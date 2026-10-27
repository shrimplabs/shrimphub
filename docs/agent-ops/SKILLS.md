# Agent-Ops Helper Docs

Optional helper docs for operators using an AI coding agent to manage the swarm
controller. These files are not required for normal installation or runtime use.
They should track the current API surface and runtime defaults, but the public
README remains the human-facing source of truth.

## Skills

### 1. swarm-project-creator

Creates and configures new projects for swarm work.

**Location:** `docs/agent-ops/skills/swarm-project-creator.md`

**Triggers:** "create a project", "set up project for swarm", "add project to swarm"

**What it does:**
- Guides project structure creation (Godot, Python, TypeScript)
- Git initialization
- Adds project to `config.json`
- Registers with swarm API via `POST /api/rescan`

---

### 2. swarm-task-planner

Plans and creates task lists for swarm agents.

**Location:** `docs/agent-ops/skills/swarm-task-planner.md`

**Triggers:** "plan tasks", "create task list", "break down feature", "convert PRD to tasks"

**What it does:**
- Breaks features into small, agent-sized tasks
- Sets priorities (refactor: 100, bug: 80, feature/polish: 50)
- Defines task dependencies (DAG, cycle detection)
- Creates tasks via `POST /api/tasks`

---

### 3. swarm-task

Pick up and complete a ready task from the swarm task graph.

**Location:** `docs/agent-ops/skills/swarm-task.md`

**Triggers:** "do a task", "pick up task", "work on task", "/swarm-task"

**What it does:**
- Finds highest-priority ready task from the dependency graph
- Claims the task, completes the work, marks it done
- Runs tests before marking complete

---

### 4. swarm-manager

Manages the swarm controller runtime.


**Location:** `docs/agent-ops/skills/swarm-manager.md`


**Triggers:** "start swarm", "check agents", "configure swarm", "switch provider", "kill agent"

**What it does:**
- Starts/stops the API server
- Spawns agents and enables auto mode
- Monitors agent progress and health
- Configures LLM provider (Minimax, Claude, OpenRouter, Kimi, custom)
- Handles troubleshooting

---

## Workflow Example

### Create → Plan → Run

```
# 1. Create the project
Use swarm-project-creator to set up "fantasy-rpg" for swarm management

# 2. Plan work
Use swarm-task-planner to create tasks for adding a combat system to fantasy-rpg

# 3. Start the swarm
Use swarm-manager to start the API server and spawn 3 agents
```

---

## API Quick Reference

### Starting

```bash
cd /path/to/swarm-controller
python swarm_runner.py api
# Dashboard: http://localhost:5001
```

### Tasks

```bash
# Create
curl -X POST http://localhost:5001/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"project": "my-game", "type": "feature", "description": "Add combat", "priority": 50}'

# List
curl http://localhost:5001/api/tasks

# Update / reset failed task
curl -X PUT http://localhost:5001/api/tasks/<id> \
  -H "Content-Type: application/json" \
  -d '{"status": "pending"}'

# Delete
curl -X DELETE http://localhost:5001/api/tasks/<id>
```

### Agents

```bash
# Spawn batch
curl -X POST http://localhost:5001/api/spawn-batch \
  -H "Content-Type: application/json" -d '{"count": 3}'

# Enable auto mode
curl -X POST http://localhost:5001/api/auto-mode \
  -H "Content-Type: application/json" -d '{"enabled": true}'

# List agents
curl http://localhost:5001/api/agents

# View output
curl http://localhost:5001/api/agents/<id>/output

# Live stream (SSE)
curl -N http://localhost:5001/api/agents/<id>/stream

# Kill agent
curl -X POST http://localhost:5001/api/agents/<id>/kill

# History
curl http://localhost:5001/api/history
```

### LLM Providers

```bash
# List providers + key status
curl http://localhost:5001/api/providers

# Switch provider
curl -X POST http://localhost:5001/api/provider \
  -H "Content-Type: application/json" -d '{"provider": "claude"}'

# Override model
curl -X POST http://localhost:5001/api/provider \
  -H "Content-Type: application/json" \
  -d '{"provider": "openrouter", "model": "google/gemini-2.0-flash-exp"}'
```

| Provider | Env var | Default model |
|----------|---------|---------------|
| `minimax` | `MINIMAX_API_KEY` | `MiniMax-M3` |
| `claude` | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` |
| `openrouter` | `OPENROUTER_API_KEY` | `anthropic/claude-3.5-sonnet` |
| `kimi` | `KIMI_API_KEY` | `k2p5` |

### Configuration

```bash
# Max concurrent agents
curl -X POST http://localhost:5001/api/max-agents \
  -H "Content-Type: application/json" -d '{"max_active_agents": 5}'

# Task selection strategy
curl -X POST http://localhost:5001/api/strategy \
  -H "Content-Type: application/json" -d '{"strategy": "round_robin"}'

# Quota stop threshold
curl -X POST http://localhost:5001/api/quota-limit \
  -H "Content-Type: application/json" -d '{"limit_percent": 85}'
```

### Projects & Health

```bash
# List projects
curl http://localhost:5001/api/projects

# Rescan file sizes
curl -X POST http://localhost:5001/api/rescan

# Per-project health (score, task counts, last commit age)
curl http://localhost:5001/api/projects/my-game/health

# Lock / unlock
curl -X PUT http://localhost:5001/api/projects/my-game \
  -H "Content-Type: application/json" -d '{"locked": false}'
```

### Dependencies

```bash
# Graph stats
curl http://localhost:5001/api/dependencies

# Tasks ready to run now
curl http://localhost:5001/api/dependencies/ready

# Execution levels (parallel groups)
curl http://localhost:5001/api/dependencies/execution-order
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| API not responding | `python swarm_runner.py api` |
| No tasks spawning | Check `/api/tasks`; check project not paused/locked |
| Agent stuck | `POST /api/agents/<id>/kill`; logs in `data/agent_<id>.log` |
| Quota exceeded | Lower `quota_limit_percent` via `/api/quota-limit` |
| Wrong LLM key | Check `/api/providers`; update `.env` |
| Project not found | Add to `managed_projects` in config.json; run `/api/rescan` |

---

## Files

```
docs/agent-ops/
├── SKILL.md
├── SKILLS.md
└── skills/
    ├── swarm-project-creator.md
    ├── swarm-task-planner.md
    └── swarm-manager.md
```
