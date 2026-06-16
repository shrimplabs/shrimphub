import json
import pytest
from unittest.mock import patch
from swarm.pipeline import TaskState, run_pipeline, register_phase, Phase, PHASE_REGISTRY
from swarm.phases.work import WorkPhase, _build_work_prompt


def test_visual_tasks_get_pipeline_vision_guidance():
    state = TaskState(
        task_id="art-1",
        task_type="art_pass",
        project="game",
        description="Improve sprites",
        project_path="/tmp/game",
    )

    prompt = _build_work_prompt(state)

    assert "TASK-TYPE WORK PROFILE" in prompt
    assert "YOU ARE AN ASSET-INTEGRATION AGENT" in prompt
    assert "vision_query" in prompt
    assert "3-screenshot budget" in prompt
    assert "WORK_COMPLETE" in prompt
    assert "TASK_COMPLETE" not in prompt
    assert "<< project >>" not in prompt
    assert "<< project_path >>" not in prompt
    assert "/tmp/game" in prompt


def test_non_visual_tasks_do_not_get_pipeline_vision_requirement():
    state = TaskState(
        task_id="feature-1",
        task_type="feature",
        project="game",
        description="Add scoring",
        project_path="/tmp/game",
    )

    prompt = _build_work_prompt(state)

    assert "VISUAL VERIFICATION REQUIRED" not in prompt
    assert "TASK-TYPE WORK PROFILE" not in prompt


def test_polish_task_gets_pipeline_ux_profile():
    state = TaskState(
        task_id="polish-1",
        task_type="polish",
        project="game",
        description="Improve menu feedback",
        project_path="/tmp/game",
    )

    prompt = _build_work_prompt(state)

    assert "TASK-TYPE WORK PROFILE" in prompt
    assert "UI/UX integrity" in prompt
    assert "screen transitions, button feedback, menu flow" in prompt
    assert "VISUAL VERIFICATION WORKFLOW" in prompt
    assert "WORK_COMPLETE" in prompt
    assert "TASK_COMPLETE" not in prompt
    assert "<< description >>" not in prompt
    assert "Improve menu feedback" in prompt


def test_work_loop_limit_can_be_overridden(tmp_path):
    state = TaskState(
        task_id="art-override",
        task_type="art_pass",
        project="game",
        description="Improve sprites",
        project_path=str(tmp_path),
        workspace=str(tmp_path),
    )

    with patch("swarm.phases.work.call_llm", return_value=("continue", {}, [])) as call_llm:
        phase = WorkPhase(config={"phase_loop_limits": {"work": 3}})
        result = phase.run(state)

    assert result.failed is True
    assert result.phase_loops["work"] == 3
    assert result.work_report["loops_used"] == 3
    assert call_llm.call_count == 3


# ===========================================================================
# WS1 — Validation repair loop
# ===========================================================================

def _make_state(tmp_path, **kwargs):
    return TaskState(
        task_id="task-ws1",
        task_type="feature",
        project="proj",
        description="Add feature X",
        project_path=str(tmp_path),
        workspace=str(tmp_path),
        **kwargs,
    )


class TestValidationRepairPrompt:
    """Work prompt includes validation failure context on repair attempts."""

    def test_no_repair_context_on_first_attempt(self, tmp_path):
        state = _make_state(tmp_path)
        prompt = _build_work_prompt(state)
        assert "VALIDATION REPAIR" not in prompt

    def test_repair_context_injected_on_repair_attempt(self, tmp_path):
        state = _make_state(tmp_path)
        state.repair_attempts = 1
        state.validation = {"passed": False, "errors": ["script.gd:10 - Parse error: Expected 'end of file'"]}
        prompt = _build_work_prompt(state)
        assert "VALIDATION REPAIR" in prompt
        assert "attempt 1" in prompt
        assert "Parse error" in prompt

    def test_repair_context_not_injected_when_validation_passed(self, tmp_path):
        state = _make_state(tmp_path)
        state.repair_attempts = 1
        state.validation = {"passed": True, "errors": []}
        prompt = _build_work_prompt(state)
        assert "VALIDATION REPAIR" not in prompt

    def test_repair_error_text_is_truncated(self, tmp_path):
        state = _make_state(tmp_path)
        state.repair_attempts = 1
        state.validation = {"passed": False, "errors": ["x" * 5000]}
        prompt = _build_work_prompt(state)
        # error text is capped at 2000 chars
        assert "x" * 2001 not in prompt
        assert "VALIDATION REPAIR" in prompt


class TestValidationRepairLoop:
    """run_pipeline retries work+validate on validation failure before giving up."""

    def _register_phases(self, work_calls, validate_results):
        """Register mock work + validate phases. work_calls tracks invocations.
        validate_results is a list of (passed: bool) in call order."""
        validate_iter = iter(validate_results)

        @register_phase
        class _MockWork(Phase):
            name = "_ws1_work"
            def run(self, state):
                work_calls.append(state.repair_attempts)
                state.work_report = {"loops_used": 5}
                return state

        @register_phase
        class _MockValidate(Phase):
            name = "_ws1_validate"
            def run(self, state):
                passed = next(validate_iter, False)
                if not passed:
                    state.validation = {"passed": False, "errors": ["script error"]}
                    state.errors.append("validate: validation failed")
                    state.failed = True
                else:
                    state.validation = {"passed": True, "errors": []}
                return state

        return _MockWork, _MockValidate

    def teardown_method(self):
        PHASE_REGISTRY.pop("_ws1_work", None)
        PHASE_REGISTRY.pop("_ws1_validate", None)

    def test_repair_not_triggered_on_success(self, tmp_path):
        calls = []
        self._register_phases(calls, [True])
        state = _make_state(tmp_path)
        final = run_pipeline(
            ["_ws1_work", "_ws1_validate"],
            state,
            config={"data_dir": tmp_path, "max_repair_attempts": 1},
            log_fn=lambda _: None,
        )
        assert final.failed is False
        assert final.repair_attempts == 0
        assert len(calls) == 1  # work ran once

    def test_repair_triggered_on_first_validation_failure(self, tmp_path):
        calls = []
        # first validate fails, repair validate passes
        self._register_phases(calls, [False, True])
        state = _make_state(tmp_path)
        final = run_pipeline(
            ["_ws1_work", "_ws1_validate"],
            state,
            config={"data_dir": tmp_path, "max_repair_attempts": 1},
            log_fn=lambda _: None,
        )
        assert final.failed is False
        assert final.repair_attempts == 1
        assert len(calls) == 2  # work ran twice (original + repair)

    def test_repair_exhausted_sets_failed(self, tmp_path):
        calls = []
        # all validates fail
        self._register_phases(calls, [False, False])
        state = _make_state(tmp_path)
        final = run_pipeline(
            ["_ws1_work", "_ws1_validate"],
            state,
            config={"data_dir": tmp_path, "max_repair_attempts": 1},
            log_fn=lambda _: None,
        )
        assert final.failed is True
        assert final.repair_attempts == 1
        assert len(calls) == 2

    def test_repair_attempts_zero_skips_repair(self, tmp_path):
        calls = []
        self._register_phases(calls, [False])
        state = _make_state(tmp_path)
        final = run_pipeline(
            ["_ws1_work", "_ws1_validate"],
            state,
            config={"data_dir": tmp_path, "max_repair_attempts": 0},
            log_fn=lambda _: None,
        )
        assert final.failed is True
        assert final.repair_attempts == 0
        assert len(calls) == 1  # work ran only once, no repair

    def test_repair_count_in_artifact(self, tmp_path):
        calls = []
        self._register_phases(calls, [False, True])
        state = _make_state(tmp_path)
        state.task_id = "task-repair-artifact"
        run_pipeline(
            ["_ws1_work", "_ws1_validate"],
            state,
            config={"data_dir": tmp_path, "max_repair_attempts": 1},
            log_fn=lambda _: None,
        )
        # Check artifact written during repair contains repair count
        artifacts = list(tmp_path.glob("agent_task-repair-artifact_repair_*.json"))
        assert artifacts, "No repair artifacts written"
        data = json.loads(artifacts[0].read_text())
        assert data["repair_attempts"] == 1
