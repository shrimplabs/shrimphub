# Cerebellar QA World Model

## Motivation

The current QA agent is slow because it leans heavily on vision queries (~9s each) to verify
game state. Most of those calls are asking questions that could be answered deterministically:
"did the score increase?", "did the ball count decrease?", "is the game over?". The StateServer
already provides this data as structured JSON at ~2ms per poll.

The biological analogy: the cerebellum runs a fast predictive loop that handles routine
expectations, escalating to the cortex only on surprise. We can build the same two-tier
architecture for playtesting:

- **Cerebellar loop**: pure Python invariant checks against StateServer at 10hz — microseconds per tick
- **Cortex escalation**: vision_query + LLM reasoning only when the cerebellar loop detects a violation

This should cut vision_query calls by ~80% on well-modelled games and catch bugs the current
agent misses (e.g. transient state violations that happen and recover within a second, invisible
to a screenshot-based agent).

## Research Basis

- **Code World Models** (Hafner et al. / follow-on work): LLMs can generate executable transition
  functions from natural language rules with zero RL training. The generated code substitutes for
  learned neural weights.
- **GliDe** (2025): Zero-shot glitch detection across 120 games using LLM reasoning over state
  transitions — no per-game training required.
- **World Models for Anomaly Detection** (arxiv 2503.02552): prediction error as anomaly signal
  during RL inference. The key insight: you don't need a neural world model if you have symbolic
  state — the prediction error is computable directly.
- **DreamerV3 / RSSM**: pixel-space world models require RL training. Symbolic state (StateServer)
  bypasses this entirely.

## Architecture

```
QA Agent (LLM, slow)
    │
    ├─ read GAME_DESIGN.md
    ├─ generate world_model.py  (write_file — LLM generates invariant code)
    ├─ launch_game()
    │
    ├─ play_macro(intro_sequence)   ← get game into testable state
    │
    ├─ run_world_model_loop(        ← NEW: cerebellar loop
    │      duration=60,
    │      world_model_path="world_model.py"
    │  )
    │   returns: {violations: [...], ticks: N}
    │
    ├─ for each violation:
    │   ├─ take_screenshot() at violation timestamp
    │   └─ vision_query(screenshot, "confirm: is this actually a bug?")
    │       ← cortex only fires on surprise
    │
    └─ create_bug_task() for confirmed violations
```

## New Tool: `run_world_model_loop`

### Signature
```python
def run_world_model_loop(
    duration: float,               # seconds to run (default 60)
    world_model_path: str,         # path to world_model.py (default: <project_path>/world_model.py)
    interval: float = 0.1,         # poll interval in seconds (10hz default)
    port: int = None,              # StateServer port; defaults to current _state_port
) -> dict:
    """
    Poll StateServer at `interval` hz for `duration` seconds.
    On each tick, call world_model.check(prev_state, curr_state, dt) from world_model_path.
    Collect violations. Return summary.
    """
```

### Return value
```json
{
  "ok": true,
  "ticks": 600,
  "duration_actual": 60.1,
  "violations": [
    {
      "rule": "score_never_decreases",
      "tick": 142,
      "timestamp": 14.2,
      "prev_state": {"score": 150, "ball_count": 3},
      "curr_state": {"score": 100, "ball_count": 3},
      "screenshot_path": "/path/to/qa_screenshots/violation_142.png"
    }
  ],
  "stateserver_errors": 0
}
```

Each violation automatically takes a screenshot at the moment of detection so the QA agent
can pass it directly to vision_query for confirmation without re-reproducing the bug.

### Implementation (swarm/qa_tools.py)

```python
def run_world_model_loop(
    duration: float = 60,
    world_model_path: str = None,
    interval: float = 0.1,
    port: int = None,
) -> dict:
    import time as _time
    import importlib.util

    # Resolve world model path
    if world_model_path is None:
        proj = _project_path or ""
        world_model_path = str(Path(proj) / "world_model.py")

    # Load world model module
    checker = None
    if Path(world_model_path).exists():
        spec = importlib.util.spec_from_file_location("world_model", world_model_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        checker = getattr(mod, "check", None)

    violations = []
    stateserver_errors = 0
    prev_state = None
    t0 = _time.monotonic()
    tick = 0

    while _time.monotonic() - t0 < duration:
        tick_start = _time.monotonic()
        state = get_game_state(port=port)

        if "error" in state:
            stateserver_errors += 1
        else:
            if checker and prev_state is not None:
                dt = _time.monotonic() - t0
                try:
                    found = checker(prev_state, state, dt)
                    for v in (found or []):
                        # Auto-screenshot at violation
                        shot_name = f"qa_screenshots/violation_{tick:04d}.png"
                        shot = take_screenshot(shot_name)
                        v["tick"] = tick
                        v["timestamp"] = round(dt, 3)
                        v["prev_state"] = prev_state
                        v["curr_state"] = state
                        v["screenshot_path"] = shot.get("path", "")
                        violations.append(v)
                        log(f"[world_model] violation at t={dt:.1f}s: {v.get('rule','?')}")
                except Exception as e:
                    log(f"[world_model] checker error: {e}")
            prev_state = state

        tick += 1
        elapsed = _time.monotonic() - tick_start
        sleep_time = max(0, interval - elapsed)
        _time.sleep(sleep_time)

    return {
        "ok": True,
        "ticks": tick,
        "duration_actual": round(_time.monotonic() - t0, 2),
        "violations": violations,
        "stateserver_errors": stateserver_errors,
        "world_model_loaded": checker is not None,
    }
```

### Registration (swarm/tool_dispatch.py)

Add to `_TOOL_REQUIRED_ARGS`:
```python
"run_world_model_loop": [],  # all args optional
```

Add to dispatch table:
```python
"run_world_model_loop": lambda args: run_world_model_loop(
    duration=args.get("duration", 60),
    world_model_path=args.get("world_model_path"),
    interval=args.get("interval", 0.1),
    port=args.get("port"),
),
```

## World Model Format

The QA agent generates `world_model.py` after reading GAME_DESIGN.md. Format:

```python
"""
World model for <game_name>.
Generated by QA agent from GAME_DESIGN.md on <date>.
Updated by subsequent QA cycles as edge cases are discovered.
"""

def check(prev: dict, curr: dict, dt: float) -> list:
    """
    Check state transition for invariant violations.
    
    prev: game_state snapshot from previous tick
    curr: game_state snapshot from current tick  
    dt:   seconds elapsed since loop started
    
    Returns list of violation dicts, each with at minimum {"rule": "<name>"}.
    Return [] or None if no violations.
    """
    violations = []

    # Score must never decrease during active play
    if (curr.get("game_active") and
            prev.get("score") is not None and
            curr.get("score", 0) < prev.get("score", 0)):
        violations.append({
            "rule": "score_never_decreases",
            "detail": f"score dropped from {prev['score']} to {curr['score']}",
        })

    # Ball count must never increase (no balls should spawn mid-game)
    if (prev.get("ball_count") is not None and
            curr.get("ball_count", 0) > prev.get("ball_count", 0)):
        violations.append({
            "rule": "ball_count_never_increases",
            "detail": f"ball_count went {prev['ball_count']} -> {curr['ball_count']}",
        })

    # Peg count must never increase (pegs don't respawn)
    if (prev.get("peg_count") is not None and
            curr.get("peg_count", 0) > prev.get("peg_count", 0)):
        violations.append({
            "rule": "peg_count_never_increases",
            "detail": f"peg_count went {prev['peg_count']} -> {curr['peg_count']}",
        })

    # Game should not stay frozen (same state for > 10s while ball is in flight)
    if (curr.get("ball", {}).get("in_flight") and
            prev.get("ball", {}).get("position") == curr.get("ball", {}).get("position") and
            dt > 10):
        violations.append({
            "rule": "ball_not_frozen",
            "detail": "ball in_flight but position unchanged",
        })

    return violations
```

The agent is instructed to derive invariants from GAME_DESIGN.md:
- Monotonic counters (score, level, coins)
- Non-respawning entities (pegs, enemies, obstacles)
- Expected scene transitions (menu → game → game_over within N seconds of action)
- Physics sanity (ball position changes while in_flight)
- Economy rules (spending can't exceed balance)

## Updated qa.yaml Flow

New step inserted after `launch_game`, before vision-heavy gameplay:

```yaml
WORLD MODEL GENERATION (do this after reading GAME_DESIGN.md, before launching):
  1. From GAME_DESIGN.md, identify all numeric state fields and their invariants
  2. write_file("<project_path>/world_model.py", <generated_code>)
  3. Launch game, play into active state via play_macro
  4. run_world_model_loop(duration=60) — let it observe a full play session
  5. For each violation in result["violations"]:
     - vision_query(violation["screenshot_path"], "Is this a real bug? Describe what went wrong.")
     - If confirmed: create_bug_task()
  6. Only use vision_query for violation confirmation — not for routine state checks
```

Additional instruction for the agent:
- If `world_model.py` already exists in the project (from a previous QA cycle), read it first
  and extend it rather than overwriting — preserves learned edge cases
- After a bug is fixed and confirmed resolved, add the fixed behaviour as a positive invariant

## World Model Persistence and Learning

`world_model.py` lives in `<project_path>/world_model.py` and is committed to the game repo.
Over QA cycles it accumulates knowledge:

- **Cycle 1**: LLM generates initial invariants from GAME_DESIGN.md
- **Bug found**: violation snapshot captured, bug task created, bug fixed
- **Cycle 2**: QA agent reads existing world_model.py, appends new invariant learned from the bug
- **Cycle N**: world model is a growing, game-specific invariant suite

This is the autonomous learning loop — no human labeling, no RL training. The swarm's own
bug-fix history drives world model improvement.

## Concurrency Constraint

`play_macro()` is a blocking TCP call — the StateServer is occupied during macro execution.
`run_world_model_loop()` cannot run concurrently with `play_macro()`.

Mitigation: interleave them:
```
play_macro(launch_sequence)      # get ball in play
run_world_model_loop(duration=30) # observe one round
play_macro(next_action)
run_world_model_loop(duration=30) # observe next round
```

For future: add push/SSE mode to StateServer so world model loop can subscribe to state
changes rather than polling, and run in a background thread alongside macros.

## Files to Change

| File | Change |
|------|--------|
| `swarm/qa_tools.py` | Add `run_world_model_loop()` (~50 lines) |
| `swarm/tool_dispatch.py` | Register `run_world_model_loop` in dispatch table + required args |
| `prompts/qa.yaml` | Add world model generation + loop instructions |
| `prompts/common/` | Optionally add shared `world_model_instructions.yaml` fragment |

No schema changes. No new dependencies. Gracefully degrades if `world_model.py` absent.

## Open Questions

1. **StateServer schema variability**: `get_game_state()` returns game-specific fields.
   The LLM-generated invariants may reference fields that don't exist in the actual response.
   Mitigation: generate defensive code (`curr.get("score")` not `curr["score"]`), catch
   exceptions in the runner.

2. **Real-time games**: 10hz polling may miss transient violations in fast-paced games.
   For now acceptable — QA is looking for bugs not frame-perfect analysis.
   Future: StateServer push mode.

3. **False positives**: world model may flag valid edge cases (score resets on new level,
   ball respawn power-up, etc.). Mitigation: vision_query confirmation step before filing bug.
   Also: agent updates world_model.py to whitelist confirmed non-bugs.

4. **get_game_state() population**: Many existing games don't implement `get_game_state()`
   returning useful fields. The cerebellar loop is only as good as the state it can observe.
   Longer term: add `get_game_state()` implementation to the standard project scaffold so
   all new games expose structured state from day one.
