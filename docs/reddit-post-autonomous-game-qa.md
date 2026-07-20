# Reddit Post

**Title:** I built a system that plays my games, finds bugs, fixes them, and verifies the fix — no human in the loop. Here's how.

---

**Subreddits to consider:** r/gamedev, r/godot, r/artificial, r/MachineLearning, r/programming

---

**Body:**

I've been building a Godot game studio run entirely by LLM agents. Not "AI generates art assets" — I mean agents that write GDScript, fix bugs, run QA, and iterate. 30+ games in parallel, no human touching the code.

Last month we crossed a milestone I've been chasing for a while: a closed fix loop. The system finds bugs by **playing the game**, not by reading the code. Then it fixes them. Then it plays again to verify. The whole cycle runs without me.

---

**How it works**

Each game gets a `tests/playthrough_bot.py` — a Python script that:

1. Launches the game (real game loop, not headless)
2. Connects to a TCP state server running inside Godot
3. Reads live game state: scene tree, custom game fields, UI elements
4. Injects real input events (clicks, drags, key presses) through Godot's input pipeline
5. Tracks a milestone ladder — main menu → first action → mid-game → completion gate

The state server is just a small autoload (`state_server.gd`) that listens on port 11009 and responds to JSON commands. You implement `get_game_state()` on your root scene and return whatever fields matter for your game.

A milestone ladder looks like this:

```python
MILESTONES = [
    Milestone("menu_passed",  lambda s: gs(s).get("wave", 0) >= 1),
    Milestone("first_tower",  lambda s: gs(s).get("towers_placed", 0) >= 1),
    Milestone("first_kill",   lambda s: gs(s).get("enemies_killed", 0) >= 1),
    Milestone("wave_5",       lambda s: gs(s).get("wave", 0) >= 5),
]
```

When the bot fails, it writes a `failure_context.json` with the last 50 game ticks, milestone timeline, and a screenshot. The orchestration system reads that, creates a bug task with the exact failure context, spawns an agent to fix the code, and re-runs the bot to verify. 

Pass → schedule regression runs. Fail → create bug task → fix → verify. Repeat.

---

**Real example: fusion-foundry-td**

Tower defense with merge mechanics. Bot found enemies weren't dying despite projectiles hitting them. The swarm traced it to three bugs:

- `projectile.gd` was checking `is_in_group("enemies")` but enemies were registered as `"enemy"` (singular)
- `game_manager.gd` cached enemy data by integer key but `wave_manager` emitted string IDs  
- `tower.gd` never fired its attack timer because `tower_data` was assigned after `_ready()` ran

Each bug found by the bot → fixed by an agent → verified by the bot. No human touched the code.

---

**Numbers after running this for a while**

- 13,991 tasks completed across 196 projects
- 8,112 bugs fixed autonomously
- 2,767 features shipped
- 39 clean playthrough completions across 5 game genres: physics puzzle, word RPG, tower defense, puzzle, space shooter

The word-wizard bot (word RPG) passed in 3.1 seconds of game-time on the first clean run. Then it reached wave 7 before stalling — not game over, enemy HP just stopped decreasing. That's a real bug. It's in the queue.

---

**Why this is different from existing tools**

modl.ai, Filuta, nunu.ai — they all find bugs and generate reports. They're good at it. But they stop there. You still have to fix it.

This closes the loop. The same system that found the bug fixes it and proves it's fixed by playing through again.

I looked and couldn't find anyone else doing the full cycle autonomously. If I'm wrong, please point me at it.

---

**It's open source**

The whole thing: [github.com/paraxenia/swarm-controller](https://github.com/paraxenia/swarm-controller)

The playthrough bot pattern works standalone — you don't need the full orchestration system. Add `state_server.gd` to your Godot project, implement `get_game_state()`, write a milestone ladder, and you have regression testing that actually plays your game. Wire it into CI and any breaking change gets caught before it ships.

Add the swarm and the bugs fix themselves.

Happy to answer questions about how any of it works.
