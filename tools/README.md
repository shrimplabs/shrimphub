# tools/

CLI utilities and test harnesses that ship with swarm-controller. All are
standalone scripts designed to work from a terminal or over SSH.

---

## shrimp-agent

An interactive local coding agent — think Claude Code, but powered by your
own LLMs (MiniMax M3, Athena qwen35, OpenRouter, etc.). Wraps
`swarm/agent_runtime.py` directly: same tool loop, same providers, same
compaction and stall detection as background swarm agents, but interactive.

```bash
# Interactive REPL in current directory
python3 tools/shrimp-agent.py

# One-shot task, exits when done
python3 tools/shrimp-agent.py "add dark mode to the settings screen"

# Override provider
python3 tools/shrimp-agent.py --provider athena-qwen35
python3 tools/shrimp-agent.py --provider claude

# Run in a specific project directory
python3 tools/shrimp-agent.py --dir ~/workspace/my-project

# Combine
python3 tools/shrimp-agent.py "fix the SSH timeout bug" --provider minimax --dir ~/workspace/my-project
```

**REPL commands:** `/clear` (wipe history), `/provider` (show active model), `/exit` or Ctrl+C

**Provider** defaults to `SWARM_PROVIDER` env var → `llm_provider` in `config.json` → `minimax`.
All providers registered in `config.json` are available.

**Pair with a mobile terminal:** SSH into your mac from your phone, run `shrimp-agent` in any project
directory — full coding agent in your mobile terminal.

**Related:** swarm-controller (this repo) · shrimp-router (model load balancer, round-robins across providers)

---

## Pipeline test harness

A family of scripts for exercising the agent pipeline end-to-end without
running the full swarm server. Run a full task type against a real or
synthetic project, get PASS/WARN/FAIL results per step.

### pipeline-probe.py — single pipeline run

Runs a single task through configurable pipeline phases against any provider.
No Flask, no SQLite, no orchestrator — just the LLM loop and tool execution.

```bash
python3 tools/pipeline-probe.py --task "add score display" --dir ~/workspace/my-game
python3 tools/pipeline-probe.py --task "fix the jump bug" --pipeline "scout → work" --provider athena
python3 tools/pipeline-probe.py --task "refactor main.gd" --pipeline "plan → scout → work" --provider ollama --model qwen2.5-coder:32b
```

### task_probe.py — dispatcher for task-type probes

Runs the probe suite for a specific task type against a project. Each probe
type has its own `<name>_probe.py` module with steps tailored to that type's
critical path.

```bash
.venv/bin/python tools/task_probe.py qa anti-grav-rush
.venv/bin/python tools/task_probe.py feature my-project
.venv/bin/python tools/task_probe.py harness_qa my-project
.venv/bin/python tools/task_probe.py art_pass my-project
.venv/bin/python tools/task_probe.py plan my-project
```

**Available probe modules:**

| Module | Task type | What it checks |
|--------|-----------|----------------|
| `qa_probe.py` | `qa` | Full QA stack: launch → TCP StateServer → screenshot → game_state → key_hold → play_macro |
| `feature_probe.py` | `feature` | Feature tool behavior + post-task validation in a temp Godot-shaped repo |
| `harness_qa_probe.py` | `harness_qa` | TestHarness checkpoint handshake with a scripted two-checkpoint scene |
| `art_pass_probe.py` | `art_pass` | Art pass critical path: asset detection, vision, write, commit |
| `plan_probe.py` | `plan` | Planner read-only enforcement + task creation via API |

### probe-batch.py — parallel multi-run comparison

Launches multiple `pipeline-probe.py` runs in parallel or sequentially,
collects JSON summaries, and prints a comparison table. Used for A/B testing
pipeline configurations.

```bash
python3 tools/probe-batch.py tools/specs/run12-baseline.json --parallel 4
python3 tools/probe-batch.py tools/specs/my-experiment.json --out data/probe-results/
```

Spec file format:
```json
{
  "default": { "dir": "/abs/path/to/project", "type": "bug", "provider": "minimax" },
  "runs": [
    {"name": "plan-scout-work",      "pipeline": "plan->scout->work",      "task": "Fix score not saving"},
    {"name": "plan-scout-diag-work", "pipeline": "plan->scout->diagnose->work", "task": "Fix score not saving"}
  ]
}
```

### pipeline-metrics.py — read/write ratio analysis

Reads agent logs and computes read/write tool call ratios for the work phase.
Useful for checking whether pipeline changes shift agents toward more targeted
writes vs. exploratory reads.

```bash
python3 tools/pipeline-metrics.py
python3 tools/pipeline-metrics.py --since 2026-07-01
```

### probe_analytics.py — probe result analytics

Aggregates probe run results across multiple sessions.

```bash
python3 tools/probe_analytics.py
```

---

## game_harness.py — LLM-driven game player

Launches a Godot game and lets an LLM play it autonomously: vision describe →
plan action → execute via StateServer. No swarm, no Flask, no SQLite. Uses
the same QA pipeline tools (`swarm/qa_tools.py`) as live QA agents.

```bash
.venv/bin/python tools/game_harness.py <project_name>
.venv/bin/python tools/game_harness.py anti-grav-rush --goal "reach 1000 score"
.venv/bin/python tools/game_harness.py anti-grav-rush --steps 30 --provider claude
```

**Related:** game-harness research project (OODA loop research on LLM-controlled game interfaces — orient/decide phases in progress).

---

## swarm-code.py — swarm API CLI

Fire tasks at the swarm, tail agent logs, chat with the co-pilot. Connects to
a running swarm-controller server (default: `http://localhost:5001`).

```bash
# Create a task and block until it finishes (streams agent log)
python3 tools/swarm-code.py <project> "<description>" --wait
python3 tools/swarm-code.py raccoon-city "fix the save bug" --type=bug --wait

# Fire and forget
python3 tools/swarm-code.py raccoon-city "add a leaderboard"

# Interactive chat REPL — global or project-scoped
python3 tools/swarm-code.py --chat
python3 tools/swarm-code.py --chat raccoon-city

# Pipe input
echo "how many agents running?" | python3 tools/swarm-code.py --chat

# Tail a running agent's log
python3 tools/swarm-code.py --watch <agent_id>

# Health + active agents + task counts
python3 tools/swarm-code.py --status
```

---

## Environment variables

| Var | Used by | Description |
|-----|---------|-------------|
| `SWARM_URL` | swarm-code | Swarm controller base URL (default: `http://localhost:5001`) |
| `SWARM_TOKEN` | swarm-code | Bearer token (if `login_required: true` in config) |
| `SWARM_PROVIDER` | shrimp-agent, pipeline-probe | Default LLM provider |
| `NO_COLOR` | all | Disable ANSI colours |
| `MINIMAX_API_KEY` | shrimp-agent, probes | MiniMax API key |
| `ANTHROPIC_API_KEY` | shrimp-agent, probes | Claude API key |
| `OPENROUTER_API_KEY` | shrimp-agent, probes | OpenRouter API key |

---

## Ecosystem links

| Component | URL | What it is |
|-----------|-----|------------|
| swarm-controller | this repo | Orchestrator + harnesses |
| shrimp-router | `http://localhost:8090` (configurable) | Model load balancer (round-robins across providers) |
| swarm dashboard | `http://localhost:5001` | Live agent dashboard |
| Local GPU scheduler | set via `ATHENA_SCHEDULER_URL` | Stable Diffusion / 3D asset generation |
