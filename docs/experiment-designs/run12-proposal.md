# Run-12 Proposal: Mechanism Isolation for the Art-Pass Win

**Date:** 2026-07-13
**Author:** Claude (advisory)
**Status:** Proposal — team decision pending
**Inputs:** `run10-analysis.md`, `run11-analysis.md`, run-11 ideas memory

---

## TL;DR recommendation

**Run three arms: per-task art (new baseline), batched art (same art quota, scheduled at sprint end), and no-art control.** Primary comparison is per-task vs batched — it isolates *timing* (warm context) from *task mix* (more value tasks in the plan), which is the one confound run 11 cannot rule out. Defer Hypothesis B (QA depth) because the primary metric is structurally biased against it. Defer Hypothesis C (bot behavioral effect) because there is no pre-bot baseline — but ship the bot gate in all three arms so run 12 *becomes* that baseline. Reject Hypothesis D as posed: it is a false dichotomy — the baseline arm inside a multi-arm run *is* the clean benchmark.

---

## 1. Which hypotheses are worth testing now

### Hypothesis A (warm context) — TEST NOW. Highest information value.

Run 11's 2.8x has two candidate mechanisms, and they demand different pipelines:

1. **Timing:** art_pass lands while the feature code is warm, so the art agent makes better scoped decisions (the original theory).
2. **Task mix:** the art arm's plan simply *contained more value-type tasks*. The value/repair ratio partially rewards this by construction — if you plan 10 art_pass tasks instead of 6, your numerator grows even if the game isn't better. Some of the 2.8x may be definitional, not causal.

Here's the detail that should worry you: **the art arm won despite the interruption.** The team reads this as "2.8x is an underestimate of the warm-context effect." The equally valid reading is that warm context wasn't the mechanism at all — the interruption should have dragged a warm-context-dependent arm back toward control levels, and it didn't. Task mix and earlier-visual-feedback (the run-10 mid-gate finding) don't need warm context and both survive interruptions. The 2.8x is real; the *explanation* is unconfirmed.

This matters for run 13 and beyond:
- If timing dominates → keep strict feature→art interleaving, protect run continuity, maybe extend the pattern (per-task polish?).
- If task mix dominates → art quota is what matters; the scheduler can batch art freely, which is simpler, more parallelizable, and robust to interruptions.

No other open question changes the pipeline design this much. **This is the question run 12 exists to answer.**

### Hypothesis B (qa_max_cycles 3→5) — DEFER. The metric can't judge it.

Two disqualifying problems:

1. **Metric bias:** more QA cycles mechanically produce more bug tasks, which inflate the repair denominator. An arm with qa_max_cycles=5 will score a *worse* value/repair ratio even if it ships a strictly better game. You would be running an experiment whose primary metric is rigged against the treatment. Until there's an endpoint quality measure (playthrough bot pass depth, screenshot rubric, human playability score), the answer will look like "QA burns budget" regardless of the truth.
2. **Weak prior:** run 11's cleaner arm (art) spent *less* on QA (7% vs 18%), suggesting the QA burden is downstream of code quality, not upstream of game quality. The expected result of B is null-or-negative, and confirming an expected null is the lowest-value use of an arm.

Revisit in run 13 once the bot gives you an endpoint quality signal that doesn't punish QA by construction.

### Hypothesis C (bot as behavioral gate) — DEFER, but instrument now.

The team's instinct is right: you can't measure whether the bot changes agent behavior when you have zero runs with the bot at all. Worse, run 12 is the bot's first deployment — its own teething failures (false gate blocks, harness bugs) would be indistinguishable from behavioral effects. First deployments are for debugging the instrument, not for measuring with it.

**But:** put the bot gate in *all* run-12 arms. That gives you (a) shakedown data on the bot itself, (b) a with-bot baseline across three pipeline variants, and (c) the bot's pass/progress data as a free secondary quality metric for Hypothesis A. Run 13 can then test bot-gate vs no-gate against a known baseline.

### Hypothesis D (clean baseline only) — REJECT as posed.

See §3.

## 2. 2×2 or sequential?

**Neither — test only A.** But answering the question as asked: a 2×2 of A×B is the wrong shape for this system. With n=1 per cell and ~40–60 tasks per arm, you cannot estimate an interaction effect; you'd have four anecdotes instead of three. The run-10/11 data shows between-arm noise is large (control completed 50 tasks, integration 27, on identical budgets) — a 4-way split guarantees at least one arm gets derailed by an infrastructure incident (run 10's `_is_infrastructure_failure` bug, run 11's symlink break; there has been one per run) and takes a quarter of your budget with it.

If you insist on both: sequential, A in run 12, B in run 13 — B needs the endpoint metric that run 12's bot deployment creates anyway. The confounding risk of running them together is concrete: QA depth changes bug-task volume, bug-task volume changes the repair denominator, and you can no longer tell whether an art arm's ratio moved because of art timing or because of QA-generated repair inflation.

## 3. Is Hypothesis D the right call?

No — it's a false dichotomy. Arm 1 of the recommended design **is** the clean production baseline: adaptive flat + per-task art + bot gate + qa_max_cycles=3, run uninterrupted. Whatever the other arms do, that arm's data is your run-13 benchmark.

What D-as-stated actually buys you: slightly less orchestration overhead and zero risk of cross-arm contamination. What it costs you: an entire run-cycle of calendar time and the answer to the mechanism question, which you would then have to buy again in run 13 — *after* having already committed the per-task-art pipeline to production on an unconfirmed causal story. If batched art turns out to be equivalent, you'll have spent a run entrenching an interleaving constraint you didn't need.

The legitimate kernel of D is "don't change too many variables at once." The design below honors that: all arms share the identical new stack (adaptive flat, bot gate, QA cap 3); the *only* axis of variation is art scheduling. That is one variable, cleanly isolated.

## 4. How many arms is too many?

**Three, at this budget.** The empirical noise floor from runs 10–11: identical-budget arms differ by ±30–40% in tasks completed for reasons unrelated to treatment (infra bugs, symlinks, scheduler nondeterminism). With n=1 per arm, an effect needs to be roughly ≥1.5–2x to be readable over that floor. The run-11 headline (2.8x vs 0.95x) cleared it comfortably; a subtler effect (say, qa 3 vs 5) would not. Every additional arm adds pairwise comparisons whose expected effect sizes shrink below the floor.

Rule of thumb going forward: **one arm per decision you would actually change in the next run, plus one control.** Run 12 has one live decision (art scheduling) → 2 treatment arms + 1 control = 3. Four-plus arms is justified only when you expect large (>2x) effects on every axis, which was true in run 10's exploratory phase and is no longer true now that you're refining a winner.

## 5. Recommended run-12 design

**Game:** void-patrol (space shooter), same base clone as runs 10–11 — preserves comparability.

**Shared stack across all arms (the "new normal"):**
- Adaptive flat pipeline (ADAPTIVE_FLAT = True)
- Playthrough bot as final gate in every arm
- qa_max_cycles = 3
- No integration checkpoints
- Clean run: **infra freeze on swarm-controller for the duration.** No punch lists, no dashboard work, no restarts. This is a hard precondition — run 11's central caveat came from violating it.
- Mid-run checkpoints: screenshot + StateServer state readout at task 20 and task 40 per arm (run-11 recommendation, adopt it).

**Arms:**

| Arm | Name | Art scheduling | Purpose |
|-----|------|----------------|---------|
| **A** | `void-patrol-art-pertask-run12` | art_pass immediately after each visible feature (run-11 art arm structure, now the production baseline) | Production benchmark + clean replication of the 2.8x |
| **B** | `void-patrol-art-batched-run12` | **Same number of art_pass tasks**, same scoped descriptions, but scheduled as a batch after the feature phase completes | Isolates timing from task mix |
| **C** | `void-patrol-control-run12` | No planned art_pass tasks (auto-QA/auto-audit unchanged) | Anchors the total art effect; clean re-run of run-11 control under the new stack |

**Critical design constraint for A vs B:** the plans must contain *identical* art_pass task counts and per-task scopes ("enemy sprites + readability", etc.). Only the dependency placement differs — interleaved vs end-batched. If B gets fewer or vaguer art tasks, the comparison collapses back into the run-11 confound. Pre-register the DAGs before launch.

**End condition (pre-register):** run each arm to task-queue exhaustion OR a 60-completed-task cap, whichever first. Run 11's unequal completion counts (50/41/27) made per-task normalization mandatory; a cap bounds the worst of it.

**Primary comparison:** A vs B on value/repair ratio and assets-on-disk.
**Secondary comparisons:** (A ∪ B) vs C — total art effect under clean conditions; A vs run-11 art arm — replication check.

**Metrics (pre-register definitions — run 11 was loose about "repair"):**
- **Primary:** value/repair = (feature + art_pass + polish completed) / (bug + research completed). Pin this exact definition.
- **Endpoint quality (new, via bot):** playthrough bot pass/fail + progress depth (waves cleared / states reached) per arm. This is the metric that de-biases future QA experiments.
- **Concrete output:** assets on disk, game scripts, scenes (exclude GUT scaffolding — run 10's scene counts were polluted by it), commits per completed task.
- **Cost:** tokens per arm, tokens per value task.
- **Trajectory:** screenshot rubric at t20/t40/final (does per-task art look better *earlier*, even if endpoints converge? That alone has value — earlier visual feedback improved agent decisions in run 10).

**Readout matrix (decide interpretation before running):**

| Result | Conclusion | Run-13 action |
|--------|-----------|---------------|
| A > B > C | Warm context is real and additive to task mix | Keep interleaving; protect run continuity as a first-class ops constraint; consider per-task polish |
| A ≈ B > C | Art quota is the mechanism; timing is free | Let the scheduler batch art; simplifies planning, enables parallelism |
| A > B ≈ C | Timing is *everything*; batched art is wasted spend | Interleaving is load-bearing; never batch |
| A ≈ B ≈ C | Run-11 result was noise or interruption artifact | Stop, re-examine metrics before building further on the art-pass story |

## 6. What run 11 didn't tell us that run 12 must

Two things, in priority order:

1. **Mechanism.** Run 11 established *that* the art arm wins; it cannot say *why*. Timing vs task mix produce identical run-11 data but demand different pipelines. The A/B/C decomposition above is the only way to separate them, and run 12 is perfectly positioned because the pipeline is otherwise frozen.

2. **Replication under clean conditions.** The 2.8x is a single observation from an interrupted run with unequal arm completions and a metric that partially rewards the treatment by construction. Before the art-pass pipeline calcifies into "the way we build games," one clean confirmation is cheap insurance. Arm A provides it for free. If A lands anywhere in the 2–3x band against C, the story holds; if it regresses toward 1x, you've caught a false foundation before run 13 built on it.

A third, quieter deliverable: run 12 produces the first with-bot baseline (bot pass rates, gate friction, false-block incidents across three pipeline variants), which is the prerequisite for testing Hypotheses B and C properly in run 13.

## What run 13 looks like from here

- If A≈B: batched-art becomes an option; test scheduler-driven art placement.
- Hypothesis B (QA depth) with the bot's progress-depth as primary metric instead of value/repair.
- Hypothesis C (bot gate vs no gate) against run 12's with-bot baseline.
- Consider a second game (the arkanoid design doc exists) — everything so far is n=1 on one genre; the art-pass effect may be shooter-specific (sprite-heavy, visually dense). One replication on a different genre is worth more than a fourth arm on void-patrol.

---

## Appendix: pre-launch checklist

- [ ] Pre-register both DAGs (A and B) with identical art_pass counts and scopes; diff them to confirm only edge placement differs
- [ ] Pin value/repair definition (research counts as repair — matches run-11 accounting)
- [ ] Confirm playthrough bot runs green on the unmodified base clone *before* launch (instrument shakedown outside the measurement window)
- [ ] Infra freeze declared: no swarm-controller changes, restarts, or punch-list work until all arms exhaust
- [ ] Verify workspace symlinks per arm (run-11 integration arm failure mode)
- [ ] Screenshot + state checkpoint automation armed for t20/t40
- [ ] 60-task cap configured per arm
- [ ] Token accounting per arm confirmed working (agent token files → DB)
