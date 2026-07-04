# Run-11 Experiment Analysis: Per-Task Art Passes vs Integration Checkpoints
**Date:** 2026-07-04
**Project:** Void Patrol (space shooter)
**Run duration:** Partial — run started then paused while swarm-controller punch list was fixed; resumed and completed naturally. All three arms ran to task-queue exhaustion.

## Hypothesis

Two variants against a control:
- **Art arm**: every visible gameplay feature gets an `art_pass` task immediately after it lands, while context is warm
- **Integration arm**: explicit integration checkpoint tasks inserted into the plan to wire systems together before moving on
- **Control**: standard adaptive-flat pipeline (no art passes, no explicit integration checkpoints)

## Results

### Task completion

| | Control | Art arm | Integration arm |
|---|---|---|---|
| Tasks completed | **50** | 41 | 27 |
| Tasks cancelled | 0 | 3 | 0 |
| — feature | 9 | **12** | 8 |
| — bug | 15 | 9 | 13 |
| — art_pass | 6 | **10** | 2 |
| — polish | 5 | **6** | 2 |
| — harness_qa | 9 | 3 | 1 |
| — research | 6 | 1 | 1 |

### Git output

| | Control | Art arm | Integration arm |
|---|---|---|---|
| Total commits | 34 | 20 | 14 |
| Feature commits | ~1 | ~1 | ~1 |
| Fix/bug commits | 10 | 2 | 4 |
| Art commits | 4 | **5** | 1 |
| Polish commits | 1 | **2** | 0 |
| Refactor commits | 10 | 6 | 4 |
| Game scripts | 19 | **24** | 15 |
| Scenes | 30* | **14** | 8 |
| Assets (images/audio) | 17 | **28** | 3 |

*Control's 30 scenes are inflated by GUT test framework scenes (~22), not game scenes.

### Caveats

- **Integration arm had a symlink issue**: the workspace symlink broke mid-run pointing to a stale worktree. Some commits (14 in the working worktree) may not fully reflect the 27 completed tasks. Results for this arm are less reliable.
- **Run was interrupted**: all three arms were paused during the swarm-controller punch-list session and resumed later. This likely reset agent context and broke any warm-context benefit the art arm was designed to exploit.
- **Short run**: 27–50 tasks is not enough to reach a complete game. These are early-phase signals only.

## Interpretation

### Art arm: directional positive

The art arm produced:
- More features (12 vs 9/8) despite completing fewer total tasks
- More art_pass tasks (10 vs 6/2) — the mechanism worked as intended
- More polish (6 vs 5/2)
- Fewer bug tasks (9 vs 15/13) — spent less budget on cleanup
- More assets on disk (28 vs 17/3) — most concrete output signal

The key ratio is **value-generating tasks** (feature + art_pass + polish) vs **repair tasks** (bug + research):
- Art arm: 28 value / 10 repair = **2.8x**
- Control: 20 value / 21 repair = **0.95x**
- Integration: 12 value / 14 repair = **0.86x**

The art arm was nearly 3x more productive per task at generating visible output. The control and integration arms spent roughly equal budget on building and fixing.

### Control: got stuck in a fix/refactor loop

50 tasks completed but 15 were bugs and 6 research — over 40% of the budget on repair. Only 20 commits landed despite 50 tasks completing. The harness_qa count (9) is high, suggesting QA kept finding regressions and spawning bug work.

### Integration arm: worse than control

The explicit integration checkpoint tasks don't appear to have helped. Bug rate was highest (13/27 = 48%) and asset output lowest (3 images). The symlink issue adds noise, but the task-type breakdown from the DB is reliable — and it shows a repair-heavy run.

## Conclusions

1. **Per-task art passes appear to work** — the mechanism (art_pass immediately after each feature) produced more usable output per task budget. Signal is directional, not conclusive.

2. **Explicit integration checkpoints do not help** — may actually add overhead by creating tasks that don't build anything directly. Worth one more run to confirm before discarding.

3. **The warm-context benefit was likely not captured** — the run interruption reset agent context. A clean run (no pauses) is needed to fairly test the art-pass hypothesis, since the theory is that art_pass agents perform better when feature code is recent.

4. **QA loop is expensive** — control spent 9 harness_qa tasks (18% of budget) vs art arm's 3 (7%). The art arm may have produced cleaner code (fewer QA regressions) or the QA cycle cap interacted differently.

## Recommendation for Run-12

- Keep art arm variant; make it the new baseline
- Drop integration checkpoints — replace with a "system wiring" feature task type or just trust the planner
- Run a clean, uninterrupted run to get a fair warm-context measurement
- Add a mid-run checkpoint (screenshot + state readout) at task 20 and task 40 to track progression without waiting for completion
- Consider increasing `qa_max_cycles` cap for one arm to see if more QA actually helps quality or just burns budget

## Raw data

Task DB query: `GET /api/tasks?project=<arm>&include_completed=true`
Git log: `/usr/bin/git -C <path> log --format="%s"`
All three arms ran on `void-patrol` (space shooter, Godot 4).
