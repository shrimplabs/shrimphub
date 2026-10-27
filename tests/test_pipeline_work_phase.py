import json
import subprocess
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

    # "feature" is not a visual task type, so it must never get the
    # art_pass/polish visual-verification fallback text.
    assert "VISUAL VERIFICATION REQUIRED" not in prompt
    # _WORK_PROFILE_TASK_TYPES was deliberately expanded to include
    # feature/bug/refactor (commit 17412e8d) so every task type gets its own
    # contract from prompts/<type>.yaml -- feature.yaml exists, so a
    # TASK-TYPE WORK PROFILE section is expected here now.
    assert "TASK-TYPE WORK PROFILE" in prompt


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
# Roadmap #8 (M4) — work-phase adaptive routing opt-in
# ===========================================================================

def test_work_phase_default_config_uses_fixed_provider_for_every_loop(tmp_path):
    """No model_policy entry -> zero behavior change: one fixed provider for
    the whole loop, even when tool calls are read-only."""
    state = TaskState(
        task_id="feature-fixed",
        task_type="feature",
        project="unconfigured-project",
        description="Add scoring",
        project_path=str(tmp_path),
        workspace=str(tmp_path),
    )
    responses = [
        ('[TOOL_CALL]{"tool": "read_file", "args": {"path": "a.txt"}}[/TOOL_CALL]', {}, []),
        ('[TOOL_CALL]{"tool": "read_file", "args": {"path": "b.txt"}}[/TOOL_CALL]', {}, []),
        ("WORK_COMPLETE", {}, []),
    ]

    with patch("swarm.phases.work.call_llm", side_effect=responses) as call_llm:
        phase = WorkPhase(config={
            "work_provider": "minimax",
            "loop_model_routing": {"enabled": True, "fast_provider": "athena", "strong_provider": "minimax"},
        })
        phase.run(state)

    providers_used = [c.kwargs["provider"] for c in call_llm.call_args_list]
    assert providers_used == ["minimax", "minimax", "minimax"]


def test_work_phase_adaptive_opt_in_switches_provider_per_loop(tmp_path):
    """Explicit model_policy opt-in enables per-loop tool-lookahead routing
    inside the work phase's internal loop -- the new M4 capability."""
    state = TaskState(
        task_id="feature-adaptive",
        task_type="feature",
        project="adaptive-project",
        description="Add scoring",
        project_path=str(tmp_path),
        workspace=str(tmp_path),
    )
    responses = [
        ('[TOOL_CALL]{"tool": "read_file", "args": {"path": "a.txt"}}[/TOOL_CALL]', {}, []),
        ('[TOOL_CALL]{"tool": "read_file", "args": {"path": "b.txt"}}[/TOOL_CALL]', {}, []),
        ("WORK_COMPLETE", {}, []),
    ]

    config = {
        "work_provider": "minimax",
        "loop_model_routing": {"enabled": True, "fast_provider": "athena", "strong_provider": "minimax"},
        "model_policy": {
            "projects": {
                "adaptive-project": {
                    "feature": {"work": {"normal": {"tier": "either", "adaptive": True}}}
                }
            }
        },
    }

    with patch("swarm.phases.work.call_llm", side_effect=responses) as call_llm:
        phase = WorkPhase(config=config)
        result = phase.run(state)

    providers_used = [c.kwargs["provider"] for c in call_llm.call_args_list]
    # loop 1: no prior signal, first-loop intent-setting -> strong.
    # loop 2 & 3: prior loop's tool was a pure read -> dip to fast.
    assert providers_used == ["minimax", "athena", "athena"]
    assert result.work_report["tier_stats"] == {"cheap_loops": 2, "strong_loops": 1, "default_loops": 0}


def test_work_phase_adaptive_opt_in_still_forces_strong_on_write(tmp_path):
    """SAFETY: even with adaptive routing opted in, a write/commit tool call
    must still force the strong provider -- this is the same tool-lookahead
    safety net proven in test_model_routing.py, exercised end-to-end here.

    Uses task_type="feature" (not in legacy STRONG_TASK_TYPES) so the strong
    routing on loop 3 is demonstrably driven by the write_file tool signal,
    not by task-type membership.

    This exercises the REAL, unmocked tool-dispatch path (only call_llm is
    mocked) -- git_commit() genuinely runs `git add -A && git commit` via
    swarm.tools.shell.run(). tmp_path must therefore be a real, isolated git
    repo, or that call resolves its cwd via swarm.tools._shared and can hit
    whatever the test process's actual cwd is. See tests/conftest.py's
    `_isolate_tools_shared_cwd` fixture for the safety-net default; this
    still needs its own real (but isolated) repo for the commit to succeed.
    """
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "README.md").write_text("placeholder\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, capture_output=True)

    state = TaskState(
        task_id="feature-adaptive-write",
        task_type="feature",
        project="adaptive-project",
        description="Add scoring",
        project_path=str(tmp_path),
        workspace=str(tmp_path),
    )
    responses = [
        ('[TOOL_CALL]{"tool": "read_file", "args": {"path": "a.txt"}}[/TOOL_CALL]', {}, []),
        ('[TOOL_CALL]{"tool": "write_file", "args": {"path": "a.txt", "content": "x"}}[/TOOL_CALL]', {}, []),
        ('[TOOL_CALL]{"tool": "git_commit", "args": {"message": "add scoring"}}[/TOOL_CALL]', {}, []),
        ("WORK_COMPLETE", {}, []),
    ]

    config = {
        "work_provider": "minimax",
        "loop_model_routing": {"enabled": True, "fast_provider": "athena", "strong_provider": "minimax"},
        "model_policy": {
            "projects": {
                "adaptive-project": {
                    "feature": {"work": {"normal": {"tier": "either", "adaptive": True}}}
                }
            }
        },
    }

    with patch("swarm.phases.work.call_llm", side_effect=responses) as call_llm:
        phase = WorkPhase(config=config)
        phase.run(state)

    providers_used = [c.kwargs["provider"] for c in call_llm.call_args_list]
    # loop 1: first-loop intent-setting -> strong.
    # loop 2: prior tool (read_file) was read-only -> dip to fast.
    # loop 3: prior tool (write_file) is a STRONG_TOOL -> force strong,
    #         regardless of the "either" tier ceiling and non-strong task type.
    assert providers_used == ["minimax", "athena", "minimax", "minimax"]


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
