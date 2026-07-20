# We Built a System That Plays Your Game, Finds Bugs, Fixes Them, and Verifies the Fix — With No Human in the Loop

There are plenty of AI tools that play games and report bugs. We built something different: a system that closes the loop. It finds the bug, writes the fix, verifies it by playing the game again, and moves on. No ticket. No human. No waiting.

This is how we got there, what it looks like in practice, and why we're open sourcing it.

---

## The Problem With Existing Game QA Tools

The current state of AI game testing — modl.ai, Filuta, nunu.ai, and others — is impressive. Agents explore your game, find crashes and regressions, and generate detailed reports with screenshots and video. Some have raised tens of millions of dollars building this.

But they all stop at the same place: a bug report.

A human developer still has to read it, understand it, write a fix, and verify the fix didn't break something else. For a AAA studio with a dedicated QA team, that workflow makes sense. For a two-person indie team shipping on Steam, it's another bottleneck in an already brutal pipeline.

We wanted to remove that bottleneck entirely.

---

## The Swarm

About a year ago we built [Swarm Controller](https://github.com/paraxenia/swarm-controller) — an open source LLM agent orchestration system designed to build, fix, and maintain Godot games autonomously. You give it a game design document and a codebase; it spawns agents that write features, fix bugs, run QA, and iterate.

It currently manages 30+ Godot game projects. Over its lifetime it has:

- Completed **13,991 tasks** across **196 projects**
- Fixed **8,112 bugs** autonomously
- Shipped **2,767 features**
- Run **897 harness QA passes**

But until recently, it had no way to answer the fundamental question: *does the game actually work end to end?* Script compilation passing doesn't mean a player can get from the main menu to wave 5. That gap required a human.

---

## The Playthrough Bot

The breakthrough was a mechanism we call the **playthrough bot**. The idea is simple but the implications are significant.

For each game, an agent writes a `tests/playthrough_bot.py` that:

1. Launches the game via Godot's headless-adjacent mode with a live game loop
2. Connects to a lightweight TCP state server running inside the game
3. Queries real-time game state — scene tree, UI elements, game logic fields
4. Injects real input events (clicks, drags, key presses) via Godot's input pipeline
5. Tracks a **milestone ladder** from start to a defined completion gate

A milestone ladder for a tower defense game looks like this:

```python
MILESTONES = [
    Milestone("menu_passed",   lambda s: gs(s).get("wave", 0) >= 1),
    Milestone("first_tower",   lambda s: gs(s).get("towers_placed", 0) >= 1),
    Milestone("first_kill",    lambda s: gs(s).get("enemies_killed", 0) >= 1),
    Milestone("wave_5",        lambda s: gs(s).get("wave", 0) >= 5),   # completion gate
]
```

The last milestone is the completion gate. The bot runs until it hits all milestones in order, or fails trying. On success it emits a structured receipt:

```
✓ PASSED: completed at tick 847 (game-time 212s)
  Milestones: menu_passed → first_tower → first_kill → wave_5
PLAYTHROUGH_RESULT: {"outcome": "complete", "furthest_milestone": "wave_5", ...}
```

On failure it writes a `failure_context.json` containing the last 50 game ticks, milestone timeline, and a screenshot — everything an agent needs to diagnose what went wrong.

---

## The Closed Loop

Here's where it gets interesting. The playthrough bot doesn't run in isolation. It's integrated into the same orchestration system that writes and fixes code.

When the bot fails, the swarm:

1. Reads `failure_context.json`
2. Creates a bug task with the exact failure context embedded
3. Spawns an agent to fix the underlying game code
4. Re-runs the bot to verify the fix

When the bot passes, the swarm schedules it to run again after the next batch of changes — regression testing automatically, on every meaningful commit.

This is the loop that doesn't exist anywhere else:

```
play → fail → diagnose → fix → play → pass → repeat
```

We ran this on a game called **fusion-foundry-td** (a tower defense with merge mechanics). The bot found that enemies weren't dying despite projectiles hitting them. The swarm traced it to three separate bugs:

- `projectile.gd` was checking `is_in_group("enemies")` but enemies were registered as `"enemy"` (singular)
- `game_manager.gd` cached enemy data by integer key but `wave_manager` emitted string IDs
- `tower.gd` never fired its attack timer because `tower_data` was assigned after `_ready()` ran

Each bug was found by the bot, fixed by an agent, and verified by the bot in sequence — without a human touching the code.

---

## Real Results

We've run the playthrough bot across five games so far:

| Game | Genre | Status |
|------|-------|--------|
| void-patrol | Space shooter | ✅ 13 consecutive clean passes |
| marble-mania | Physics puzzle | ✅ 20 consecutive clean passes |
| word-wizard | Word RPG | ✅ Completed, combat bug found at wave 5+ |
| tetris-neon | Puzzle | 🔄 In progress |
| star-sovereigns | 4X strategy | 🔄 In progress (complex codebase) |
| fusion-foundry-td | Tower defense | 🔄 In progress (multiple game bugs being fixed) |

The pattern is consistent: the bot runs, finds real bugs, agents fix them, the bot eventually passes. On simpler games (word-wizard) this happens in a single agent session. On complex games (fusion-foundry-td, star-sovereigns) it takes multiple iterations.

The word-wizard run was particularly clean. The bot passed in 3.1 seconds of game-time, hitting all four milestones in order. Then we removed the wave 3 completion gate to see how far it could go — it reached wave 7 with full health before the bot stalled (not game over — enemy HP stopped decreasing, a real game bug). That bug is now in the queue.

---

## How It Works Technically

### The State Server

Every Godot game in the swarm includes a `state_server.gd` autoload — a lightweight TCP server listening on port 11009. It responds to JSON commands:

```json
{"command": "state"}          // → full scene tree + game_state dict
{"command": "a11y_tree"}      // → flat list of all interactive UI elements
{"command": "input", "type": "click", "x": 400, "y": 300}  // → inject input
{"command": "input", "type": "drag", "x1": 100, "y1": 200, "x2": 300, "y2": 200}
```

The `game_state` field is populated by implementing `get_game_state() -> Dictionary` on your root scene — return whatever fields your bot needs to make decisions.

### The Playthrough Kit

`swarm/tools/playthrough_kit.py` is the Python library that bots import. It handles:

- Game launch and port allocation (so multiple bots can run concurrently)
- StateServer connection and command dispatch
- The milestone tracker and stuck detector
- Trace output and failure context writing
- The CLI harness (`run_bot_cli`) that wires everything together

A minimal bot looks like this:

```python
from swarm.tools.playthrough_kit import Action, Milestone, run_bot_cli

MILESTONES = [
    Milestone("started", lambda s: s.get("game_state", {}).get("wave", 0) >= 1),
    Milestone("wave_5",  lambda s: s.get("game_state", {}).get("wave", 0) >= 5),
]

def decide(state, a11y, history):
    # Return an Action based on current game state
    g = state.get("game_state", {})
    if g.get("wave", 0) >= 5:
        return Action(kind="noop")
    # Find and click the "End Turn" button
    return Action(kind="click_label", label="End Turn")

def classify_failure(state):
    if state.get("game_state", {}).get("game_over"):
        return "game_over"
    return None

def progress(state):
    g = state.get("game_state", {})
    return {"completed": g.get("wave", 0) >= 5, "wave": g.get("wave", 0)}

if __name__ == "__main__":
    run_bot_cli(decide, milestones=MILESTONES,
                classify_failure=classify_failure, progress=progress)
```

The `decide()` function runs every tick. It receives the current game state and a11y tree, returns an `Action`. The kit handles the rest.

### Concurrent Bots

Multiple bots can run simultaneously — each gets its own port pair allocated at launch. We ran five games concurrently without interference: marble-mania, tetris-neon, star-sovereigns, fusion-foundry-td, and word-wizard all running playthrough bots at the same time.

### The Stuck Detector

The kit includes a `StuckDetector` that fires if no milestone advances within N game-seconds. When stuck, the bot writes `failure_context.json` and exits with a non-zero code — the swarm treats this as a failure and creates a diagnostic task.

---

## What This Means for Indie Developers

You don't need the full swarm to use this. The playthrough bot pattern works standalone:

1. Add `state_server.gd` to your Godot project (copy from our templates)
2. Implement `get_game_state()` on your root scene
3. Write `tests/playthrough_bot.py` with your milestone ladder
4. Run `python3 tests/playthrough_bot.py --project-path /path/to/game`

You get a pass/fail signal with a structured receipt on every run. Wire it into your CI and you have regression testing that actually plays your game.

Add the swarm and you get the closed loop: bots that not only find the bugs but fix them.

---

## Why We're Open Sourcing It

The QA-only tools that exist today charge enterprise prices for enterprise studios. Nobody is solving this for the two-person team shipping their first game on Steam.

We could have tried to build a business around this. But the mechanism isn't that hard to copy once you see it — the moat isn't the playthrough bot, it's the closed fix loop, and that requires the whole swarm infrastructure. Building a product on top of that is a longer road than just shipping the thing and letting the community run with it.

The Godot ecosystem in particular is full of developers who would use this tomorrow if it existed and was free. So here it is.

---

## Get Started

- **Swarm Controller** (full orchestration system): [github.com/paraxenia/swarm-controller](https://github.com/paraxenia/swarm-controller)
- **State server template**: `templates/godot/autoload/state_server.gd`
- **Playthrough kit**: `swarm/tools/playthrough_kit.py`
- **Example bots**: `tests/playthrough_bot.py` in any managed game project

The playthrough bot prompt (`prompts/playthrough_bot.yaml`) tells the LLM agent exactly how to write a bot for a new game — milestone ladder design, agency evidence requirements, the completion gate rules. An agent can write a working bot for a new game in a single session.

---

*Swarm Controller is open source under MIT. The games being developed with it are separate projects. We're a small team building autonomous game development infrastructure — if this is interesting to you, open an issue or reach out.*
