---
name: swarm-project-creator
description: Create and set up new projects (Godot, Python, TypeScript) ready for swarm agent work. Helps with project structure, initial files, git setup, and swarm configuration.
---

# Swarm Project Creator Skill

Set up new projects for the swarm controller to build and maintain. Supports Godot, Python, and TypeScript projects.

## When to Use

- Create a new project from scratch
- Prepare an existing project for swarm management
- Initialize git repositories
- Add a project to swarm config

## Project Types

| Type | Detection | Validation |
|------|-----------|------------|
| Godot | `project.godot` exists | `godot --headless --script res://check_scripts.gd` |
| Python | `requirements.txt`, `pyproject.toml`, or Python sources | prefer project-local `pytest` when tests exist, otherwise `python -m py_compile` |
| TypeScript | `package.json` exists | `tsc --noEmit` |

## Project Creation Workflow

### Step 1: Create the project directory and files

**Godot project:**
```
my-game/
├── project.godot
├── main.tscn
├── scripts/
│   └── main.gd
├── scenes/
├── assets/
└── .gitignore
```

**Python project:**
```
my-app/
├── main.py
├── requirements.txt
├── .gitignore
└── README.md
```

### Step 2: Initialize git

```bash
cd /path/to/project
git init
git add -A
git commit -m "Initial commit"
```

### Step 3: Add to swarm config

Update `swarm-controller/config.json`:
```json
{
  "workspace": "/path/to/projects-directory",
  "managed_projects": ["my-game"]
}
```

### Step 4: Register with swarm API

```bash
# Rescan to pick up the new project
curl -X POST http://localhost:5001/api/rescan

# Or explicitly add it
curl -X POST http://localhost:5001/api/projects \
  -H "Content-Type: application/json" \
  -d '{"name": "my-game"}'
```

### Step 5: Add initial tasks

```bash
curl -X POST http://localhost:5001/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "project": "my-game",
    "type": "feature",
    "description": "Implement player movement with WASD controls",
    "priority": 50
  }'
```

## Godot .gitignore

```gitignore
# Godot 4 .gitignore
.godot/
*.uid
export_presets.cfg
```

## Python .gitignore

```gitignore
__pycache__/
*.pyc
*.pyo
.venv/
venv/
*.egg-info/
dist/
.env
```

## Post-Creation Checklist

- [ ] Git initialized with initial commit
- [ ] Project added to `config.json` `managed_projects`
- [ ] Swarm rescanned (`POST /api/rescan`)
- [ ] Initial tasks added via API
- [ ] Swarm started: `python swarm_runner.py api`
- [ ] For Godot projects, verify `docs/new_project_setup.md` requirements are satisfied

## GUT Auto-Setup (Godot projects)

When a managed Godot project is rescanned and `addons/gut/` is missing, the
controller can create a `setup-gut-<project>` task to install it. Treat this as
an automated bootstrap helper, not a replacement for the full checklist in
`docs/new_project_setup.md`.

The task will:
1. Copy pinned GUT from the local controller cache, or populate the cache from the configured source
2. Enable the plugin in `project.godot`
3. Create the `tests/` directory
4. Leave the project in a validation-ready state

This fires once per project. If GUT is already present, the task is skipped.

## Tips

- Keep project names lowercase with hyphens: `my-awesome-game`
- Start with minimal MVP scope — let the swarm build it out
- The swarm auto-generates refactor tasks for files exceeding `max_lines` (default 5000)
- Post-task validation runs automatically; failures spawn priority-100 bug tasks
