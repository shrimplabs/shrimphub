"""Tests for WS5: normalized handoff artifacts between pipeline phases."""

import json
import pytest
from unittest.mock import patch

from swarm.pipeline import TaskState, run_pipeline, Phase, register_phase, PHASE_REGISTRY
from swarm.phases.work import _build_work_prompt


def _base_plan():
    return {
        "goal": "Add score display",
        "constraints": ["No new dependencies"],
        "success_criteria": ["Score renders in HUD"],
        "unknowns": ["Which scene owns the HUD?"],
        "risk_areas": ["hud.gd"],
        "files_to_inspect_first": ["hud.gd"],
        "likely_files_to_change": ["hud.gd", "main.gd"],
        "implementation_steps": ["Edit hud.gd"],
        "test_plan": ["Run validation"],
        "scope": "small",
        "fast_path": False,
    }


def _base_scout_report():
    return {
        "files_inspected": ["hud.gd", "main.gd"],
        "findings": ["HUD node exists", "Score label missing"],
        "hypotheses": ["Need to add Label node to HUD"],
        "recommended_actions": ["Add Label to hud.gd", "Wire score signal"],
        "confidence": 0.8,
    }


class TestHandoffFieldOnTaskState:
    def test_handoff_defaults_to_empty_dict(self):
        state = TaskState()
        assert state.handoff == {}

    def test_handoff_in_pipeline_artifact(self, tmp_path):
        """Artifact written after a phase includes the handoff dict."""
        scout_json = json.dumps({
            "files_inspected": ["hud.gd"],
            "findings": ["Label missing"],
            "hypotheses": ["Add label"],
            "recommended_actions": ["Patch hud.gd"],
            "confidence": 0.7,
        })
        scout_response = "SCOUT_COMPLETE\n" + scout_json

        state = TaskState(
            task_id="task-ws5",
            task_type="feature",
            project="proj",
            description="Add score",
            project_path=str(tmp_path),
            workspace=str(tmp_path),
            plan=_base_plan(),
        )

        with patch("swarm.phases.scout.call_llm", return_value=(scout_response, {}, [])):
            final = run_pipeline(
                ["scout"],
                state,
                config={"data_dir": str(tmp_path)},
                log_fn=lambda _: None,
            )

        artifact = tmp_path / "agent_task-ws5_scout.json"
        assert artifact.exists()
        data = json.loads(artifact.read_text())
        assert "handoff" in data
        assert data["handoff"]["goal"] == "Add score display"
        assert "hud.gd" in data["handoff"]["files_inspected"]
        assert "Label missing" in data["handoff"]["facts"]
        assert "Add label" in data["handoff"]["hypotheses"]


class TestWorkPromptIncludesHandoff:
    def test_hypotheses_from_handoff_in_work_prompt(self):
        state = TaskState(
            task_id="ws5-work",
            task_type="feature",
            project="proj",
            description="Add score",
            project_path="/tmp/proj",
            plan=_base_plan(),
            scout_report=_base_scout_report(),
            handoff={
                "goal": "Add score display",
                "facts": ["HUD node exists"],
                "files_inspected": ["hud.gd"],
                "files_to_modify": ["hud.gd"],
                "known_failures": [],
                "constraints": ["No new dependencies"],
                "next_actions": ["Add Label to hud.gd"],
                "unknowns": ["Which scene owns the HUD?"],
                "hypotheses": ["Need to add Label node to HUD"],
            },
        )
        prompt = _build_work_prompt(state)

        assert "HYPOTHESES (from scout):" in prompt
        assert "Need to add Label node to HUD" in prompt

    def test_known_failures_from_handoff_in_work_prompt(self):
        state = TaskState(
            task_id="ws5-repair",
            task_type="feature",
            project="proj",
            description="Add score",
            project_path="/tmp/proj",
            plan=_base_plan(),
            scout_report=_base_scout_report(),
            handoff={
                "goal": "Add score display",
                "facts": [],
                "files_inspected": [],
                "files_to_modify": [],
                "known_failures": ["script error at hud.gd:42 — unexpected token"],
                "constraints": [],
                "next_actions": [],
                "unknowns": [],
                "hypotheses": [],
            },
        )
        prompt = _build_work_prompt(state)

        assert "KNOWN FAILURES FROM PRIOR RUNS:" in prompt
        assert "script error at hud.gd:42" in prompt

    def test_empty_handoff_does_not_add_sections(self):
        state = TaskState(
            task_id="ws5-empty",
            task_type="feature",
            project="proj",
            description="Add score",
            project_path="/tmp/proj",
            plan=_base_plan(),
            scout_report=_base_scout_report(),
            handoff={},
        )
        prompt = _build_work_prompt(state)

        assert "HYPOTHESES (from scout):" not in prompt
        assert "KNOWN FAILURES FROM PRIOR RUNS:" not in prompt

    def test_work_prompt_still_includes_scout_findings(self):
        """Scout findings in scout_report should still appear even when handoff is set."""
        state = TaskState(
            task_id="ws5-findings",
            task_type="feature",
            project="proj",
            description="Add score",
            project_path="/tmp/proj",
            plan=_base_plan(),
            scout_report=_base_scout_report(),
            handoff={
                "hypotheses": ["Need Label node"],
                "known_failures": [],
            },
        )
        prompt = _build_work_prompt(state)

        # Scout findings still rendered
        assert "HUD node exists" in prompt
        assert "Score label missing" in prompt
        # Handoff hypotheses also rendered
        assert "Need Label node" in prompt


class TestValidatePopulatesHandoff:
    def test_validate_phase_populates_known_failures_on_failure(self, tmp_path):
        """ValidatePhase writes validation errors into state.handoff['known_failures']."""
        from swarm.phases.validate import ValidatePhase

        state = TaskState(
            task_id="ws5-val",
            task_type="feature",
            project="proj",
            description="Add score",
            project_path=str(tmp_path),
            workspace=str(tmp_path),
            handoff={"goal": "Add score", "facts": [], "files_inspected": [],
                     "files_to_modify": [], "known_failures": [], "constraints": [],
                     "next_actions": [], "unknowns": [], "hypotheses": []},
        )

        error_output = "script error: hud.gd:10 — unexpected token"
        with patch("swarm.validation._post_task_validation_in_worktree",
                   return_value=(True, error_output)), \
             patch("swarm.db.init"):
            phase = ValidatePhase(config={"data_dir": str(tmp_path)})
            result = phase.run(state)

        assert result.failed is True
        assert "known_failures" in result.handoff
        assert any("hud.gd" in f for f in result.handoff["known_failures"])

    def test_validate_phase_clears_known_failures_on_pass(self, tmp_path):
        """ValidatePhase clears known_failures when validation passes."""
        from swarm.phases.validate import ValidatePhase

        state = TaskState(
            task_id="ws5-val-pass",
            task_type="feature",
            project="proj",
            description="Add score",
            project_path=str(tmp_path),
            workspace=str(tmp_path),
            handoff={"known_failures": ["old error from previous run"]},
        )

        with patch("swarm.validation._post_task_validation_in_worktree",
                   return_value=(False, "")), \
             patch("swarm.db.init"):
            phase = ValidatePhase(config={"data_dir": str(tmp_path)})
            result = phase.run(state)

        assert result.failed is False
        assert result.handoff["known_failures"] == []
