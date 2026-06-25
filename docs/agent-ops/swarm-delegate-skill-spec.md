# Swarm Delegate Skill — Spec

## Problem

Delegating work to the swarm from a Claude Code session currently requires:
- Multiple manual API calls to create tasks, check deps, verify genesis nodes
- No way to watch a task run in real time from inside Claude Code
- No structured lifecycle for "I want this done, tell me when it's finished"
- Project creation has footguns (scaffold tasks, missing managed_projects, genesis node issues)

## What We're Building

Three related things:

### 1. `/swarm-delegate` skill — fire-and-watch task delegation

For when Claude Code wants to hand off a specific piece of work to a swarm agent and report back.

**Invocation:**
```
/swarm-delegate project=dragon-mmo "Add a health regeneration system to the player"
/swarm-delegate project=swarm-controller type=bug "Fix the monitor lag caused by full table scans"
/swarm-delegate project=my-python-lib type=feature "Add JWT authentication middleware"
```

**Lifecycle the skill handles:**
1. Validate project exists and is managed
2. Create task via `POST /api/tasks/batch` (chained to project HEAD automatically)
3. Wait for agent to pick it up (poll `/api/tasks/<id>` until `in_progress`)
4. Stream agent log via SSE (`GET /api/agents/<agent_id>/stream`) — print key lines to terminal
5. Poll for completion
6. Report: success/failure, diff stat, any validation errors, next steps

**What it does NOT do:**
- Create the project (that's `/swarm-project`)
- Block the user — it can run in background and notify when done
- Retry indefinitely — after 3 attempts it surfaces the failure and stops

---

### 2. `/swarm-project` skill — full project creation (already exists, needs updating)

Extend the existing skill to handle project types cleanly:

**Godot project:**
- `swarm_create_project()` handles everything (git, Gitea, bootstrap, task seed)
- NO scaffold task — bootstrap is automatic
- First task = first real gameplay feature
- Verify genesis node after creation

**Python project:**
- `swarm_create_project(type="python")` 
- Scaffold task IS valid here (setup venv, install deps, create pyproject.toml)
- Auto-validates with pytest after each task

**General programming (TypeScript, Rust, Go, etc.):**
- Register as `python` type (closest match for validation)
- Pass language context in task descriptions
- Agents use generic prompts — task description carries the language/framework

**Expo/React Native:**
- `swarm_create_project(type="expo")`
- Scaffold task sets up Expo project structure

**New: instant project mode** (no LLM planning step):
```
/swarm-project name=my-service type=python overview="REST API for user management" instant=true
tasks=[
  "Setup FastAPI app with health endpoint",
  "Add SQLAlchemy models for users",
  "Add JWT auth middleware",
  "Add pytest test suite",
]
```

---

### 3. `swarm-code` CLI — Claude Code-like terminal harness

A standalone Python CLI that wraps the swarm API for interactive/fire-and-forget use.

**Usage:**
```bash
# Fire and forget — create task, stream log, exit when done
swarm-code dragon-mmo "add a leaderboard"

# Interactive chat mode (like Claude Code) — uses unified chat endpoint
swarm-code --chat dragon-mmo

# Watch a running agent
swarm-code --watch <agent_id>

# Status
swarm-code --status
```

**Implementation plan:**
- Single file: `tools/swarm-code.py` (or `bin/swarm-code`)
- Uses `requests` + `sseclient` for SSE streaming
- Reads `SWARM_URL` env var (default `http://localhost:5001`)
- For `--chat`: POST to `/api/unified-chat`, print reply, loop
- For fire-and-forget: POST task → poll for agent → stream SSE → print diff/result
- For `--watch`: directly connect SSE stream for a known agent_id

---

## Skill File Plan

### `/swarm-delegate` (new skill at `~/.claude/skills/swarm-delegate/SKILL.md`)

```markdown
---
name: swarm-delegate
description: Delegate a specific task to a swarm agent and watch it run. Use when you want to offload multi-file work, get a commit made, or run validation — and you want to stay informed of progress. Invoke with /swarm-delegate project=<name> [type=feature|bug|...] "<description>"
argument-hint: "project=<name> [type=feature|bug|refactor|polish|qa|research] \"<task description>\""
allowed-tools: Read, Grep, Glob, Bash
---
```

**Sections:**
1. Parse args (project, type, description)
2. Pre-flight: check project exists, check it's managed, check health
3. Create task (batch endpoint, chain to HEAD)
4. Wait for pickup (poll with timeout)
5. Stream log (SSE, filter to key lines: phase transitions, errors, TASK_COMPLETE)
6. Report result

**Key lines to surface from SSE stream:**
- `[Pipeline] Starting:` — what phases will run
- `PHASE:` markers — plan / scout / work / validate / repair
- `[PostValidation]` — script errors
- `Gateway error` — provider issues (don't panic the user, just note retrying)
- `Done. OK` / `Done. FAILED` — terminal result
- `diff:` — what changed

---

## Gaps in current `/swarm` skill to fix

1. **No watch/stream capability** — after creating tasks, no way to follow progress
2. **Project type specifics not documented** — Python vs Godot vs general differ in scaffold handling
3. **No instant-mode path** — always assumes LLM planning step via wizard
4. **No cleanup guidance** — what to do if delegation fails (reset task, check log, etc.)

---

## Build Order

1. Write `/swarm-delegate` skill file (pure markdown, immediately useful)
2. Update `/swarm-project` skill with Python/general project guidance
3. Build `tools/swarm-code.py` CLI (fire-and-forget mode first, then `--chat`)
4. Add `--watch` flag to `swarm-code` for tailing running agents

---

## Open Questions

- **SSE streaming in Claude Code context**: Claude Code's Bash tool runs commands to completion — for long-running streams we'd need to poll periodically rather than true stream. `/swarm-delegate` should probably poll `/api/tasks/<id>` + `/api/agents/<id>` every 30s and print a summary update rather than trying to tail SSE directly.
- **Notification on completion**: `/swarm-delegate` could use `PushNotification` when the task finishes so the user gets a phone notification even if they've switched to something else.
- **Project type detection**: for `/swarm-project`, should we auto-detect from the description ("I want a Godot game" vs "I want a FastAPI service") or always ask?
- **swarm-code auth**: if `login_required: true` is set in config, the CLI needs to handle session cookies. Probably just pass a `--token` flag.
