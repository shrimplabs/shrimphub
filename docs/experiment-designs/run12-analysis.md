# Run-12 Experiment Analysis: Art Scheduling — Per-Task vs Batched vs None

**Date:** 2026-07-14
**Project:** Void Patrol (space shooter, same base as run-11)
**Variable:** When art passes are inserted into the task DAG

---

## Hypothesis

Art passes improve game quality. The open question from run-11 was *when* to schedule them.
Three arms tested one variable — art scheduling — with all other pipeline config held constant
(adaptive flat, MiniMax M3, same game design doc).

| Arm | Art scheduling |
|-----|----------------|
| **A — per-task-art** | Art pass immediately after every visible feature, while agent context is warm |
| **B — batched-art** | Art passes batched at the mid-point and tail of the run, after a cluster of features land |
| **C — no-art (control)** | No art passes at all |

---

## Results

### Task completion

| | Arm A (per-task) | Arm B (batched) | Arm C (control) |
|---|---|---|---|
| Features completed | 14 | 8 | 7 |
| Bug fixes | 24 | 14 | 14 |
| Art passes | 7 | 5 | 0 |
| Polish | 4 | 2 | 2 |
| harness_qa completed | 4 | 3 | 2 |
| **Total completed** | **58** | **36** | **28** |
| Cancelled | 6 | 6 | 3 |

### Value/Repair ratio

Includes features + polish + art_pass as "value"; bug + research as "repair."

| | Arm A | Arm B | Arm C |
|---|---|---|---|
| Value tasks | 25 | 15 | 9 |
| Repair tasks | 25 | 14 | 14 |
| **V/R ratio** | **1.0** | **1.07** | **0.64** |

Arm C spent more than half its budget on bugs — more repair than value, and no art to show for it.
Arms A and B were near parity on V/R. Arm B's higher ratio (1.07 vs 1.0) is marginal and within noise.

### Git output

| | Arm A | Arm B | Arm C |
|---|---|---|---|
| Total commits | 54 | 51 | 39 |
| GD scripts (non-addon) | 16 | 13 | 13 |
| Scenes (non-addon) | 10 | 9 | 9 |
| Assets (images/audio) | 6 | 6 | 1 |

Arm C had almost no art assets — 1 vs 6 in both art arms. Scripts and scenes were similar across
B and C, confirming that no-art didn't block feature work, just left it unpolished.

### Playthrough bot results

All three arms eventually achieved a bot completion after research-feeder escalation.
Initial bot attempts failed across all arms due to the StateServer headless bug (fixed mid-run).

| | Arm A | Arm B | Arm C |
|---|---|---|---|
| First bot | cancelled (a=3/3) | cancelled (a=3/3) | cancelled (a=3/3) |
| Recovery bot | completed (a=3/3) | completed (a=1/3) | completed (a=1/3) |
| Final bot | completed (a=0/2) | completed (a=0/5) | — |

All three arms are bot-completable. Bot success is not a differentiator at this scale.

---

## Qualitative Observations (human playtest)

**Arm A (per-task-art): diverged into a different genre.**
The rotation-thrust movement system (Star Control / space combat style) emerged from repeated art
passes reshaping the feel of the ship after each feature. The player ship uses `extends Area2D`
with thrust/drag/rot_speed physics, wraps the viewport via `fposmod`, and auto-fires. The game
is recognizably a space shooter but not the vertical scrolling game the design doc specified. It
also had weaker art despite having the most art pass tasks — per-task art passes may have been
too granular to achieve visual coherence, each one optimizing a local patch rather than the whole.

The warm-context hypothesis was correct in one sense: the agent did respond to the current visual
state when doing each art pass. But it also meant art shaped each subsequent *feature*, compounding
into a full design drift over 14 features.

**Arm B (batched-art): best game.**
Coherent vertical shooter with a 3-HP lives system (arcade style, intentionally tight). Batching
art passes meant features stabilized first, then art passes responded to the whole cohesive system.
The result was a more intentional visual and mechanical identity. The HP system (`HP_MAX = 3`,
bullets do 1 damage) creates short plays — the game ends quickly, which is a design choice that
reads as deliberate rather than broken. The playthrough bot even had zigzag evasion logic added
(`critical-lives zigzag mode, lateral motion + fire when lives <= 1`).

**Arm C (no-art): worst by far.**
Mechanically functional but visually bare (1 asset). The bug budget was consumed keeping the
game playable rather than improving it. Without art passes breaking the repair cycle, the pipeline
stayed stuck in fix-test-fix loops. V/R of 0.64 means nearly 2 bug tasks per value task — the
control arm was the least efficient use of compute.

---

## Key Findings

### 1. Per-task art causes design drift

Inserting an art pass after every single feature creates a feedback loop where art reshapes
the *next* feature. Over 14 features + 7 art passes this compounds: arm A ended up a different
genre. This is not a pipeline failure — the agent was doing what it was asked — but it is an
unintended consequence of warm-context art at high frequency.

**Implication:** Per-task art is appropriate for cosmetic/texture passes (swap a placeholder
asset), not for feel/motion/physics decisions. Art passes that can reshape control feel
should be deferred until the mechanic is stable.

### 2. Batched art wins on game quality

Letting features settle, then applying art in a focused batch, produced the most coherent
result. The agent had a complete system to respond to rather than an evolving one. This is
consistent with how human studios work: art direction happens after a gameplay prototype
stabilizes, not while it's changing.

**Implication:** The run-11 art arm hypothesis was right that art matters. The run-12 result
refines it: *when* art lands matters as much as *whether* it does. Batch after milestone, not
after each task.

### 3. No-art is the least efficient pipeline

The control arm spent more budget on repair than value. Art passes are not just visual — they
appear to force agents to engage with the game holistically, which catches integration issues
that pure feature/bug cycles miss. Removing art entirely makes the pipeline less efficient, not
more focused.

### 4. V/R does not predict playability gap

Arms A and B had nearly identical V/R (1.0 vs 1.07), but arm B was clearly the better game.
V/R measures pipeline efficiency, not game quality. The human playtest provides the signal V/R
cannot: arm B's tighter, more intentional design felt better despite similar pipeline metrics.
This replicates the DOE finding that V/R anti-correlates with playability.

---

## Design Drift: A New Failure Mode

Run-12 surfaced a failure mode not previously tracked: **design drift** — the game at the end
of the run implements a different design than specified, without any error or failure signal.

Arm A's drift was detectable only by playing the game. The pipeline saw it as a success:
features completed, bot passed, QA passed. The design doc specified a vertical scrolling shooter;
arm A delivered a rotation-thrust space combat game. Both are valid games. Only one is the game
that was planned.

**Mitigation candidates:**
- Design doc conformance check as a QA criterion (does the control scheme match the spec?)
- Throttle art pass frequency (max 1 per N features, or only at milestones)
- Art pass scope restriction: cosmetic-only passes cannot modify physics or control scripts

---

## Run-13 Direction

The evidence points toward batched art as the new baseline. The open questions:

1. **Art batch timing:** mid-run only, tail-only, or both? Arm B batched at both — test
   mid-only vs tail-only to isolate where the value concentrates.

2. **Design drift monitoring:** add a design-conformance check to the QA gate. The check
   should compare the live control scheme and core mechanic against the design doc, not just
   whether the game runs.

3. **Genre stability:** run a non-void-patrol game to see if these findings generalize. Twelve
   runs of the same game are valuable for variable isolation but limit external validity.

---

## Confounders

- **StateServer headless bug** affected all arms equally (all initial bots failed and needed
  research-feeder recovery). Fixed mid-run; did not differentially impact any arm.
- **Arm A task count inflation:** 58 completed tasks vs 36/28 partly reflects more features
  generating more bugs to fix, not necessarily more progress. The design drift may have created
  more repair surface.
- **Human playtest is one session:** qualitative ranking (B > A > C) is a single observer's
  judgment. The rubric from doc 07 was not applied formally.
