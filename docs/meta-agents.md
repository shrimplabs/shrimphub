# Meta-Agent Reference

Meta-agents are scheduled agents that operate across the entire swarm rather than on a single project. They form an observability and self-improvement layer on top of the standard agent pool. Each meta-agent has read access to all projects and limited write access (task creation only — no direct code edits to projects outside their designated scope).

All meta-agents are **off by default** and are gated by the global `meta_mode_enabled` flag. Enable individually via `config.json`.

---

## Taxonomy

```
Standard agents       → one project, one task, write code
────────────────────────────────────────────────────────
Meta-agents           → all projects, read + create tasks
  Gardener            → cross-project pattern detection     [IMPLEMENTED]
  Librarian           → prompt quality feedback loop        [IMPLEMENTED]
  Archaeologist       → stall detection + unblock tasks     [IMPLEMENTED]
  Cartographer        → project health narrative            [IMPLEMENTED]
  Meta-auditor        → systemic code quality / drift       [IMPLEMENTED]
  Scheduler           → time-based periodic task creation   [IMPLEMENTED]
```

---

## Gardener — Cross-Project Pattern Detection

**Status:** Implemented (`swarm/api_gardener.py`, `swarm/gardener_knowledge.py`, `prompts/gardener.yaml`)

**Role:** Survey all active projects, identify failure patterns that appear across multiple codebases, create targeted fix tasks, and maintain the shared knowledge base.

**Trigger:** Every 6 hours (configurable), or manually via `POST /api/gardener/run`.

**Inputs:**
- `GET /api/tasks` — recent failures across all projects
- `GET /api/agents` — active agent states
- `data/swarm_knowledge.jsonl` — existing known patterns

**Outputs:**
- New entries in `data/swarm_knowledge.jsonl` (JSONL, one pattern per line)
- `data/SWARM_KNOWLEDGE.md` (auto-generated markdown view)
- `data/GARDENER_REPORT.md` (human-readable run summary)
- Fix tasks created on affected projects

**Knowledge schema:**
```json
{
  "id": "uuid",
  "pattern_signature": "StateServer port collision on multi-agent projects",
  "confidence": "confirmed | suspected | disputed",
  "godot_version": "4.3",
  "first_seen": "2026-05-01",
  "last_seen": "2026-05-29",
  "ttl_days": 90,
  "affected_projects": ["pebble-pop", "gem-blaster"],
  "evidence_task_ids": ["bug-abc123", "bug-def456"],
  "fix_summary": "Add unique port per project in StateServer autoload",
  "status": "active | expired",
  "created_by": "gardener"
}
```

**API:**
- `GET /api/gardener/status` — enabled, last_run_ts, knowledge_count, last_report
- `POST /api/gardener/run` — trigger immediately
- `GET /api/gardener/knowledge` — all knowledge entries
- `GET|POST /api/gardener/config` — enabled, schedule, max_tasks_per_run, skip_projects

---

## Librarian — Prompt Quality Feedback Loop

**Status:** Implemented (`swarm/api_librarian.py`, `prompts/librarian.yaml`)

**Role:** Close the feedback loop between agent behavior in the field and the prompt files that drive them. Reads task failure histories, identifies where agents consistently misunderstand instructions or make the same mistakes, and proposes specific prompt edits as tasks.

**The problem it solves:** Prompt files are written once and rarely updated. Agents in production reveal gaps — repeated wrong tool usage, misunderstood task scope, incorrect validation patterns — but there's no mechanism to feed those observations back into the prompts. The Librarian automates this.

**Trigger:** After every N task completions (suggested: 50), or weekly.

**Inputs:**
- `data/task-history.jsonl` — completed and failed task records
- `data/agent-history.jsonl` — agent metadata including exit reason and failure context
- `prompts/*.yaml` — current prompt contents
- `data/swarm_knowledge.jsonl` — known cross-project patterns (informs what to look for)

**Process:**
1. Group recent failures by task type (bug, feature, qa, etc.)
2. For each type, identify recurring failure signatures in `metadata.last_failure`
3. Cross-reference with the relevant prompt file
4. Identify the specific instruction gap that likely caused the pattern
5. Propose a concrete prompt edit as a `refactor` task on `swarm-controller`

**Outputs:**
- `refactor` tasks on `swarm-controller` that modify specific `prompts/*.yaml` files
- `data/LIBRARIAN_REPORT.md` with analysis and proposed changes
- Optionally updates `data/swarm_knowledge.jsonl` with prompt-related patterns

**Two modes (toggled via dashboard or `POST /api/librarian/autonomous-edits`):**

- **Autonomous edits OFF** (default): Creates refactor tasks on `swarm-controller` with before/after proposed changes. Human-reviewed agent applies the edit.
- **Autonomous edits ON**: Directly edits `prompts/*.yaml` and commits via git with a clear message (e.g. `librarian: tighten bug.yaml validation instructions`). Git is the rollback safety net. Findings still written to `LIBRARIAN_REPORT.md`.

**Constraints:**
- Max 3 prompt edits/tasks per run regardless of mode
- Never touches non-prompt files
- Must include before/after in task description when in task-creation mode

**Config keys:**
```json
{
  "librarian_enabled": false,
  "librarian_autonomous_edits": false,
  "librarian_trigger_interval": 50,
  "librarian_max_prompt_tasks": 3
}
```

---

## Archaeologist — Stall Detection and Recovery

**Status:** Implemented (`swarm/api_archaeologist.py`, `prompts/archaeologist.yaml`)

**Role:** Investigate projects that have gone silent and produce a recovery plan. Where the Gardener looks for patterns across many projects, the Archaeologist goes deep on one specific dead or stalled project and figures out what happened, what the current state is, and what it would take to continue.

**The problem it solves:** Projects fall into death spirals — repeated validation failures, cascading recovery tasks, or simply no more tasks in the queue. The standard system doesn't know what to do next. The Archaeologist provides a diagnosis and a concrete recovery path.

**Trigger:** Projects that meet one or more of:
- No successful task completion in > 72 hours
- All tasks failed or cancelled, queue empty
- Stuck in a recovery chain with > 5 failed attempts
- Manually requested via `POST /api/archaeologist/investigate/<project>`

**Inputs:**
- `git log` of the project (last 30 commits)
- Current task queue state for the project
- Recent agent logs for the project
- `GAME_DESIGN.md` (if present)
- Project file structure

**Process:**
1. Read the git history to understand what was last attempted
2. Read recent failure context from task metadata
3. Assess current code state (does it run? does it compile? what's missing?)
4. Identify the root cause of the stall
5. Produce a sequenced recovery plan as a `project_plan`-style task DAG

**Outputs:**
- `ARCHAEOLOGY_REPORT.md` in the project root with findings
- A sequenced set of tasks (bug fixes → feature work → QA) to resume the project
- Optionally resets failed tasks to pending if they're safe to retry

**Constraints:**
- Read-only on the project codebase (no direct edits)
- Creates tasks rather than fixing things directly
- Skips projects that are paused or in `gardener_skip_projects`

**Config keys:**
```json
{
  "archaeologist_enabled": false,
  "archaeologist_stall_threshold_hours": 72,
  "archaeologist_max_concurrent": 2
}
```

---

## Cartographer — Project Health Narrative

**Status:** Implemented (`swarm/api_cartographer.py`, `prompts/cartographer.yaml`)

**Role:** Maintain a live, human-readable map of the entire swarm's state. Goes beyond the numeric health score to produce narrative descriptions of what each project is doing, where it's stuck, and how long it's been in that state. Both the dashboard and other meta-agents consume this.

**The problem it solves:** The dashboard shows metrics and task counts, but doesn't answer the question "what is actually happening with this project right now?" A project with 3 failed tasks and 1 pending could be healthy (agents are working through bugs) or in crisis (same bug failing for 3 days). The Cartographer provides the narrative context.

**Trigger:** Every 2 hours, or on-demand. Output is a flat file that other components read — does not need to block anything.

**Inputs:**
- All project task queues
- Agent activity history
- Health scores
- Recent commit dates
- `data/swarm_knowledge.jsonl` (to annotate known patterns)

**Output:** `data/PROJECT_MAP.md`

```markdown
## pebble-pop  ⚠ STALLED
Health: 42/100 | Last completion: 18h ago | Active agents: 0

Stuck on a memory leak in the bubble physics system. 12 failed attempts across
2 recovery chains over 3 days. The error signature matches a known Godot 4.3
physics body issue (see swarm_knowledge: "RigidBody2D linear_velocity reset bug").
Archaeologist trigger threshold reached — investigation recommended.

## gem-blaster  ✓ ACTIVE  
Health: 87/100 | Last completion: 23min ago | Active agents: 2

Feature work in progress: gem matching logic and score multiplier. QA scheduled
after next 6 completions. No known issues.
```

**Additional outputs:**
- `data/SWARM_SUMMARY.json` — machine-readable version for API/dashboard consumption
- Annotations on known-pattern matches from the Gardener knowledge base

**Config keys:**
```json
{
  "cartographer_enabled": false,
  "cartographer_interval_hours": 2
}
```

---

## Meta-Auditor — Systemic Code Quality

**Status:** Implemented (`swarm/api_meta_auditor.py`, `prompts/meta_auditor.yaml`)

**Role:** The existing `audit` task type runs on-demand for a single project. The meta-Auditor looks *across* all projects for systemic issues — structural problems that affect many projects because they share a common origin, template, or pattern.

**The problem it solves:** When a template file (e.g. `state_server.gd`) has a bug, every project that copied it has the same bug. Per-project audits find it in each project independently and create N identical fix tasks. The meta-Auditor finds it once and creates N coordinated fix tasks with correct dependency ordering.

**Trigger:** Weekly, or after a template file is updated in `swarm-controller`.

**Focus areas:**
- Template drift: projects using outdated versions of `state_server.gd`, `test_harness.gd`
- Missing required files: StateServer not registered in `project.godot`, GUT installed but no tests
- Structural anti-patterns: shared scene naming conflicts, autoload collisions
- Dependency hygiene: tasks with no dependencies (floating), tasks depending on deleted tasks

**Outputs:**
- Coordinated fix tasks across affected projects (not N independent tasks — properly sequenced)
- `data/AUDIT_REPORT.md` with systemic findings
- Template sync tasks if `templates/godot/` files have drifted

**Constraints:**
- Does not duplicate work already covered by per-project audits
- Groups related fixes into batches rather than spamming individual tasks
- Max 20 tasks per run

**Config keys:**
```json
{
  "meta_auditor_enabled": false,
  "meta_auditor_interval_days": 7,
  "meta_auditor_max_tasks": 20
}
```

---

## Scheduler — Periodic Task Creation

**Status:** Implemented (`swarm/api_scheduler.py`)

**Role:** Creates tasks for projects on a configured time-based schedule. Distinct from the orchestrator's slot-filling logic — the Scheduler fires specific task types at specific intervals regardless of queue depth.

**The problem it solves:** The current system fills slots naively (least-recently-worked project gets the next agent). It doesn't know that running 20 feature agents and 5 QA agents simultaneously causes rate limit pressure, or that a project with a demo tomorrow should get priority, or that 3am is a good time to run expensive research tasks.

**Trigger:** Every 15 minutes (does not spawn agents itself — adjusts config for the orchestrator to act on).

**Inputs:**
- Current agent slot usage and project distribution
- Task type breakdown (how many QA vs feature vs bug agents are running)
- API quota usage (from `GET /api/quota-limit`)
- Project health scores
- Time of day / day of week
- `data/PROJECT_MAP.md` (from Cartographer, if available)

**Capabilities:**
- Adjust `max_active_agents` ceiling based on quota pressure
- Pause/unpause specific projects dynamically
- Promote project priority based on rules (e.g. projects with QA failures get more bug agent slots)
- Schedule expensive task types (research, harness_qa) for off-peak hours via `run_after`

**Outputs:**
- Config adjustments via internal API calls (no external file writes)
- `data/SCHEDULER_LOG.md` — decision log with reasoning for each adjustment

**Constraints:**
- Never kills running agents (only affects new spawns)
- All adjustments are reversible and logged
- Human can override via dashboard at any time

**Config keys:**
```json
{
  "scheduler_enabled": false,
  "scheduler_interval_minutes": 15,
  "scheduler_allow_pause": true,
  "scheduler_allow_agent_ceiling_adjust": true,
  "scheduler_off_peak_hours": [0, 6]
}
```

---

## Implementation Status

| Agent | Status | Config key |
|---|---|---|
| Gardener | ✅ Implemented | `gardener_enabled` |
| Librarian | ✅ Implemented | `librarian_enabled` |
| Cartographer | ✅ Implemented | `cartographer_enabled` |
| Archaeologist | ✅ Implemented | `archaeologist_enabled` |
| Meta-auditor | ✅ Implemented | `meta_auditor_*` |
| Scheduler | ✅ Implemented | `scheduler_enabled` |

All meta-agents require `meta_mode_enabled: true` in `config.json` as a master gate.

---

## Shared Infrastructure

All meta-agents share:

- **Prompt location:** `prompts/<agent_name>.yaml`
- **API module:** `swarm/api_<agent_name>.py` (registered in `api.py`)
- **Report output:** `data/<AGENT_NAME>_REPORT.md`
- **Config pattern:** `<agent_name>_enabled`, `<agent_name>_interval_*`, persisted to `config.json`
- **Task creation:** Always chained to project head via `chain_to_project_head()`
- **Scheduling:** `threading.Timer` with auto-reschedule, started in `register_routes()`
- **Task type:** Agent's own name (e.g. `type: "gardener"`) so the prompt loader finds the right YAML

The `api.py` registration block for each meta-agent follows the same pattern as the Gardener:
```python
from swarm.api_<name> import register_routes as _reg_<name>
_reg_<name>(app, config, data_dir, config_file=config_file, _config_write_lock=_config_write_lock)
```
