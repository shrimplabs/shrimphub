# Swarm Controller - Project Specification

## Overview

**Project Name:** Swarm Controller  
**Type:** Agent Orchestration System (Modular)  
**Core Functionality:** A fully modular agent orchestration system that manages sub-agents across multiple projects with pluggable prompt templates, project profiles, task selection strategies, and dependency management.  
**Target Users:** Agent operators, developers needing parallel task execution

---

## Problem Statement (Original)

- Cron jobs pick from paused projects despite pause flags
- Multiple agents can work on same project simultaneously (merge conflicts)
- No enforcement of code quality rules (monolithic files grow unchecked)
- No centralized task prioritization across projects

**Solved:** The modular architecture now supports file-level locking, configurable strategies, and dependency graphs.

---

## Core Features

### 1. Project Registry
- Tracks all projects with status: `active`, `paused`, `locked`
- Stores line counts per file
- **File-level locking** - Multiple agents can work on same project if editing different files
- Records last activity timestamp
- Auto-detects project type (Godot, Python, TypeScript, etc.)

### 2. Task Queue System
- Priority-based task queue across all projects
- Task types: `feature`, `refactor`, `bug`, `polish` (pluggable)
- Task dependencies - declare `dependencies: ["task-id"]`
- Auto-prioritization based on strategy

### 3. Pluggable Task Selection Strategies
- `refactor_first` - Always prioritize refactor tasks
- `priority` - Highest priority first
- `round_robin` - Cycle through projects evenly
- `skill_match` - Match task to agent skills
- `dependency_aware` - Prioritize tasks with completed deps
- `least_recently_worked` - Pick project not worked on longest

### 4. Prompt Plugin System
- YAML templates in `prompts/` directory
- Add new task types by creating YAML files
- Variables: `{{project_name}}`, `{{language}}`, `{{max_lines}}`, etc.
- Default templates: refactor, feature, bug, polish

### 5. Project Profiles
- YAML profiles in `profiles/` directory
- Auto-detection based on project files
- Per-type settings: file extensions, ignore patterns, commands
- Default profiles: godot, python, typescript

### 6. Dependency Graph
- Track task dependencies as DAG
- Detect cycles
- Calculate parallel execution levels
- Find ready/blocked tasks
- Generate DOT visualization

---

## Data Structures

### projects.json
```json
{
  "projects": {
    "my-project": {
      "status": "active",
      "locked": false,
      "files": {
        "main.gd": 11434,
        "player.gd": 2300
      },
      "file_locks": {
        "main.gd": {
          "file_path": "main.gd",
          "locked_by": "agent-uuid",
          "locked_at": "2026-03-03T20:00:00Z",
          "task_id": "task-uuid"
        }
      },
      "profile": "godot"
    }
  }
}
```

### task-queue.json
```json
{
  "tasks": [
    {
      "id": "uuid",
      "project": "my-project",
      "type": "refactor",
      "description": "Split main.gd",
      "priority": 100,
      "status": "pending",
      "dependencies": [],
      "created": "2026-03-03T20:00:00Z"
    }
  ]
}
```

---

## Architecture

```
swarm-controller/
├── prompts/                    # Prompt templates (YAML)
│   ├── refactor.yaml
│   ├── feature.yaml
│   ├── bug.yaml
│   └── polish.yaml
├── profiles/                   # Project profiles (YAML)
│   ├── godot.yaml
│   ├── python.yaml
│   └── typescript.yaml
├── swarm/                     # Core package
│   ├── tasks.py              # Task abstraction layer
│   ├── projects.py           # Project registry + file locking
│   ├── agents.py             # Agent factory + spawning
│   ├── strategies.py         # Task selection strategies
│   ├── dependencies.py      # Task dependency graph
│   └── api.py                # Flask API
├── swarm_runner.py           # Original entry point
├── swarm_runner_modular.py   # New modular entry point
└── data/                      # Runtime state
    ├── projects.json
    ├── task-queue.json
    └── agents.json
```

---

## Implementation Status

### Phase 1: Foundation ✅
- [x] Project structure
- [x] ProjectRegistry (CRUD)
- [x] TaskQueue (priority)
- [x] Python runner
- [x] Auto-generate refactor tasks

### Phase 2: Coordination ✅
- [x] SubAgentManager (spawn/track)
- [x] File-level locking
- [x] Task status tracking
- [x] Agent capacity limits
- [x] Parallel work on single project

### Phase 3: Intelligence ✅
- [x] Auto-detect monolithic files
- [x] Auto-generate refactor tasks
- [x] Pluggable prompt templates
- [x] Project profile auto-detection
- [x] Task selection strategies
- [x] Dependency graph

### Phase 4: Extensibility ✅
- [x] Plugin system for prompts
- [x] Plugin system for profiles
- [x] Plugin system for strategies
- [x] Python API for programmatic use

---

## API Endpoints

### Projects
- `GET /api/projects` - List all
- `POST /api/projects` - Add project
- `GET /api/projects/<name>/locks` - File locks
- `POST /api/projects/<name>/lock` - Lock file
- `POST /api/projects/<name>/spawn` - Parallel spawn

### Tasks
- `GET /api/tasks` - List all
- `POST /api/tasks` - Add task
- `PUT /api/tasks/<id>` - Update task

### Agents
- `GET /api/agents` - List all
- `GET /api/agents/<id>/output` - Get output

### Strategies
- `GET /api/strategies` - List available
- `POST /api/strategy` - Set strategy

### Dependencies
- `GET /api/dependencies` - Graph stats
- `GET /api/dependencies/dot` - DOT visualization
- `GET /api/dependencies/ready` - Ready tasks

---

## Success Criteria

1. ✅ Zero work on paused projects
2. ✅ Zero merge conflicts (file-level locking)
3. ✅ Files stay under max_lines (enforced)
4. ✅ Task dependencies respected
5. ✅ Parallel work on single project supported
6. ✅ Multiple project types supported

---

## Usage Examples

### Using the Python API

```python
from swarm import (
    get_task_source,
    get_project_registry,
    get_agent_spawner,
    get_strategy,
    create_feature_task
)

# Initialize components
tasks = get_task_source()
projects = get_project_registry()
spawner = get_agent_spawner()
strategy = get_strategy("round_robin")

# Create task with dependencies
task = create_feature_task("my-project", "Add login")
tasks.add_task(task)

# Get next task using strategy
context = {"available_projects": {"my-project"}, "locked_projects": set()}
next_task = strategy.select_next(tasks, context)

# Spawn agent
spawner.spawn(next_task, {"max_lines": 5000})
```

### Adding a Custom Prompt

```yaml
# prompts/security_audit.yaml
name: security_audit
description: Run security analysis
priority: 90

system_prompt: |
  You are a security expert. Analyze {{project_path}} for vulnerabilities...

user_template: |
  Project: {{project_name}}
  Run a security audit...

tools:
  - name: search_code
  - name: run_command
```

---

## Notes

- State persisted to JSON files in `data/`
- Backwards compatible with original `swarm_runner.py`
- New modular runner recommended for new features
- All components can be swapped via dependency injection
