# Playthrough Bot

A deterministic, zero-LLM completion bot system for Godot games. An agent
writes a small per-project bot script; the bot then plays the game
start-to-finish using only structured state and real coordinate-based clicks,
with no LLM or vision calls in its decision loop.

---

## Why it exists

Two problems with vision-based QA that playthrough bots solve:

1. **Dead signal connections are invisible to `press_button`.** StateServer's
   `press_button` command fires `button.emit_signal("pressed")` directly,
   bypassing Godot's real input pipeline. A button whose signal connection
   silently failed to bind looks functional to QA agents — but is dead to a
   real player's mouse click. A bot using real coordinate injection
   (`Input.parse_input_event`) catches this: a real click on a dead button
   produces no state change, which the stuck detector reports as a failure.

2. **Vision QA is slow and capped.** Every QA action round-trips through an
   LLM + vision model call. `qa_max_cycles` (default 3) limits how far a
   session plays before giving up. A bot runs at native game speed with no
   inference-latency bottleneck.

---

## Architecture

```
swarm/tools/playthrough_kit.py      Genre-agnostic scaffold (shared, zero game logic)
tests/playthrough_bot.py            Per-project bot (written by a swarm agent)
prompts/playthrough_bot.yaml        Agent prompt for building per-project bots
```

**The key invariant**: `playthrough_kit.py` contains zero game-specific logic
— no genre names, no "shooter/puzzle" branches, no assumptions about
`get_game_state()` shape. Verify with:
```bash
grep -in "void\|patrol\|shooter\|puzzle\|platformer" swarm/tools/playthrough_kit.py
```
Only doc-comment hits are acceptable. All game-specific logic lives in the
project's own `tests/playthrough_bot.py`.

---

## How it works

1. A `playthrough_bot` swarm task is created for a game project
2. An agent reads `GAME_DESIGN.md`, studies `get_game_state()`, and writes
   `tests/playthrough_bot.py` in the game repo
3. The bot imports `playthrough_kit` for the shared scaffold and supplies:
   - `decide(state, a11y, history) -> Action` — project-specific decision logic
   - `MILESTONES` — ordered ladder of completion predicates (recommended)
   - optionally `is_complete(state) -> bool` for simple single-screen games
4. The task is only marked `completed` after the agent actually runs the bot
   and the kit emits `✓ PASSED:` in stdout — bare `TASK_COMPLETE` without a
   real bot invocation is rejected

---

## Writing a per-project bot

Agents follow `prompts/playthrough_bot.yaml`. The resulting
`tests/playthrough_bot.py` looks like:

```python
from swarm.tools.playthrough_kit import run_bot_cli, Action, Milestone

# Ordered completion milestones — last one is the win condition
MILESTONES = [
    Milestone("menu_passed",    lambda s: s.get("game_state", {}).get("scene") == "game"),
    Milestone("reached_wave_3", lambda s: s.get("game_state", {}).get("wave", 0) >= 3),
    Milestone("boss_killed",    lambda s: s.get("game_state", {}).get("boss_hp", 1) == 0),
    Milestone("victory",        lambda s: s.get("game_state", {}).get("status") == "victory"),
]

def decide(state, a11y, history):
    """Return an Action based on current game state and a11y tree."""
    gs = state.get("game_state", {})
    tree = a11y.get("a11y_tree", [])

    # Example: click the first visible button labelled "Play" or "Start"
    for node in tree:
        if node.get("visible") and any(w in node.get("label","") for w in ("Play","Start")):
            x, y = node["bounds"][0] + node["bounds"][2]//2, node["bounds"][1] + node["bounds"][3]//2
            return Action.click(x, y, wait=0.5)

    return Action.wait(0.2)

if __name__ == "__main__":
    run_bot_cli(decide, milestones=MILESTONES, project_name="my-game")
```

### Action types

| Action | Description |
|--------|-------------|
| `Action.click(x, y, wait=0.3)` | Real coordinate click via `Input.parse_input_event` |
| `Action.click_label(label, wait=0.3)` | Click by `qa_label` or node label (resolves coords via a11y tree) |
| `Action.key(scancode, wait=0.2)` | Inject a keypress |
| `Action.wait(seconds)` | No-op pause |
| `Action.seconds` | All `wait` values are in game-seconds; divided by `--time-scale` |

### Milestone ladder

The milestone ladder gives the stuck detector a monotone progress signal. A
run that advances no milestone for N game-seconds is reported as stuck
(regardless of visual churn). The receipt records the furthest milestone
reached, making `"reached_wave_3, failed at boss_killed"` a richer failure
report than `"GAME_OVER"`.

For simple single-screen arcade games, `is_complete(state) -> bool` is
sufficient instead of a ladder.

---

## Running a bot manually

```bash
# From the game project directory
python3 tests/playthrough_bot.py --project my-game

# Fast-forward at 4× game speed
python3 tests/playthrough_bot.py --project my-game --time-scale 4

# Limit run to 300 game-seconds before declaring stuck
python3 tests/playthrough_bot.py --project my-game --timeout 300
```

Exit codes: `0` = passed (all milestones reached), `1` = failed or stuck.

The kit writes two output files on every run:
- `data/playthrough_receipt_<project>.json` — outcome, milestones reached,
  tick count, time-scale, furthest milestone, failure context
- `data/playthrough_trace_<project>.json` — full tick-by-tick state log

---

## Stuck detection

The kit tracks game-time between milestone advances. If no milestone fires
within `stuck_timeout` game-seconds (default: 30s), the run is aborted with
`outcome=stuck` and a `failure_context.json` containing the last 50 ticks.

This fires on both real stuck states (bot is looping on the same action) and
bugs (game froze, transition never completed, state never updated).

---

## Wiring into closure

Once a bot reliably completes a full playthrough, wire it into the project's
closure spec as a `smoke_check` / `critical_flows` entry. The existing
`type: "command"` dispatch in `swarm/closure/verification.py` already handles
bot exit codes — no changes to closure core needed.

```python
# In swarm/closure/project_seeds.py, add to the project's seed:
{
    "id": "playthrough_complete",
    "type": "command",
    "command": "python3 tests/playthrough_bot.py --project my-game --time-scale 4",
    "description": "Bot completes full game without getting stuck",
}
```

Do not add to closure until the bot has demonstrated a full playthrough (all
milestones reached, not just an early GAME_OVER).

---

## Task type reference

| Field | Value |
|-------|-------|
| Task type | `playthrough_bot` |
| Priority | 50 (standard) |
| Prompt | `prompts/playthrough_bot.yaml` |
| Escalation | `on_exhaust: "cancel"` (no research feeder) |
| Completion guard | Requires `✓ PASSED:` in a real `run_command` invocation |
| Auto-scheduled | After QA cycle completes (if `playthrough_bot` task type is in the project pipeline) |

---

## Open items (as of v0.3.0)

- **Full end-to-end completion not yet demonstrated** on a non-trivial game.
  Early GAME_OVER (losing quickly) satisfies the task guard but is not a
  meaningful playthrough. The run-12 baseline (`void-patrol-bot-proof-run12`)
  is the intended test subject — paused until a full victory trace is produced.
- **Closure wiring** (above) blocked on the above.
- **Spawn-refusal 500 bug** (minor, pre-existing): `POST /api/spawn` against a
  project whose only pending task is already `in_progress` returns HTTP 500
  instead of a clean 409. See `swarm/agent_lifecycle.py:247`.

---

## Key files

```
swarm/tools/playthrough_kit.py           Shared scaffold (841 lines)
prompts/playthrough_bot.yaml             Agent prompt
swarm/agent_runtime.py                   TASK_TYPE dispatch (grep PLAYTHROUGH_BOT)
swarm/agent_recovery.py                  Escalation policy
templates/godot/check_scripts.gd         Connection-binding validator (Part 1)
docs/playthrough-bot-design-plan.md      Original implementation design
docs/handoff-playthrough-bot-2026-07-05.md  Post-build handoff and open items
```
