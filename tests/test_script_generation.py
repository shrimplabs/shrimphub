"""
Tests for generate_task_script() in swarm_runner.py.

This is a pure function — no subprocess or network I/O required.
"""
import ast
import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(task_type="feature", project="my-game", description="Do something",
               task_id="task-001", metadata=None):
    return {
        "id": task_id,
        "project": project,
        "type": task_type,
        "description": description,
        "metadata": metadata or {},
    }


def _gen(task, workspace=None, is_python=False):
    """Call generate_task_script with optional workspace override."""
    from swarm_runner import generate_task_script
    import swarm_runner as sr

    orig_workspace = sr.WORKSPACE
    if workspace:
        sr.WORKSPACE = workspace
    try:
        return generate_task_script(task)
    finally:
        sr.WORKSPACE = orig_workspace


# ---------------------------------------------------------------------------
# Syntax validity
# ---------------------------------------------------------------------------

class TestSyntaxValidity:
    def test_output_is_valid_python(self, tmp_path):
        source = _gen(_make_task(), workspace=tmp_path)
        # compile() raises SyntaxError on bad syntax
        compile(source, "<generated>", "exec")

    def test_output_is_valid_for_all_task_types(self, tmp_path):
        for task_type in ("feature", "bug", "polish", "refactor"):
            source = _gen(_make_task(task_type=task_type), workspace=tmp_path)
            compile(source, f"<{task_type}>", "exec")

    def test_shebang_line_present(self, tmp_path):
        source = _gen(_make_task(), workspace=tmp_path)
        assert source.startswith("#!/usr/bin/env python3")


# ---------------------------------------------------------------------------
# Config values embedded in script
# ---------------------------------------------------------------------------

class TestEmbeddedConfig:
    def test_task_id_embedded(self, tmp_path):
        source = _gen(_make_task(task_id="my-task-42"), workspace=tmp_path)
        assert "my-task-42" in source

    def test_task_metadata_embedded(self, tmp_path):
        source = _gen(
            _make_task(metadata={"experiment_id": "exp-123", "pipeline": [], "is_valid_order": True}),
            workspace=tmp_path,
        )
        compile(source, "<generated>", "exec")
        assert (
            "rt.TASK_METADATA    = {'experiment_id': 'exp-123', "
            "'pipeline': [], 'is_valid_order': True}"
        ) in source

    def test_project_name_embedded(self, tmp_path):
        source = _gen(_make_task(project="space-game"), workspace=tmp_path)
        assert "space-game" in source

    def test_task_type_embedded(self, tmp_path):
        source = _gen(_make_task(task_type="bug"), workspace=tmp_path)
        assert "rt.TASK_TYPE" in source
        assert "bug" in source

    def test_description_embedded(self, tmp_path):
        source = _gen(_make_task(description="Fix the player jump"), workspace=tmp_path)
        assert "Fix the player jump" in source

    def test_llm_provider_embedded(self, tmp_path):
        import swarm_runner as sr
        source = _gen(_make_task(), workspace=tmp_path)
        assert "rt.LLM_PROVIDER" in source
        assert sr.LLM_PROVIDER in source

    def test_max_lines_embedded(self, tmp_path):
        import swarm_runner as sr
        source = _gen(_make_task(), workspace=tmp_path)
        assert str(sr.MAX_LINES) in source

    def test_imports_agent_runtime(self, tmp_path):
        source = _gen(_make_task(), workspace=tmp_path)
        assert "import swarm.agent_runtime as rt" in source

    def test_calls_rt_main(self, tmp_path):
        source = _gen(_make_task(), workspace=tmp_path)
        assert "rt.main()" in source

    def test_shared_knowledge_is_not_auto_injected(self, tmp_path):
        import swarm_runner as sr

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "shared_knowledge.md").write_text(
            "[other-project] stale implementation note that must not enter prompts\n"
        )

        old_data_dir = sr.DATA_DIR
        sr.DATA_DIR = data_dir
        try:
            source = _gen(
                _make_task(project="clean-project", description="Implement clean feature"),
                workspace=tmp_path,
            )
        finally:
            sr.DATA_DIR = old_data_dir

        assert "Implement clean feature" in source
        assert "stale implementation note" not in source
        assert "SHARED KNOWLEDGE:" not in source

    def test_writes_exit_file(self, tmp_path):
        source = _gen(_make_task(), workspace=tmp_path)
        assert ".exit" in source

    def test_main_guard_present(self, tmp_path):
        source = _gen(_make_task(), workspace=tmp_path)
        assert '__name__ == "__main__"' in source

    def test_feature_prompt_includes_project_intent_doc_guidance(self, tmp_path):
        source = _gen(_make_task(task_type="feature", project="intent-proj"), workspace=tmp_path)
        assert "GAME_DESIGN.md" in source
        assert "PROJECT_CLOSURE.md" in source
        assert "Investigate broadly and deliberately." in source
        assert "Assume the first obvious file may not be the whole story." in source

    def test_bug_prompt_includes_project_intent_doc_guidance(self, tmp_path):
        source = _gen(_make_task(task_type="bug", project="intent-proj"), workspace=tmp_path)
        assert "GAME_DESIGN.md" in source
        assert "PROJECT_CLOSURE.md" in source
        assert "use them to understand the intended behavior before debugging" in source

    def test_feature_prompt_can_render_baseline_intent_variant(self, tmp_path):
        import swarm_runner as sr
        old_variant = sr.PROJECT_INTENT_PROMPT_VARIANT
        sr.PROJECT_INTENT_PROMPT_VARIANT = "baseline"
        try:
            source = _gen(_make_task(task_type="feature", project="intent-proj"), workspace=tmp_path)
        finally:
            sr.PROJECT_INTENT_PROMPT_VARIANT = old_variant
        assert "Look for nearby systems, tests, configs, scenes/routes, and existing patterns" in source
        assert "Investigate broadly and deliberately." not in source

    def test_feature_prompt_default_variant_is_exploratory(self, tmp_path):
        source = _gen(_make_task(task_type="feature", project="intent-proj"), workspace=tmp_path)
        assert "Investigate broadly and deliberately." in source

    def test_feature_prompt_injects_project_context_packet(self, tmp_path):
        import swarm_runner as sr

        proj_dir = tmp_path / "ctx-proj"
        proj_dir.mkdir()
        (proj_dir / "GAME_DESIGN.md").write_text(
            "# ctx-proj\n\n## Overview\n\nBuild a neon blob arena where the player spawns blobs and survives enemy waves.\n\n"
            "## Quality Gates\n\n- gut --run --exit\n- Main scene loads without parse errors.\n"
        )
        (proj_dir / "PROJECT_CLOSURE.md").write_text("# ctx-proj Closure Contract\n")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "task-history.jsonl").write_text(
            json.dumps({
                "id": "ctx-proj-t01",
                "project": "ctx-proj",
                "type": "feature",
                "status": "completed",
                "description": "Arena scene and camera framing",
            }) + "\n"
        )

        task = _make_task(
            task_type="feature",
            project="ctx-proj",
            task_id="ctx-proj-t02",
            description="Spawn player blobs on click",
        )
        dep_task = _make_task(
            task_type="feature",
            project="ctx-proj",
            task_id="ctx-proj-t01",
            description="Arena scene and camera framing",
        )
        live_tasks = [
            {
                **dep_task,
                "status": "completed",
                "completed": "2026-05-02T12:00:00",
                "dependencies": [],
            },
            {
                **task,
                "status": "pending",
                "dependencies": ["ctx-proj-t01"],
            },
            {
                "id": "ctx-proj-t03",
                "project": "ctx-proj",
                "type": "feature",
                "description": "Blob AI seeks food",
                "status": "pending",
                "dependencies": ["ctx-proj-t02"],
            },
        ]
        project_row = {
            "name": "ctx-proj",
            "profile": "godot",
            "closure_mode": "stabilize",
            "closure_status": "yellow",
            "closure_spec": {
                "mode": "stabilize",
                "gates": {"boot_ok": True, "tests_ok": True},
                "critical_flows": [
                    {"id": "spawn-loop", "description": "Spawn blobs, survive a wave, and keep total mass rising."}
                ],
            },
        }

        old_data_dir = sr.DATA_DIR
        sr.DATA_DIR = data_dir
        try:
            with patch("swarm.db.task_get", return_value=task), \
                 patch("swarm.db.project_get", return_value=project_row), \
                 patch("swarm.db.task_get_by_project", return_value=live_tasks), \
                 patch("swarm.db.verification_run_list_by_project", return_value=[{
                     "results_json": {
                         "boot_ok": True,
                         "tests_ok": False,
                         "smoke_ok": True,
                         "critical_flows": {"spawn-loop": True},
                         "errors": [],
                     }
                 }]):
                source = _gen(task, workspace=tmp_path)
        finally:
            sr.DATA_DIR = old_data_dir

        assert "## PROJECT CONTEXT PACKET" in source
        assert "Project goal: Build a neon blob arena where the player spawns blobs and survives enemy waves." in source
        assert "Intent docs: GAME_DESIGN.md, PROJECT_CLOSURE.md" in source
        assert "depends on ctx-proj-t01 (Arena scene and camera framing)" in source
        assert "unlocks ctx-proj-t03: Blob AI seeks food" in source
        assert "critical flows: Spawn blobs, survive a wave, and keep total mass rising." in source
        assert "gut --run --exit" in source
        assert "boot_ok=True" in source
        assert "tests_ok=False" in source
        assert "critical_flows_passed=1/1" in source
        assert "Recent completed tasks:" in source
        assert "ctx-proj-t01: Arena scene and camera framing" in source

    def test_feature_prompt_filters_low_signal_recent_task_memory(self, tmp_path):
        import swarm_runner as sr

        proj_dir = tmp_path / "ctx-proj"
        proj_dir.mkdir()
        (proj_dir / "GAME_DESIGN.md").write_text("# ctx-proj\n\n## Overview\n\nBuild a test project.\n")
        task = _make_task(
            task_type="feature",
            project="ctx-proj",
            task_id="ctx-proj-t02",
            description="Build the next feature",
        )
        live_tasks = [
            {
                "id": "ctx-proj-noisy",
                "project": "ctx-proj",
                "type": "bug",
                "description": "VALIDATION BUG TASK:\n\nFix smoke check",
                "status": "completed",
                "completed": "2026-05-02T12:10:00",
                "dependencies": [],
            },
            {
                "id": "ctx-proj-good",
                "project": "ctx-proj",
                "type": "feature",
                "description": "Maze scene foundation",
                "status": "completed",
                "completed": "2026-05-02T12:00:00",
                "dependencies": [],
            },
            {
                **task,
                "status": "pending",
                "dependencies": [],
            },
        ]
        project_row = {
            "name": "ctx-proj",
            "profile": "godot",
            "closure_mode": "stabilize",
            "closure_status": "yellow",
            "closure_spec": {"mode": "stabilize", "critical_flows": []},
        }

        with patch("swarm.db.task_get", return_value=task), \
             patch("swarm.db.project_get", return_value=project_row), \
             patch("swarm.db.task_get_by_project", return_value=live_tasks), \
             patch("swarm.db.verification_run_list_by_project", return_value=[]):
            source = _gen(task, workspace=tmp_path)

        assert "ctx-proj-good: Maze scene foundation" in source
        assert "ctx-proj-noisy: VALIDATION BUG TASK" not in source


# ---------------------------------------------------------------------------
# Godot vs Python project detection
# ---------------------------------------------------------------------------

def _primary_feature_system(source: str) -> str:
    """Extract the value assigned to rt.FEATURE_SYSTEM (the primary prompt line)."""
    idx = source.index("rt.FEATURE_SYSTEM")
    # Grab ~300 chars starting at the assignment — enough to distinguish prompts
    return source[idx:idx + 300]


class TestProjectTypeDetection:
    def test_godot_project_uses_godot_prompts(self, tmp_path):
        # No requirements.txt or pyproject.toml → Godot project
        proj_dir = tmp_path / "my-game"
        proj_dir.mkdir()
        source = _gen(_make_task(project="my-game"), workspace=tmp_path)
        # Primary FEATURE_SYSTEM should be the Godot prompt
        assert "Godot" in _primary_feature_system(source)

    def test_python_project_via_requirements_txt(self, tmp_path):
        proj_dir = tmp_path / "my-game"
        proj_dir.mkdir()
        (proj_dir / "requirements.txt").write_text("flask\n")
        source = _gen(_make_task(project="my-game"), workspace=tmp_path)
        # Primary FEATURE_SYSTEM should be the Python prompt
        assert "Python developer" in _primary_feature_system(source)

    def test_python_project_via_pyproject_toml(self, tmp_path):
        proj_dir = tmp_path / "my-game"
        proj_dir.mkdir()
        (proj_dir / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        source = _gen(_make_task(project="my-game"), workspace=tmp_path)
        assert "Python developer" in _primary_feature_system(source)

    def test_nonexistent_project_dir_uses_godot_prompts(self, tmp_path):
        # project dir doesn't exist → defaults to Godot
        source = _gen(_make_task(project="ghost-project"), workspace=tmp_path)
        assert "Godot" in _primary_feature_system(source)

    def test_godot_path_is_embedded_in_generated_prompts(self, tmp_path, monkeypatch):
        godot = tmp_path / "Godot With Spaces.exe"
        godot.write_text("")
        godot.chmod(0o755)
        monkeypatch.setenv("GODOT_PATH", str(godot))

        source = _gen(_make_task(project="my-game"), workspace=tmp_path)

        assert str(godot).replace("\\", "\\\\") in source
        assert "Godot executable configured by the swarm controller" in source
        assert "--headless --path" in source

    def test_missing_godot_tells_agent_to_report_config_blocker(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GODOT_PATH", raising=False)
        with patch("swarm_runner.resolve_godot_binary", return_value="godot"), \
             patch("swarm_runner.godot_binary_available", return_value=False):
            source = _gen(_make_task(project="my-game"), workspace=tmp_path)

        assert "Use the controller configuration as the source of truth" in source
        assert "configuration blocker" in source


# ---------------------------------------------------------------------------
# Python project: correct prompt selection per task type
# ---------------------------------------------------------------------------

class TestPythonPromptSelection:
    def _python_task(self, task_type, tmp_path):
        proj_dir = tmp_path / "pyproj"
        proj_dir.mkdir(exist_ok=True)
        (proj_dir / "requirements.txt").write_text("flask\n")
        return _gen(_make_task(task_type=task_type, project="pyproj"), workspace=tmp_path)

    def test_python_feature_uses_python_prompt(self, tmp_path):
        source = self._python_task("feature", tmp_path)
        # Primary FEATURE_SYSTEM should be the Python prompt, not Godot
        assert "Python developer" in _primary_feature_system(source)
        assert "Godot" not in _primary_feature_system(source)

    def test_python_bug_uses_python_prompt(self, tmp_path):
        source = self._python_task("bug", tmp_path)
        # For bug tasks, check rt.BUG_SYSTEM is the Python version
        idx = source.index("rt.BUG_SYSTEM")
        snippet = source[idx:idx + 300]
        assert "Python developer" in snippet
        assert "Godot" not in snippet


# ---------------------------------------------------------------------------
# Retry context injection
# ---------------------------------------------------------------------------

class TestRetryContext:
    def test_retry_prefix_added_when_last_failure_set(self, tmp_path):
        task = _make_task(metadata={
            "last_failure": "SyntaxError: unexpected EOF",
            "failure_attempt": 2,
        })
        source = _gen(task, workspace=tmp_path)
        assert "// RETRY CONTEXT" in source
        assert "previous_error" in source
        assert "SyntaxError" in source

    def test_attempt_number_in_prefix(self, tmp_path):
        task = _make_task(metadata={
            "last_failure": "some error",
            "failure_attempt": 3,
        })
        source = _gen(task, workspace=tmp_path)
        assert '"attempt": 3' in source

    def test_no_retry_prefix_when_no_failure_metadata(self, tmp_path):
        task = _make_task(metadata={})
        source = _gen(task, workspace=tmp_path)
        assert "// RETRY CONTEXT" not in source

    def test_no_retry_prefix_when_failure_attempt_is_zero(self, tmp_path):
        task = _make_task(metadata={"last_failure": "error", "failure_attempt": 0})
        source = _gen(task, workspace=tmp_path)
        assert "// RETRY CONTEXT" not in source
