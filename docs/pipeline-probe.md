# pipeline-probe

`tools/pipeline-probe.py` — a standalone harness for testing pipeline phases against any project directory, any LLM provider, with zero blast radius on the swarm.

## What it is

The swarm runs pipeline agents (plan → scout → work → validate) as background subprocesses with full DB state, task queues, and agent lifecycle. That makes it hard to experiment: every test ties up a slot, costs a real task record, and can affect dependent tasks.

`pipeline-probe` bypasses all of that. It patches the `agent_runtime` globals directly, calls `run_pipeline()` with a one-shot `TaskState`, streams output to your terminal, and exits. No DB writes, no agent spawn, no worktrees, no orchestrator involvement.

Use it when you want to:
- Test whether scout actually finds the right files in a new project
- Experiment with a local LLM (Athena, Ollama) without touching live work
- Diagnose why the scout isn't converging — see model reasoning in real time
- Benchmark how long the plan phase takes on a given provider
- Test a prompt change before deploying it to the fleet

## Usage

```bash
# Full pipeline against a project (uses minimax by default)
python3 tools/pipeline-probe.py \
  --task "add a high score display to the main HUD" \
  --dir ~/workspace/my-game

# Scout phase only — fast way to check if the model finds the right files
python3 tools/pipeline-probe.py \
  --task "find where score is tracked" \
  --dir ~/workspace/chronosymphony \
  --pipeline "scout" \
  --type research

# Test a local LLM (Athena) on the full pipeline
python3 tools/pipeline-probe.py \
  --task "fix the jump feel" \
  --dir ~/workspace/my-game \
  --pipeline "plan → scout → work" \
  --provider athena \
  --model laguna-q5ks-ctx256k-noop

# See the model's reasoning between tool calls
python3 tools/pipeline-probe.py \
  --task "find where score is tracked" \
  --dir ~/workspace/chronosymphony \
  --pipeline "scout" \
  --verbose

# Dry run — print config without running
python3 tools/pipeline-probe.py --task "..." --dir ... --dry-run
```

## Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--task` | required | Task description |
| `--dir` | `.` | Project directory |
| `--type` | `feature` | Task type (`feature`, `bug`, `research`, `refactor`, etc.) |
| `--pipeline` | `plan → scout → work` | Phases to run (any subset, any order) |
| `--provider` | `minimax` | LLM provider (`minimax`, `athena`, `claude`, `openrouter`, etc.) |
| `--model` | provider default | Override model name |
| `--verbose` | off | Print model reasoning prose between tool calls |
| `--dry-run` | off | Print config and exit |

## What the output means

```
[Pipeline:scout] Scout loop 12/24         ← loop counter within phase
[Pipeline:scout] Tools: read_file_range   ← tool(s) called this loop
[Agent] Executing tool: read_file_range   ← execution confirmed
--- model reasoning ---                   ← only with --verbose
Now let me check the score display...     ← model's actual prose
[Pipeline:scout] Scout complete at loop 18
[Pipeline:scout] Report: 8 files, 11 findings, confidence=0.85
  ✓ SCOUT complete (94.2s)
```

Confidence < 0.5 + loop limit hit = scout didn't produce `SCOUT_COMPLETE`, fallback report used. Confidence ≥ 0.7 + completed before limit = good structured handoff to work phase.

## What it reveals

Running with `--verbose` lets you see the model's decision-making between tool calls. This has already exposed two real issues:

1. **`search_code` scope leak** (fixed): Without `PROJECT_PATH_OVERRIDE`, `search_code` walked the swarm-controller repo instead of the target project. The model self-corrected at loop 10, wasting 2-3 loops.

2. **Scout early-completion floor** (fixed, two iterations): Originally used `max(2, len(reading_list))` as the minimum loop count — a 15-file reading list produced min=15, blocking scout at loop 13 with spurious "too early" rejections. Fixed: floor is now a flat 3 loops when a reading list exists (the list already guided real work, so a light floor is sufficient), 5 loops without a reading list. Also fixed: when a SCOUT_COMPLETE is rejected for being too early, the `_consecutive_stalls` counter is now reset — previously the rejection would cause a stall increment and fire a contradictory "output SCOUT_COMPLETE now" nudge on the very next loop.

3. **Plan phase stalling** (fixed): Plan loops with no tool call and no PLAN_COMPLETE (model generating prose without committing) now get escalating nudges — stall 1: soft prompt, stall 2+: "output PLAN_COMPLETE now, no more prose."

4. **Plan loop budget** (fixed): Bug/refactor/research tasks need to search the codebase before they can plan. The flat 10-loop limit caused them to hit the ceiling mid-search. New per-type defaults: bug=15, refactor=15, research=15, audit=15. Feature/polish keep 10. Config overrides still take precedence.

## Known limitations

- No git worktree: writes go directly to the project directory. Only run work phase on a project you're OK modifying, or on a throwaway copy.
- Provider configs come from `.env` in the swarm-controller root. Athena needs no key.
- The scout's `search_code` now correctly scopes to the project path (via `PROJECT_PATH_OVERRIDE`). Older probes before this fix would pick up hits from the swarm-controller codebase itself.
- Plan phase requires the LLM to output structured JSON. Some small models fail this and need prompt tuning.

## Adding a new provider

Add it to `LLM_PROVIDERS` in the harness, or configure it in `config.json` and the probe will pick it up via the standard provider resolution path:

```python
# In pipeline-probe.py
_providers["my-local"] = {
    "base_url": "http://localhost:11434/v1",
    "api_key": "ollama",
    "model": "qwen2.5-coder:32b",
    "format": "openai",
}
```
