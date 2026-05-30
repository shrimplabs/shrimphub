# Task Probe Handoff

**Project:** swarm-controller  
**Date:** 2026-05-24  
**Tracking:** beads epic `swarm-controller-wdqp`  
**Contact:** Maintainer

---

## What This Is

The Paraxenia swarm is an agent orchestration system that spawns LLM-powered subprocesses to build, refactor, QA, and plan Godot games. It has several task types: `feature`, `bug`, `qa`, `harness_qa`, `plan`, `art_pass`, `refactor`.

We recently found **8 silent bugs** in the QA pipeline — bugs that caused every QA agent to fail without any visible error. We found them by building a systematic probe tool (`qa_probe.py`) that validates each layer of the pipeline independently. The result: QA agents went from 0% success to 15/15 successful steps.

The goal of this work is to apply the same probe methodology to the remaining task types. Each task type has a critical path — a sequence of operations that must all work for the agent to succeed. We want a probe for each one so we can catch silent failures before they waste agent cycles.

---

## The Reference Implementation

Before writing any new code, **read and run these two tools**. They are the pattern everything else should follow.

### `tools/qa_probe.py` — the probe pattern

```bash
.venv/bin/python tools/qa_probe.py anti-grav-rush
```

This runs 6 numbered probes against the QA pipeline:

```
✓ 1-launch          PASS  pid=1234  state_port=11011
✓ 2-state-server    PASS  root node type=Window
✓ 3-screenshot      PASS  source=state_server
✓ 4-game-state      PASS  11 keys
✓ 5-key-hold        PASS  position changed
✓ 6-play-macro      PASS  3 actions executed

6/6 passed, 0 warnings, 0 failures
```

Key design principles:
- Each probe is independent — a failure in probe 3 doesn't skip 4-6
- Each probe tests exactly one thing
- Output is human-readable and unambiguous
- The tool cleans up after itself (kills the game)

### `tools/game_harness.py` — the full loop

```bash
.venv/bin/python tools/game_harness.py anti-grav-rush --steps 15 --provider minimax
```

This launches a game, takes screenshots, asks an LLM what to do, and executes actions via StateServer. It's what we built *after* the probe confirmed the stack was healthy. You don't need to touch this for the probe work.

### Research context

`game-harness/research/02a-qa-pipeline-validation.md` — full write-up of every bug found during QA probe development, architectural insights, and the probe template for each task type. **Read this before starting.**

---

## What Needs to Be Built

Beads epic: `swarm-controller-wdqp`  
Check current status with: `bd list --status=open`

### Issue 1: Framework (`swarm-controller-wdqp`) — START HERE

Build `tools/task_probe.py` — a shared base that all probes use.

The current `qa_probe.py` is ~300 lines and mostly boilerplate (argument parsing, colour output, summary table, cleanup). Extract this into a reusable runner so each new probe only needs to define its steps.

Suggested interface:

```python
# tools/task_probe.py
class ProbeStep:
    name: str
    run: Callable[[], tuple[bool, str]]  # (passed, detail)

class ProbeRunner:
    def __init__(self, steps: list[ProbeStep], project: str, config: dict): ...
    def run_all(self) -> bool: ...  # prints results, returns True if all passed
```

Each probe file (`feature_probe.py`, `plan_probe.py`, etc.) imports `ProbeRunner` and defines its steps. The CLI dispatches by name:

```bash
.venv/bin/python tools/task_probe.py qa anti-grav-rush
.venv/bin/python tools/task_probe.py feature my-project
.venv/bin/python tools/task_probe.py plan my-project
```

Output format: match `qa_probe.py` exactly (coloured checkmarks, summary table).

---

### Issue 2: `feature_probe` (`swarm-controller-kil9`)

Validates the critical path a feature agent must execute:

| Step | What to test | How to verify |
|------|-------------|---------------|
| 1-read | `read_file` on a known file | File contents returned, no error |
| 2-write | `write_file` with a small change | File on disk matches what was written |
| 3-commit | `git_commit` with a message | `git log -1` shows the commit |
| 4-validation-fires | Post-task validation runs | Check that `check_scripts.gd` is invoked after commit |
| 5-pass-no-bug | Clean code → no bug task spawned | No new tasks in DB with `type=bug` |
| 6-fail-spawns-bug | Bad code → bug task spawned | Bug task appears with `priority=100`, `last_failure` in metadata |

**Setup needed:** The probe needs to make actual API calls to the swarm at `http://localhost:5001`. It does not spawn a real agent — it calls the tool functions directly via `swarm/tools/core.py` and checks the side effects.

For steps 5 and 6: you'll need to trigger post-task validation directly. See `swarm/agent_finish.py:_post_task_validation_in_worktree()` and `swarm/validation.py`.

Use `anti-grav-rush` or any managed Godot project as the target. Don't commit garbage to real projects — use a git worktree or a scratch branch.

---

### Issue 3: `harness_qa_probe` (`swarm-controller-p072`)

Validates the `harness_qa` checkpoint handshake. This is the least-tested path in the system.

| Step | What to test | How to verify |
|------|-------------|---------------|
| 1-launch | `harness_launch_game` connects to harness port | Returns `{ok: true, pid: N}` |
| 2-port | Harness TCP port accepts a connection | `socket.connect(localhost, harness_port)` succeeds |
| 3-checkpoint | Game calls `TestHarness.checkpoint(state_dict)` | Probe receives checkpoint over TCP within 10s |
| 4-pass | Probe sends pass response | Game continues (verify via game_state change) |
| 5-fail | Probe sends fail response | Game reports failure / halts checkpoint sequence |

**Requires:** A project with `TestHarness` autoload registered in `project.godot` and at least one `await TestHarness.checkpoint(...)` call in game code. Use `anti-grav-rush` if it has this, otherwise check `templates/godot/autoload/test_harness.gd` and wire it into a test project.

See `swarm/qa_tools.py:harness_launch_game()` and `harness_step()` for the harness-side implementation.

---

### Issue 4: `plan_probe` (`swarm-controller-pgkc`)

Validates that plan agents can create tasks and wire dependencies. Simplest probe to build.

| Step | What to test | How to verify |
|------|-------------|---------------|
| 1-list-tasks | `GET /api/tasks?project=X` | Returns JSON list, no error |
| 2-create-task | `POST /api/tasks` with description | Task appears in DB, has an ID |
| 3-dep-wiring | Create task with `depends_on=[<id>]` | `GET /api/tasks/<id>/dependencies` returns parent |
| 4-write-blocked | Attempt `write_file` via agent tool | Returns `{ok: false, error: "write tools are blocked..."}` |
| 5-cleanup | Delete the probe tasks | Tasks gone from DB |

This probe makes direct HTTP calls to `http://localhost:5001/api/`. No game process needed.

For step 4: call `swarm.tools.core.write_file()` with `task_type="plan"` set in the agent config to verify the block is enforced. See `swarm/tools/core.py` for how write blocking is implemented.

---

### Issue 5: `art_pass_probe` (`swarm-controller-y0s0`)

Validates the art pass pipeline: vision assessment → asset discovery → file write.

| Step | What to test | How to verify |
|------|-------------|---------------|
| 1-launch | Same as qa_probe step 1 | Game running, StateServer up |
| 2-screenshot | `take_screenshot()` | Image file on disk, non-zero size |
| 3-vision | `vision_query(path, "describe the UI")` | Non-empty string returned within 30s |
| 4-asset-list | `list_directory` of asset library path | Returns files |
| 5-file-copy | Copy a known asset into project dir | File exists at destination |
| 6-commit | `git_commit` | Commit appears in log |
| 7-cleanup | Remove the test asset, commit cleanup | Project back to clean state |

**Requires:** An asset library path configured in `config.json`. Check current config with `cat config.json | python3 -m json.tool`. If no asset library is configured, this probe can be deferred.

---

## Environment Setup

```bash
cd /path/to/swarm-controller

# Create virtualenv and install deps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Copy and edit config
cp config.example.json config.json
# Set: workspace path, godot_path, llm_provider, MINIMAX_API_KEY in .env

# Start the swarm API (needed for plan_probe and feature_probe)
.venv/bin/python swarm_runner.py api

# Verify the QA probe passes before starting new work
.venv/bin/python tools/qa_probe.py anti-grav-rush
```

The swarm dashboard is at `http://localhost:5001`. Use it to verify tasks are being created/deleted during probe runs.

---

## Key Files to Read

| File | Why |
|------|-----|
| `tools/qa_probe.py` | The reference probe — read this first |
| `tools/game_harness.py` | The full harness loop — understand the architecture |
| `game-harness/research/02a-qa-pipeline-validation.md` | Full write-up of what was built and why |
| `swarm/qa_tools.py` | All QA tool implementations (launch, screenshot, state, key_hold, etc.) |
| `swarm/tools/core.py` | Agent tool implementations (read_file, write_file, git_commit, create_task, etc.) |
| `swarm/agent_finish.py` | Post-task validation pipeline |
| `swarm/validation.py` | Project type detection and validation logic |
| `CLAUDE.md` | Full system architecture — required reading |

---

## Workflow

Use beads (`bd`) for tracking:

```bash
bd ready                          # see what's available to work on
bd show swarm-controller-wdqp    # see the epic and children
bd update swarm-controller-wdqp --claim   # claim before starting
bd close swarm-controller-wdqp           # close when done
```

Start with the framework (`swarm-controller-wdqp`) — the probe issues are blocked on it. Build the framework first, then implement probes in priority order: `feature_probe` → `harness_qa_probe` → `plan_probe` → `art_pass_probe`.

Each probe should produce output that matches `qa_probe.py`: numbered steps, coloured PASS/FAIL, summary table. Run it, get all green, move on.
