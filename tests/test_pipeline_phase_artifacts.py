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
