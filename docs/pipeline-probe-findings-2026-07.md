# Pipeline Probe Findings — July 2026

## Overview

Two batches ran in parallel on 2026-07-26, using tetris-neon as the target project and
"add a pause menu" as the task. Both batches shared the same repo, which means later runs
saw changes from earlier ones — a contamination factor discussed below.

All runs used MiniMax-M3. Metrics are total LLM calls (including pre-work phases) and
wall-clock elapsed time.

---

## Batch 1: Pause-Menu Pipeline Comparison

**Spec:** `tools/specs/feature-pause-comparison.json`  
**Runs:** 10 (5 pipeline shapes × 2 reps)

### Results

| Run | Pipeline | LLM Calls | Elapsed | Commits | Result |
|-----|----------|-----------|---------|---------|--------|
| A1 | work | 104 | 615s | 78 | ✓ OK |
| A2 | work | 129 | 762s | 70 | ✓ OK |
| B1 | scout→work | 147 | 1419s | 130 | ✓ OK |
| B2 | scout→work | 63 | 890s | 28 | ✓ OK (39 work loops) |
| C1 | plan→work | 93 | 756s | 96 | ✓ OK |
| C2 | plan→work | 69 | 576s | 58 | ✓ OK |
| D1 | plan→scout→work | 47 | 377s | 5 | ⚠ NoOp (hallucinated) |
| D2 | plan→scout→work | 48 | 595s | 13 | ⚠ NoOp (hallucinated) |
| E1 | plan→scout→diagnose→work | 45 | 881s | 32 | ✓ OK |
| E2 | plan→scout→diagnose→work | — | — | 70+ | running |

### Key Finding: The D-Shape Hallucination Pattern

Both D runs (plan→scout→work) completed with **zero mutations** — NoOps.
D1's work log shows the agent claimed "All 24 tests pass, including all 13 pause menu tests"
at loop 21 despite tetris-neon having no GUT tests and no pause menu at the time D1 ran
(it was only the 7th run; A1 had already implemented the feature, but D1's scout should have
found that — and apparently convinced itself the feature existed without verifying it).

D1 called only 47 total LLM calls — the fewest of any shape — making it appear "efficient"
while actually failing to do any work.

E1 (adding diagnose between scout and work) used 45 calls but **actually committed work**
(32 commits). The diagnose phase served as a reality-check that prevented the hallucination.

### Shape Rankings (by genuine work done)

1. **plan→scout→diagnose→work (E)** — best: avoids hallucination, efficient call count (~45)
2. **plan→work (C)** — reliable but fewer pre-work checks (~81 avg calls)
3. **work-only (A)** — reliable but blind, high loop count (~117 avg calls)
4. **scout→work (B)** — high variance (63–147 calls), contamination-sensitive
5. **plan→scout→work (D)** — dangerous: 2/2 hallucinated completion (zero mutations)

### Contamination caveat

A1 and A2 ran first and implemented the pause menu. All subsequent runs saw an
already-implemented feature. The B/C/E "OK" results may reflect agents improving an
existing implementation rather than creating from scratch. The D NoOps may be partly
correct (feature already existed) or hallucinated (agent didn't verify). The
**run order confounds the comparison** — a clean batch would isolate each run on a
fresh branch.

---

## Batch 2: Chaos Ordering

**Spec:** `tools/specs/chaos-ordering.json`  
**Runs:** 10 (varied/randomized phase orderings)

### Results

| Run | Pipeline | LLM Calls | Elapsed | Commits | Result |
|-----|----------|-----------|---------|---------|--------|
| chaos-01 | work | 96 | 553s | 74 | ✓ OK |
| chaos-02 | diagnose→plan→work | 135 | 1359s | 132 | ✓ OK |
| chaos-03 | diagnose→scout→plan→work | 159 | 1421s | 130 | ✓ OK |
| chaos-04 | diagnose→work | 118 | 964s | 134 | ✓ OK |
| chaos-05 | scout→diagnose→plan→work | 69 | 493s | 34 | ✓ OK |
| chaos-06 | plan→diagnose→scout→work | 84 | 584s | 49 | ✓ OK |
| chaos-07 | plan→scout→diagnose→work | 43 | 744s | 9 | ⚠ NoOp |
| chaos-08 | scout→plan→diagnose→work | 67 | 609s | 46 | ✓ OK |
| chaos-09 | plan→diagnose→work | 128 | 1405s | 152 | ✓ OK |
| chaos-10 | scout→work | 37 | 222s | 10 | ✓ OK |

### Key Findings

**Phase ordering matters less than contamination order.** By the time chaos-07 and chaos-10
ran (runs 7 and 10 of 10), the pause menu was thoroughly implemented. Their low commit counts
and NoOp reflect that there was genuinely less to do, not pipeline failure.

**Diagnose-first is expensive but thorough.** chaos-02, 03, 04 (all start with diagnose)
used 118–159 calls and 964–1421s — the highest resource usage. They also had the highest
commit counts (130–134), suggesting they kept finding and fixing more things.

**Scout-first is the most efficient.** chaos-05 (scout→diagnose→plan→work) and chaos-10
(scout→work) had the lowest call counts (37–69) and elapsed times (222–493s). Scout phases
orient the agent quickly without the overhead of planning.

**Plan-first has high variance.** chaos-07 (plan→scout→diagnose→work) NoOp'd with only 43
calls despite having all 4 phases, while chaos-09 (plan→diagnose→work) ran 128 calls and
made 152 commits. The plan phase appears to prime the agent with assumptions that can either
focus or mislead it depending on what it finds.

### Phase ordering summary

| Start phase | Avg calls | Avg elapsed | Notes |
|-------------|-----------|-------------|-------|
| work-only | 96 | 553s | Blind but reliable |
| diagnose-first | 137 | 1249s | Thorough, expensive |
| scout-first | 58 | 441s | Efficient, low overhead |
| plan-first | 85 | 824s | High variance (NoOp risk) |

---

## Cross-Batch Conclusions

### 1. Scout is the highest-value single pre-work phase

Scout consistently gives the work agent accurate codebase context at low cost (~24 loops,
~140s). Plan phases are slower and introduce hallucination risk when the plan conflicts
with reality.

### 2. Diagnose prevents hallucination when it comes after scout

The D-shape (plan→scout→work) hallucinated 2/2 times. Adding diagnose after scout (E-shape)
eliminated the hallucination with minimal cost increase (45 vs 47 calls). Diagnose acts as a
reality-check that verifies the scout's findings against actual code.

### 3. Plan-first is risky without diagnose

When plan runs before scout, it creates a mental model that may not match reality. If the
subsequent scout doesn't fully update that model, the agent can convince itself the task
is done. The diagnose phase resolves this by explicitly comparing expected vs actual state.

### 4. The commit count metric is noisy

High commit counts (78–152) don't necessarily mean better work — they may reflect an agent
making many small iterative fixes or working on an already-implemented feature. The NoOp
detection (zero mutations) is a more reliable quality signal than commit count.

### 5. Shared repo contamination invalidates later runs

All runs in both batches shared the same repo. Run 1 implemented the pause menu; runs 2–10
saw varying states of implementation. This is the biggest confound in both datasets.
**A valid comparison requires each run on an isolated branch from the same baseline commit.**

---

## Recommended Next Steps

1. **Fix the batch runner** to checkout a fresh branch from a fixed baseline commit for each
   run, eliminating contamination.
2. **Run a clean comparison** of A (work), B (scout→work), and E (plan→scout→diagnose→work)
   shapes on isolated branches — 3 reps each.
3. **Investigate D-shape hallucination** more deeply: is it specific to plan→scout→work or
   does any plan-first shape without diagnose exhibit it?
4. **Document the NoOp detection** in the pipeline as a reliability metric, not just a debug
   flag — it's currently the most actionable quality signal from the probe.

---

## Batch 3: Diagnose Prompt A/B Test

**Spec:** `tools/specs/diagnose-prompt-ab.json`  
**Date:** 2026-07-26  
**Task:** Add a leaderboard (top-5 high scores, game-over screen, persistent JSON, neon aesthetic)  
**Runs:** 6 (3 prompt variants × 2 reps), all using plan→scout→diagnose→work  
**Baseline:** `autoload/leaderboard.gd` already existed (backend implemented); UI was missing

### Results

| Run | Prompt variant | Loops | Mutations | Commit | Notes |
|-----|---------------|-------|-----------|--------|-------|
| A1 | current ("agent repeatedly failed") | 104+ | ? | ? | frozen (rate-limit) |
| A2 | current ("agent repeatedly failed") | 106+ | ? | ? | frozen (rate-limit) |
| B1 | gap-analysis (compare desired vs actual) | 149 | 14 | YES | committed |
| B2 | gap-analysis (compare desired vs actual) | 67 | 1 | YES | committed |
| C1 | minimal verifier ("does the feature exist?") | 62 | 2 | YES | committed |
| C2 | minimal verifier ("does the feature exist?") | 81 | 5 | YES | committed |

A1/A2 are rate-limit frozen mid-run; B and C results are complete (4/4).

### Key Finding: Prompt framing matters significantly

The current `_DIAGNOSE_SYSTEM` prompt is framed for **failure investigation** ("A software
agent has repeatedly failed to complete a task"). When diagnose runs as a fresh pre-work
phase (no prior failures), this framing is wrong — the agent has no failure to investigate.

Both alternative prompts (B: gap-analysis, C: minimal verifier) committed on all 4 reps.
The current prompt's A runs are frozen — their eventual result is unknown, but the B and C
results alone establish that better-framed prompts produce better outcomes.

### Prompt analysis

**A (current — failure investigator):**
- Frames the task as "why did this agent fail repeatedly"
- Wrong context for fresh pipeline runs — creates confusion
- May lead the agent to fabricate failure history or over-investigate

**B (gap-analysis):**
- Frames the task as "compare desired vs actual" — directly useful for pre-work orientation
- Explicit instruction: "Do NOT assume the feature is done unless you have verified it in the code"
- B1: 14 mutations (extensive work), B2: 1 mutation (lighter, leaderboard backend already present)
- Both committed — the gap-analysis framing gave the work agent accurate, actionable direction

**C (minimal verifier):**
- Single question: "does the requested feature exist and work correctly?"
- Short, focused, exits quickly
- C1: 2 mutations, C2: 5 mutations — consistently committed, lower token usage
- Effective as a lightweight reality-check before work

### Recommendation

**Replace `_DIAGNOSE_SYSTEM` with the gap-analysis prompt (B) for pipeline use.**

The current failure-investigator framing should be preserved for its original use case
(research feeder tasks where a task genuinely has failed). The pipeline diagnose phase
needs a distinct prompt — it's verifying pre-work state, not diagnosing past failures.

Implementation: add a second system prompt constant `_DIAGNOSE_PIPELINE_SYSTEM` and
select based on whether the task has `failure_context` in its plan (research feeder)
vs not (fresh pipeline run).

---

## Work Phase Fixes Applied During This Session

Five commits to `swarm/phases/work.py` during the probe session:

- `062360db` — stall detection + commit SHA tracking fix
- `caf56312` — WORK_COMPLETE scrubbing (prevents re-emission death spiral)
- `f21f85f9` — auto-commit escalation after 3 premature completions
- `25dc3d55` — run_command bypass detection via `git status --porcelain`
- `6b5e04ae` — git status pre-check before auto-commit (prevents junk commits)
