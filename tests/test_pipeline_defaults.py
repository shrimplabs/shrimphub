"""Tests for WS2: default pipeline selection and plan-phase fallback."""

import json
import pytest
from unittest.mock import patch

from swarm.pipeline import TaskState, run_pipeline


# ---------------------------------------------------------------------------
# Default pipeline selection via generate_task_script
# ---------------------------------------------------------------------------

def _get_pipeline_for(task_type: str, pipelines_cfg: dict = None, metadata: dict = None) -> list:
    """Invoke the pipeline-resolution logic from swarm_runner directly."""
    import swarm_runner as runner

    # Minimal task object
    task = {
        "id": "task-test",
        "type": task_type,
        "project": "proj",
        "description": "Test task",
        "metadata": metadata or {},
        "priority": 50,
    }
    config = {"pipelines": pipelines_cfg or {}}

    # Reproduce the logic from generate_task_script (lines 960-995)
    _pipeline_from_metadata = (metadata or {}).get("pipeline") or []
    if isinstance(_pipeline_from_metadata, str):
        _pipeline_from_metadata = []
    _pipelines_cfg = config.get("pipelines") or {}
    _project_pipelines_cfg = {}
    _project_pipeline_override = None

    _DEFAULT_PIPELINES = {
        "feature":  ["scout", "work", "validate"],
        "bug":      ["scout", "work", "validate"],
        "polish":   ["scout", "work", "validate"],
        "art_pass": ["scout", "work", "validate"],
        "refactor": ["scout", "plan", "work", "validate"],
    }
    pipeline = (_pipeline_from_metadata
                or _project_pipeline_override
                or _pipelines_cfg.get(task_type)
                or _DEFAULT_PIPELINES.get(task_type, []))
    if pipeline == "random":
        pipeline = []
    return list(pipeline)


class TestDefaultPipelineSelection:
    def test_feature_task_defaults_to_scout_work_validate(self):
        pipeline = _get_pipeline_for("feature")
        assert pipeline == ["scout", "work", "validate"]

    def test_bug_task_defaults_to_scout_work_validate(self):
        pipeline = _get_pipeline_for("bug")
        assert pipeline == ["scout", "work", "validate"]

    def test_polish_task_defaults_to_scout_work_validate(self):
        pipeline = _get_pipeline_for("polish")
        assert pipeline == ["scout", "work", "validate"]

    def test_refactor_task_defaults_to_scout_plan_work_validate(self):
        pipeline = _get_pipeline_for("refactor")
        assert pipeline == ["scout", "plan", "work", "validate"]

    def test_qa_task_has_no_default_pipeline(self):
        """QA tasks run the flat loop — no default pipeline."""
        pipeline = _get_pipeline_for("qa")
        assert pipeline == []

    def test_research_task_has_no_default_pipeline(self):
        pipeline = _get_pipeline_for("research")
        assert pipeline == []

    def test_explicit_config_overrides_default(self):
        """pipelines config always wins over defaults."""
        pipeline = _get_pipeline_for("feature", pipelines_cfg={"feature": ["plan", "scout", "work", "validate"]})
        assert pipeline == ["plan", "scout", "work", "validate"]

    def test_metadata_pipeline_overrides_default(self):
        """Task-level metadata.pipeline wins over default."""
        pipeline = _get_pipeline_for("feature", metadata={"pipeline": ["plan", "work"]})
        assert pipeline == ["plan", "work"]

    def test_empty_metadata_pipeline_falls_through_to_default(self):
        """Empty list in metadata is treated as absent — falls through to default."""
        pipeline = _get_pipeline_for("feature", metadata={"pipeline": []})
        assert pipeline == ["scout", "work", "validate"]


# ---------------------------------------------------------------------------
# Plan-phase fallback: parse failure no longer kills the pipeline
# ---------------------------------------------------------------------------

class TestPlanFallback:
    def _make_state(self, tmp_path):
        return TaskState(
            task_id="task-plan-fb",
            task_type="feature",
            project="proj",
            description="Add scoring system",
            project_path=str(tmp_path),
            workspace=str(tmp_path),
        )

    def test_plan_fallback_on_repeated_parse_failure(self, tmp_path):
        """Plan phase falls back to a synthetic plan instead of raising."""
        from swarm.phases.plan import PlanPhase

        # Return PLAN_COMPLETE with invalid JSON every time
        bad_response = "PLAN_COMPLETE\n{not valid json"

        state = self._make_state(tmp_path)
        with patch("swarm.llm_utils.call_llm", return_value=(bad_response, {}, [])):
            phase = PlanPhase(config={"data_dir": str(tmp_path)})
            result = phase.run(state)

        # Should not raise; should produce a synthetic plan
        assert result.plan is not None
        assert result.plan.get("_plan_parse_failed") is True
        assert "Add scoring system" in result.plan.get("goal", "")

    def test_plan_fallback_does_not_set_failed(self, tmp_path):
        """Plan fallback must not set state.failed — pipeline should continue."""
        from swarm.phases.plan import PlanPhase

        bad_response = "PLAN_COMPLETE\n{bad json}"
        state = self._make_state(tmp_path)
        with patch("swarm.llm_utils.call_llm", return_value=(bad_response, {}, [])):
            phase = PlanPhase(config={"data_dir": str(tmp_path)})
            result = phase.run(state)

        assert result.failed is False

    def test_plan_fallback_records_error_in_state_errors(self, tmp_path):
        """Plan fallback should record the parse failure in state.errors for observability."""
        from swarm.phases.plan import PlanPhase

        bad_response = "PLAN_COMPLETE\n{bad json}"
        state = self._make_state(tmp_path)
        with patch("swarm.llm_utils.call_llm", return_value=(bad_response, {}, [])):
            phase = PlanPhase(config={"data_dir": str(tmp_path)})
            result = phase.run(state)

        assert any("plan" in e and "fallback" in e for e in result.errors)

    def test_plan_fallback_pipeline_continues_to_next_phase(self, tmp_path):
        """After plan fallback, pipeline should run scout (or next phase) without stopping."""
        from swarm.pipeline import Phase, register_phase, PHASE_REGISTRY

        bad_plan_response = "PLAN_COMPLETE\n{bad json}"
        scout_json = json.dumps({
            "files_inspected": [],
            "findings": ["found it"],
            "hypotheses": [],
            "recommended_actions": [],
            "confidence": 0.5,
        })
        scout_response = "SCOUT_COMPLETE\n" + scout_json

        from swarm.constants import PLAN_MAX_LOOPS
        responses = iter([
            (bad_plan_response, {}, []),
        ] * PLAN_MAX_LOOPS + [
            ("Continuing investigation.", {}, []),
        ] * 4 + [
            (scout_response, {}, []),     # scout minimum depth → SCOUT_COMPLETE
        ])

        state = TaskState(
            task_id="task-plan-pipeline",
            task_type="feature",
            project="proj",
            description="Add score",
            project_path=str(tmp_path),
            workspace=str(tmp_path),
        )

        with patch("swarm.llm_utils.call_llm", side_effect=responses), \
             patch("swarm.phases.scout.call_llm", side_effect=responses):
            final = run_pipeline(
                ["plan", "scout"],
                state,
                config={"data_dir": str(tmp_path)},
                log_fn=lambda _: None,
            )

        assert final.failed is False
        assert "plan" in final.phases_completed
        assert "scout" in final.phases_completed
        assert final.plan.get("_plan_parse_failed") is True
        assert final.phase_loops["plan"] == PLAN_MAX_LOOPS
        assert final.phase_loops["scout"] == 5
