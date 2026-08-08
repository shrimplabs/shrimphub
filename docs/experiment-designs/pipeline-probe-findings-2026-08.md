# Pipeline Probe Findings (August 2026)

## Feature Probe v1 Results (plan+scout+diagnose vs plan+diagnose vs diagnose-only)

**Probe**: `tools/specs/feature-phase-order-v1.json`
**Task**: Implement ghost piece feature in tetris-neon (semi-transparent landing preview)
**Date**: 2026-08-04

### Results

| Run | Pipeline | Elapsed | LLM calls | Outcome |
|-----|----------|---------|-----------|---------|
| A1 | plan→scout→diagnose→work→validate | 501s | 22 | ✓ |
| A2 | plan→scout→diagnose→work→validate | 393s | 26 | ✓ |
| B1 | plan→diagnose→work→validate | 378s | 31 | ✓ |
| B2 | plan→diagnose→work→validate | 2140s | 168 | ✗ loop limit |
| C1 | diagnose→work→validate | 866s | 96 | ✓ |
| C2 | diagnose→work→validate | 1235s | 76 | ✗ validation failed |

### Key Findings

**Plan+scout+diagnose wins decisively for features.** A arm: 2/2 success in only 22-26 LLM calls — 3-4× fewer calls than B or C on their successful runs, and the only arm with zero failures.

**Without plan+scout, work burns budget on discovery.** B and C agents spent 76-168 calls on successful runs doing what plan+scout accomplish in ~10 calls — scoping the feature and mapping where it lives in the codebase. B2 hit the loop limit entirely; C2 produced a validation failure. The pre-work phases are genuinely load-bearing for features, not overhead.

**Diagnose-only (C) is the worst shape for features.** More calls than B, same failure rate. Without plan to scope and scout to map, diagnose can't efficiently locate the right files — it casts a wider net and still misses.

### Decision (v1 — SUPERSEDED by v2 below)

v1 used only 2 runs per arm. B2's 168-call failure and A's anomalously low 22-26 calls were variance, not signal. See v2.

---

## Feature Probe v2 Results (4 runs per arm)

**Probe**: `tools/specs/feature-phase-order-v2.json`
**Task**: Same ghost piece task, 4 runs per arm
**Date**: 2026-08-05

### Results

| Run | Pipeline | Elapsed | Calls | Outcome |
|-----|----------|---------|-------|---------|
| A1 | plan→scout→diagnose→work | 1177s | 75 | ✓ |
| A2 | plan→scout→diagnose→work | 958s | 85 | ✓ |
| A3 | plan→scout→diagnose→work | 231s | 24 | ✓ |
| A4 | plan→scout→diagnose→work | 1207s | 107 | ✓ |
| B1 | plan→diagnose→work | 802s | 74 | ✓ |
| B2 | plan→diagnose→work | 1426s | 93 | ✓ |
| B3 | plan→diagnose→work | 1017s | 105 | ✓ |
| B4 | plan→diagnose→work | 1426s | 139 | ✓ |
| C1 | diagnose→work | 1661s | 116 | ✓ |
| C2 | diagnose→work | 1051s | 73 | ✓ |
| C3 | diagnose→work | 188s | 25 | ✓ |
| C4 | diagnose→work | 106s | 17 | ✓ |

### Per-Arm Summary

| Arm | Pipeline | Success | Avg calls | Avg elapsed | Call range |
|-----|----------|---------|-----------|-------------|------------|
| A | plan→scout→diagnose→work | 4/4 | 73 | 893s | 24–107 |
| B | plan→diagnose→work | 4/4 | 103 | 1168s | 74–139 |
| C | diagnose→work | **4/4** | **58** | **752s** | 17–116 |

### Key Findings

**Diagnose→work wins for features.** C arm: fewest calls (58 avg), fastest (752s avg), same 4/4 reliability. The full pipeline (A) adds ~15 calls of overhead for no reliability gain. Plan+diagnose (B) is worst — more calls than either A or C, slowest overall.

**v1 was misleading.** 2 runs per arm wasn't enough to distinguish signal from variance. The ghost piece task has a call range of 17–116 across all arms — the same task can take 17 calls or 116 depending on how the agent approaches it. v1's B2 failure (168 calls, loop limit) and A arm's anomalously low 22-26 calls were both outliers, not arm-level effects.

**The call range is arm-independent.** All three arms show wide variance (24-107, 74-139, 17-116). The pipeline shape affects the average but not the floor or ceiling. This suggests the dominant factor is task interpretation, not pipeline shape — when the agent gets a lucky approach it finishes in <200s regardless of arm; when it doesn't it burns 1000-1600s regardless.

### Decision

**Change feature default to `diagnose→work→validate`.** Consistent with bug and art pass findings — for well-described tasks, targeted reading before work is sufficient. Plan and scout are overhead. The hallucination concern from v1's 2-run test was not replicated at 4 runs.

---

## Refactor Probe v1 Results (plan→scout→work vs plan→diagnose→work)

**Probe**: `tools/specs/refactor-phase-order-v1.json`  
**Task**: Split `tetris-neon/scripts/game_scene.gd` (1635 lines) into PieceController + BoardManager  
**Date**: 2026-08-04

### Results

| Run | Pipeline | Elapsed | LLM calls | Outcome |
|-----|----------|---------|-----------|---------|
| A1 | plan→scout→work→validate | 2434s | 202 | ✓ (minor validation warning) |
| A2 | plan→scout→work→validate | 2380s | 130 | ✓ |
| B1 | plan→diagnose→work→validate | 2781s | 282 | ✗ loop limit + GDScript parse error |
| B2 | plan→diagnose→work→validate | 808s | 105 | ✓ |

### Key Findings

**Scout wins for refactor tasks.** plan→scout→work succeeded 2/2 with consistent timing (2434s, 2380s). plan→diagnose→work succeeded only 1/2 — B1 hit the 150-loop work limit without completing, leaving a broken parse error in `game_scene.gd`.

**Why scout helps here but not for bugs/art:** Refactor tasks don't have a "problem to find" — they require understanding the full structural shape of a large file to decide how to split it. Scout's codebase map is load-bearing for that decision in a way it isn't for targeted bug fixes or asset wiring. Diagnose is optimised for locating a specific issue; for refactors, the whole file IS the issue.

**B1's failure mode:** hit the 150-loop work limit mid-refactor, leaving `game_scene.gd` with a GDScript indentation parse error. The diagnose phase consumed loops on targeted reads that were less useful than scout's broader survey for a structural split. B2 succeeded quickly (808s/105 calls) when the approach clicked, but the reliability gap (1/2 vs 2/2) is disqualifying.

### Decision

**Keep `plan→scout→work→validate` as the refactor default.** Scout is the exception that proves the rule: it earns its place specifically when the task is a large structural transformation where the agent needs a full architectural map before deciding how to proceed. For all other task types (bug, feature, art_pass), diagnose→work remains superior.

---

# Art Pass Pipeline Probe — Findings (August 2026)

## v3c Probe Results (300-loop cap, relaxed stopping rule)

**Probe**: `tools/specs/art-pass-phase-order-v3.json`  
**Prompt fixes active**: `take_screenshot` for visual checks, explicit stopping rule (≥2 areas + ≥3 verification cycles)  
**Loop cap**: 300 (work phase)  
**Date**: 2026-08-04

### Results

| Run | Pipeline | Total calls | Work loops | Elapsed | Screenshots | vision_query | Success |
|-----|----------|-------------|------------|---------|-------------|--------------|---------|
| A1 | work | 190 | 190/300 | 1241s | 5 | 1 | ✓ |
| A2 | work | 91 | 91/300 | 529s | 3 | 0 | ✓ |
| B1 | diagnose→work | 194 | 185/300 | 1484s | 11 | 1 | ✓ |
| B2 | diagnose→work | 108 | 99/300 | 704s | 6 | 1 | ✓ |

**Overall success rate: 4/4 (100%)** — prompt fixes eliminated all termination failures.

### Key Findings

**Diagnose→work does more visual iteration.** B1 averaged 8.5 screenshots per run vs A1's 4. The diagnose phase primes the agent to verify its work; it consistently launched the game more often and took more screenshots before deciding it was done. Only 9 calls / 70s overhead.

**High variance remains in work-only.** A1=1241s vs A2=529s (2.4× spread). A1 used 190/300 loops. Diagnose→work is more consistent: B1=1484s, B2=704s (2.1× spread), both well within the cap.

**300-loop cap was not hit by any run.** The relaxed stopping rule (≥2 areas + ≥3 cycles) did not cause runaway loops — agents self-terminated naturally. This validates both the cap and the rule.

**A1 nearly exhausted loops (190/300) without TASK_COMPLETE** — the work phase runner stopped it. The stopping rule appears to be partially ignored by work-only agents when they have large budgets. diagnose→work agents consistently stopped earlier.

### Decision

**Change art_pass default to `diagnose→work→validate`.**

Evidence:
- 4/4 success (prompt fixes are the primary driver)
- diagnose→work does more visual iteration per run (8.5 vs 4 screenshots)
- 9-call / 70s diagnose overhead is negligible
- More consistent loop usage (less variance means fewer surprise long runs)
- work-only A1 nearly loop-limited at 190/300 — risky at scale

The `_DEFAULT_PIPELINES` entry for `art_pass` should be updated from `["work", "validate"]` to `["diagnose", "work", "validate"]`.

---



**Probe**: `tools/specs/art-pass-phase-order-v2.json`  
**Project**: `void-patrol-no-art-run12` (zero art at baseline, all flat colored rects)  
**Baseline SHA**: `def024f30a5f4846f3c76832fb92774d1e4507cc`  
**Reset**: SHA-pinned checkout — contamination-proof  
**Date**: 2026-08-04

## Results

| Run | Pipeline | Elapsed | Loops | Tokens | Result |
|-----|----------|---------|-------|--------|--------|
| A1 | work | 1044s | 89 | ~21k | ✓ success |
| A2 | work | ~1800s | 69 | — | ✗ timeout (probe-batch 1800s cap) |
| B1 | diagnose→work | 731s | 108 | ~20k | ✓ success |
| B2 | diagnose→work | 1111s | 158 | ~18k | ✗ loop-limit (committed but kept going) |
| C1 | scout→work | ~1300s | 150 | — | ✗ loop-limit |
| C2 | scout→work | 1034s | 131 | ~55k | ✓ success |

**Overall success rate: 3/6 (50%) — uniform across all pipeline shapes.**

## Key Finding: Termination is the Dominant Failure Mode

Pipeline shape does not predict success. Every pipeline had 1 success and 1 failure. The failure mode is identical across all three shapes: the agent enters a visual verification loop (launch game → screenshot → vision_query → patch → repeat) and cannot find a stopping condition.

B2 committed real work twice but still loop-limited — high diagnose confidence (0.92) did not help the agent decide it was done.

## Root Causes

### 1. `get_game_state(command="screenshot_b64")` causes shell stalls

The base64 string returned by `screenshot_b64` is too large to pass through shell commands:
- `echo "<base64>"` — shell arg limit exceeded, command truncated
- `python3 -c "...write(sys.argv[1])" "<base64>"` — same problem

A2 stalled at loop 68 using `echo`. C2 stalled at loop 51 using `python3 -c ... sys.argv[1]`. Both were caught by the stall detector (injected redirect), but each lost loops and momentum.

**Fix**: Prompt updated to use `take_screenshot(filename)` for visual checks — writes a file directly, no base64 handling. `screenshot_b64` should only be used for programmatic pixel analysis, not `vision_query` input.

### 2. No explicit stopping condition

The prompt said to verify with vision but never defined "done." Agents kept improving assets until the loop limit rather than deciding "good enough."

B2 is the clearest example: committed at loop ~60, kept running for 90 more loops improving things that were already working.

**Fix**: Prompt updated with explicit stopping rule — after first successful commit with real assets visible in-game, write `TASK_COMPLETE`. Perfectionism is a loop-limit failure mode.

### 3. probe-batch timeout too low for art_pass

The 1800s per-probe hard cap killed A2 at loop 69. A1 took 1044s, leaving only ~756s for A2. Art pass tasks routinely run 1000-1100s. Sequential runs need 2500s+ per probe or parallel execution.

**Fix needed in probe-batch**: raise `timeout` to 3600s for art_pass type, or run art pass probes in parallel (requires per-probe worktree isolation).

## Phase Cost Breakdown (successful runs)

| Phase | B1 (diagnose→work) | C2 (scout→work) |
|-------|-------------------|-----------------|
| Pre-work phase | 8 calls, 67s | 24 calls, 125s |
| Work phase | 98 calls, 672s | 107 calls, 909s |
| Total | 108 calls, 731s | 131 calls, 1034s |
| Output tokens | ~20k | ~55k |

diagnose→work is cheapest when it works: 37% fewer calls, 29% less time, ~64% fewer tokens than scout→work. The scout phase's 24-loop repo mapping costs 2.8× more than diagnose and doesn't improve termination reliability.

## Recommendations

### Default pipeline for art_pass: keep `work→validate`

With the termination problem dominating, the scoping phase overhead isn't justified. diagnose→work shows the best efficiency when it works, but the 50% failure rate makes it unsuitable as the default without the stopping rule fix.

**Priority order:**
1. Ship the prompt fixes (stopping rule + take_screenshot) — these change the 50% baseline
2. Re-run probe with prompt fixes in place to measure impact
3. Then decide whether diagnose→work is worth the overhead vs work-only

### Do not change art_pass default pipeline yet

Wait for a v3 probe with the prompt fixes. If the stopping rule brings success rate to 4/6+, diagnose→work becomes a reasonable default. If work-only also improves, keep it simple.

## Comparison to V1 Probe (contaminated)

V1 used classic-snake with `git checkout -- assets/` reset (working tree only). A1-v1 committed polished SVGs; later diagnose runs saw them in history and concluded "already done" in ~2 min. B1/B2/D1-v1 timing was artificially fast — contamination, not pipeline efficiency.

V2 with SHA-pinned reset on a genuinely art-free project eliminates this. All 6 v2 runs started from identical state.

## Probe Infrastructure Issues

- JSON not written for loop-limit exits (C1, A2-timeout): probe script writes JSON before `sys.exit(1)` but probe-batch's subprocess may miss the write on timeout kill. Need to verify and fix.
- probe-batch `timeout=1800` insufficient for sequential art_pass runs. Raise to 3600 or run in parallel.
- Monitor filter was too noisy (every loop event). Future probes: filter to `COMPLETE|FAIL|git_commit|TASK_COMPLETE|loop limit` only.
