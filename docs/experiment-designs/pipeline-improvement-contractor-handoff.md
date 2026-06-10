# Pipeline Improvement Contractor Handoff

Date: 2026-06-10

Repo: `swarm-controller`

Primary context:

- `docs/experiment-designs/pipeline-ab-experiment.md`
- `docs/experiment-designs/run4-analysis-findings.md`
- `docs/experiment-designs/pipeline-run-lifecycle.md`

## Executive Summary

Run 4 showed that the flat legacy agent loop, variant F, currently outperforms
the explicit phase pipeline on clean task completion. This does not mean the
pipeline model is wrong. It means the current phase boundaries, recovery
behavior, and validation loop are losing too much context compared with one
continuous agent loop.

The best pipeline candidate from Run 4 is variant C:

```text
scout -> work -> validate
```

The weakest parts of the current system are:

- recovery/meta tasks inheriting experiment pipelines
- validation acting as a terminal judge instead of a repair loop
- plan phase brittleness and weak handoff value
- research feeder tasks using generic implementation work behavior
- infrastructure exceptions being counted like project failures
- metrics overcounting recovery cascades instead of scoring source-task outcome

The goal of this handoff is to make the pipeline competitive with the flat loop
without losing the research value of phase-level instrumentation.

## Current Evidence

Final Run 4 export with D included:

```text
data/experiment_exports/run4-final-with-d-20260609T202000Z
```

Final source-task outcomes:

| Variant | Pipeline | Source done | Source failed | Source attempts | Source feeder cycles |
| --- | --- | ---: | ---: | ---: | ---: |
| control | `plan -> scout -> work -> validate` | 8/8 | 0 | 4 | 2 |
| A | `plan -> work -> validate` | 8/8 | 0 | 5 | 10 |
| B | `plan -> scout -> synthesize -> work -> validate` | 8/8 | 0 | 5 | 8 |
| C | `scout -> work -> validate` | 8/8 | 0 | 4 | 4 |
| D | randomized per task | 7/8 | 1 | 10 | 67 |
| E | `scout -> plan -> work -> validate` | 8/8 | 0 | 6 | 6 |
| F | flat legacy loop, no phases | 8/8 | 0 | 0 | 1 |

Event metric summary:

| Variant | Rows | Validation pass rate | Avg work loops | Avg attempts |
| --- | ---: | ---: | ---: | ---: |
| control | 71 | 0.437 | 33.7 | 0.49 |
| A | 87 | 0.241 | 32.8 | 0.64 |
| B | 79 | 0.190 | 31.0 | 0.67 |
| C | 84 | 0.476 | 34.2 | 0.48 |
| D | 424 | 0.087 | 16.9 | 0.75 |
| E | 78 | 0.231 | 33.9 | 0.59 |
| F | 52 | 0.885 | 78.7 | 0.12 |

Interpretation:

- F wins on clean completion and low retry count, but spends more continuous
  work loops.
- C is the best-looking actual pipeline.
- D is valuable as chaos/stress data, not as throughput evidence.
- A, B, and E do not currently justify their added phase overhead.

## Completed Fixes

These changes have already been implemented locally.

### 1. Research feeder pipeline pinning

File:

```text
swarm/agent_recovery.py
```

Research feeders now preserve experiment labels but force a stable operational
pipeline:

```text
scout -> work
```

This prevents variant D from randomizing recovery feeders into invalid orders
such as `validate -> plan -> scout -> work`.

Behavior:

- stores inherited pipeline metadata for analysis
- sets feeder `pipeline`, `pipeline_variant`, and `phase_order` to
  `["scout", "work"]`
- marks `recovery_pipeline_override=true`
- clears invalid order metadata

### 2. Research feeder cycle cap

File:

```text
swarm/agent_recovery.py
```

Research feeder cycles are capped at 2. Once the cap is reached, the source task
is marked for human review and no new feeder is spawned.

Important behavior:

- terminal source failure remains failed instead of resetting to pending
- feeder result application also drains over-cap source tasks instead of waking
  them again
- metadata records:
  - `research_feeder_cap_reached=true`
  - `research_feeder_cap=2`
  - `needs_human_review=true`

### 3. Art and polish work profiles

File:

```text
swarm/phases/work.py
```

Pipeline work now has task-type profiles for `art_pass` and `polish`, using the
original prompt intent and visual-tool guidance instead of treating them like
generic implementation tasks.

### 4. Tests added

Relevant test files:

```text
tests/test_improvements.py
tests/test_lifecycle.py
tests/test_pipeline_work_phase.py
```

Known passing test commands:

```bash
python3 -m pytest tests/test_improvements.py -k "research_feeder or review_task" -q
python3 -m pytest tests/test_lifecycle.py::TestResearchFeederRunAfter tests/test_pipeline_work_phase.py -q
```

## Remaining Work

The items below are the proposed contractor scope.

## Workstream 1: Add a Validation Repair Loop

### Problem

Validation currently behaves like a terminal judge. If validation fails, the
task often enters recovery rather than giving the same pipeline a chance to use
the validation output while context is still warm.

Run 4 failure counts show validation failures are the largest direct pipeline
failure class:

```text
D:       55 validate failures
B:        6 validate failures
E:        4 validate failures
C:        3 validate failures
control: 2 validate failures
A:        2 validate failures
```

### Required behavior

Add a bounded local repair loop:

```text
scout -> work -> validate
              ^      |
              |______|
```

If `validate` fails:

1. Capture the validation output as structured failure context.
2. Re-enter `work` once, or at most a small configured number of times.
3. Inject the validation failure into the work prompt.
4. Re-run `validate`.
5. Only after repair attempts are exhausted should task-level failure/recovery
   begin.

### Acceptance criteria

- Configurable max repair attempts, default `1`.
- Repair attempts are recorded in pipeline state and experiment metrics.
- Work prompt receives the exact validation failure output.
- Existing terminal failure behavior still happens after repair attempts are
  exhausted.
- Tests cover:
  - validation failure then repair success
  - validation failure then repair exhaustion
  - metrics include repair count

## Workstream 2: Reduce or Fold the Plan Phase

### Problem

The plan phase often fails to produce useful concrete plans and sometimes fails
schema parsing entirely:

```text
plan phase could not produce a concrete plan: parse error
```

Variant C, `scout -> work -> validate`, performed better than variants that
included plan. The standalone plan phase is not currently earning its cost.

### Recommended path

Do not remove planning concepts entirely. Instead:

- keep variant C as the default pipeline candidate
- fold lightweight planning into scout or work as a checklist
- reserve full `plan` phase for large or explicitly marked tasks

Suggested default:

```text
feature/bug/refactor: scout -> work -> validate
large/refactor-heavy: scout -> plan -> work -> validate
```

### Acceptance criteria

- Add task-size or metadata gate for full plan phase.
- Small/normal tasks default to `scout -> work -> validate`.
- Existing configured pipelines still work when explicitly set.
- Plan parse failures do not kill a task without a retry/repair attempt.
- Tests verify project-level and task-level pipeline selection.

## Workstream 3: Split Research Diagnosis From Implementation Work

### Problem

Research feeders are supposed to diagnose and feed context back to the source
task. The current generic work phase encourages implementation, commits, and
normal task completion behavior. This conflicts with the research feeder's
read-only diagnosis purpose.

### Required behavior

Introduce one of these:

```text
research-diagnose phase
```

or a strict research-specific work profile:

```text
scout -> diagnose
```

The phase/profile must:

- be read-only by default
- not commit code
- not create tasks
- write exact findings into the feeder output/scratchpad
- identify root cause, files, lines, and next retry instructions
- end with `TASK_COMPLETE` or the pipeline-equivalent success marker

### Acceptance criteria

- Research feeders cannot call source-writing or git commit tools unless an
  explicit override is set.
- Feeder output schema includes:
  - root cause
  - files inspected
  - exact failure
  - recommended fix
  - confidence
- Feeder result injection preserves this schema in source task metadata.
- Tests cover read-only enforcement and metadata injection.

## Workstream 4: Fix Pipeline Infrastructure Exceptions

### Problem

The recurring error:

```text
unsupported operand type(s) for /: 'str' and 'str'
```

appears across variants and historical logs. This is controller/tool
contamination, not a game-project failure.

### Required behavior

- Find and fix the path handling bug causing string division.
- Add full traceback capture for phase exceptions.
- Store exception class, traceback, phase, task id, and tool call context in the
  phase artifact.
- Mark these as infrastructure failures in metrics, separate from validation or
  project failures.

### Acceptance criteria

- Regression test reproduces the original `str / str` failure path.
- Test passes after fix.
- Metrics include `failure_kind`, with values such as:
  - `project_validation`
  - `agent_loop_exhausted`
  - `provider_timeout`
  - `infrastructure_exception`
- Infrastructure failures can be excluded from experiment scoring.

## Workstream 5: Improve Handoff Artifacts Between Phases

### Problem

The flat loop keeps all context in one continuous agent session. The pipeline
breaks that context across phases, but the handoff artifacts are not yet strong
enough to compensate.

### Required behavior

Each phase should produce structured state that the next phase can directly use.

Recommended minimum shape:

```json
{
  "goal": "...",
  "facts": ["..."],
  "files_inspected": ["..."],
  "files_to_modify": ["..."],
  "known_failures": ["..."],
  "constraints": ["..."],
  "next_actions": ["..."],
  "unknowns": ["..."]
}
```

Work should receive the full useful handoff, not only a lossy summary.

### Acceptance criteria

- Scout output includes actionable files and constraints.
- Work prompt includes structured scout output and prior validation failures.
- Pipeline state JSON records all handoffs.
- Tests assert that work prompt construction includes scout findings and
  validation-repair findings.

## Workstream 6: Improve Experiment Metrics

### Problem

Raw metric rows overcount retries, recovery tasks, and feeder cascades. This
made D look very active while it was actually churning.

### Required behavior

Add source-task-level aggregation keyed by:

```text
(experiment_id, source_project, source_task_id, experiment_variant)
```

Primary scoring metrics:

- original source tasks completed / total
- original source tasks failed / total
- attempts per original source task
- feeder cycles per source task
- recovery tasks spawned per source task
- final validation status
- infrastructure failure count
- repair-loop count

Secondary metrics:

- total agent loops
- wall clock
- provider/model cost
- diff size
- generated follow-on task count

### Acceptance criteria

- Add script or endpoint that emits per-source-task CSV/JSON.
- Existing event metrics remain append-only.
- D-style recovery cascades are visible but do not dominate source-task
  completion scoring.
- Tests cover aggregation with duplicate recovery events.

## Workstream 7: Closure Status Consistency

### Problem

Run 4 had projects where:

```text
last_verification_status=passed
open_regression_count=0
closure_status=red
```

This makes dashboards misleading.

### Required behavior

Closure status should be derived or reconciled after each verification run.

### Acceptance criteria

- If verification passes and open regression count is zero, closure cannot
  remain stale red.
- If closure remains red for another reason, that reason must be explicit in
  metadata.
- Add a consistency check and test.

## Recommended Next Experiment After Fixes

Do not rerun every variant immediately. Use a smaller focused run:

| Arm | Purpose |
| --- | --- |
| F | flat legacy baseline |
| C | best current pipeline candidate: `scout -> work -> validate` |
| control | continuity baseline: `plan -> scout -> work -> validate` |
| D-lite | chaos/stress arm with recovery/meta tasks pinned stable |

D-lite should randomize only implementation tasks unless the experiment is
explicitly about chaotic recovery.

## Contractor Deliverables

Expected deliverables:

1. Code changes for workstreams 1-7, or a mutually agreed subset.
2. Unit tests and focused integration tests.
3. A migration note for any config changes.
4. A short post-implementation report with:
   - files changed
   - behavior changed
   - tests run
   - known remaining risks
5. No destructive mutation of existing Run 4 data.

## Non-Goals

Do not optimize for wall-clock speed as the primary metric. The swarm runs under
limited agent concurrency and provider headroom, so wall-clock is heavily
confounded.

Do not interpret D as a throughput winner. D is a chaos/stress instrument.

Do not delete or overwrite existing experiment exports. The past is immutable;
new analysis should create new labeled artifacts.

## Suggested Implementation Order

1. Fix infrastructure exception classification and traceback capture.
2. Add validation repair loop.
3. Add research-specific diagnosis phase/profile.
4. Strengthen handoff artifacts.
5. Make C the default candidate pipeline for normal tasks.
6. Add source-task-level metrics.
7. Fix closure consistency.

This order reduces contamination first, then improves pipeline behavior, then
improves scoring.
