# Run-10 Experiment Analysis: Void Patrol 4-Arm Comparison

**Date:** 2026-06-16
**Game:** Void Patrol (space shooter)
**Experiment duration:** ~1 run cycle (same base project, 4 independent pipelines)

---

## Raw Data

### Arm 1: void-patrol-variant-f-tail-run10 (baseline tail)

| Metric | Value |
|--------|-------|
| Total commits | 24 |
| feat | 13 |
| fix | 7 |
| art | 0 |
| polish | 0 |
| chore | 0 |
| Tasks total | 45 |
| completed | 42 |
| cancelled | 3 |
| failed/pending | 0 |
| GD scripts | 32 |
| TSCN scenes | 113 |

Task breakdown: feature×10, bug×12, art_pass×5, harness_qa×4, polish×5, research×6 — all completed clean (2 harness_qa cancelled, 1 research cancelled).

### Arm 2: void-patrol-variant-f-tail-quality-run10 (quality gate)

| Metric | Value |
|--------|-------|
| Total commits | 32 |
| feat | 10 |
| fix | 16 |
| art | 1 |
| chore | 2 |
| Tasks total | 43 |
| completed | 32 |
| cancelled | 4 |
| failed | 2 |
| pending | 5 |
| GD scripts | 30 |
| TSCN scenes | 104 |

**Outstanding at run end:** 2 failed features (boss fight, power-ups), 4 tasks blocked behind them (game flow, visual polish, final art gate, final polish gate, final harness QA). The infrastructure failure bug froze these features in an infinite loop without consuming attempts, preventing escalation. Fix landed mid-run but too late to clear the queue.

Heavy research phase: 21 completed research tasks (vs 6 for the tail baseline). The quality gate forced extensive investigation before implementation, which paid off in more fixes per feature committed but stalled the final delivery gate.

### Arm 3: void-patrol-variant-f-tail-quality-parallel-run10 (quality + parallel)

| Metric | Value |
|--------|-------|
| Total commits | 20 |
| feat | 11 |
| fix | 5 |
| art | 0 |
| Tasks total | 31 |
| completed | 31 |
| cancelled | 0 |
| failed/pending | 0 |
| GD scripts | 34 |
| TSCN scenes | 115 |

Cleanest execution of the run: 100% task completion, zero cancellations, zero failures. Fewest commits but highest scene count (34 GD scripts, 115 scenes vs 30/104 for quality-serial). Parallel execution within quality gates allowed more scene-level work to land without serial bottlenecks. Least rework: only 5 fix commits vs 16 for the serial quality arm.

### Arm 4: void-patrol-adaptive-flat-run10 (adaptive flat)

| Metric | Value |
|--------|-------|
| Total commits | 79 |
| feat | 6 |
  | fix | 12 |
| polish | 13 |
| art | 5 |
| chore | 4 |
| Tasks total | 61 |
| completed | 55 |
| cancelled | 6 |
| failed/pending | 0 |
| GD scripts | 37 |
| TSCN scenes | 120 |

Highest raw throughput by every volumetric measure: most commits (79), most tasks (61/55 completed), most GD scripts (37), most scenes (120). Task mix was bug-dominant (30 bug tasks completed) with significant polish (7) and art_pass (3) — the adaptive scheduler routed heavily toward fixing and finishing rather than building new features. 5 research tasks cancelled (orphaned after their consumers completed via other paths).

---

## Analysis

### Most output

**Adaptive flat** wins on every raw metric: 79 commits, 61 tasks, 37 scripts, 120 scenes. The flat queue with adaptive routing keeps agents busy and avoids gate-induced blocking. However, only 6 feat commits suggest the adaptive scheduler prioritised consolidation over new capability.

The tail baseline (arm 1) produced the second-highest feature commit count (13) at the lowest task overhead (45 total tasks), making it the most *efficient* arm per task created.

### Cleanest execution

**Quality parallel** (arm 3) had the cleanest run: 31/31 tasks completed, no cancellations, no failures, no pending work at run end. The combination of quality gates and parallelism eliminated both stalling (quality serial problem) and drift (flat arm's 6 cancelled research tasks).

### Work type dominance

- **Tail baseline:** feature-heavy (13 feat, 7 fix) — builds fast, fixes as needed
- **Quality serial:** fix-heavy (16 fix, 10 feat) — quality gates generate discovery and rework; research-saturated (21 research tasks)
- **Quality parallel:** balanced (11 feat, 5 fix) — parallel execution avoids rework by not stacking serial feedback loops
- **Adaptive flat:** polish/bug-heavy (13 polish, 12 fix, 5 art, 6 feat) — adaptive routing gravitates toward consolidating existing work rather than building new

### Quality arms vs flat adaptive

The infrastructure failure bug (`_is_infrastructure_failure` false positives) hit the two quality arms hardest. Serial quality (arm 2) was most severely damaged: 7 tasks frozen at the implementation gate, blocking the entire delivery tail. By the time the fix landed, there was insufficient runway to clear them. Parallel quality (arm 3) was buffered against this by its parallel structure — tasks that would have stalled in a serial gate instead ran independently, and the bug's window of damage was smaller per blocked chain.

The flat adaptive arm was essentially unaffected: no quality gates to block on, no serial chains to freeze. This is a structural advantage in the presence of infrastructure bugs but comes at the cost of less rigorous validation before delivery.

The quality serial arm's 21 completed research tasks show the mechanism working as intended (investigate before build), but they also represent a massive overhead: the research burden exceeded the feature delivery capacity within the run window.

### Carry forward into run-11

1. **Quality parallel is the winner pattern.** 100% completion, highest scene density, fewest rework commits, zero wasted tasks. Use it as the default quality arm structure.

2. **Drop quality serial.** The research overhead (21 tasks for ~10 delivered features) is not justified. Serial gates compound infrastructure failures and starve the delivery tail.

3. **Adaptive flat has a distinct role.** Its polish/art/bug dominance makes it a better *maintenance* arm than a *build* arm. Consider deploying it after a feature-complete gate rather than in parallel from project start.

4. **Infrastructure failure resilience:** build gate-bypass logic so tasks stuck in an infinite loop without attempt consumption are force-escalated after a wall-clock threshold, independent of attempt count. The quality parallel structure is inherently more resilient, but the fix should be defensive at the orchestrator level.

5. **Research cap:** quality arms should cap research at ~8 tasks per implementation cycle. The serial arm's 21-research overhead shows uncapped research crowds out delivery.

6. **Run-11 proposed structure:** quality-parallel as primary build arm + adaptive flat as a post-feature-complete polish/QA arm, activated when the parallel arm empties its feature queue.
