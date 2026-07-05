# Plan: Deterministic Completion Bot + Closure Playthrough Gate

## Context

The Void Patrol run-11 "art run" investigation found a menu button whose signal
connection silently failed to bind (`to="Main"` instead of `to="."`). Nothing
in the swarm caught it: `check_scripts.gd` only validates that scripts
*compile*, and QA agents click buttons via StateServer's `press_button`
command, which calls `button.emit_signal("pressed")` **directly** — bypassing
Godot's real input pipeline the same way a human's mouse click doesn't. A dead
signal connection is invisible to that path.

Separately, the existing vision-based QA loop (`qa` task type) is slow and
expensive: every action round-trips through an LLM + vision model call. QA
cycles are capped (`qa_max_cycles`, default 3) specifically because this loop
is costly, which limits how much of a game actually gets played before QA
gives up.

Both problems point at the same fix: a **deterministic completion bot** that
plays a game start-to-finish using only structured state (`a11y_tree` /
`get_game_state()`) and **real coordinate-based clicks** (not the
`press_button` bypass), with zero LLM/vision calls in its decision loop. This
catches dead UI wiring (a real click that lands on a button but produces no
state change is a first-class detectable failure) and runs at native game
speed instead of being bottlenecked by inference latency.

Research confirmed this slots into the existing system with no changes to
`swarm/closure/` core logic:
- `swarm/tools/scenario_qa.py` already implements the "zero LLM calls per
  action" executor pattern (`StateServerClient` class at line 53,
  `launch_game()` reuse, invariant checks, CLI exit-code harness) — reusable,
  not something to build from scratch.
- `swarm/closure/verification.py:222` (`execute_check()`) already dispatches
  `type: "command"` checks via subprocess exit code
  (`run_command_check`, `verification.py:74`).
- `swarm/validation.py:1193` matches a `smoke_check`'s `id` against
  `critical_flows` entries — a `critical_flow` "passes" purely by having a
  matching smoke check exit 0. No new closure check-type is required for v1.
- `swarm/closure/project_seeds.py:16-44` shows the exact seed shape to add a
  new check to a project's closure spec.
- StateServer's `{"command":"input","type":"click","x":N,"y":N}` (real
  `Input.parse_input_event` injection through Godot's actual Control hit-test
  pipeline) is the fidelity-correct action — NOT `press_button`, which bypasses
  hit-testing entirely via direct `emit_signal()`.

## Scope for this iteration

Ship two things:
1. **Connection-binding validator** (quick, standalone) — catches the exact
   bug class found in run-11, usable immediately in post-task validation.
2. **Completion bot v1** (`swarm/tools/playthrough_bot.py`) — genre-agnostic
   heuristic loop, exposed as a CLI with exit code 0/1, wired into Void
   Patrol's closure spec as a `critical_flow` for the run-12 trial.

Explicitly **not** in this iteration: a distinct `type: "playthrough"` closure
check (richer reporting than a bare exit code), swarm-wide auto-seeding for
every managed project, or genre-specific decision policies beyond
"shooter-style" (matches Void Patrol, the run-12 test subject).

## Part 1: Connection-binding validator

Extend `templates/godot/check_scripts.gd`'s existing scan pass with a second
check that runs in the same `godot --headless --script res://check_scripts.gd
--quit` invocation already wired into every managed Godot project's post-task
validation — no new Python plumbing, no new task type.

For every `.tscn` under `res://`:
1. Parse the raw file text for `[connection signal="..." from="..." to="..."
   method="..."]` lines (regex, same style as the existing per-file `_scan()`
   walk).
2. Load and instantiate the scene.
3. Resolve the `from` NodePath and call
   `node.get_signal_connection_list(signal)`; assert the `method` appears in
   the returned list's `callable` field.
4. Report any mismatch as an `ERROR:` line (same convention as the existing
   compile-error reporting) — this already causes `quit(1)` and triggers
   `_spawn_validation_bug_task()` via the existing failure path in
   `swarm/validation.py`.

Syncing the updated template to already-running sibling projects (per
CLAUDE.md's "Keeping templates in sync" section) is a later, separate step —
this plan only covers writing the check itself.

## Part 2: Completion bot — shared kit + swarm-built per-project bot

**Key design decision:** the decision policy is deliberately **not**
centralized in swarm code, and it is **not written by hand by a human
planning this feature** — it is built by a swarm agent, per project, the same
way features and art passes are. A single shared "genre-aware" decision
engine would inevitably get tuned against whichever game it's first tested on
(Void Patrol) and silently overfit — passing on shooters, useless elsewhere,
and nobody would notice until a very different genre shipped. Instead, the
swarm ships a genre-agnostic **scaffold** (StateServer plumbing, click
injection, progress/stuck detection, trace output, exit-code CLI convention),
and a new task type asks an agent to build `tests/playthrough_bot.py` for its
own project on top of that scaffold — so the agent focuses on "what does
progress/completion look like in THIS game," not "how do I talk to
StateServer," which it would otherwise reinvent per project (badly, at
varying quality) every time.

This mirrors how `GAME_DESIGN.md` and `get_game_state()` already work: shared
scaffolding + template (`templates/godot/`), per-project content authored by
an agent task.

### New file: `swarm/tools/playthrough_kit.py` (shared library, not a bot)

Structural sibling to `scenario_qa.py`'s plumbing, reusing rather than
duplicating:
- `StateServerClient` (import from `swarm.tools.scenario_qa` — already a
  clean, dependency-free TCP client)
- `launch_game()` from `swarm/qa_tools.py:531` for process startup

Exposes generic, genre-agnostic helpers only:
- `click_at(client, x, y)` — real coordinate click via
  `{"command":"input","type":"click","x":N,"y":N}` (NOT `press_button` —
  that bypasses hit-testing via direct `emit_signal()`, which is exactly the
  bypass that let the run-11 bug slip through everywhere else)
- `click_label(client, a11y_tree, label_substring)` — resolves an
  `a11y_tree` entry by case-insensitive label match and clicks its `bounds`
  center; returns `False` if no match (so a project's own bot can decide how
  to handle "expected button isn't there")
- `StuckDetector(window=N)` — tracks a rolling window of `get_state()`
  snapshots and reports `True` once no field has changed for N ticks (genre-
  agnostic: works by structural diff, no knowledge of what the fields mean)
- `PlaythroughResult` / `write_trace(path, ticks)` — same
  `scenario_trace.jsonl` convention as `scenario_qa.py`, so existing trace
  tooling/log rotation treats it identically
- `run_bot_cli(decide_fn, terminal_fn, max_ticks, timeout)` — the actual
  poll loop (launch → tick → `decide_fn(state, a11y, history)` → click/wait →
  re-poll → check `terminal_fn(state)` / stuck detector → exit 0/1 with
  trace) — a project's bot script supplies only `decide_fn` and
  `terminal_fn`, both plain Python callables operating on that project's own
  `get_game_state()` shape.

This file contains **zero game-specific logic** — no genre names, no
"shooter" branch, nothing that could overfit.

### New task type: `playthrough_bot`

New prompt file `prompts/playthrough_bot.yaml`, registered the same way
every other task type is (`swarm/prompts.py`'s `PromptLoader.load(task_type)`
resolves `prompts/<task_type>.yaml` by `name` field — no dispatch-table
changes needed, this is data-driven).

Prompt instructs the agent to:
1. Read `GAME_DESIGN.md` and `get_game_state()` (in `autoload/state_server.gd`
   or wherever the project implements it) to understand the game's state
   shape and win/lose conditions.
2. Import `swarm.tools.playthrough_kit` and write
   `tests/playthrough_bot.py`, implementing only `decide(state, a11y,
   history)` and `is_terminal(state)` — genre-specific logic, informed by
   that project's actual mechanics (menu button labels, scoring, terminal
   conditions), not a generic template.
3. Self-test: run the bot against the project's own running instance and
   iterate until it reaches a terminal state or produces a clean stuck
   report (not silently exits 1 on the first try).
4. Report the outcome (reached terminal state / stuck-at-X) as the task's
   completion summary, same convention as QA bug-filing.

Escalation policy: add `"playthrough_bot": {"max_attempts": _A, "on_exhaust":
"research", "research_max_attempts": _AR}` to
`_DEFAULT_ESCALATION_POLICY` in `swarm/agent_recovery.py:58` — grouped with
`feature`/`art_pass` (build-and-iterate), not `qa`/`audit` (verify-only),
since this task produces a new file rather than just a report. Without this
entry the generic fallback (`max_attempts: 3, on_exhaust: "cancel"`) still
works, so this is a quality-of-life addition, not a hard requirement.

CLAUDE.md: add one row to the task types table and the Prompt Types table
(`prompts/playthrough_bot.yaml` → `playthrough_bot`).

### Run-12 trial: create one `playthrough_bot` task

Once run-12's target project (Void Patrol art-pipeline baseline) has reached
a reasonably complete state — depends on the last feature/art task in the
DAG, same pattern as the existing final `harness_qa` gate — create a
`playthrough_bot` task via `swarm_create_task(project, "playthrough_bot",
...)`. This is the actual "try it in the next run" trial: a real agent, given
only the shared kit and the project's own game, builds
`tests/playthrough_bot.py` from scratch.

### Wiring into Void Patrol's closure spec (run-12 trial)

Add a seed-equivalent entry (either via `project_seeds.py` or a direct
`POST /api/closure/projects/void-patrol/spec` call, matching the exact shape
in `project_seeds.py:16-44`), pointing the smoke check at the **project's
own** bot script, not shared swarm code:

```json
{
  "verification": {
    "smoke_checks": [
      {
        "id": "full-playthrough",
        "type": "command",
        "command": "python tests/playthrough_bot.py --project-path . --max-ticks 2000 --timeout 300",
        "timeout": 320
      }
    ]
  },
  "critical_flows": [
    {"id": "full-playthrough", "description": "Project's own bot reaches victory or a scored game-over from the main menu with zero LLM calls."}
  ],
  "gates": {"critical_flow_count": 1}
}
```

This requires zero changes to `swarm/closure/verification.py` or
`swarm/validation.py` — the existing `type: "command"` dispatch and
id-matching logic (`validation.py:1193`) handles it as-is.

## Follow-on work (not this iteration)

Once the run-12 trial validates that an agent can build a working
`playthrough_bot` from just the kit + its own project, promote this from "one
task created manually for the trial" to "automatically seeded" — e.g. auto-
create a `playthrough_bot` task when a project's task queue empties (mirrors
`auto_replan_projects`) or fold it into `project_plan`'s generated DAG as a
standard late-stage task. Also revisit whether `closure/project_seeds.py`
should default-include the matching `smoke_check`/`critical_flows` entry for
every Godot project once the pattern is proven, rather than per-project
opt-in. Not designed here — deliberately deferred until run-12 shows whether
one agent, one project, one genre actually works end-to-end.

## Files touched

- `templates/godot/check_scripts.gd` — add connection-binding scan pass (Part 1)
- `swarm/tools/playthrough_kit.py` — new shared library, genre-agnostic (Part 2)
- `prompts/playthrough_bot.yaml` — new prompt/task type (Part 2)
- `swarm/agent_recovery.py:58` — one new escalation policy entry (Part 2)
- `CLAUDE.md` — task types table + prompt types table rows (Part 2)
- Void Patrol's closure spec — one API call or seed entry, pointing at the
  agent-built bot script (not a swarm code change)

## Verification

1. **Part 1**: re-introduce the exact `to="Main"` bug in a scratch scene,
   run `godot --headless --script res://check_scripts.gd --quit` against it,
   confirm it now reports the mismatch and exits 1. Confirm a clean project
   (correct `to="."`) still exits 0.
2. **Part 2 kit**: confirm `playthrough_kit.py` itself contains no
   project-specific logic (grep for "void", "patrol", "shooter" — should be
   zero hits) — this is the actual test of the "no overfitting" design goal.
3. **Part 2 trial**: create the `playthrough_bot` task against the run-12
   target project via the swarm, let it run, and inspect the result:
   - Does `tests/playthrough_bot.py` get created and import the kit correctly?
   - Does the agent's self-test loop actually reach a terminal state (or a
     clean, informative stuck-report) rather than just exiting 1 immediately?
   - Read the agent log (`swarm_agent_log`) to confirm it didn't reinvent
     StateServer plumbing instead of using the kit — that's the specific
     failure mode this design is meant to prevent.
4. Wire the closure smoke check, trigger `POST
   /api/projects/void-patrol/replan` or a manual closure verification run,
   confirm `critical_flows.full-playthrough` reflects the bot's exit code in
   the closure status response.
5. Run the full test suite (`pytest -q`) to confirm no regressions in
   `swarm/closure/` or `swarm/validation.py` from any incidental touch.
