"""Tests for WS6: source-task-level experiment metrics aggregation."""

import json
import pytest
from swarm.api_metrics import _aggregate_source_tasks


def _row(**kwargs):
    defaults = {
        "experiment_id": "exp-1",
        "source_project": "proj-a",
        "source_task_id": "task-src-1",
        "experiment_variant": "C",
        "task_type": "feature",
        "task_id": "task-abc",
        "project": "proj-a-clone",
        "work_loops": 10,
        "validation_passed": True,
        "pipeline_validation_passed": True,
        "bug_spawned": False,
        "attempts": 1,
        "diff_insertions": 20,
        "diff_deletions": 5,
        "failure_kind": "",
        "failure_phase": "",
        "repair_attempts": 0,
        "infrastructure_failure": False,
        "research_feeder_cycles": 0,
        "is_recovery_task": False,
        "is_research_feeder": False,
        "completed_at": "2026-06-10T10:00:00",
        "status": "completed",
    }
    defaults.update(kwargs)
    return defaults


class TestAggregateSourceTasks:
    def test_single_row_produces_one_source_task(self):
        records = [_row()]
        result = _aggregate_source_tasks(records)
        assert len(result) == 1
        r = result[0]
        assert r["source_task_id"] == "task-src-1"
        assert r["completed"] is True
        assert r["total_attempts"] == 1

    def test_recovery_row_counted_as_overhead_not_completion(self):
        """Recovery tasks should NOT count as independent completions."""
        records = [
            _row(task_id="task-src-1", is_recovery_task=False),
            _row(task_id="task-recovery-1", is_recovery_task=True, validation_passed=False, status="failed"),
        ]
        result = _aggregate_source_tasks(records)
        # Only one source-task aggregate
        assert len(result) == 1
        r = result[0]
        assert r["completed"] is True  # main task completed
        assert r["recovery_tasks_spawned"] == 1

    def test_research_feeder_row_counted_as_feeder_cycles(self):
        """Research feeder rows should bump feeder_cycles, not completion count."""
        records = [
            _row(task_id="task-src-1", is_research_feeder=False, research_feeder_cycles=0),
            _row(task_id="task-feeder-1", is_research_feeder=True, research_feeder_cycles=1,
                 validation_passed=False, status="failed"),
        ]
        result = _aggregate_source_tasks(records)
        assert len(result) == 1
        r = result[0]
        assert r["completed"] is True
        assert r["feeder_cycles"] == 1

    def test_infrastructure_failure_counted_separately(self):
        records = [
            _row(infrastructure_failure=True, failure_kind="infrastructure_exception",
                 validation_passed=False, status="failed"),
            _row(infrastructure_failure=False, attempts=2, validation_passed=True, status="completed"),
        ]
        result = _aggregate_source_tasks(records)
        assert len(result) == 1
        r = result[0]
        assert r["infrastructure_failure_count"] == 1
        assert r["completed"] is True

    def test_repair_attempts_aggregated(self):
        records = [
            _row(repair_attempts=0, validation_passed=False, status="failed"),
            _row(repair_attempts=1, validation_passed=True, attempts=1, status="completed"),
        ]
        result = _aggregate_source_tasks(records)
        assert len(result) == 1
        r = result[0]
        assert r["repair_loop_count"] == 1

    def test_different_variants_produce_separate_aggregates(self):
        records = [
            _row(experiment_variant="C", task_id="task-c"),
            _row(experiment_variant="F", task_id="task-f"),
        ]
        result = _aggregate_source_tasks(records)
        assert len(result) == 2
        variants = {r["experiment_variant"] for r in result}
        assert variants == {"C", "F"}

    def test_different_source_tasks_produce_separate_aggregates(self):
        records = [
            _row(source_task_id="task-1", task_id="task-1a"),
            _row(source_task_id="task-2", task_id="task-2a"),
        ]
        result = _aggregate_source_tasks(records)
        assert len(result) == 2

    def test_validation_pass_rate_computed(self):
        records = [
            _row(validation_passed=True),
            _row(validation_passed=False, status="failed"),
        ]
        result = _aggregate_source_tasks(records)
        assert len(result) == 1
        r = result[0]
        assert r["validation_pass_rate"] == 0.5

    def test_diff_sizes_summed_across_attempts(self):
        records = [
            _row(diff_insertions=10, diff_deletions=2, validation_passed=False, status="failed"),
            _row(diff_insertions=30, diff_deletions=5, attempts=2, validation_passed=True, status="completed"),
        ]
        result = _aggregate_source_tasks(records)
        assert len(result) == 1
        r = result[0]
        assert r["diff_insertions"] == 40
        assert r["diff_deletions"] == 7

    def test_total_work_loops_summed(self):
        records = [
            _row(work_loops=15, validation_passed=False, status="failed"),
            _row(work_loops=22, attempts=2, validation_passed=True, status="completed"),
        ]
        result = _aggregate_source_tasks(records)
        assert len(result) == 1
        r = result[0]
        assert r["total_work_loops"] == 37

    def test_empty_records_returns_empty(self):
        assert _aggregate_source_tasks([]) == []

    def test_row_without_source_task_id_uses_task_id(self):
        """If source_task_id is absent, fall back to task_id as the group key."""
        records = [_row(source_task_id="", task_id="task-xyz")]
        result = _aggregate_source_tasks(records)
        assert len(result) == 1
        assert result[0]["source_task_id"] == "task-xyz"

    def test_max_attempts_used_not_sum(self):
        """total_attempts should be max across rows, not sum — retries share a task."""
        records = [
            _row(attempts=1, validation_passed=False, status="failed"),
            _row(attempts=2, validation_passed=False, status="failed"),
            _row(attempts=3, validation_passed=True, status="completed"),
        ]
        result = _aggregate_source_tasks(records)
        assert len(result) == 1
        assert result[0]["total_attempts"] == 3
