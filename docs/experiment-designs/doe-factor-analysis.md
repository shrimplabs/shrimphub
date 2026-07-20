# DOE Factor Analysis: Void Patrol Pipeline Experiments, Runs 1–11

**Date:** 2026-07-13
**Scope:** Retrospective Design-of-Experiments analysis of the void-patrol pipeline run series
**Sources:** `run4-analysis-findings.md`, `run7-quantitative-findings.md`, `run8-analysis-run9-proposal.md`, `run6-run9-presentation-findings.md`, `run10-analysis.md`, `run10-incidents.md`, `run11-analysis.md`, `pipeline-ab-experiment.md`, `pipeline-run-lifecycle.md`, `run12-proposal.md`, run-5 intervention log (memory)

---

## 0. Ground rules for reading this document

Before any factor claims, three structural facts about the run series that bound every conclusion below:

1. **Every cell is n=1.** No arm configuration has ever been run twice under identical conditions. The closest thing to replication is the flat family appearing in runs 4, 7, 8, 9, 10 — but the surrounding stack (controller code, recovery policy, DAG shape, telemetry) changed between every run. Treat all effect estimates as direction + rough magnitude, never as calibrated numbers.

2. **There has been roughly one infrastructure incident per run** (run 4: `str/str` tool exception + chaos recovery inheritance; run 5: seven manual interventions; run 7: provider/router 502 outages; run 8: agent-lifecycle bug fixed mid-run + two live DB repairs; run 10: `_is_infrastructure_failure` false-positive loop; run 11: workspace symlink break + mid-run pause). The empirical between-arm noise floor is ±30–40% in tasks completed on identical budgets. **Any effect smaller than ~1.5–2x on the primary metric is unreadable in this series.**

3. **The primary metric has drifted across runs.** Run 4 scored validation pass rate and feeder churn; run 7 scored loop efficiency; runs 8–10 scored human playability; run 11 introduced value/repair ratio. These are *not* interchangeable — see §2.0, where they actively disagree. Cross-run effect estimates below are reconstructed onto value/repair where the task-type breakdowns allow it, with the reconstruction flagged.

Genre/codebase is deliberately held constant (void-patrol, Godot 4 space shooter, same base clone). This is correct DOE practice for isolating pipeline factors — and it means **external validity is exactly zero** until a second game is run. Every effect below is formally "effect on void-patrol."

---

## 1. Factor identification

### F1. Pipeline phase structure (flat vs structured decomposition)

The founding factor of the series. Levels actually tested:

| Level | Definition | Runs |
|---|---|---|
| Flat (F) | no phases, single continuous loop | 4, 7, 8 |
| Flat + tail (F-tail) | flat loop + 3× art/polish/QA end gates | 8, 9, 10 |
| Adaptive-flat | flat transcript + per-loop model routing | 9, 10, 11 (all arms) |
| plan→scout→work→validate (control) | full structured | 4, 7 |
| plan→work→validate (A) | drop scout | 4 |
| plan→scout→synthesize→work→validate (B) | add synthesize | 4 |
| scout→work→validate (C) | drop plan | 4, 7, 8, 9 |
| scout→plan→work→validate (E) | scout before plan | 4, 7, 8, 9 |
| work→scout→plan→validate (G) | chaos-derived fixed order | 7 |
| random per task (D) | chaos arm | 4 |
| adaptive per task type | phase order by task type | 7 |

**Cleanliness:** the *best-manipulated* factor in the series — runs 4 and 7 varied it with everything else held nominally constant. But run 4's chaos arm (D) was contaminated by recovery-pipeline inheritance (feeders got randomized orders, 43 invalid), and run 7 had provider outages censored post hoc. From run 9 onward the factor was effectively *retired*: runs 10–11 run flat-family arms only, so all later factors are nested inside "flat won."

### F2. Quality tail gates (end-placed art/polish/QA)

| Level | Runs |
|---|---|
| None | run 4 (all arms), run 8 F |
| 3× art_pass→polish→harness_qa tail | run 7 (all arms), run 8 (C, E, E-long, F-tail), run 9 |

**Cleanliness:** run 8's F vs F-tail is the one near-clean paired manipulation (same flat pipeline, ± tail). Everything else introduced tails simultaneously across all arms (run 7), so tail-vs-no-tail is unmeasurable within run 7.

### F3. Art pass placement/timing

The factor the series has converged on:

| Level | Runs |
|---|---|
| None planned | run 11 control |
| End-tail batched (with polish/QA) | runs 7–9 |
| Mid + final gates | run 10 (observational note: mid gate "started to look like things") |
| Per-task interleaved (art_pass after every visible feature) | run 11 art arm |

**Cleanliness:** run 11's manipulation is structural (different DAGs), which means the art arm's *plan contained more value-type tasks* — the value/repair numerator is partially inflated by construction. This is the central confound the run-12 proposal exists to break (timing vs task mix). Additionally run 11 was interrupted mid-run, destroying the warm-context condition the treatment was designed to test. So: real effect observed, mechanism unidentified, treatment integrity compromised. Directional only.

### F4. Explicit integration checkpoints

| Level | Runs |
|---|---|
| None | everything except run 11 integration arm |
| `bug`-type integration task every 2 features | run 11 |

**Cleanliness:** poor. The integration arm suffered a broken workspace symlink mid-run; git output figures are unreliable (task DB figures are OK). One contaminated data point.

### F5. Model routing / model tier per phase

| Level | Runs |
|---|---|
| Single model (MiniMax M3 equivalent) all loops | runs 4–8 |
| M2.7 for bounded read-only scout, M3 for work/complete (adaptive-flat routing) | runs 9–11 |

**Cleanliness:** never isolated. Adaptive-flat arrived in run 9 *bundled with* the flat-transcript structure and cheap-completion blocking. There is no arm anywhere that runs the identical pipeline with and without routing. The run 8 finding "M2.7 should not own long freeform work" is observational (from failures), not experimental.

### F6. Plan-phase depth (loop budget for plan)

| Level | Runs |
|---|---|
| plan=10 loops (E) | run 8 |
| plan=20 loops (E-long-plan) | run 8 |

**Cleanliness:** genuinely clean paired manipulation within run 8 — the only single-knob A/B in the whole series. Result was null-to-negative on the thing that mattered (did not prevent the core combat-loop playability failure; E-long had lowest bug+research churn at 1.67 but a broken game).

### F7. Parallelism / DAG fanout

| Level | Runs |
|---|---|
| Serial chains | most runs; run 10 accidentally *strictly linear* |
| Loosened feature dependencies (parallel arms) | run 9 (F-tail-parallel, adaptive-flat-parallel), run 10 (quality-parallel) |

**Cleanliness:** compromised twice. Run 9's parallel arms were "optional bandwidth arms" competing for a shared agent pool (wall-clock and spend not independent). Run 10's serial-vs-parallel comparison was hit asymmetrically by the `_is_infrastructure_failure` bug, which damaged the serial quality arm far more than the parallel one — the observed parallel advantage is partly bug-shadow. Also run 10's DAG was accidentally linear vs run 9's branching, an unlogged level change discovered mid-run.

### F8. Work-loop budget

Levels: 80 → 150 (run 5 hotfix), 200 for art/polish (voxel-forge override, adopted into run 9 gates). **Never manipulated experimentally** — every change was a mid-run repair or production tweak. Observational conclusion only: 80 caused panic-patching spirals.

### F9. Recovery/feeder pipeline policy

Levels: inherit experiment variant (run 4) → pinned stable recovery pipeline (run 5+). Changed once, permanently, in response to the run 4 D-arm cascade. Not a designed factor; a discovered constraint. Its main analytical role is as a *documented contaminant of run 4*.

### F10. QA depth (`qa_max_cycles`)

**Constant at 3 in every run. Never manipulated.** Listed because it keeps being proposed (run 11 recs, run 12 Hypothesis B) and because the primary metric is structurally biased against it (more QA cycles → more bug tasks → bigger repair denominator).

### Nuisance variables (uncontrolled, varied anyway)

- Controller code: changed mid-run in runs 5, 8, 10 (documented interventions), between all runs.
- Run continuity: run 11 paused/resumed; others continuous.
- Telemetry: token recording usable only from run 6 onward; flat vs structured loop counts not equivalent (legacy vs phase telemetry, run 7 caveat).
- Provider health: 502 storms in run 7; router policy changes in run 9 smoke tests.
- DAG shape drift: run 10 linear vs run 9 branching (accidental).

---

## 2. Effect estimates

### 2.0 First, the uncomfortable finding: the primary metric disagrees with the goal metric

Reconstructing value/repair (value = feature + art_pass + polish completed; repair = bug + research completed) where task-type breakdowns exist:

| Run | Arm | Value | Repair | V/R | Human playability verdict |
|---|---|---:|---:|---:|---|
| 10 | F-tail | 20 (10f+5a+5p) | 18 (12b+6r) | **1.11** | not the winner |
| 10 | adaptive-flat | ~16 (6f+3a+7p) | ~35 (30b+5r) | **~0.46** | **only genuinely playable game of run 10** (human tested) |
| 11 | art arm | 28 | 10 | **2.80** | best asset output; endpoint not human-scored |
| 11 | control | 20 | 21 | **0.95** | — |
| 11 | integration | 12 | 14 | **0.86** | — |

And the run 4/7 inverse metric (bug+research per source feature — lower is "better" on repair burden):

| Run | Arm | Bug+research per feature | Artifact verdict |
|---|---|---:|---|
| 4 | F (flat) | 3.75 (worst except D) | **strongest artifact of run 4** |
| 4 | B (heaviest pipeline) | 1.88 (best) | weak |
| 7 | F flat | 3.62 (worst) | strong |
| 7 | C scout-work | 1.50 (best) | not best game |

**In runs 4, 7, and 10, the arm with the best repair-ratio was never the arm with the best game, and the arm with the best game twice had the *worst* repair ratio.** The mechanism is visible in the run-10 skill notes: bug/fix churn *was the integration mechanism* for adaptive-flat — repair tasks were doing the wiring work that made the game playable. Run 11's art arm is the first case where value/repair and concrete-output signals (assets on disk) point the same way, and even there the endpoint was not human-playtested.

Implication for everything below: **value/repair is a cost-efficiency metric, not a quality metric.** Effects on it are real and worth optimizing, but any factor decision made on value/repair alone risks re-running the run-10 mistake (quality-parallel: 100% completion, broken game). Every effect estimate below states which metric it is an effect *on*.

### 2.1 F1 Pipeline structure — STRONG signal, but two-faced

- **On artifact quality/playability: flat-family wins, consistently.** Run 4 F (zero feature retries, strongest artifact), run 7 F competitive, run 8 F-tail strongest practical, run 9 adaptive-flat and F-tail top two, run 10 adaptive-flat only playable game. Five runs, same direction. This is the most-replicated finding in the series and the reason runs 10–11 stopped testing it.
- **On repair burden / value-repair: structured pipelines win.** Run 7 C 1.50 vs F 3.62 churn; run 4 B 1.88 vs F 3.75. Also consistent across runs.
- Magnitude on churn: roughly 2–2.5x more repair per feature for flat. Magnitude on quality: unquantified (no rubric existed).
- **Interpretation:** structure suppresses repair *tasks* without producing better *games* — it front-loads coherence into phases the graph doesn't count, while flat pushes integration into countable bug tasks. Effect is strong but the sign depends entirely on which metric you care about.
- Sub-effects within structured: E (scout→plan) best structured efficiency (run 7: 80.6% validation pass, 53 avg loops); G (work-first) clearly worst (run 7, all metrics); synthesize (B) and heavy planning added overhead without benefit (run 4). One run each beyond E/C — weak.

### 2.2 F2 Quality tails — MODERATE signal, one clean pair

- Run 8 F vs F-tail: tail arm was the practical winner on playability; pure F churned more (2.62 vs 2.09). Run 9 F-tail again strong and cheap (64.5M tokens vs 258.3M adaptive-flat).
- Run 8 also produced the key *placement* caveat: end-tails are **too late** — "cannot damage enemies" class bugs burned half the budget before any gate saw the game.
- Effect on value/repair: not properly computable (tails add both art/polish value tasks and QA-spawned bug tasks). Effect on playability: positive, ~1 clean data point + 1 consistent replication. **Moderate confidence in "gates help"; high confidence in "end-only gates are mistimed."**

### 2.3 F3 Art placement — STRONG magnitude, WEAK identification

- Run 11: per-task art 2.80 vs control 0.95 vs integration 0.86 → ~3x on value/repair, comfortably above the noise floor. Corroborated by non-metric signals that don't share the definitional bias: assets on disk (28 vs 17 vs 3), fewer bug tasks (9 vs 15/13), more features completed (12 vs 9/8), less QA spend (7% vs 18% of budget).
- **But:** (a) task-mix confound — the treatment DAG contains more value-type tasks by construction, so part of the 2.8x is definitional; (b) the interruption destroyed the warm-context mechanism being tested, and the arm won *anyway*, which actively argues the mechanism is task mix or early-visual-feedback rather than warm context; (c) n=1; (d) endpoint quality not human-verified.
- Verdict: **effect real, magnitude inflated by unknown amount, mechanism unidentified.** The single most important open identification problem in the series (correctly targeted by run 12).

### 2.4 F4 Integration checkpoints — WEAK negative

0.86 vs 0.95 control is inside the noise floor; the arm was symlink-contaminated; the *direction* (highest bug rate 48%, lowest assets 3) is consistent enough to justify dropping the pattern, not enough to declare it harmful. One contaminated data point. **Weak.**

### 2.5 F5 Model routing — UNRESOLVED (confounded at birth)

Adaptive-flat has never been separated from adaptive routing. Run 9's comparison (adaptive-flat 258.3M tokens, best quality vs F-tail 64.5M, robust) confounds routing with the arms' different recovery/iteration appetites. The cheap-loop stats exist in token JSONs (`cheap_loops`, `strong_loops`, `model_switches`) but no analysis has computed whether routing actually saved money at equal quality. Observational support only for "M2.7 must not own long work" (run 8). **Unresolved.**

### 2.6 F6 Plan depth — WEAK null (but clean)

Run 8 E vs E-long-plan: doubling plan loops did not prevent the fundamental playability failure; churn slightly improved (1.67 vs 2.89). The only clean single-knob manipulation in the series and it returned "not the bottleneck." Useful as a *negative*: planning depth is not where quality comes from. **Weak-but-clean null.**

### 2.7 F7 Parallelism — UNRESOLVED, contradictory

- Run 9: parallel arms cheaper and cleaner, but best artifacts came from serial/coherent histories → parallelism *costs* coherence.
- Run 10: quality-parallel was the cleanest arm (31/31, zero failures, least rework 5 fix commits) and beat quality-serial decisively → parallelism *helps*.
- These aren't necessarily contradictory (run 10's serial arm was disproportionately damaged by the infra bug; and "clean execution" ≠ "good game" — quality-parallel's game was reportedly broken), but the factor has never been tested without an asymmetric confound. **Unresolved.**

### 2.8 F8–F10 — no experimental data

Loop budgets (F8): observational floor-effect only (80 too low). Recovery pinning (F9): adopted as constraint, untested as factor. QA depth (F10): never varied; current metric cannot judge it.

---

## 3. Factor interactions

Interactions are where an n=1-per-cell series is weakest; all of the following are hypotheses with observational support, none are estimated effects.

1. **Structure × infrastructure robustness (best-supported interaction).** Serial quality gates amplify infra failures: run 10's `_is_infrastructure_failure` bug froze 7 tasks in the serial quality arm, mildly dented the parallel arm, and left both flat arms untouched. Longer pipelines mechanically raise the probability of encountering a transient provider error, and serial chains propagate the freeze. Flat arms are structurally immune. Given ~1 infra incident per run, **flat's cross-run winning streak is partly a robustness effect, not purely a quality effect.**

2. **Repair churn × quality (the flat paradox).** Flat's high bug volume is not pure waste — in runs 4 and 10 it functioned as the integration mechanism. Suppressing repair tasks (via structure, or via optimizing value/repair) may suppress the process that makes games work. The art-pass result is interesting precisely because it's the first intervention that reduced repair *while* increasing concrete output — if it replicates, it breaks this interaction.

3. **Gate placement × budget phase.** Gates work when they fire early enough to redirect spend (run 10 mid-gate observation, run 11 per-task art) and fail when placed after the budget is spent (run 8 tails). "Gates help" is conditional on placement — F2 and F3 are really one factor: *quality-pressure timing*, with levels none / end / mid / per-task, and the estimated ordering per-task > mid > end > none is monotone in earliness. This is the tidiest cross-run pattern available, assembled from three different runs' metrics, so treat as strong hypothesis rather than established.

4. **Model tier × phase boundedness.** M2.7 acceptable for bounded read-only loops, harmful for open-ended work/completion (run 8 rule, run 9 smoke enforcement via cheap-completion blocking). Never tested factorially.

5. **Parallelism × integration-heaviness.** Run 9's synthesis: parallelize independent implementation, serialize integration-heavy gameplay/polish. Directly untested; run 10's accidental all-linear DAG and run 11's all-arms-same-DAG mean no run since has varied fanout deliberately.

6. **Art timing × run continuity (hypothesized, load-bearing, untested).** The warm-context story requires uninterrupted runs. Run 11 broke continuity and the art arm won anyway — evidence *against* this interaction existing, which is exactly what run 12's per-task vs batched design will resolve.

---

## 4. The factor map

| # | Factor | Levels tested | Best level (on stated metric) | Effect direction & magnitude | Confidence |
|---|---|---|---|---|---|
| F1 | Pipeline structure | flat, flat+tail, adaptive-flat, 7 structured orders, random | flat-family (playability); C/E (repair burden) | Flat: better artifacts across 5 runs; ~2–2.5x *more* repair churn | **Strong** (most replicated); sign metric-dependent |
| F1a | Structured order (within F1) | control, A, B, C, D, E, G, adaptive | E for efficiency; G/D eliminated | E: 80.6% validation pass, lowest loops (run 7) | Weak-moderate (1–2 runs per order) |
| F2 | Quality tail gates | none, 3× end-tail | tail present | Positive on playability; end placement too late | Moderate (1 clean pair + 1 replication) |
| F3 | Art placement | none, end-batch, mid+final, per-task | per-task | +~3x value/repair, +65% assets vs control | **Strong magnitude, weak identification** (n=1, task-mix confound, interrupted run) |
| F4 | Integration checkpoints | none, every-2-features | none | ~null-to-negative (0.86 vs 0.95) | Weak (1 contaminated point) |
| F5 | Model routing (cheap/strong) | single-model, adaptive-flat routing | unknown | quality↑ + cost 4x in run 9, but bundled with structure | **Unresolved** (never isolated) |
| F6 | Plan depth | 10 loops, 20 loops | indifferent | null on playability; small churn improvement | Weak null (but the series' only clean A/B) |
| F7 | Parallelism | serial, loosened fanout | contradictory | run 9: coherence cost; run 10: cleanliness win | **Unresolved** (asymmetric confounds both times) |
| F8 | Work-loop budget | 80, 150, 200 (all hotfixes) | ≥150 | floor effect only | Observational only |
| F9 | Recovery pipeline pinning | inherit, pinned | pinned | run 4 D-arm cascade (67 feeder cycles) | Adopted constraint, not a factor |
| F10 | QA depth (qa_max_cycles) | 3 only | untested | — | No data; metric currently biased against it |

**What the series has actually established** (the short list you can build on):

1. Flat/continuous context beats heavy phase decomposition for artifact quality on this game (5 runs, consistent).
2. Quality pressure (art/polish/QA) is part of the intervention, not measurement — and earlier placement is better (3 runs, monotone pattern).
3. Chaos ordering, work-first ordering, synthesize phases, and deep planning are dead ends (runs 4, 7, 8).
4. Graph metrics (completion %, validation pass, value/repair) do not predict playability and have anti-correlated with it at least three times.
5. Everything else is one data point or confounded.

---

## 5. What's still unresolved

**Never cleanly isolated:**

1. **Art-pass mechanism** — timing (warm context) vs task mix vs early-visual-feedback. The 2.8x headline rests on an unidentified mechanism. *(Run 12 is correctly aimed at this.)*
2. **Model routing value** — no same-pipeline ± routing comparison exists; run 9's token JSONs contain the raw data for a partial retrospective (cheap-loop share vs rework), which is free and should be done before any routing arm is designed.
3. **Parallelism** — both attempts confounded (shared agent pool; asymmetric infra bug).
4. **QA depth** — never varied, and cannot be judged until an endpoint quality metric exists that doesn't count QA-spawned bugs against the treatment.
5. **Genre generality** — zero external validity by design. The art-pass effect is plausibly shooter-specific (sprite-dense, visually legible progress).
6. **Endpoint quality measurement itself** — human playtests have been ad hoc and unblinded; the screenshot rubric and playthrough-bot progress-depth metric are designed but never deployed. This is an instrument gap, not a factor, but it blocks factors 3, 4, and 10.

**Never-tested combinations worth noting:** structured E-pipeline + per-task art (the two individually-best "efficiency" levels have never met); adaptive-flat without routing; per-task art at varying quotas (dose-response); parallel fanout within the art-arm structure.

**Minimum future-run set to resolve the important unknowns** (assuming 3 arms/run, one instrument-quality metric landing in run 12):

- 1 run for art mechanism (per-task vs batched-same-quota vs none) — run 12 as proposed.
- 1 run for genre replication of whatever run 12 confirms (arkanoid clone, same arm structure).
- 1 run for QA depth + bot-gate behavioral effect, using bot progress-depth as primary (needs run 12's with-bot baseline first).
- 1 run for parallelism, clean (identical DAGs ± fanout loosening, infra-freeze, per-arm agent quotas).

That is four runs to convert the four live unresolveds into readable answers. Model routing can likely be resolved retrospectively from existing run 9–11 token data at zero run cost.

---

## 6. Recommended factor priority for runs 12–15

Ordering principle: (a) resolve factors that change pipeline *design* before factors that tune parameters; (b) fix the measurement instrument before running experiments the current metric can't judge; (c) buy external validity before deepening void-patrol-specific optimization.

**Run 12 — Art mechanism (F3 identification). Endorse the existing proposal as written.**
Per-task vs batched-same-quota vs no-art, identical shared stack, pre-registered DAGs with identical art task counts/scopes, infra freeze, 60-task cap, bot gate in all arms as instrument shakedown. This is the highest-information run available: the A vs B contrast is the only way to decompose the 2.8x, and arm A doubles as the clean replication the interrupted run 11 never provided. Secondary deliverable: first with-bot baseline + t20/t40 screenshot trajectory — the instrument runs 13–14 need.
*Also do the free work:* retrospective routing analysis from run 9–11 token JSONs (F5), and pin the value/repair definition permanently.

**Run 13 — Genre replication (external validity).**
Take the run 12 winner structure to the arkanoid design doc, 2–3 arms (winner structure vs control, optionally winner-at-half-art-quota for a crude dose-response point). Rationale for doing this *before* QA depth: every conclusion in this document is currently conditional on one game; if the art effect is genre-specific, runs 13–15 spent tuning it on void-patrol are wasted. One replication on a second genre is worth more than any fourth arm on void-patrol (run 12 proposal already concedes this).

**Run 14 — QA depth + bot gate (F10 + Hypothesis C), now judgeable.**
qa_max_cycles 3 vs 5, and bot-gate vs no-gate, scored on bot progress-depth / screenshot rubric as primary (value/repair demoted to cost diagnostic for this run — it is structurally biased against the QA treatment). This run only makes sense after run 12 proves the bot instrument and run 13 confirms the baseline generalizes.

**Run 15 — Parallelism, finally clean (F7).**
Identical winner-structure DAGs ± loosened feature fanout, with per-arm agent quotas (the run 9 shared-pool confound) and infra freeze (the run 10 asymmetry confound). Tests interaction #5 directly: parallel implementation + serialized quality gates vs fully serial. If run 12 lands "A ≈ B" (task mix, not timing), parallelism gets more attractive and this run could swap earlier with run 14.

**Standing protocol changes regardless of sequence:**

1. **Pre-register per run:** arm DAGs, metric definitions, end condition, and the readout matrix (run 12's format is the template — adopt it for every run).
2. **Infra freeze is a hard precondition,** and design for one lost arm anyway: at the observed one-incident-per-run rate, never run an experiment whose conclusion dies if a single arm is contaminated.
3. **Never judge a quality-pressure or QA factor on value/repair alone** — pair it with assets-on-disk, bot progress-depth, and a human playtest. The series has three documented cases of the graph metric pointing away from the playable game.
4. **One decision axis per run** (run 12 proposal's rule: one arm per decision you'd actually change, plus control). The exploratory 5–7 arm era (runs 4–7) was appropriate then; it is over.
