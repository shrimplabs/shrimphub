# Pipeline Improvement Review Notes

Date: 2026-06-10

Reviewed commit:

```text
fe87a83e feat(pipeline): WS2-WS7 pipeline improvement workstreams
```

This review focused on the latest commit only. There are unrelated local
uncommitted edits in the working tree, so do not treat this as a full repo
audit.

## Summary

The implementation direction is good. The new diagnose phase, default pipeline
selection, handoff artifacts, closure consistency logic, and tests are broadly
aligned with the requested work.

However, the new source-task metrics path is not reliable enough to use for
experiment scoring yet. The endpoint currently overcounts generated/recovery
tasks as source tasks and cannot identify failed source tasks from the raw
metric records. There is also a serialization gap where pipeline failure/repair
fields captured by `run_pipeline()` are not written into the final
`agent_<task>_pipeline.json` file that experiment metrics read.

Fix these before using `/api/experiment/source-tasks` to compare variants.

## Tests Run

Focused new tests:

```bash
python3 -m pytest \
  tests/test_pipeline_defaults.py \
  tests/test_pipeline_handoff.py \
  tests/test_pipeline_phase_artifacts.py \
  tests/test_experiment_source_tasks.py \
  tests/test_closure_status.py \
  -q
```

Result:

```text
67 passed in 69.47s
```

Regression slice:

```bash
python3 -m pytest \
  tests/test_improvements.py -k "research_feeder or review_task" \
  tests/test_lifecycle.py::TestResearchFeederRunAfter \
  -q
```

Result:

```text
1 failed, 11 passed
```

The failure is a stale local test expectation. It expected research feeders to
use:

```text
scout -> work
```

The latest commit intentionally changed research feeders to:

```text
scout -> diagnose
```

Update the test expectation if `diagnose` is the intended final feeder phase.

## Finding 1: Source-Task Aggregation Overcounts Generated Tasks

Severity: high for experiment scoring

File:

```text
swarm/api_metrics.py
```

Problem location:

```python
src_task = r.get("source_task_id") or r.get("task_id", "")
key = (
    r.get("experiment_id", ""),
    r.get("source_project", ""),
    src_task,
    r.get("experiment_variant", ""),
)
```

Problem:

When `source_task_id` is missing, the aggregator falls back to the row's own
`task_id`. That makes generated tasks, recovery tasks, closure tasks, QA tasks,
art/polish tasks, and research feeders appear as independent source tasks.

This defeats the purpose of source-task-level scoring.

Observed against current Run 4 metrics:

```text
raw records: 875
aggregates returned: 319
```

Variant D alone becomes:

```text
variant-d aggregates=107
```

This is not source-task-level output. It is mostly task-level output with some
partial source grouping.

Expected behavior:

For the original pipeline experiment, source-task-level aggregation should group
around the cloned original source tasks, e.g. the eight original `void-patrol`
source tasks per variant, with recovery/generated work counted as overhead
against those source tasks where lineage exists.

Suggested fixes:

1. Do not blindly fall back to `task_id` for experiment scoring.
2. Require `source_task_id` for rows included in source-task scoring, or mark
   missing lineage rows as `unattributed_overhead`.
3. Propagate source lineage when creating generated/recovery tasks:
   - `source_task_id`
   - `source_project`
   - possibly `root_source_task_id`
4. For research feeders, set their source key from the task they feed into.
5. Add a warning/count in the endpoint response:

```json
{
  "unattributed_rows": 42,
  "unattributed_task_ids": [...]
}
```

Acceptance criteria:

- Run 4 source-task endpoint no longer returns hundreds of aggregates for a
  run with eight original source tasks per variant.
- Generated/recovery rows are either attributed to a source task or reported as
  unattributed overhead, not silently treated as source tasks.
- Tests include generated tasks without `source_task_id`.

## Finding 2: Failed Source Tasks Are Not Represented As Failed

Severity: high for experiment scoring

Files:

```text
swarm/agent_finish.py
swarm/api_metrics.py
```

Problem locations:

```python
# swarm/agent_finish.py
record = {
    ...
    "validation_passed": success,
    ...
}
```

```python
# swarm/api_metrics.py
status = r.get("status") or ("completed" if r.get("validation_passed") else "")
if status == "completed" or r.get("validation_passed"):
    g["completed"] = True
    g["failed"] = False
elif status == "failed" and not g["completed"]:
    g["failed"] = True
```

Problem:

Experiment metric records do not include the task's terminal `status`. The
aggregator only marks a task failed if `status == "failed"`, but that field is
not present in the records being written.

Observed against current Run 4 metrics:

```text
control   failed=0
variant-a failed=0
variant-b failed=0
variant-c failed=0
variant-d failed=0
variant-e failed=0
variant-f failed=0
```

This is wrong. Variant D has a known failed source task:

```text
void-patrol-variant--1780782397727-0008
status=failed
attempts=3/3
research_feeder_cap_reached=true
```

Expected behavior:

The metrics writer should include terminal task status and the aggregator should
use it.

Suggested fixes:

1. Add `status` to experiment metric records:

```python
"status": task_snapshot.get("status", "")
```

2. Ensure `_write_experiment_metrics(...)` is called after the task snapshot
reflects the final status, or pass the final status explicitly.
3. Add fields for:

```text
completed
failed
cancelled
terminal_status
```

4. In the source-task aggregator, treat terminal failed/cancelled source tasks
as non-completed outcomes.

Acceptance criteria:

- D's failed source task appears as failed in `/api/experiment/source-tasks`.
- Failed/cancelled generated tasks do not make unrelated source tasks look
  failed unless they are attributed overhead with explicit lineage.
- Tests cover a metric row with `status=failed`.

## Finding 3: Final Pipeline State Omits New Failure and Repair Fields

Severity: medium-high

Files:

```text
swarm/agent_runtime.py
swarm/agent_finish.py
```

Problem location:

```python
# swarm/agent_runtime.py
_state_record = {
    "task_id": TASK_ID,
    "project": PROJECT,
    "task_type": TASK_TYPE,
    "pipeline": list(PIPELINE),
    "phases_completed": list(_final_state.phases_completed),
    "phase_timings": dict(_final_state.phase_timings),
    "plan": _final_state.plan,
    "scout_report": _final_state.scout_report,
    "synthesis": _final_state.synthesis,
    "work_report": _final_state.work_report,
    "validation": _final_state.validation,
    "errors": list(_final_state.errors),
    "failed": bool(_final_state.failed),
}
```

Problem:

`run_pipeline()` now records:

```text
failure_kind
failure_phase
failure_exception_class
failure_traceback
repair_attempts
max_repair_attempts
handoff
```

Phase artifacts include these fields, but the final
`agent_<task_id>_pipeline.json` written by `agent_runtime.py` does not.

`_write_experiment_metrics(...)` reads from the final pipeline file:

```python
"failure_kind": pipeline_state.get("failure_kind", ""),
"failure_phase": pipeline_state.get("failure_phase", ""),
"repair_attempts": pipeline_state.get("repair_attempts", 0),
"infrastructure_failure": pipeline_state.get("failure_kind") == "infrastructure_exception",
```

Because the final file omits those fields, the new metric columns are usually
blank or zero.

Suggested fix:

Add the missing fields to the final state record:

```python
_state_record = {
    ...
    "failure_kind": _final_state.failure_kind,
    "failure_phase": _final_state.failure_phase,
    "failure_exception_class": _final_state.failure_exception_class,
    "failure_traceback": _final_state.failure_traceback,
    "repair_attempts": _final_state.repair_attempts,
    "max_repair_attempts": _final_state.max_repair_attempts,
    "handoff": dict(_final_state.handoff),
}
```

Acceptance criteria:

- A pipeline infrastructure exception writes `failure_kind=infrastructure_exception`
  into `agent_<task>_pipeline.json`.
- A validation repair attempt writes `repair_attempts > 0`.
- Experiment metrics inherit those fields correctly.
- Tests cover final pipeline state serialization, not only per-phase artifact
  serialization.

## Finding 4: Research Feeder Test Needs Updating

Severity: low

File:

```text
tests/test_improvements.py
```

Problem:

The existing test expects:

```text
research feeder pipeline == ["scout", "work"]
```

The latest commit intentionally changed this to:

```text
research feeder pipeline == ["scout", "diagnose"]
```

Suggested fix:

Update the expected pipeline in:

```text
TestSelfImprovementReviewTask.test_research_feeder_uses_stable_pipeline_under_random_experiment
```

Also assert:

```text
recovery_pipeline_override == true
experiment_inherited_pipeline preserved
is_valid_order == true
invalidity_reason == ""
```

Those are still the important behavioral guarantees.

## Additional Observations

### Validation repair loop exists despite commit title saying WS2-WS7

The commit title says WS2-WS7, but `swarm/pipeline.py` includes a validation
repair loop. That is good, but it should be explicitly documented and tested as
WS1 if intended.

Recommended follow-up:

- Add direct tests for:
  - validate fails, repair work runs, validate passes
  - validate fails, repair work runs, validate fails
  - infrastructure exception does not enter repair loop

### Closure reconciliation is reasonable but intentionally lossy

The stale-red fix downgrades red to yellow when verification passed and open
regressions are zero. That matches the Run 4 dashboard problem. Keep this as
yellow, not green, because missing critical-flow coverage is still a coverage
gap.

## Recommended Fix Order

1. Add terminal `status` to experiment metric records.
2. Fix source-task aggregation so missing lineage is reported, not treated as a
   new source task.
3. Propagate source lineage into recovery/generated tasks where possible.
4. Serialize failure/repair/handoff fields in final pipeline state.
5. Update stale research feeder test to expect `scout -> diagnose`.
6. Add tests for real source-task aggregation edge cases.

## Quick Reproduction Commands

Run the focused tests:

```bash
python3 -m pytest \
  tests/test_pipeline_defaults.py \
  tests/test_pipeline_handoff.py \
  tests/test_pipeline_phase_artifacts.py \
  tests/test_experiment_source_tasks.py \
  tests/test_closure_status.py \
  -q
```

Show current aggregation overcount on Run 4 metrics:

```bash
python3 - <<'PY'
import json
from pathlib import Path
from collections import Counter
from swarm.api_metrics import _aggregate_source_tasks

records = []
for line in Path("data/experiment_metrics.jsonl").read_text().splitlines():
    try:
        row = json.loads(line)
    except Exception:
        continue
    if row.get("experiment_id") == "void-patrol-pipeline-ab-run4-20260606":
        records.append(row)

agg, unattributed = _aggregate_source_tasks(records)
print("raw_records", len(records))
print("aggregates", len(agg))
print("unattributed", len(unattributed))
print(Counter(row["experiment_variant"] for row in agg))
for variant in sorted(Counter(row["experiment_variant"] for row in agg)):
    rows = [row for row in agg if row["experiment_variant"] == variant]
    print(
        variant,
        "aggregates=", len(rows),
        "completed=", sum(1 for row in rows if row["completed"]),
        "failed=", sum(1 for row in rows if row["failed"]),
    )
PY
```

Current problematic output shape:

```text
raw_records 875
aggregates 319
variant-d aggregates=107 failed=0
```
