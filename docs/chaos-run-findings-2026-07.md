# Chaos Run Findings — July 2026

## What we tested

30 adversarial pipeline probes across three batches (C1–C21 + batch 1 probes), run against live
Godot projects using `tools/pipeline-probe.py` in isolated mode. Scenarios covered:

- All task types: bug, feature, refactor, research, project_plan
- All phase combinations: plan-only, scout-only, no-plan, no-scout, full chains, synthesize+create_tasks
- Adversarial inputs: nonexistent files, contradictory requirements, empty projects, vague descriptions,
  massive task descriptions, wide refactors, deep dep chains, handoff gaps
- All 10 in batch 3 (C12–C21) targeted handoff correctness specifically

Zero pipeline crashes or phase failures across all 30 runs.

---

## Bugs found and fixed

| # | Bug | Where | Fix |
|---|-----|--------|-----|
| 1 | Scout min-loop floor too rigid — large reading list (15 files) set min_loops=15, blocking completion at loop 13 | `scout.py` | Flat floor: 3 loops when reading list exists, 5 without |
| 2 | Stall counter cascade after early-completion rejection — rejection fell through to stall detection, fired contradictory nudge next loop | `scout.py` | `_consecutive_stalls = 0; continue` after rejection |
| 3 | Malformed SCOUT_COMPLETE treated as stall instead of JSON repair nudge | `scout.py` | Separate `saw_scout_complete = "SCOUT_COMPLETE" in text` check; send repair nudge |
| 4 | create_tasks duplicate loop — model called create_tasks successfully but didn't emit TASKS_CREATED, looped 15× creating 120 duplicate tasks | `create_tasks.py` | Post-creation nudge + auto-stop at `proposed_count` |
| 5 | Diagnose→work handoff gap — root_cause/recommended_fix written to `state.synthesis` only; work prompt doesn't render those fields | `diagnose.py` | Inject root_cause into `handoff.hypotheses`; inject recommended_fix into `scout_report.recommended_actions` |
| 6 | Work agent emits WORK_COMPLETE with uncommitted file writes | `work.py` | Block WORK_COMPLETE when `mutation_tool_calls > 0` and `commit_sha is None`; inject nudge to call git_commit first |

All six fixes are committed and pushed.

---

## What we learned about pipeline shapes

### The key clarification

The chaos runs tested **task decomposition** pipelines, not implementation pipelines. Runs that ended
at `synthesize` or `create_tasks` produced task lists for other agents to execute. Runs that ended at
`work` did implementation. These are fundamentally different outputs and should not be compared on
the same efficiency axis.

### Task decomposition shapes (planning outputs)

These shapes produce tasks, not commits. They are the right tool for `project_plan`, `plan`,
`research`, and multi-file refactors that need careful dep ordering.

| Shape | Calls | Time | Output | Notes |
|-------|-------|------|--------|-------|
| plan→scout→synthesize→create_tasks | 10–26 | 87–244s | 5–8 tasks | Most efficient; C18 did it in 10 calls |
| plan→scout→diagnose→synthesize | 19–40 | 176–357s | 5–8 tasks | Worth adding diagnose for bugs — root cause makes tasks precise |
| plan→scout→synthesize | ~15 | ~120s | findings only | Right for research |

**C20 was the most revealing**: synthesis came back with 0 tasks and confidence=0.3 because the
feature was already implemented. The pipeline correctly refused to create redundant work. That is
the right answer, not a failure.

**C21 was the highest quality**: intermittent wave-3 freeze diagnosed as three simultaneous root
causes (duplicate signal connections, Timer node leaks, modal MouseFilter). Diagnose identified all
three in 5 loops. The resulting 8-task plan had exact line numbers and fix directives.

### Implementation shapes (commit outputs)

These shapes produce git commits. They are the right tool for `bug`, `feature`, `refactor`, `polish`.

| Shape | Calls | Time | Mutations | Notes |
|-------|-------|------|-----------|-------|
| plan→scout→work | 74 | 542s | 10 | C17: standard feature; scout context reduced exploration |
| plan→work (no scout) | 82 | 573s | 18 | C14: more raw mutations but no scout context — quality unknown |
| scout→work (no plan) | 54 | 396s | 8 | C13: faster but no plan goal to anchor work |
| plan→scout→synthesize→work | 109 | 956s | 15+8 tasks | C15: synthesize before work added overhead without clear benefit |
| plan→scout→diagnose→synthesize→work | 115 | 1719s | 6 | C12: correct fix, but 29 minutes and no commit |

**Key finding on C12 (full 5-phase chain)**: the work agent correctly identified and acted on the
diagnose root cause (`main._save_achievements()` → `achievement_manager._save_achievements()`),
validating that the diagnose→work handoff fix works. But 115 calls and 29 minutes for 6 uncommitted
mutations is too expensive for a self-contained bug fix.

**C14 (no scout)**: produced the most raw mutations (18) at the lowest calls-per-mutation, but
without scout context we cannot verify correctness. Scout adds a quality floor at a ~10-call cost.

### The uncommitted-writes pattern

C12, C13, C14, C17 all completed with mutations but no git commit. The work agent gets deep into a
validation-fix loop and eventually calls WORK_COMPLETE without committing. Fixed in `work.py` (bug
#6 above), but the pattern suggests work agents need clearer commit prompting earlier in the loop —
not just a gate at WORK_COMPLETE.

---

## Efficiency summary

**Calls per unit of output** (task decomposition):
- `plan→scout→synthesize→create_tasks`: ~2 calls/task (C18: 10 calls → 5 tasks)
- `plan→scout→diagnose→synthesize`: ~5 calls/task (C16: 40 calls → 7 tasks)

**Calls per unit of output** (implementation):
- `plan→scout→work`: ~7.4 calls/mutation (C17)
- `plan→work`: ~4.6 calls/mutation (C14) — cheaper but quality unknown
- Full 5-phase chain: ~11.5 calls/unit (C12) — most expensive

**Wall time per LLM call**: 7–9s across all runs except C12 (14.9s due to context compaction
mid-run and heavy Godot validation commands).

---

## Recommended pipeline shapes (current best guess)

These are hypotheses, not conclusions. The implementation shapes need a dedicated chaos batch to
validate quality (not just throughput).

| Task type | Recommended shape | Rationale |
|-----------|------------------|-----------|
| `project_plan` | plan→scout→synthesize→create_tasks | Cheapest path to a correct dep-ordered task DAG |
| `plan` | plan→scout→synthesize | Findings only; no implementation risk |
| `research` | plan→scout→synthesize | Same |
| `bug` | plan→scout→diagnose→work | Diagnose root cause before touching code; avoid thrashing |
| `feature` | plan→scout→work | Scout context prevents blind exploration in work |
| `refactor` | plan→scout→work | Same; scout maps blast radius |
| `polish` | scout→work | Plan overhead not worth it for scoped visual fixes |

**What we don't know yet**: whether `plan→scout→diagnose→work` produces *better* bug fixes than
`plan→scout→work`, and at what cost. C12 validated the handoff but not the quality delta. This is
the primary open question for the next chaos batch.

---

## Next steps

1. **Add analytics to the probe harness** before running more chaos runs — current logs require
   manual grep to extract numbers. Need: token counts per phase, loop counts per phase, tool call
   breakdown, commit presence, and a structured JSON summary at the end of each run.

2. **Feature implementation chaos batch** — run 10–15 probes specifically testing implementation
   quality for `feature` and `bug` task types:
   - Compare `plan→scout→work` vs `plan→scout→diagnose→work` on the same bug
   - Measure commit rate (currently 0/4 in implementation runs — all unmitted)
   - Measure validation pass rate (does Godot headless check pass after work?)
   - Test whether synthesize before work (C15 shape) actually improves implementation quality

3. **Commit nudge improvement** — work agents need earlier commit prompting, not just a gate at
   WORK_COMPLETE. Consider injecting a "remember to commit incrementally" reminder at loop 20 if
   mutations > 0 and commit_sha is None.
