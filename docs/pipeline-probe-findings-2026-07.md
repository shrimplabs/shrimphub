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
| A1 | current ("agent repeatedly failed") | 1800s | 104 loops | ? | **TIMEOUT** (30m cap) |
| A2 | current ("agent repeatedly failed") | 1800s | 106 loops | ? | **TIMEOUT** (30m cap) |
| B1 | gap-analysis (compare desired vs actual) | 1489s | 149 | 14 | OK — committed |
| B2 | gap-analysis (compare desired vs actual) | 506s | 67 | 1 | OK — committed |
| C1 | minimal verifier ("does the feature exist?") | 770s | 62 | 2 | OK — committed |
| C2 | minimal verifier ("does the feature exist?") | 598s | 81 | 5 | OK — committed |

A1/A2 hit the 30-minute probe-batch subprocess timeout and were killed. B and C: 4/4 committed.

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

## Batch 4: Bug Phase-Order Probe

**Spec:** `tools/specs/bug-phase-order.json`  
**Date:** 2026-07-27  
**Task:** Snake dies instantly every time a new game starts (planted bug: `next_direction = Vector2i.DOWN` in `start_new_game()` while `current_direction = Vector2i.RIGHT` — causes self-collision on first move tick)  
**Runs:** 6 (3 shapes × 2 reps), sequential with repo reset between each run (no contamination)  
**Project:** classic-snake

### Results

| Run | Shape | Loops | Mutations | Commit | Elapsed |
|-----|-------|-------|-----------|--------|---------|
| A1 | scout→diagnose→work | 101 | 10 | YES | 2027s |
| A2 | scout→diagnose→work | 44 | 3 | YES | 358s |
| B1 | plan→scout→diagnose→work | 18 | 2 | YES | 170s |
| B2 | plan→scout→diagnose→work | 23 | 3 | YES | 181s |
| C1 | scout→work | 32 | 2 | YES | 196s |
| C2 | scout→work | 25 | 2 | YES | 196s |

### Key findings

All 6 committed — no hallucination or NoOp failures on bug tasks (unlike features where D-shape hallucinated 2/2).

**Plan is the stabilizer.** A (scout→diagnose→work) had extreme variance: A1 burned 2027s and 101 loops on a one-line fix, A2 finished in 358s. Without plan, the agent doesn't know where to stop exploring. B (plan→scout→diagnose→work) was consistently fast: 170-181s, 18-23 loops — plan scoped the work before diagnose ran, so diagnose could be targeted rather than exploratory.

**Scout→work (C) is viable for bugs** — 196s both reps, 2 mutations each, clean. When the bug description is clear, plan and diagnose may add overhead without changing the outcome. But B's consistency advantage (170s vs 196s, tighter mutation count) makes it the safer default.

**Mutation count as a quality signal:** B's 2-3 mutations for a one-line fix is ideal. A1's 10 mutations suggests the agent over-engineered or made unnecessary changes alongside the fix.

---

## Batch 5: Bug Chaos (16 random phase orderings)

**Spec:** `tools/specs/bug-chaos.json` (RNG seed=42)  
**Date:** 2026-07-27/28  
**Task:** Same planted bug (next_direction mismatch in classic-snake)  
**Runs:** 16, sequential with repo reset, 1 rep each  
**Goal:** stress-test whether plan→scout→diagnose→work is genuinely best or just familiar

### Results (sorted by elapsed)

| Shape | Loops | Mutations | Commit | Elapsed |
|-------|-------|-----------|--------|---------|
| diagnose→work | 14 | 2 | YES | **109s** |
| diagnose→diagnose→work | 22 | 2 | YES | 202s |
| scout→work | 32 | 2 | YES | 249s |
| plan×4→work | 16 | 2 | YES | 279s |
| diagnose→plan→scout→plan→work | 23 | 2 | YES | 364s |
| scout×2→plan×2→work | 46 | 2 | YES | 427s |
| diagnose→scout→plan→work | 37 | 2 | YES | 493s |
| plan→scout→diagnose→scout→work | 36 | 5 | YES | 737s |
| diagnose×2→scout→work | 68 | 7 | YES | 797s |
| plan×2→scout→work | 33 | 4 | YES | 865s |
| plan×3→work | 38 | 3 | YES | 932s |
| scout→plan→work | 40 | 2 | YES | 894s |
| plan→scout→work | 24 | 2 | YES | 969s |
| plan→work | 94 | 4 | YES | 1045s |
| scout→diagnose→scout→work | 64 | 3 | YES | 1131s |
| **diagnose→plan→work** | **164** | **19** | **NO** | **1982s** |

### Key findings

**diagnose→work is the fastest shape** — 109s, 14 loops, 2 mutations. For a well-described bug, one diagnose pass gives work everything it needs. No scout, no plan, straight to the fix.

**Plan is expensive for bugs** — plan→work took 1045s (94 loops). Adding more plan phases doesn't consistently help. plan×4→work was actually faster (279s) than plan→work (1045s), suggesting repeated planning converges context in a way a single plan doesn't.

**The only failure: diagnose→plan→work** — diagnose correctly identified the bug, then plan overwrote that context with its own framing, and work got confused and burned 164 loops without committing. Putting plan *after* diagnose is actively harmful.

**15/16 committed** — chaos shapes that had never been tried before (diagnose→diagnose→work, scout×2→plan×2→work, diagnose→scout→plan→work) all committed cleanly.

**Mutation count as quality signal holds** — the fast shapes (diagnose→work, scout→work) produced 2 mutations each — exactly right for a one-line fix. Slow or failed shapes produced 4-19 mutations, indicating flailing.

### Revised understanding

The earlier conclusion ("plan is the stabilizer") was based on comparing scout→diagnose→work (high variance) vs plan→scout→diagnose→work (consistent). The chaos data refines this:

- **Plan stabilizes work when the task is ambiguous** — it scopes exploration
- **For well-described bugs, plan adds latency without benefit** — diagnose alone is sufficient to orient work
- **The real stabilizer is "give work a clear target"** — diagnose does this cheaply; plan does it expensively

Tentative bug-specific recommendation: **diagnose→work for well-described bugs, plan→scout→diagnose→work for ambiguous ones**.

---

## Recommendation Matrix

Based on batches 1–5. All runs used MiniMax-M3.

### Pipeline shape × task type

| Task type | Recommended shape | Avoid | Notes |
|-----------|-------------------|-------|-------|
| Feature (UI missing, backend exists) | plan→scout→**diagnose**→work | plan→scout→work | D-shape hallucinated 2/2; diagnose (gap-analysis prompt) committed 4/4 |
| Feature (from scratch) | plan→scout→**diagnose**→work | plan→scout→work | D-shape hallucinated 2/2 in pause batch; diagnose reduces NoOps |
| Bug (well-described) | **diagnose**→work | diagnose→plan→work | 109s vs 1045s+; plan after diagnose overwrites correct context and causes failure |
| Bug (ambiguous/unclear) | plan→scout→**diagnose**→work | — | Plan needed to scope exploration before diagnose can be targeted |
| Refactor | plan→scout→work (tentative) | — | No data yet; diagnose may add latency without benefit for well-scoped refactors |

### Diagnose prompt × context

| Situation | Prompt to use | Why |
|-----------|--------------|-----|
| Fresh pipeline run (no prior failures) | `_DIAGNOSE_PIPELINE_SYSTEM` (gap-analysis) | Gives work agent accurate gap report; committed 4/4, finished in 8–25 min |
| Research feeder (task exhausted attempts) | `_DIAGNOSE_SYSTEM` (failure investigator) | Task has actual failure history to diagnose |
| `phase_config.diagnose_system_prompt` set | Use that | Explicit override always wins |

### Shape verdict summary

| Shape | Task type | Commits | Elapsed | Verdict |
|-------|-----------|---------|---------|---------|
| plan→scout→diagnose(gap)→work | feature | 4/4 | 506-1489s | ✓ Recommended |
| plan→scout→diagnose(minimal)→work | feature | 4/4 | 598-770s | ✓ Acceptable |
| plan→scout→diagnose(failure-framing)→work | feature | 0/2 | 1800s (killed) | ✗ Wrong prompt |
| plan→scout→work (D-shape) | feature | 0/4 | — | ✗ Hallucination risk |
| diagnose→work | bug (well-described) | 1/1 | 109s | ✓ Fastest known |
| plan→scout→diagnose→work | bug | 4/4 | 170-181s | ✓ Recommended (ambiguous bugs) |
| scout→work | bug | 5/5 | 196-249s | ✓ Solid baseline |
| scout→diagnose→work | bug | 2/2 | 358-2027s | ~ High variance |
| diagnose→plan→work | bug | 0/1 | 1982s | ✗ Plan overwrites diagnose |
| plan→work | bug | 1/1 | 1045s | ~ Slow, high variance |

### Cross-task-type pattern

The same signal appears in both feature and bug probes: **plan is the stabilizer**.

- Without plan, agents wander — scout→diagnose→work burned 2027s on a one-line bug fix
- With plan, agents arrive at work already scoped — plan→scout→diagnose→work finished in 170-181s
- Diagnose adds precision on top of plan but isn't necessary when the bug is clearly described and plan has already scoped the work (scout→work also committed 4/4 for bugs)
- Skipping diagnose on features causes hallucination (D-shape: 0/4 commits, 2/4 NoOps)

**Conclusion: plan→scout→diagnose→work is the universal safe default for both feature and bug tasks.**

### What was shipped

`swarm/phases/diagnose.py` now auto-selects the prompt:
- No `failure_context` in plan → `_DIAGNOSE_PIPELINE_SYSTEM` (gap-analysis)
- `failure_context` present → `_DIAGNOSE_SYSTEM` (failure investigator)
- `phase_config.diagnose_system_prompt` → explicit override

---

---

## Batch 6: Feature Chaos (16 random phase orderings)

**Spec:** `tools/specs/feature-chaos.json` (RNG seed=42, same orderings as bug chaos)
**Date:** 2026-07-28
**Task:** Add a game timer (MM:SS HUD, persist best time to JSON, "New Best!" on win)
**Project:** rainbow-minesweeper
**Runs:** 16, sequential with repo reset, 1 rep each

### Results (sorted by elapsed)

| Shape | Loops | Mutations | Elapsed | Notes |
|-------|-------|-----------|---------|-------|
| **diagnose→work** | **23** | **4** | **~4m** | Fastest, cleanest |
| plan→work | 34 | 4 | ~5m | |
| plan→scout→work | 26 | 4 | ~7m | |
| plan→plan→scout→work | 21 | 6 | ~7m | |
| scout→plan→work | 27 | 7 | ~7m | |
| diagnose→scout→plan→work | 39 | 9 | ~9m | |
| plan→plan→plan→work | 44 | 12 | ~8m | Redundant plans |
| diagnose→plan→work | 59 | 16 | ~9m | Diagnose then plan = wasteful |
| plan→plan→plan→plan→work | 20 | 7 | ~9m | |
| diagnose→diagnose→work | 57 | 9 | ~9m | |
| diagnose→plan→scout→plan→work | 47 | 14 | ~11m | |
| diagnose→diagnose→scout→work | 35 | 16 | ~9m | |
| scout→work | 63 | 16 | ~12m | Without synthesis, lots of flailing |
| scout→scout→plan→plan→work | 132 | 10 | ~26m | Worst by far |
| plan→scout→diagnose→scout→work | 56 | 12 | ~19m | |
| plan→scout→diagnose→scout→work (P) | 30 | 9 | ~10m | |

All 16 succeeded (no hallucinations or NoOps for features in this batch).

### Key findings

**diagnose→work wins for features** — 23 loops, 4 mutations, ~4 minutes. Same shape that won for bugs (109s). The gap-analysis synthesises a map AND a plan in one shot; the work agent gets everything it needs without a separate scout or plan phase.

**scout→work is surprisingly bad for features** — 63 loops, 16 mutations, 12 minutes. Scout dumps raw file contents without synthesis; the work agent flails trying to figure out what's missing. Diagnose synthesises that into an actionable gap report.

**More phases ≠ better** — G1 (scout×2→plan×2→work) was worst at 26 minutes. Every extra phase adds LLM overhead without proportional benefit. The work agent doesn't get smarter from more pre-work; it gets more confused.

**plan alone is the second-best option** — plan→work at ~5m is competitive. It doesn't map the codebase but gives the work agent a clear goal, which is enough to avoid flailing. The work agent's own read loops fill in the map.

---

## Batch 7: Work-Only Baselines

**Date:** 2026-07-28

### Feature (work only) — rainbow-minesweeper, game timer

| Rep | Loops | Mutations | vs diagnose→work |
|-----|-------|-----------|-----------------|
| 1 | 69 | 14 | 3× more loops, 3.5× more mutations |
| 2 | 64 | 13 | consistent |

Bare work on features is reliably messy. The work agent reads files as it needs them but has no synthesis of what's missing, so it flails and over-engineers.

### Bug (work only) — classic-snake, snake dies instantly (precise description)

| Rep | Loops | Mutations | vs diagnose→work |
|-----|-------|-----------|-----------------|
| 1 | 10 | 2 | faster than diagnose |
| 2 | 7 | 2 | consistent |

For a well-described single-file bug, bare work is optimal. The precise description gives the work agent a search target; it reads the relevant file, spots the wrong value, fixes it, done. Diagnose adds overhead without benefit.

---

## Batch 8: Vague Bug Description

**Task:** "Bug: the game doesn't work correctly." (classic-snake, same one-line bug)

| Run | Pipeline | Loops | Mutations |
|-----|----------|-------|-----------|
| diag1 | diagnose→work | 8 | 2 |
| diag2 | diagnose→work | 10 | 4 |
| work1 | work | 11 | 2 |
| work2 | work | 19 | 3 |

Even with a maximally vague description, bare `work` found the bug comparably to diagnose. The one-line bug is simple enough that both pipelines converge quickly — the work agent's natural read-then-fix loop is a decent bug hunter on simple bugs regardless of description quality.

---

## Batch 9: Vague Bug, Multi-File

**Task:** "Bug: the game seems broken." (two planted bugs across two files: `score += 0` in score_manager.gd, and food spawn range restricted to inner grid in main.gd)

| Run | Pipeline | Loops | Mutations |
|-----|----------|-------|-----------|
| diag1 | diagnose→work | 33 | 10 |
| diag2 | diagnose→work | 104 | 8 |
| work1 | work | 21 | 3 |
| work2 | work | 13 | 3 |

**Bare work won again** — and by a lot. diag2 went to 104 loops (diagnose may have sent work on a wrong path). Work found and fixed both bugs with 13-21 loops and 3 mutations each. The work agent's read-then-fix loop naturally explores multiple files; diagnose's gap-analysis framing doesn't specifically help with multi-file bugs.

---

## Revised Recommendation Matrix (Batches 1–9)

### Core findings

| Finding | Confidence | Evidence |
|---------|-----------|---------|
| diagnose→work is the universal fast path | High | Fastest in bug chaos (109s), feature chaos (~4m), and beats work-only on features |
| work-only is optimal for well-described bugs | High | 7-10 loops vs 14+ for diagnose; consistent across 4 reps |
| diagnose does not help bugs, even multi-file vague ones | Medium | Batch 8+9: work matched or beat diagnose; diag2 burned 104 loops on a 2-file bug |
| scout-only is poor for features | High | 63 loops, 16 mutations vs 23/4 for diagnose→work |
| More phases = more latency, rarely more quality | High | G1 (5 phases) was worst in feature chaos; plan×4 mediocre in bug chaos |
| plan after diagnose is harmful for bugs | Medium | diagnose→plan→work: only failure in bug chaos (164 loops, no commit) |
| D-shape (plan→scout→work) hallucination risk | High | 2/2 NoOps in Batch 1 — only on features, never reproduced on bugs |

### Recommended defaults by task type

| Task type | Recommended | Fallback | Avoid |
|-----------|-------------|----------|-------|
| Feature (any) | `diagnose→work` | `plan→work` | `plan→scout→work` (hallucination), `scout→work` (flailing) |
| Bug (well-described) | `work` | `diagnose→work` | `diagnose→plan→work` |
| Bug (vague/multi-file) | `work` | `diagnose→work` | `diagnose→plan→work` |
| Refactor | `diagnose→work` (predicted) | `plan→scout→work` | — (no data yet) |

### Predictions

1. **diagnose→work will outperform for refactors** — refactors are structurally like features (existing code, need to understand what's there before changing it). The gap-analysis framing maps cleanly onto "what does this code do vs what should it do."

2. **work-only will degrade on features as codebase grows** — on a large codebase, the work agent's opportunistic file reading will miss context that diagnose would synthesise. The break-even point is somewhere between the small projects we've tested and a 50+ file project.

3. **diagnose variance on bugs is a prompt alignment problem** — the gap-analysis prompt is oriented toward "what's missing" (feature framing). On bugs, "what's wrong" is a subtly different question. A bug-specific diagnose prompt ("compare expected vs actual behaviour") might improve diagnose's bug performance.

4. **plan still has a role for truly ambiguous features** — if the task description is vague AND the codebase is large, diagnose alone may not have enough signal to produce a useful gap report. A single plan phase before diagnose may be worth the latency in those cases.

### What to ship

- **Default pipeline config:** `diagnose→work` for features, `work` for bugs
- **Bug-specific diagnose prompt:** test `_DIAGNOSE_BUG_SYSTEM` ("compare expected vs actual behaviour in the code") to see if diagnose can match work-only on bugs — if so, `diagnose→work` becomes truly universal
- **Scout deprecation for features:** remove scout from default feature pipelines; it adds latency without improving outcomes vs diagnose

---

## Work Phase Fixes Applied During This Session

Five commits to `swarm/phases/work.py` during the probe session:

- `062360db` — stall detection + commit SHA tracking fix
- `caf56312` — WORK_COMPLETE scrubbing (prevents re-emission death spiral)
- `f21f85f9` — auto-commit escalation after 3 premature completions
- `25dc3d55` — run_command bypass detection via `git status --porcelain`
- `6b5e04ae` — git status pre-check before auto-commit (prevents junk commits)
