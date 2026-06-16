# Run 8 Analysis And Run 9 Proposal

Generated: 2026-06-16

Experiment:

```text
void-patrol-pipeline-ab-run8-20260614
```

Primary data sources:

- `data/experiments/void-patrol-pipeline-ab-run8-20260614/events.jsonl`
- `data/swarm.db`
- `data/agent_*run8*.json`
- `data/learnings/void-patrol-variant-*-run8/*.md`
- Manual playtest notes from the operator

## Executive Read

Run 8 is useful, but not pristine. It completed under a mid-run controller
intervention and should be treated as a stress/confirmation run, not as a clean
statistical replicate.

The main result is still clear enough to guide run 9:

- Flat/continuous work remains the strongest practical baseline for artifact
  quality.
- Scout-grounded planning can reduce feature-loop counts, but did not by itself
  guarantee playable mechanics.
- Art, polish, QA, screenshot, and vision loops are not optional measurement
  noise. They are part of the intervention that makes games usable.
- M2.7 should not be used for long freeform work/completion right now. It is
  still useful for bounded read-only scout/search loops.
- Run 9 should test fewer arms, with identical mid-run and final quality gates,
  and should score playable-game outcomes directly.

## Run 8 Treatment Arms

| Project | Variant | Main workflow | Tail |
| --- | --- | --- | --- |
| `void-patrol-variant-c-run8` | C | `scout -> work -> validate` | 3x `art_pass -> polish -> harness_qa` |
| `void-patrol-variant-e-run8` | E | `scout -> plan -> work -> validate` | 3x `art_pass -> polish -> harness_qa` |
| `void-patrol-variant-e-long-plan-run8` | E long-plan | `scout -> plan -> work -> validate`, plan limit 20 | 3x `art_pass -> polish -> harness_qa` |
| `void-patrol-variant-f-run8` | F | flat MiniMax-M3 loop | none |
| `void-patrol-variant-f-tail-run8` | F tail | flat MiniMax-M3 loop | 3x `art_pass -> polish -> harness_qa` |

## Cleanliness Notes

Run 8 had no recorded infrastructure failure events in the completed experiment
log, but it did have live controller interventions:

- Active-agent/task lifecycle repair landed mid-run.
- Art/polish soft quality gates landed mid-run.
- Two stranded quality gates were repaired directly in the live DB after backup:
  `void-patrol-variant-c-run8-tail1-polish` and
  `void-patrol-variant-e-long-plan-run8-tail1-art`.
- `variant-e-run8` retained one stale pending bug task blocked by a cancelled
  research feeder.

Interpretation: compare run 8 arms for direction, not as a clean p-value style
experiment.

## Quantitative Summary

Final project verification was green for all five primary run 8 arms:

| Project | Final verification | Open regressions |
| --- | --- | ---: |
| `void-patrol-variant-c-run8` | passed | 0 |
| `void-patrol-variant-e-run8` | passed | 0 |
| `void-patrol-variant-e-long-plan-run8` | passed | 0 |
| `void-patrol-variant-f-run8` | passed | 0 |
| `void-patrol-variant-f-tail-run8` | passed | 0 |

Terminal task state:

| Variant | Completed | Cancelled | Pending | Notes |
| --- | ---: | ---: | ---: | --- |
| C | 31 | 4 | 0 | one QA cancelled; soft-gate repairs present |
| E | 36 | 7 | 1 | least clean arm; stale pending lock-conflict follow-up |
| E long-plan | 31 | 2 | 0 | lower churn than E; one soft-gate repair |
| F | 68 | 4 | 0 | no seeded tail, but spawned many auto art/polish/QA tasks |
| F tail | 37 | 6 | 0 | practical hybrid arm; one QA cancelled |

Source-feature coverage:

All five arms touched the same eight source feature IDs:

```text
void-patrol-t01-9635
void-patrol-t02-9636
void-patrol-t03-9637
void-patrol-t04-9638
void-patrol-t05-9639
void-patrol-t06-9640
void-patrol-t07-9641
void-patrol-t08-9642
```

Average loop counts for source feature completion events:

| Variant | Avg source-feature loops | Median source-feature loops |
| --- | ---: | ---: |
| C | 43.7 | 40 |
| E | 46.2 | 0 |
| E long-plan | 27.7 | 0 |
| F | 91.4 | 105 |
| F tail | 64.2 | 58 |

The zero medians in E/E-long are a telemetry artifact from duplicate/follow-up
completion events with zero loop totals. Use the averages directionally, not as
absolute work estimates.

Bug/research churn per source feature from final task rows:

| Variant | Bug + research tasks per source feature |
| --- | ---: |
| E long-plan | 1.67 |
| C | 1.78 |
| F tail | 2.09 |
| F | 2.62 |
| E | 2.89 |

This supports a narrow quantitative claim: E long-plan and C were efficient at
draining the graph. It does not prove they made the best games.

## Vision And Screenshot Use

Recorded agent artifact tool-use counts:

| Variant | Vision/screenshot signal |
| --- | --- |
| C | some vision use, mostly art/polish |
| E | no recorded `vision_query`, `take_screenshot`, or `launch_game` use in agent JSON artifacts |
| E long-plan | multiple `launch_game`, `take_screenshot`, and `vision_query` hits |
| F | heavy `launch_game`, `take_screenshot`, and `vision_query` use through auto art/polish tasks |
| F tail | vision/launch use in tail art; learnings also report polish vision loops |

This is a major run 8 finding. A green graph without screenshot/vision
inspection is not enough for game quality. Run 9 should make screenshot/vision
evidence a required telemetry field for art, polish, and QA tasks.

## Qualitative Playtest Observations

Operator observations:

- C made a game, but wave 1 did not end.
- E long-plan made an interesting game, but the player could not damage enemies.
- F tail was pretty good and was the first played run with powerups, but still
  had show-stopper bugs.
- None of the run 8 artifacts matched the strongest run 4 F artifact.

Interpretation:

- Structured graph completion catches build/test health but misses core
  playability loops.
- Long planning did not prevent fundamental combat-loop failure.
- Flat or hybrid flat arms still appear better at producing rich gameplay,
  probably because they preserve one continuous implementation context.
- Tails help, but placing all quality gates at the end is too late. Bugs like
  "cannot damage enemies" and "wave does not end" should be caught before the
  graph spends the second half of the budget.

## Model-Routing Implication

The MiniMax quota probe changed the operational interpretation:

- Public PAYG token pricing currently makes M3 and M2.7 look similar for
  <=512k standard calls.
- The quota meter still behaved as if M3 consumes materially more quota percent
  than M2.7.
- M2.7 showed serious long-output issues through the Anthropic-compatible path:
  many calls spent output tokens but produced empty or tiny visible responses.

Run 9 routing rule:

- Use M2.7 for bounded read-only scout/search loops only.
- Use M3 for planning that must produce a durable plan, all work/edit loops,
  final completion, and any decision that can mutate the graph.
- Do not let M2.7 emit `TASK_COMPLETE` or own long freeform work output.

## Run 9 Proposal

Run 9 should be smaller and stricter than run 8. The goal is not to discover
every possible phase ordering. The goal is to decide which production workflow
we should build around.

### Setup Support

Run 9 setup is supported through the snapshot clone API:

- `quality_gate_mode: "run9_mid_final"` or `run9_quality_gates: true` inserts
  both mid-run and final `art_pass -> polish -> harness_qa` gate chains.
- Playability QA is represented as task type `harness_qa` with metadata
  `{"qa_focus": "playability"}`. Do not use a new literal `playability_qa`
  task type unless runtime support is added.
- Seeded art and polish gates are stamped with
  `phase_loop_limits: {"work": 200}` so every arm gets the same visual
  iteration budget.
- `dependency_overrides` can be passed to clone setup for the optional parallel
  arms. Use it only for explicitly approved feature dependency changes; do not
  apply a blanket dependency wipe to the source DAG.

Recommended arms:

| Arm | Workflow | Why keep it |
| --- | --- | --- |
| `F-tail-v2` | flat M3 main work + enforced quality gates | Best practical baseline; preserves continuous transcript while adding the quality gates that run 8 showed matter. |
| `E-long-v2` | `scout(M2.7) -> plan(M3, 20 loops) -> work(M3) -> validate` + same gates | Best structured-efficiency candidate from run 8; tests whether better gates fix playability. |
| `Adaptive-flat-v1` | single flat transcript with M2.7 for bounded read-only/tool-search loops and M3 for work/complete | Tests the likely production direction: flat-context quality with quota-aware routing. |
| `C-v2` | `scout(M2.7) -> work(M3) -> validate` + same gates | Lean structured baseline; keeps planning overhead out. |

Optional bandwidth arms:

| Arm | Workflow | Why add it |
| --- | --- | --- |
| `F-tail-parallel-v2` | flat M3 main work + quality gates, but with source feature dependencies loosened where safe | Tests whether the apparent slowdown is graph serialization rather than agent capability. |
| `Adaptive-flat-parallel-v1` | adaptive-flat routing with the same loosened source feature fanout | Tests the most likely production candidate under higher concurrency pressure. |

These two arms are not primarily phase-order tests. They are throughput tests.
If agent capacity is underfilled, they help determine whether the graph shape is
leaving useful parallelism on the table.

Drop from run 9:

- Standard E. Run 8 did not justify keeping E separate from E-long.
- F with no tail. It is no longer a useful practical baseline because we know
  quality gates are part of making a usable game.
- Chaos/work-first arms. They were useful for discovery, but run 9 should be a
  focused confirmation run.

## Run 9 Graph Shape

Use the same source project and same source task list for every arm.

Recommended graph:

```text
feature 1
feature 2
feature 3
feature 4
mid_art_pass
mid_polish
mid_harness_qa  # metadata: {"qa_focus": "playability"}
feature 5
feature 6
feature 7
feature 8
final_art_pass
final_polish
final_harness_qa  # metadata: {"qa_focus": "playability"}
```

Optional parallel-arm graph:

```text
feature 1 ┐
feature 2 ├─ dependency-compatible feature fanout
feature 3 ┤
feature 4 ┘
mid_art_pass
mid_polish
mid_harness_qa  # metadata: {"qa_focus": "playability"}
feature 5 ┐
feature 6 ├─ dependency-compatible feature fanout
feature 7 ┤
feature 8 ┘
final_art_pass
final_polish
final_harness_qa  # metadata: {"qa_focus": "playability"}
```

Do not remove real semantic dependencies. Only loosen dependencies that exist
because the seed graph was conservative rather than because the feature truly
needs prior work.

Rationale:

- Run 8 tails were too late to prevent core playability bugs.
- A mid-run gate catches "wave cannot progress", "player cannot damage enemies",
  "controls are unclear", and "game is visually unreadable" before later
  features build on broken mechanics.
- Two quality gates should be cheaper and cleaner than three full tail cycles.

Quality-gate policy:

- `art_pass` and `polish` are soft gates. If they exhaust attempts, mark
  `quality_gate_incomplete` and continue with warnings.
- The playability QA gate should be represented as task type `harness_qa`
  with metadata `{"qa_focus": "playability"}`. Do not create a literal
  `playability_qa` task type unless the runtime gains first-class support for
  that type.
- This `harness_qa` playability gate is a hard gate only for critical mechanics:
  player can move, player can damage enemies, enemies can damage player, wave
  can complete, game can restart, and no fatal startup/runtime errors occur.
- QA should spawn targeted bug tasks for critical failures instead of cancelling
  downstream graph work directly.

Loop budgets:

| Task type | Suggested phase budget |
| --- | --- |
| Feature flat work | 200 legacy loops |
| Structured work | 150 work loops |
| Structured plan | default 10, except E-long uses 20 |
| Art work | 200 work loops |
| Polish work | 200 work loops |
| Playability QA scout | 24 scout loops |
| Playability QA work | 60 work loops |

The art/polish 200-loop budget is intentional. Run 8 and prior flat runs suggest
visual/UX work needs uninterrupted iteration, screenshot capture, VLM review,
asset wiring, and retest loops.

## Required Run 9 Telemetry

Add or verify these fields before starting run 9:

- `model` and `provider` per phase and per loop.
- `phase_loop_count` for every phase, including flat/adaptive loops.
- `vision_query_count`, `screenshot_count`, and screenshot artifact paths.
- `launch_game_count` and harness session IDs for art/polish/QA.
- `quality_gate_incomplete` and `soft_gate_failed` counts.
- `critical_playability_failures` with a fixed enum:
  `cannot_move`, `cannot_damage`, `cannot_take_damage`, `wave_stuck`,
  `restart_broken`, `startup_error`, `runtime_error`.
- Manual operator score after playtest:
  `playable`, `visual_quality`, `controls_clear`, `fun_factor`,
  `showstopper_bug`.

## Success Criteria

Primary:

- Produces a playable game with no show-stopper bug in a 5-minute manual run.
- Final verification green with zero open regressions.
- Mid and final playability QA completed or produced resolved critical bugs.

Secondary:

- Lower bug/research churn per source feature.
- Lower total loops per completed source feature.
- Lower MiniMax quota percentage consumed.
- Higher screenshot/VLM visual score.

## Expected Outcome

My prior for run 9:

1. `Adaptive-flat-v1` is the most promising production candidate if the routing
   implementation is stable. It preserves the flat-loop advantage while using
   cheaper read-only exploration where safe.
2. `F-tail-v2` is the strongest baseline and may still win on artifact quality.
3. `E-long-v2` may be the most efficient structured arm, but it needs the mid
   playability gate to avoid elegant graphs that produce broken mechanics.
4. `C-v2` is useful as a lean control, but I do not expect it to beat the hybrid
   flat arms on final game quality.

The run should not be judged only by task completion. Run 8 showed that green
closure can coexist with unplayable mechanics.
