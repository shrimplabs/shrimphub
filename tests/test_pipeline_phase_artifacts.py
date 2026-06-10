import json
from unittest.mock import patch

from swarm.pipeline import TaskState, run_pipeline
from swarm.phases.scout import _build_scout_prompt, _extract_findings_from_history
from swarm.phases.synthesize import _build_synthesize_prompt
from swarm.tool_dispatch import validate_tool_call


def test_run_pipeline_writes_phase_artifacts(tmp_path):
    (tmp_path / "GAME_DESIGN.md").write_text("Build the best tiny game.\n")
    state = TaskState(
        task_id="task-1",
        task_type="feature",
        project="proj",
        description="Do it",
        project_path=str(tmp_path),
        workspace=str(tmp_path),
    )
    plan_response = json.dumps({
        "goal": "Do it well",
        "constraints": ["Keep docs in mind"],
        "success_criteria": ["It works"],
        "unknowns": ["Which file owns it?"],
        "risk_areas": ["main.gd"],
        "files_to_inspect_first": ["GAME_DESIGN.md"],
        "likely_files_to_change": ["scripts/main.gd"],
        "implementation_steps": ["Read docs", "Patch main"],
        "test_plan": ["Run validation"],
        "stop_conditions": ["Missing project files"],
        "scope": "small",
        "fast_path": False,
    })

    with patch("swarm.llm_utils.call_llm", return_value=(plan_response, {}, [])) as call:
        final = run_pipeline(["plan"], state, config={"data_dir": tmp_path}, log_fn=lambda _msg: None)

    artifact = tmp_path / "agent_task-1_plan.json"
    assert artifact.exists()
    data = json.loads(artifact.read_text())
    assert data["task_id"] == "task-1"
    assert data["phase"] == "plan"
    assert data["plan"] == final.plan
    assert data["plan"]["implementation_steps"] == ["Read docs", "Patch main"]

    user_msg = call.call_args[0][1][0]["content"]
    assert "GAME_DESIGN.md" in user_msg
    assert "Build the best tiny game." in user_msg


def test_plan_phase_can_use_read_only_tool_before_plan(tmp_path):
    (tmp_path / "README.md").write_text("Important repo context.\n")
    state = TaskState(
        task_id="task-2",
        task_type="feature",
        project="proj",
        description="Plan with tools",
        project_path=str(tmp_path),
        workspace=str(tmp_path),
    )
    plan_response = json.dumps({
        "goal": "Use inspected context",
        "constraints": [],
        "success_criteria": ["context was read"],
        "unknowns": [],
        "risk_areas": [],
        "files_to_inspect_first": ["README.md"],
        "likely_files_to_change": [],
        "implementation_steps": ["Apply the context"],
        "test_plan": ["Run the relevant check"],
        "stop_conditions": [],
        "scope": "small",
        "fast_path": False,
    })
    responses = [
        ('[TOOL_CALL]{"tool": "read_file", "args": {"path": "README.md"}}[/TOOL_CALL]', {}, []),
        ("PLAN_COMPLETE\n" + plan_response, {}, []),
    ]

    with patch("swarm.llm_utils.call_llm", side_effect=responses) as call:
        final = run_pipeline(["plan"], state, config={"data_dir": tmp_path}, log_fn=lambda _msg: None)

    assert call.call_count == 2
    assert final.plan["goal"] == "Use inspected context"
    artifact = json.loads((tmp_path / "agent_task-2_plan.json").read_text())
    assert artifact["plan"]["success_criteria"] == ["context was read"]


def test_plan_phase_rejects_generic_fallback_plan(tmp_path):
    state = TaskState(
        task_id="task-empty-plan",
        task_type="feature",
        project="proj",
        description="Build a real feature",
        project_path=str(tmp_path),
        workspace=str(tmp_path),
    )
    empty_plan = json.dumps({
        "goal": "Build a real feature",
        "constraints": [],
        "success_criteria": ["task completes without errors"],
        "unknowns": [],
        "risk_areas": [],
        "files_to_inspect_first": [],
        "likely_files_to_change": [],
        "implementation_steps": [],
        "test_plan": [],
        "stop_conditions": [],
        "scope": "medium",
        "fast_path": False,
    })

    with patch("swarm.llm_utils.call_llm", return_value=("PLAN_COMPLETE\n" + empty_plan, {}, [])):
        final = run_pipeline(["plan"], state, config={"data_dir": tmp_path}, log_fn=lambda _msg: None)

    assert final.failed is True
    assert any("could not produce a concrete plan" in error for error in final.errors)
    assert not (tmp_path / "agent_task-empty-plan_plan.json").exists()


def test_scout_prompt_uses_canonical_tools_and_rich_plan():
    state = TaskState(project="proj", project_path="/tmp/proj", description="Do it")
    state.plan = {
        "goal": "Build movement",
        "success_criteria": ["Movement works"],
        "constraints": ["Do not edit addons"],
        "files_to_inspect_first": ["scripts/player.gd"],
        "likely_files_to_change": ["scripts/main.gd"],
        "implementation_steps": ["Inspect player", "Patch main"],
        "unknowns": ["Input map"],
        "risk_areas": ["HUD"],
    }

    prompt = _build_scout_prompt(state)

    assert "list_files" in prompt
    assert "search_code" in prompt
    assert "list_dir" not in prompt
    assert "search_files" not in prompt
    assert "Movement works" in prompt
    assert "scripts/player.gd" in prompt
    assert "Patch main" in prompt


def test_scout_fallback_extracts_files_from_tool_calls():
    messages = [
        {
                "role": "assistant",
                "content": (
                    "Inspecting player movement ownership before implementation.\n"
                    '[TOOL_CALL]{"tool": "read_file", "args": {"path": "scripts/player.gd"}}[/TOOL_CALL]'
                ),
        }
    ]

    report = _extract_findings_from_history(messages)

    assert report["files_inspected"] == ["scripts/player.gd"]
    assert any("Inspecting player" in finding for finding in report["findings"])


def test_synthesize_prompt_includes_rich_plan():
    state = TaskState(project="proj", project_path="/tmp/proj", description="Do it")
    state.plan = {
        "goal": "Build movement",
        "success_criteria": ["Movement works"],
        "constraints": ["Do not edit addons"],
        "implementation_steps": ["Inspect player", "Patch main"],
        "likely_files_to_change": ["scripts/main.gd"],
        "test_plan": ["Run Godot check"],
    }
    state.scout_report = {
        "findings": ["main.gd is stubbed"],
        "hypotheses": [],
        "recommended_actions": [],
        "files_inspected": ["scripts/main.gd"],
    }

    prompt = _build_synthesize_prompt(state)

    assert "CONSTRAINTS" in prompt
    assert "Do not edit addons" in prompt
    assert "PLAN IMPLEMENTATION STEPS" in prompt
    assert "Patch main" in prompt
    assert "PLAN TESTS" in prompt
    assert "Run Godot check" in prompt


def test_legacy_tool_aliases_still_validate():
    assert validate_tool_call({"tool": "list_dir", "args": {"path": "."}}) == ""
    assert validate_tool_call({"tool": "search_files", "args": {"query": "Player"}}) == ""


# ===========================================================================
# WS4 — Infrastructure exception classification and traceback capture
# ===========================================================================

class TestInfrastructureExceptionClassification:
    """run_pipeline: phase exceptions are classified and captured with full traceback."""

    def _make_state(self, tmp_path):
        return TaskState(
            task_id="task-ws4",
            task_type="feature",
            project="proj",
            description="Test infra exceptions",
            project_path=str(tmp_path),
            workspace=str(tmp_path),
        )

    def test_str_div_str_classified_as_infrastructure(self, tmp_path):
        """The original str/str TypeError is classified as infrastructure_exception."""
        from swarm.pipeline import _classify_exception
        exc = TypeError("unsupported operand type(s) for /: 'str' and 'str'")
        assert _classify_exception(exc) == "infrastructure_exception"

    def test_timeout_classified_as_provider_timeout(self):
        from swarm.pipeline import _classify_exception
        exc = TimeoutError("read timed out")
        assert _classify_exception(exc) == "provider_timeout"

    def test_validation_failure_classified_correctly(self):
        from swarm.pipeline import _classify_exception
        exc = RuntimeError("validation failed: script errors found")
        assert _classify_exception(exc) == "project_validation"

    def test_loop_exhaustion_classified_correctly(self):
        from swarm.pipeline import _classify_exception
        exc = RuntimeError("hit loop limit without WORK_COMPLETE")
        assert _classify_exception(exc) == "agent_loop_exhausted"

    def test_phase_exception_captured_in_state(self, tmp_path):
        """When a phase raises, state.failure_kind/phase/traceback are populated."""
        from swarm.pipeline import Phase, TaskState, register_phase, run_pipeline, PHASE_REGISTRY

        @register_phase
        class _BoomPhase(Phase):
            name = "_test_boom_ws4"

            def run(self, state):
                raise TypeError("unsupported operand type(s) for /: 'str' and 'str'")

        try:
            state = self._make_state(tmp_path)
            final = run_pipeline(
                ["_test_boom_ws4"],
                state,
                config={"data_dir": tmp_path},
                log_fn=lambda _msg: None,
            )

            assert final.failed is True
            assert final.failure_kind == "infrastructure_exception"
            assert final.failure_phase == "_test_boom_ws4"
            assert final.failure_exception_class == "TypeError"
            assert "str" in final.failure_traceback
        finally:
            PHASE_REGISTRY.pop("_test_boom_ws4", None)

    def test_phase_exception_written_to_artifact(self, tmp_path):
        """failure_kind and traceback appear in the JSON artifact on disk."""
        from swarm.pipeline import Phase, TaskState, register_phase, run_pipeline, PHASE_REGISTRY

        @register_phase
        class _BoomPhase2(Phase):
            name = "_test_boom_ws4_artifact"

            def run(self, state):
                raise AttributeError("'NoneType' object has no attribute 'split'")

        try:
            state = self._make_state(tmp_path)
            state.task_id = "task-ws4-artifact"
            run_pipeline(
                ["_test_boom_ws4_artifact"],
                state,
                config={"data_dir": tmp_path},
                log_fn=lambda _msg: None,
            )

            artifact = tmp_path / "agent_task-ws4-artifact__test_boom_ws4_artifact.json"
            assert artifact.exists(), f"Artifact not found at {artifact}"
            data = json.loads(artifact.read_text())
            assert data["failure_kind"] == "infrastructure_exception"
            assert data["failure_exception_class"] == "AttributeError"
            assert data["failure_traceback"] != ""
            assert data["failed"] is True
        finally:
            PHASE_REGISTRY.pop("_test_boom_ws4_artifact", None)


class TestWorkspacePathCoercion:
    """tool_dispatch.execute_tool: WORKSPACE as str must not cause str/str TypeError."""

    def test_workspace_str_does_not_raise_on_path_op(self, tmp_path):
        """Regression: execute_tool must coerce str WORKSPACE to Path before / operator."""
        import swarm.agent_runtime as rt
        orig_workspace = rt.WORKSPACE
        orig_project = rt.PROJECT

        proj_dir = tmp_path / "myproj"
        proj_dir.mkdir()
        (proj_dir / "test.gd").write_text("extends Node\n")

        try:
            rt.WORKSPACE = str(tmp_path)   # string, not Path — triggers the original bug
            rt.PROJECT = "myproj"
            rt._sync_core_globals()

            from swarm.tool_dispatch import execute_tool
            # list_files triggers workspace / project path construction
            result = execute_tool({"tool": "list_files", "args": {"path": "."}})
            # Should not raise TypeError — result is whatever list_files returns
            assert result is not None
        finally:
            rt.WORKSPACE = orig_workspace
            rt.PROJECT = orig_project
            rt._sync_core_globals()
