---
name: swarm-task-planner
description: Plan and create task lists for swarm agents. Helps break down features into actionable tasks, manage dependencies, and prioritize work.
---

# Swarm Task Planner Skill

Break down features into tasks, manage dependencies, and prioritize work for the swarm controller.

## When to Use

- Plan a feature and break it into tasks
- Create a task list for the swarm
- Convert a PRD or specification into tasks
- Manage task dependencies and priorities
- Organize refactoring work

## Task Types

| Type | Priority | Description |
|------|----------|-------------|
| `refactor` | 100 | Split oversized files, improve code structure |
| `bug` | 80 | Fix bugs and issues |
| `feature` | 50 | Implement new features |
| `polish` | 50 | Cleanup, improvements, optimizations |

## Creating Tasks

```bash
# Simple feature task
curl -X POST http://localhost:5001/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "project": "my-game",
    "type": "feature",
    "description": "Add player movement with WASD controls",
    "priority": 50
  }'

# Task with dependencies
curl -X POST http://localhost:5001/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "project": "my-game",
    "type": "feature",
    "description": "Add enemy AI that chases player",
    "priority": 50,
    "dependencies": ["player-movement-task-id"]
  }'

# High-retry task for complex work
curl -X POST http://localhost:5001/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "project": "my-game",
    "type": "feature",
    "description": "Implement physics-based platformer movement",
    "priority": 50,
    "max_attempts": 5
  }'
```

For larger seeds, prefer `POST /api/tasks/batch` so you can submit a full DAG in
one request and use `depends_on` indices instead of creating tasks incrementally.

## Task Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `project` | yes | — | Project folder name |
| `type` | yes | — | `feature`, `bug`, `refactor`, `polish` |
| `description` | yes | — | What the agent should do (be specific) |
| `priority` | no | 50 | 1–100, higher = sooner |
| `dependencies` | no | `[]` | Task IDs that must complete first |
| `max_attempts` | no | 3 | Auto-retries on failure |

## Planning Workflow

### Step 1: Define the goal
- What feature/fix are you achieving?
- What is the minimum viable version?
- What are the natural dependencies between parts?

### Step 2: Break into atomic tasks

**Atomicity rules — a task MUST be split if it:**
- Touches more than 2–3 files
- Has two distinct "Parts" or "Steps" listed in the description
- Asks for both implementation AND tests (split into separate tasks with dependency)
- Asks for both a new scene AND a new script
- Has "Fix X + do Y" in the title — any `+` or `and` is a split signal

**Target size:** one task = one logical unit of work an agent can do in ~30 tool loops.

**Split heuristics:**
| If the task says... | Split into... |
|---|---|
| "Create X and write tests for it" | Task 1: Create X / Task 2: Write tests (deps: Task 1) |
| "Fix bug + add feature" | Task 1: Fix bug / Task 2: Add feature (deps: Task 1) |
| "Create scene, script, and update project.godot" | Task 1: Script / Task 2: Scene (deps: Task 1) / Task 3: Wire up (deps: Task 2) |
| "Refactor A, B, and C" | One task per file |
| "Fix grey screen + write GUT tests" | Task 1: Fix scene / Task 2: Write tests (deps: Task 1) |

Example: "Add player combat system"
```
1. Create player health component (PlayerHealth.gd)   (feature, no deps)
2. Add weapon system (weapon.gd + weapon_registry.gd)  (feature, depends on 1)
3. Implement enemy damage response                     (feature, depends on 2)
4. Add hit detection and knockback                     (feature, depends on 2, 3)
5. Write GUT tests for combat system                   (feature, depends on 4)
6. Polish combat UI and feedback                        (polish, depends on 5)
```

### Step 3: Set dependencies
- Tasks that can run in parallel should have no shared dependencies
- Keep dependency chains short (3–4 max)
- Don't make everything depend on one task

### Step 4: Set priorities
- Blockers → 100
- Bugs → 80
- Core features → 50
- Nice-to-have / polish → 30–50

## Dependency Patterns

### Parallel (preferred where possible)
```
Task A (no deps)  ──┐
Task B (no deps)  ──┼──> Task D (deps: A, B)
Task C (no deps)  ──┘
```

### Sequential (unavoidable)
```
Task A → Task B → Task C → Task D
```

Keep chains to 3–4 tasks max. Long chains block everything.

## Checking Task Status

```bash
# All tasks
curl http://localhost:5001/api/tasks

# Dependency graph stats
curl http://localhost:5001/api/dependencies

# Tasks ready to run right now
curl http://localhost:5001/api/dependencies/ready

# Execution levels (what can run in parallel)
curl http://localhost:5001/api/dependencies/execution-order
```

## Updating Tasks

```bash
# Change priority
curl -X PUT http://localhost:5001/api/tasks/<id> \
  -H "Content-Type: application/json" \
  -d '{"priority": 100}'

# Reset a failed task
curl -X PUT http://localhost:5001/api/tasks/<id> \
  -H "Content-Type: application/json" \
  -d '{"status": "pending"}'

# Delete a task
curl -X DELETE http://localhost:5001/api/tasks/<id>
```

## Auto-Generated Tasks

The swarm automatically creates:
- **Refactor tasks** (priority 100) when any file exceeds `max_lines` (default 5000)
- **Validation bug tasks** (priority 100) when a completed task causes Godot/Python errors

You don't need to create these manually.

## Example: Feature Breakdown

### Inventory System
```
inventory-1: Create inventory data model and storage           (feature, 50, no deps)
inventory-2: Add item pickup and collection                    (feature, 50, deps: inventory-1)
inventory-3: Implement inventory UI display                    (feature, 50, deps: inventory-1)
inventory-4: Add item use/drop functionality                   (feature, 50, deps: inventory-2, inventory-3)
inventory-5: Polish inventory animations and sound effects     (polish,  50, deps: inventory-4)
```

### From PRD to Tasks
```
Use this skill to break down this PRD into actionable swarm tasks:
[paste PRD content here]
```

## Tips

1. **Be specific in descriptions** — the agent only has the description to go on
2. **Small tasks beat large ones** — one file change per task is ideal
3. **Any `+` or `and` in a task title is a red flag** — split before creating
4. **Tests are always a separate task** — with a dependency on the implementation task
5. **Check retries** — failed tasks auto-retry 3× by default; set `max_attempts` higher for complex work
6. **Don't over-depend** — parallel tasks finish faster than sequential chains
7. **Review history** — `GET /api/history` shows what agents actually did, helps refine future tasks

## Self-check before creating tasks

Before posting any task, ask:
- Does this touch more than 3 files? → Split
- Does the description have more than one sentence starting with a verb? → Split
- Does it say "and" between two distinct things? → Split
- Is implementation + tests bundled? → Split
