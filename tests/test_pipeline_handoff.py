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


class TestDiagnoseHandoff:
    """Regression: DiagnosePhase must surface root_cause and recommended_fix into
    state.handoff so the work phase (which reads handoff.hypotheses and
    handoff.next_actions) receives the diagnose findings."""

    def test_diagnose_root_cause_injected_into_handoff_hypotheses(self, tmp_path):
        from swarm.phases.diagnose import DiagnosePhase

        state = TaskState(
            task_id="diag-hw",
            task_type="bug",
            project="proj",
            description="Fix crash on game over",
            project_path=str(tmp_path),
            plan=_base_plan(),
            scout_report=_base_scout_report(),
        )

        diagnose_json = json.dumps({
            "root_cause": "ScoreLabel node freed before game_over() fires",
            "files_inspected": ["hud.gd", "main.gd"],
            "exact_failure": "null reference on hud.score_label at line 87",
            "recommended_fix": "Guard with is_instance_valid(score_label) before updating",
            "confidence": 0.9,
        })
        diagnose_response = "DIAGNOSE_COMPLETE\n" + diagnose_json

        with patch("swarm.phases.diagnose.call_llm", return_value=(diagnose_response, {}, [])):
            phase = DiagnosePhase(config={"data_dir": str(tmp_path)})
            result = phase.run(state)

        hypotheses = result.handoff.get("hypotheses", [])
        # root_cause → handoff.hypotheses
        assert any("ScoreLabel node freed" in h for h in hypotheses), (
            f"root_cause not in handoff.hypotheses: {hypotheses}"
        )
        # recommended_fix → scout_report.recommended_actions (rendered as SCOUT RECOMMENDED ACTIONS in work)
        scout_actions = result.scout_report.get("recommended_actions", [])
        assert any("is_instance_valid" in a for a in scout_actions), (
            f"recommended_fix not prepended to scout_report.recommended_actions: {scout_actions}"
        )

    def test_diagnose_findings_visible_in_work_prompt(self, tmp_path):
        """End-to-end: work prompt built after diagnose must contain the root_cause."""
        from swarm.phases.diagnose import DiagnosePhase

        state = TaskState(
            task_id="diag-work",
            task_type="bug",
            project="proj",
            description="Fix crash on game over",
            project_path=str(tmp_path),
            plan=_base_plan(),
            scout_report=_base_scout_report(),
        )

        diagnose_json = json.dumps({
            "root_cause": "ScoreLabel freed before signal fires",
            "files_inspected": ["hud.gd"],
            "exact_failure": "null ref at hud.gd:87",
            "recommended_fix": "Use is_instance_valid before update",
            "confidence": 0.88,
        })
        with patch("swarm.phases.diagnose.call_llm",
                   return_value=("DIAGNOSE_COMPLETE\n" + diagnose_json, {}, [])):
            phase = DiagnosePhase(config={"data_dir": str(tmp_path)})
            state = phase.run(state)

        prompt = _build_work_prompt(state)

        assert "ScoreLabel freed before signal fires" in prompt, (
            "diagnose root_cause must appear in work prompt via handoff.hypotheses"
        )
        assert "is_instance_valid" in prompt, (
            "diagnose recommended_fix must appear in work prompt via handoff.next_actions"
        )
