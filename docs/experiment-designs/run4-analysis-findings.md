# Run 4 Analysis Findings

Date analyzed: 2026-06-09

Experiment: `void-patrol-pipeline-ab-run4-20260606`

This note summarizes evidence from `data/swarm.db`,
`data/experiment_metrics.jsonl`, and
`data/experiments/void-patrol-pipeline-ab-run4-20260606/events.jsonl`.

## Current Outcome Snapshot

Run 4 is now terminal for all variants. `variant-d` drained after the
research-feeder cap fix, but it did not cleanly complete: one original source
feature task failed after repeated recovery churn.

Project closure state:

| Variant | Project | Closure | Last verification | Open regressions |
| --- | --- | --- | --- | ---: |
| control | `void-patrol-control-run4` | green | passed | 0 |
| A | `void-patrol-variant-a-run4` | green | passed | 0 |
| B | `void-patrol-variant-b-run4` | green | passed | 0 |
| C | `void-patrol-variant-c-run4` | red | passed | 0 |
| D | `void-patrol-variant-d-run4` | red | passed | 0 |
| E | `void-patrol-variant-e-run4` | green | passed | 0 |
| F | `void-patrol-variant-f-run4` | green | passed | 0 |

`C` and `D` showing `closure_status=red` despite passed verification and zero
open regressions indicates stale closure bookkeeping. Do not treat that field
alone as ground truth for final quality.

Final dataset export with D included:

```text
data/experiment_exports/run4-final-with-d-20260609T202000Z
```

## Original Feature Completion

Each non-D variant completed all eight original `void-patrol` feature tasks.
D completed seven original source tasks and failed the eighth after the feeder
cycle cap stopped automatic recovery.

| Variant | Original features completed | Original features failed | Sum of current feature attempts | Source feeder cycles |
| --- | ---: | ---: | ---: | ---: |
| control | 8/8 | 0 | 4 | 2 |
| A | 8/8 | 0 | 5 | 10 |
| B | 8/8 | 0 | 5 | 8 |
| C | 8/8 | 0 | 4 | 4 |
| D | 7/8 | 1 | 10 | 67 |
| E | 8/8 | 0 | 6 | 6 |
| F | 8/8 | 0 | 0 | 1 |

The most important feature-level signal is that flat/legacy mode (`F`) required
zero recorded retries on the original eight feature tasks, while every pipeline
variant needed retries.

## Event Metrics

Metrics rows include retries and recovery events, so these are not final
per-source-task scores. They are useful as churn and validation-noise measures.

| Variant | Metrics rows | Validation pass rate | Avg work loops | Avg attempts |
| --- | ---: | ---: | ---: | ---: |
| control | 71 | 0.437 | 33.7 | 0.49 |
| A | 87 | 0.241 | 32.8 | 0.64 |
| B | 79 | 0.190 | 31.0 | 0.67 |
| C | 84 | 0.476 | 34.2 | 0.48 |
| D | 424 | 0.087 | 16.9 | 0.75 |
| E | 78 | 0.231 | 33.9 | 0.59 |
| F | 52 | 0.885 | 78.7 | 0.12 |

Interpretation supported by the counts:

- `F` is high-pass, high-loop: fewer failures, more work loops.
- `C` and control have the best validation pass rates among pipeline variants.
- `A`, `B`, and `E` have notably lower validation pass rates.
- `D` generated far more rows than any other variant, which is recovery churn,
  not ordinary task throughput.

## Recovery Load

Recovery load is where the largest differences appear.

| Variant | Completed bugs | Failed bugs | Research total | Research cancelled | All cancelled |
| --- | ---: | ---: | ---: | ---: | ---: |
| control | 12 | 0 | 6 | 3 | 5 |
| A | 6 | 1 | 11 | 8 | 10 |
| B | 2 | 1 | 12 | 10 | 10 |
| C | 21 | 1 | 6 | 4 | 5 |
| D | 20 | 2 | 75 | 67 | 68 |
| E | 5 | 1 | 12 | 10 | 10 |
| F | 29 | 0 | 1 | 0 | 0 |

Bug+research tasks per completed original feature:

| Variant | Bug+research / completed original feature |
| --- | ---: |
| control | 2.25 |
| A | 2.25 |
| B | 1.88 |
| C | 3.50 |
| D | 13.86 |
| E | 2.25 |
| F | 3.75 |

`D` is the clear outlier. `F` also has high bug volume, but without failed bugs
or cancelled research feeders. That suggests F's flat loop created and resolved
more bug work directly instead of getting stuck in research recovery.

## Research Feeder Behavior

Research feeders inherited the project variant pipeline. This is confirmed by
task metadata and by code path:

- `_spawn_research_feeder(...)` builds `research_meta`.
- It then calls `stamp_experiment_metadata(project, research_meta)`.
- `stamp_experiment_metadata(...)` applies the project-level experiment config.
- For `variant-d`, `pipeline_mode == "random"` samples a fresh phase order.

Research feeder outcomes:

| Variant | Feeder pipeline | Feeders | Completed | Cancelled | In progress |
| --- | --- | ---: | ---: | ---: | ---: |
| control | `plan -> scout -> work -> validate` | 6 | 3 | 3 | 0 |
| A | `plan -> work -> validate` | 11 | 3 | 8 | 0 |
| B | `plan -> scout -> synthesize -> work -> validate` | 12 | 2 | 10 | 0 |
| C | `scout -> work -> validate` | 6 | 2 | 4 | 0 |
| D | randomized per feeder | 75 | 8 | 67 | 0 |
| E | `scout -> plan -> work -> validate` | 12 | 2 | 10 | 0 |
| F | flat / legacy prompt path | 1 | 1 | 0 | 0 |

For D, 43 research feeders received invalid randomized order because
`validate` ran before `work`. This made recovery tasks participate in the chaos
arm and amplified failures.

The final D source task was:

```text
void-patrol-variant--1780782397727-0008
status=failed
attempts=3/3
research_feeder_cycles=43
research_feeder_cap_reached=true
needs_human_review=true
```

## Failure Signatures

Dominant failure signatures by count:

- D: `validate: validation failed` appeared 50+ times.
- D: `work: hit loop limit without WORK_COMPLETE` appeared 5 times.
- D: `work: unsupported operand type(s) for /: 'str' and 'str'` appeared 3
  times inside run 4.
- The `str / str` error appears broadly in historical agent logs as a pipeline
  work-phase exception, not a project-specific failure.

The `str / str` issue should be treated as controller/tool robustness debt. It
contaminates task-level evaluation because it is an infrastructure exception
inside the agent tool loop.

## Evidence-Backed Recommendations

### 1. Pin recovery/meta task pipelines

Recovery and meta tasks should preserve experiment labels for analysis but use
stable operational pipelines by default. Do not let variant D randomize them
unless the experiment is explicitly testing chaotic recovery.

Recommended first policy:

```text
research feeder: scout -> work
```

or, if a validation artifact is required:

```text
research feeder: scout -> work -> validate
```

In either case, `work` must be a diagnosis/report-writing phase, not a generic
implementation-and-commit phase.

### 2. Split research feeder work from implementation work

Run 4 shows research feeders can drift into code editing because pipeline
`work` says "implement the change, commit, then output WORK_COMPLETE." That
conflicts with feeder instructions saying read-only diagnosis.

Update needed:

- Add a research-specific work profile or separate `diagnose` phase.
- Remove `git_commit`, source writes, and direct implementation pressure from
  research-feeder execution.
- Require feeder output to include exact root cause, exact files/lines, and
  next retry instructions.

### 3. Fix the `str / str` pipeline exception before another scored run

The repeated `unsupported operand type(s) for /: 'str' and 'str'` failure is
controller/tool contamination. Run 4 contains this failure in multiple variants,
and historical logs show it is broader than this experiment.

Update needed:

- Capture full tracebacks for phase exceptions into phase artifacts.
- Add regression tests around `run_python` and work-loop tool result handling.
- Treat infrastructure exceptions separately from project validation failures
  in metrics.

### 4. Treat closure status as derived, not authoritative

`variant-c` and `variant-d` both had `last_verification_status=passed` and
`open_regression_count=0` while `closure_status=red`.

Update needed:

- Recompute closure status from verification state after each verification run.
- Add a consistency check: `passed + open_regression_count=0 + red` should be
  flagged as stale closure status, not project failure.

### 5. Rework experiment metrics around source-task outcomes

Raw event rows overcount retries and recovery cascades. For scoring, aggregate
by `(experiment_id, source_task_id, experiment_variant)`.

Recommended primary metrics:

- original source tasks completed / total
- attempts per original source task
- recovery tasks spawned per original source task
- validation-pass rate on final attempt
- infrastructure-failure count
- wall-clock only as secondary, because concurrency and provider headroom vary

Recommended recovery metrics:

- feeder cycles per parent
- parent completed on next retry after feeder
- feeder produced actionable diagnosis
- feeder edited code despite read-only scope
- feeder failed due to infrastructure vs project validation

### 6. Keep F and C in the next focused comparison

Run 4 supports keeping:

- `F` as a strong flat/legacy baseline: highest validation pass rate and zero
  current feature retries, but expensive in loops.
- `C` as the lean pipeline candidate: best pipeline validation pass rate and
  low feeder churn among pipeline variants.
- control as continuity baseline.

Run 4 does not support keeping B/E in a broad next run unless the goal is
specifically to study planning/synthesis overhead. B and E had low validation
pass rates and high feeder cancellation without a clear benefit over C.

### 7. Keep D, but change what it measures

D is not clean evidence for main-task throughput because recovery inherited
chaos and amplified failures. It is valuable for studying recovery robustness.

Recommended use:

- Keep a chaos/stress arm, but pin recovery/meta tasks.
- Separately run a small explicit "chaotic recovery" experiment if needed.
- Analyze D by exact phase order and invalidity reason, not by variant label
  alone.
