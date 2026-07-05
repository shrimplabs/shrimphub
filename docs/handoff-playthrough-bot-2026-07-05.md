# Handoff: Playthrough Bot Mechanism

**Status as of 2026-07-05**: Core mechanism built, wired end-to-end, and validated
in a real test run. Two real bugs found and fixed along the way. Run-12 (the next
experiment run) is **paused** until this is confirmed solid — do not resume it
until the open items below are closed out.

## Why this exists

While investigating a user report ("I can't hit Start" on the Void Patrol
`art-run11` project), we found a signal connection in `main.tscn` that silently
failed to bind at runtime (`to="Main"` — the scene root's own name — instead of
`to="."`, the correct self-reference NodePath). Every menu/pause button was dead.
Nothing caught it: scripts still compiled fine, and QA agents click buttons via
StateServer's `press_button` command, which calls `button.emit_signal("pressed")`
directly — bypassing Godot's real input/hit-testing pipeline the same way the bug
bypassed a human's mouse.

Separately, the user pointed out that the existing vision-based QA loop (`qa` task
type) is slow: every action round-trips through an LLM + vision-model call. This
caps how much of a game QA can actually play through (`qa_max_cycles`, default 3).

Both problems point at the same fix: a **deterministic completion bot** that plays
a game start-to-finish using only structured state (`a11y_tree` /
`get_game_state()`) and **real coordinate-based clicks** (not the `press_button`
bypass), with zero LLM/vision calls in its decision loop. This catches dead UI
wiring (a real click that lands on a button but produces no state change is a
detectable failure) and runs at native game speed.

## Design principle: agents build their own bot, not swarm code

**Critical constraint, do not violate this**: the per-game decision logic
(what to click on a menu, what counts as "done") is **not** centralized in swarm
code. A single shared "genre-aware" bot would inevitably get tuned against
whichever game exercises it first and silently overfit. Instead:

- `swarm/tools/playthrough_kit.py` — a shared, genre-agnostic **scaffold**
  (StateServer TCP client, real click injection, stuck detection, trace output,
  CLI harness). Contains **zero game-specific logic** by design — verify this
  with `grep -in "void\|patrol\|shooter\|puzzle\|platformer"
  swarm/tools/playthrough_kit.py` before merging any change to it; only doc-comment
  hits referencing the motivating bug are acceptable, never logic branches.
- A new `playthrough_bot` task type asks a swarm **agent** to write
  `tests/playthrough_bot.py` **inside each game project**, importing the kit and
  implementing only `decide(state, a11y, history)` and `is_terminal(state)` —
  tailored to that specific game, using that project's own `get_game_state()`
  shape and menu labels.

If you're asked to "improve the bot," the answer is almost always "improve the
kit's generic scaffold" or "help agents write better per-project bots via prompt
tuning" — not "add game-type branches to playthrough_kit.py."

## What's built and committed (swarm-controller repo)

All on `main`, in this order:

1. **`8d7979ea` — Connection-binding validator**
   `templates/godot/check_scripts.gd` now parses every `.tscn`'s `[connection]`
   lines, instantiates the scene, and verifies `get_signal_connection_list()`
   actually contains the declared binding. Reports `ERROR:` + exit 1 on mismatch
   — this rides the existing post-task validation pipeline every managed Godot
   project already runs (`godot --headless --script res://check_scripts.gd
   --quit`), so it requires no new task type or Python plumbing.
   Verified: catches the `to="Main"` bug when reintroduced; zero false positives
   against a 100-scene real project (raccoon-city).

2. **`75c64b60` — Shared kit**
   `swarm/tools/playthrough_kit.py` (new file). Exposes:
   - `Action` dataclass (`kind`: `"click"` / `"click_label"` / `"wait"` / `"noop"`)
   - `click_at(client, x, y)` / `click_label(client, a11y, label_substring)` —
     real coordinate clicks via `{"command":"input","type":"click",...}`,
     **never** `press_button`
   - `StuckDetector(window=N)` — structural diff of state snapshots, ignores only
     the `timestamp` field, reports stuck after N ticks with no change
   - `write_trace()` — same `scenario_trace.jsonl` convention as the existing
     `swarm/tools/scenario_qa.py`
   - `run_bot_cli(decide, is_terminal)` — the actual poll loop + CLI arg parsing
     + exit-code convention (0 = terminal state reached, 1 = stuck/timeout).
     A project's own bot script calls this and supplies nothing else.
   - `_launch()` reuses `swarm.qa_tools.launch_game` / `StateServerClient` from
     `swarm.tools.scenario_qa` rather than duplicating TCP/process plumbing.

3. **`40fb4a0c` — Task type wiring**
   New `prompts/playthrough_bot.yaml`. Full dispatch wiring:
   - `swarm/agent_runtime.py`: `PLAYTHROUGH_BOT_SYSTEM`/`_USER` globals +
     `elif TASK_TYPE == "playthrough_bot"` dispatch branch
   - `swarm_runner.py`: `_load_prompt("playthrough_bot", **_common)` call +
     `rt.PLAYTHROUGH_BOT_SYSTEM/_USER` injection into the generated wrapper script
   - `swarm/agent_recovery.py`: escalation policy entry, grouped with
     `feature`/`art_pass` (`on_exhaust: "research"`, not `"cancel"` — this task
     produces a new file, it's build-and-iterate work)
   - `swarm/agent_runtime.py` auto-commit prefix map: `"playthrough_bot": "test"`
   - `CLAUDE.md`: task types table + prompt types table rows

4. **`0fa707b4` — `_launch()` CWD portability fix**
   `qa_tools.DATA_DIR` defaults to the relative string `"data"`, which only
   resolves correctly when CWD is the swarm-controller repo. A project's own
   `tests/playthrough_bot.py` runs from **within its own project directory**, so
   without this fix, `launch_game()`'s PID-tracking write failed with
   `No such file or directory: 'data/godot_pids.json'` (non-fatal to the actual
   game launch, but real, and now fixed by setting `qa_tools.DATA_DIR` to the
   resolved absolute path before calling `launch_game()`).

All four commits: full test suite green (1460/1460) after each one.

## What's built and committed (per-project fixes, separate repos)

These are **not** in swarm-controller — they're fixes to the actual game project
that surfaced during testing.

### `void-patrol-adaptive-flat-art-run11` (the real, original project)

- `11564b0` (already existed before this work started) — the `to="Main"` →
  `to="."` signal-connection fix (this is what originally prompted the whole
  investigation).
- **`d72f544` — HUD click-blocking fix.** `scenes/hud.tscn`'s full-viewport
  `Root` Control had no explicit `mouse_filter`, defaulting to
  `MOUSE_FILTER_STOP`. Since HUD is wrapped in a `CanvasLayer` (rendering/input-
  picking above the plain `Menu` Control, which has no `CanvasLayer` of its
  own), HUD's invisible Root intercepted **every click anywhere on screen at all
  times** — including the Start button — even after the signal-connection fix
  above. That fix was correct and necessary but **not sufficient**: a real
  player's mouse click was still being swallowed before it ever reached the
  button. Fix: added `mouse_filter = 2` (IGNORE) to match `power_up_hud.tscn`'s
  Root, which already had it correctly set.

  **This bug is exactly why the playthrough_bot mechanism matters**: it was
  found by a bot using real coordinate clicks (not the `press_button` bypass)
  getting cleanly stuck, then diagnosed by comparing `click_label` (real click,
  no state change) against `press_button` (bypass, state advances) on the same
  button in the same running instance.

  Verified end-to-end with a real StateServer coordinate click (not
  `press_button`, not `emit_signal`) against this exact project after the fix:
  `game_state` correctly advances `0 → 1` on click.

### `void-patrol-playthrough-bot-test` (disposable clone, see below)

A clone of `art-run11` used purely for testing this mechanism without touching
the "real" project mid-investigation. Has its own copies of both fixes above
(`492624d` added StateServer autoload + the agent's own `tests/playthrough_bot.py`,
`3c5432f` the HUD fix). **This directory can be deleted once the contractor
confirms they don't need it as a reference** — it is registered with the swarm
as project `void-patrol-playthrough-bot-test` (see Loose Ends below).

## What actually happened in the one real test run

Created one `playthrough_bot` task against `void-patrol-playthrough-bot-test`.
Two attempts:

- **Attempt 1** (agent `c75ba1f6`): wrote `tests/playthrough_bot.py`, discovered
  and added the missing `StateServer` autoload, ran the bot, hit the stuck-click
  issue, correctly diagnosed it as a separate bug (not its own logic) by comparing
  `click_label` vs `press_button`, then kept working on unrelated game bugs
  (viewport spawn position, power-up parenting) before eventually failing/timing
  out. Multiple legitimate commits landed from this attempt.
- **Attempt 2** (agent `86a1aa5e`, auto-retried per escalation policy): got stuck
  in a "response truncated -- injecting targeted retry" loop for ~15 loops,
  never made a tool call, gave up with a bare `TASK_COMPLETE` claiming the file
  was uncommitted — **which was wrong**; the file was already committed by
  attempt 1. Task shows `status: "completed"` in the DB despite this confused
  self-report. **This is a real gap**: nothing validates that a `playthrough_bot`
  task's own self-report matches ground truth (e.g. an actual git diff / running
  the bot's exit code) before marking the task `completed`. Worth a follow-up:
  either a post-task validation step specific to this task type, or at minimum
  don't trust bare `TASK_COMPLETE` text without checking `git log` for a new
  commit.

The agent's own diagnosis (comparing real-click vs `press_button`) was correct
and is what led us to the actual HUD bug and fix above.

## Open items / loose ends for the contractor

1. **Re-run the mechanism cleanly, end to end, now that both bugs are fixed.**
   Everything above was found and fixed *during* imperfect test runs. There
   has not yet been one clean run where: agent builds bot → bot runs against an
   already-correct project → reaches a real terminal state (victory/game-over)
   → commits cleanly → task marked complete truthfully. Do this next.

2. **The "response truncated -- injecting targeted retry" loop** that swallowed
   attempt 2 for ~15 loops is a pre-existing swarm mechanism (unrelated to this
   feature specifically) but it's what caused the confusing "completed but
   agent thinks it failed" outcome. Worth understanding why it fired and got
   stuck rather than recovering, independent of this feature.

3. **Task completion validation gap** (see above) — a `playthrough_bot` task
   marked `completed` should not just trust a bare `TASK_COMPLETE`; consider
   checking for an actual new commit and/or running the bot's own exit code as
   part of finishing the task.

4. **Test project cleanup**: `void-patrol-playthrough-bot-test` is registered
   with the swarm (`POST /api/projects` was called for it, profile `godot`,
   managed `true`). Decide whether to keep it as a permanent regression-test
   fixture or delete it (`DELETE /api/projects/void-patrol-playthrough-bot-test`,
   the API refuses if any agent is still active on it — none currently are).

5. **Spawn-refusal 500 bug** (minor, pre-existing, noticed but not fixed):
   `POST /api/spawn` against a project whose only pending task is already
   `in_progress` returns HTTP 500 instead of a clean message. Log line:
   `swarm/agent_lifecycle.py:247`. Not blocking, not part of this feature, flagged
   for whoever has spawn-endpoint cleanup on their list.

6. **Follow-on roadmap item (not started, don't start yet)**: once a few clean
   playthrough_bot runs build confidence, wire the resulting per-project bot into
   that project's closure spec as a `smoke_check`/`critical_flows` entry (see
   `swarm/closure/project_seeds.py` for the exact shape — no changes needed to
   `swarm/closure/verification.py`, the existing `type: "command"` dispatch
   already handles a bot's exit code as a gate). This makes "can a bot complete
   this game" a real, enforced closure requirement. **Do not start this until
   item 1 above is done at least once, cleanly.**

7. **Run-12 is paused.** Do not resume creating run-12 experiment tasks/projects
   until the contractor (or whoever picks this back up) confirms items 1-3 above
   are resolved and at least one clean playthrough_bot run has completed
   truthfully against a real project.

## Key files for orientation

```
swarm/tools/playthrough_kit.py       # shared scaffold, genre-agnostic
prompts/playthrough_bot.yaml         # agent instructions for building a per-project bot
swarm/agent_runtime.py               # TASK_TYPE dispatch (grep PLAYTHROUGH_BOT)
swarm/agent_recovery.py              # escalation policy (grep playthrough_bot)
swarm_runner.py                      # prompt loading + wrapper script injection
templates/godot/check_scripts.gd     # connection-binding validator (Part 1 of this work)
swarm/tools/scenario_qa.py           # StateServerClient + launch_game reuse target
```

Original design plan (more implementation detail, written before execution
started): `docs/playthrough-bot-design-plan.md` in this repo.
