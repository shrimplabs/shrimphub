# The spawn-test-proj Incident

## What Happened

On the night of May 28, 2026 — while its operator was asleep — the SHRIMP swarm created a new software project, named it, built it, tested it, polished it, art-passed it, QA'd it, and kept iterating on it for the next 35 hours. Nobody asked it to.

The project is called **spawn-test-proj**.

## Discovery

The project was found by accident. A screenshot showed a dark-themed Godot window titled "spawn-test-proj (DEBUG)" with a live HUD reading "Spawns: 6 | Active: 6" and five animated unit sprites on screen: Data Core, Operative, Worker, Guard, and Extraction Zone.

Initial assumption: it was a blank test harness, the icons were GUT test indicators. Both wrong. It was a fully built, polished, QA-verified piece of software — with procedural audio, particle effects, screen shake, and 37 passing unit tests.

## What It Built

**spawn-test-proj** is a visual demo and test harness for a signal-driven entity spawn system. It has two parallel implementations of the same concept:

- **`scripts/spawn_manager.gd`** — a GDScript autoload that tracks entity spawns in a dictionary, emits signals on single and batch spawn events, and exposes a full lifecycle API (spawn, remove, clear, reset for test isolation)
- **`service.py`** — a Python HTTP server on port 8080 with `POST /spawn` and `GET /health` endpoints, backed by a thread-safe counter

Neither implementation talks to the other. They are parallel expressions of the same pattern in two languages.

The visual scene (`main.gd`) wires up to the GDScript SpawnManager signals and renders the results: animated sprites appearing with a 1.5x brightness flash, a 3px screen shake, 6-particle burst rings, hover sine animations, and a pulsing HUD counter. The batch spawn path shows a progress indicator that updates as each entity lands.

**14 UI/UX polish features** were implemented and verified, including: dark background with responsive viewport sizing, HUD panel with fade-in, entity type labels, rotation wiggle on spawn, ripple ring effect, FPS counter, and batch progress pulse.

## The Evidence Trail

The oldest task in the task history log is already a *continuation* — the original genesis task ran, hit the agent loop limit, and was pruned from the database before the JSONL export captured it. The first surviving record is:

```
2026-05-28T21:48  feature-19337327-208  (continuation of parallel-spawn-test-proj-0-1780015235)
```

The original task was `parallel-spawn-test-proj-0-1780015235`. The naming pattern — `parallel-<project>-<index>-<timestamp>` — is what SHRIMP generates when a `project_plan` agent creates parallel feature tasks. This means a planner agent, working on some other project, decided to create a new project called `spawn-test-proj` as part of its plan, registered it with the swarm, and the swarm treated it as legitimate work.

The continuation task's description reveals the original agent was building something different: `ball_spawner.gd` and `ball.tscn`. The continuation agent, unable to find those files (probably because the first agent's commits went to a temp path), started fresh and built the SpawnManager system instead.

## The Full Timeline

From the task history, 77 tasks ran against spawn-test-proj between May 28 at ~21:48 and May 30 at ~08:48 — about 35 hours of continuous autonomous development:

- **May 28, 21:48** — First surviving task (continuation). Agent builds SpawnManager from scratch.
- **May 28, 22:16** — Integration task wires up signals.
- **May 28, 22:25** — Auto-QA, art pass, and polish tasks fire automatically (8-completion threshold hit).
- **May 28, 23:09** — Second QA/art/polish cycle.
- **May 29, 01:05–01:17** — Third full cycle. Polish, integration, QA.
- **May 29, 12:48–13:58** — QA agent finds bugs, spawns bug tasks, reruns itself.
- **May 29, 14:00–16:31** — Four more feature tasks, three more QA cycles.
- **May 29, 17:18–20:46** — Five more feature tasks, integration, art, QA.
- **May 30, 01:25–08:48** — Overnight: six more feature tasks, continuous integration and QA cycles.

The QA report on file shows 37/37 tests passing, all critical flows verified, zero bugs found. The project considers itself complete.

## What We Don't Know

- **Which project's plan agent created it.** The genesis task is gone. Something running around 20:00–21:00 on May 28 decided to call `create_project("spawn-test-proj", ...)` and the swarm accepted it.
- **Why it was named that.** The name is self-describing in a very agent-flavored way — it sounds like what you'd call something if you were an agent trying to test spawn infrastructure.
- **Why it targeted the real workspace.** The first continuation task shows file paths in a pytest temp directory, but the git repo ended up in the real `~USER/workspace/spawn-test-proj`. Either the registration and the build diverged, or there were two separate attempts with the same project name.

## What This Means

The swarm's `create_project` tool is callable by plan agents. Plan agents are supposed to create *tasks*, not *projects* — but the tool was available and nothing prevented them from using it. One did.

Once the project existed, every automatic system in SHRIMP kicked in: auto-QA after 8 completions, auto-art-pass, auto-polish. The project became self-sustaining. It generated its own work queue. By the time anyone looked, it had been running for over a day and had a QA-verified final state.

The project was not deleted. It works. The 37 tests pass. The game runs. The swarm built something real, by accident, overnight, without being asked, and it was good enough that there was no reason to tear it down.

## Postscript

The operator's response upon learning this: *"I have literally no idea. I was asleep probably."*

That seems right.
