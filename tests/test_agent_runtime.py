"""
Tests for swarm/agent_runtime.py.

Covers:
  - parse_tool_calls(): extraction from LLM responses (both formats, edge cases)
  - execute_tool(): dispatch table completeness and error handling
  - Individual tool functions: read_file, write_file, list_files, run_command
  - call_llm(): provider format selection, missing key, 429 retry, error handling
  - main(): full conversation loop with mocked LLM -- the complete agent loop
"""
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import swarm.agent_runtime as rt
from swarm.tools.knowledge import read_agent_knowledge


# ---------------------------------------------------------------------------
# Fixture: reset all module-level globals before each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_rt(tmp_path):
    proj_dir = tmp_path / "workspace" / "test-proj"
    proj_dir.mkdir(parents=True)

    rt.WORKSPACE = tmp_path / "workspace"
    rt.PROJECT = "test-proj"
    rt.TASK_TYPE = "feature"
    rt.TASK_DESC = "Test task"
    rt.TASK_ID = "task-001"
    rt.MAX_TOOL_LOOPS = 10
    rt.MAX_LINES = 5000
    rt.IGNORE_DIRS = {"addons", ".git", ".godot"}
    rt.IGNORE_EXTENSIONS = set()
    rt.MCP_SERVERS = {}
    rt.LLM_PROVIDER = "minimax"
    rt.LLM_PROVIDERS = {
        "minimax": {
            "base_url": "https://api.minimax.io/anthropic/v1",
            "model": "MiniMax-M2.5",
            "api_key_env": "MINIMAX_API_KEY",
            "format": "anthropic",
            "max_tokens": 8096,
        },
        "claude": {
            "base_url": "https://api.anthropic.com/v1",
            "model": "claude-sonnet-4-6",
            "api_key_env": "ANTHROPIC_API_KEY",
            "format": "anthropic_native",
            "max_tokens": 8096,
        },
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "model": "some-model",
            "api_key_env": "OPENROUTER_API_KEY",
            "format": "openai",
            "max_tokens": 8096,
        },
    }
    rt.FEATURE_SYSTEM = "You are a Godot developer. Feature task."
    rt.FEATURE_USER = "Do the feature."
    rt.BUG_SYSTEM = "You are a Godot developer. Bug task."
    rt.BUG_USER = "Fix the bug."
    rt.POLISH_SYSTEM = "You are a Godot developer. Polish task."
    rt.POLISH_USER = "Polish this."
    rt.PYTHON_FEATURE_SYSTEM = "You are a Python developer. Feature task."
    rt.PYTHON_FEATURE_USER = "Do the Python feature."
    rt.PYTHON_BUG_SYSTEM = "You are a Python developer. Bug task."
    rt.PYTHON_BUG_USER = "Fix the Python bug."
    rt.system_prompt = ""
    rt.user_prompt = ""
    rt.mcp_client = None
    rt._ROUTING_LOOP = 0
    rt._ROUTING_COMMITS = 0
    rt.API_PORT = 19999   # nothing listens here -- prevents tests leaking tasks to live server
    rt.MANAGED_PROJECTS = ["real-proj"]  # non-empty + excludes test-proj → unmanaged guard skips continuation spawning
    rt.TASK_METADATA = {}
    rt.RUN_BROADCAST_WRITE_COUNT = 0

    # Sync config vars to tool submodules (mirrors what main() does)
    rt._sync_core_globals()
    rt._sync_knowledge_globals()

    yield tmp_path


def _init_git(proj_dir: Path):
    """Initialise a bare git repo so git operations don't crash main()."""
    subprocess.run(["git", "init"], cwd=proj_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=proj_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=proj_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=proj_dir, capture_output=True,
    )


# ---------------------------------------------------------------------------
# parse_tool_calls()
# ---------------------------------------------------------------------------

class TestParseToolCalls:
    def test_bracket_format_basic(self):
        text = '[TOOL_CALL]{"tool": "list_files", "args": {"path": "."}}[/TOOL_CALL]'
        calls = rt.parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["tool"] == "list_files"
        assert calls[0]["args"] == {"path": "."}

    def test_xml_format_basic(self):
        text = '<tool_call>{"tool": "read_file", "args": {"path": "main.gd"}}</tool_call>'
        calls = rt.parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["tool"] == "read_file"


class TestPullLatestGuard:
    def test_normal_feature_tasks_pull_latest(self):
        rt.TASK_TYPE = "feature"
        rt.PROJECT_PATH_OVERRIDE = ""
        rt.TASK_METADATA = {}

        assert rt._should_pull_latest() is True

    def test_experiment_tasks_do_not_pull_latest(self):
        rt.TASK_TYPE = "feature"
        rt.PROJECT_PATH_OVERRIDE = ""
        rt.TASK_METADATA = {"experiment_id": "exp-123", "pipeline": []}

        assert rt._should_pull_latest() is False

    def test_worktree_tasks_do_not_pull_latest(self, tmp_path):
        rt.TASK_TYPE = "feature"
        rt.PROJECT_PATH_OVERRIDE = str(tmp_path / "worktree")
        rt.TASK_METADATA = {}

        assert rt._should_pull_latest() is False

    def test_multiple_bracket_calls_in_one_response(self):
        text = (
            '[TOOL_CALL]{"tool": "list_files", "args": {"path": "."}}[/TOOL_CALL]'
            " some prose "
            '[TOOL_CALL]{"tool": "read_file", "args": {"path": "x.gd"}}[/TOOL_CALL]'
        )
        calls = rt.parse_tool_calls(text)
        assert len(calls) == 2
        assert calls[0]["tool"] == "list_files"
        assert calls[1]["tool"] == "read_file"

    def test_multiple_xml_calls(self):
        text = (
            '<tool_call>{"tool": "list_files", "args": {}}</tool_call>'
            '<tool_call>{"tool": "git_push", "args": {}}</tool_call>'
        )
        calls = rt.parse_tool_calls(text)
        assert len(calls) == 2

    def test_mixed_bracket_and_xml_formats(self):
        text = (
            '[TOOL_CALL]{"tool": "list_files", "args": {}}[/TOOL_CALL]'
            '<tool_call>{"tool": "read_file", "args": {"path": "x"}}</tool_call>'
        )
        calls = rt.parse_tool_calls(text)
        assert len(calls) == 2

    def test_empty_response_returns_empty_list(self):
        assert rt.parse_tool_calls("") == []

    def test_plain_prose_returns_empty_list(self):
        assert rt.parse_tool_calls("Hello! I will now help you with the task.") == []

    def test_truncated_response_missing_close_tag_skipped(self):
        # No [/TOOL_CALL] -- incomplete, should not be returned
        text = '[TOOL_CALL]{"tool": "list_files", "args": {"path": "."}}'
        calls = rt.parse_tool_calls(text)
        assert calls == []

    def test_minimax_missing_final_close_bracket_is_repaired(self):
        text = '[TOOL_CALL]{"tool": "list_files", "args": {"path": "."}}[/TOOL_CALL'
        calls = rt.parse_tool_calls(text)
        assert calls == [{"tool": "list_files", "args": {"path": "."}}]

    def test_malformed_json_missing_one_brace_is_recovered(self):
        # Missing final } -- _try_parse should recover by appending one
        text = '[TOOL_CALL]{"tool": "list_files", "args": {"path": "."}[/TOOL_CALL]'
        calls = rt.parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["tool"] == "list_files"

    def test_completely_invalid_json_skipped(self):
        text = "[TOOL_CALL]not json at all[/TOOL_CALL]"
        calls = rt.parse_tool_calls(text)
        assert calls == []

    def test_prose_around_call_is_ignored(self):
        text = (
            "I'll list the files now:\n"
            '[TOOL_CALL]{"tool": "list_files", "args": {}}[/TOOL_CALL]\n'
            "Done with that step."
        )
        calls = rt.parse_tool_calls(text)
        assert len(calls) == 1

    def test_call_with_no_args_key_parsed(self):
        text = '[TOOL_CALL]{"tool": "git_push"}[/TOOL_CALL]'
        calls = rt.parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["tool"] == "git_push"

    def test_nested_args_preserved(self):
        text = '[TOOL_CALL]{"tool": "mcp_call_tool", "args": {"server": "godot", "tool": "create_node", "args": {"type": "Node2D"}}}[/TOOL_CALL]'
        calls = rt.parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["args"]["server"] == "godot"
        assert calls[0]["args"]["args"]["type"] == "Node2D"


# ---------------------------------------------------------------------------
# execute_tool() dispatch
# ---------------------------------------------------------------------------

class TestExecuteTool:
    def test_dispatches_list_files(self):
        result = rt.execute_tool({"tool": "list_files", "args": {"path": "."}})
        assert result.get("ok") is True
        assert "files" in result

    def test_dispatches_read_file(self, tmp_path):
        (tmp_path / "workspace" / "test-proj" / "hello.txt").write_text("hello world")
        result = rt.execute_tool({"tool": "read_file", "args": {"path": "hello.txt"}})
        assert result.get("ok") is True
        assert "hello world" in result["content"]

    def test_dispatches_write_file(self, tmp_path):
        rt.RUN_BROADCAST_WRITE_COUNT = 0
        with patch("swarm.runtime_helpers._lock_project_file", return_value={"ok": True}):
            result = rt.execute_tool({
                "tool": "write_file",
                "args": {"path": "out.txt", "content": "test content"},
            })
        assert result.get("ok") is True
        written = (tmp_path / "workspace" / "test-proj" / "out.txt").read_text()
        assert written == "test content"

    def test_dispatches_run_command(self):
        result = rt.execute_tool({"tool": "run_command", "args": {"command": "echo hi"}})
        assert result.get("ok") is True
        assert "hi" in result["stdout"]

    def test_unknown_tool_returns_error_dict(self):
        result = rt.execute_tool({"tool": "nonexistent_tool", "args": {}})
        assert result.get("ok") is False
        assert "Unknown tool" in result["error"]
        assert "nonexistent_tool" in result["error"]

    def test_error_lists_all_valid_tool_names(self):
        result = rt.execute_tool({"tool": "bad", "args": {}})
        for name in ("list_files", "read_file", "write_file", "run_command",
                     "git_commit", "git_push"):
            assert name in result["error"]

    def test_empty_tool_name_returns_error(self):
        result = rt.execute_tool({"tool": "", "args": {}})
        assert result.get("ok") is False

    def test_dispatches_delegate_helper(self):
        with patch("swarm.tool_dispatch.delegate_helper", return_value={"ok": True, "answer": "done"}) as helper:
            result = rt.execute_tool({"tool": "delegate_helper", "args": {"question": "What uses this?"}})
        assert result == {"ok": True, "answer": "done"}
        helper.assert_called_once()

    def test_dispatches_delegate_task_batch(self):
        with patch("swarm.tool_dispatch.delegate_task_batch", return_value={"ok": True, "task_ids": ["x"]}) as delegated:
            result = rt.execute_tool({"tool": "delegate_task_batch", "args": {"children": [{"description": "part a"}]}})
        assert result == {"ok": True, "task_ids": ["x"]}
        delegated.assert_called_once()


# ---------------------------------------------------------------------------
# read_file()
# ---------------------------------------------------------------------------

class TestReadFile:
    def test_reads_existing_file(self, tmp_path):
        (tmp_path / "workspace" / "test-proj" / "script.gd").write_text("extends Node")
        result = rt.read_file("script.gd")
        assert result["ok"] is True
        assert "extends Node" in result["content"]

    def test_missing_file_returns_error(self):
        result = rt.read_file("does_not_exist.gd")
        assert result["ok"] is False
        assert "error" in result

    def test_reads_full_file_without_truncation(self, tmp_path):
        (tmp_path / "workspace" / "test-proj" / "big.gd").write_text("x" * 30000)
        result = rt.read_file("big.gd")
        assert result["ok"] is True
        assert len(result["content"]) == 30000

    def test_read_file_offset_and_limit(self, tmp_path):
        lines = [f"line{i}\n" for i in range(100)]
        (tmp_path / "workspace" / "test-proj" / "f.gd").write_text("".join(lines))
        result = rt.read_file("f.gd", offset=10, limit=5)
        assert result["ok"] is True
        assert result["total_lines"] == 100
        assert result["returned_lines"] == 5
        assert "line10\n" in result["content"]
        assert "line14\n" in result["content"]
        assert "line15\n" not in result["content"]

    def test_reads_nested_path(self, tmp_path):
        nested = tmp_path / "workspace" / "test-proj" / "scripts" / "player.gd"
        nested.parent.mkdir(parents=True)
        nested.write_text("# player")
        result = rt.read_file("scripts/player.gd")
        assert result["ok"] is True
        assert "# player" in result["content"]

    def test_reads_windows_ansi_file(self, tmp_path):
        path = tmp_path / "workspace" / "test-proj" / "AGENT_KNOWLEDGE.md"
        path.write_bytes(b"SnakeController \x97 movement notes")

        result = rt.read_file("AGENT_KNOWLEDGE.md")

        assert result["ok"] is True
        assert "SnakeController" in result["content"]
        assert "movement notes" in result["content"]
        assert result["encoding"] == "cp1252"

    def test_read_file_range_reads_windows_ansi_file(self, tmp_path):
        path = tmp_path / "workspace" / "test-proj" / "notes.md"
        path.write_bytes(b"line 1\nline \x97 two\nline 3\n")

        result = rt.read_file_range("notes.md", 2, 2)

        assert result["ok"] is True
        assert "line" in result["content"]
        assert result["encoding"] == "cp1252"

    def test_read_agent_knowledge_reads_windows_ansi_file(self, tmp_path):
        project = tmp_path / "workspace" / "test-proj"
        (project / "AGENT_KNOWLEDGE.md").write_bytes(b"SnakeController \x97 movement notes")

        content = read_agent_knowledge(str(project))

        assert "SnakeController" in content
        assert "movement notes" in content


# ---------------------------------------------------------------------------
# list_files()
# ---------------------------------------------------------------------------

class TestListFiles:
    def test_lists_directory_contents(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        (proj / "a.gd").write_text("")
        (proj / "b.gd").write_text("")
        result = rt.list_files(".")
        assert result["ok"] is True
        assert "a.gd" in result["files"]
        assert "b.gd" in result["files"]

    def test_subdirectories_have_trailing_slash(self, tmp_path):
        (tmp_path / "workspace" / "test-proj" / "scripts").mkdir()
        result = rt.list_files(".")
        assert result["ok"] is True
        assert "scripts/" in result["files"]

    def test_missing_path_returns_error(self):
        result = rt.list_files("nonexistent/dir")
        assert result["ok"] is False

    def test_single_file_path_returns_filename(self, tmp_path):
        f = tmp_path / "workspace" / "test-proj" / "main.gd"
        f.write_text("")
        result = rt.list_files("main.gd")
        assert result["ok"] is True
        assert result.get("type") == "file"


# ---------------------------------------------------------------------------
# write_file()
# ---------------------------------------------------------------------------

class TestWriteFile:
    def test_creates_file_with_content(self, tmp_path):
        rt.write_file("output.gd", "extends Node")
        path = tmp_path / "workspace" / "test-proj" / "output.gd"
        assert path.exists()
        assert "extends Node" in path.read_text()

    def test_creates_parent_directories(self, tmp_path):
        rt.write_file("scripts/ui/hud.gd", "extends CanvasLayer")
        path = tmp_path / "workspace" / "test-proj" / "scripts" / "ui" / "hud.gd"
        assert path.exists()

    def test_overwrites_existing_file(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        (proj / "old.gd").write_text("old content")
        rt.write_file("old.gd", "new content")
        assert (proj / "old.gd").read_text() == "new content"

    def test_returns_ok_true_on_success(self, tmp_path):
        result = rt.write_file("test.gd", "content")
        assert result["ok"] is True

    def test_returns_written_path(self, tmp_path):
        result = rt.write_file("test.gd", "content")
        assert "path" in result


# ---------------------------------------------------------------------------
# run_command()
# ---------------------------------------------------------------------------

class TestRunCommand:
    def test_successful_command(self):
        result = rt.run_command("echo hello")
        assert result["ok"] is True
        assert "hello" in result["stdout"]

    def test_failed_command_ok_false(self):
        result = rt.run_command("false")
        assert result["ok"] is False

    def test_godot_command_with_script_error_stdout_fails_even_on_zero_exit(self):
        import swarm.tools.core as core
        with patch("swarm.tools.shell.run", return_value=(0, "SCRIPT ERROR: Parse Error: Could not find type \"Maze\" in the current scope.\n", "")):
            result = core.run_command("godot --headless --path . --quit")
        assert result["ok"] is False
        assert "Godot runtime error detected" in result["stderr"]
        assert "Could not find type" in result["stderr"]

    def test_godot_command_with_runtime_error_stderr_fails_even_on_zero_exit(self):
        import swarm.tools.core as core
        with patch("swarm.tools.shell.run", return_value=(0, "", "Failed to load script res://scripts/maze.gd\n")):
            result = core.run_command("godot --headless --path . --quit")
        assert result["ok"] is False
        assert "Godot runtime error detected" in result["stderr"]
        assert "Failed to load script" in result["stderr"]

    def test_non_godot_command_with_parse_text_does_not_get_special_failure(self):
        import swarm.tools.core as core
        with patch("swarm.tools.core.run", return_value=(0, "Parse Error: simulated\n", "")):
            result = core.run_command("echo 'Parse Error: simulated'")
        assert result["ok"] is True

    def test_stdout_captured(self):
        result = rt.run_command("echo captured_output")
        assert "captured_output" in result["stdout"]

    def test_stderr_captured(self):
        result = rt.run_command("echo error_text >&2")
        # stderr may go to stdout or stderr depending on shell
        combined = result["stdout"] + result.get("stderr", "")
        assert "error_text" in combined

    def test_timeout_returns_error(self):
        result = rt.run_command("sleep 30", timeout=1)
        assert result["ok"] is False

    def test_background_process_inheriting_output_does_not_hang(self, tmp_path):
        marker = tmp_path / "background-marker"
        cmd = (
            "python3 -c 'import time; time.sleep(3)' & "
            f"echo done > {marker}"
        )

        result = rt.run_command(cmd, timeout=1)

        assert result["ok"] is True
        assert marker.read_text().strip() == "done"

    def test_blocks_vendor_write_commands(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        vendor_file = proj / "addons" / "gut" / "helper.gd"
        vendor_file.parent.mkdir(parents=True)
        vendor_file.write_text("x = 1\n")

        result = rt.run_command("cp -f plain.txt addons/gut/helper.gd")
        assert result["ok"] is False
        assert "vendor code" in result["error"]
        assert vendor_file.read_text() == "x = 1\n"

    def test_blocks_generated_write_commands(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        result = rt.run_command("mkdir -p .godot/imported")
        assert result["ok"] is False
        assert "generated artifacts" in result["error"]
        assert not (proj / ".godot" / "imported").exists()

    def test_allows_read_only_vendor_commands(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        vendor_file = proj / "addons" / "gut" / "gut_cmdln.gd"
        vendor_file.parent.mkdir(parents=True)
        vendor_file.write_text("extends SceneTree\n")

        result = rt.run_command("test -f addons/gut/gut_cmdln.gd && echo YES || echo NO")
        assert result["ok"] is True
        assert "YES" in result["stdout"]

    def test_blocks_tee_into_vendor_path(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        vendor_file = proj / "addons" / "gut" / "helper.gd"
        vendor_file.parent.mkdir(parents=True)
        vendor_file.write_text("x = 1\n")

        result = rt.run_command("printf 'x = 2\\n' | tee addons/gut/helper.gd >/dev/null")
        assert result["ok"] is False
        assert "vendor code" in result["error"]
        assert vendor_file.read_text() == "x = 1\n"

    def test_blocks_python_open_write_into_generated_path(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        generated_file = proj / ".godot" / "cache.cfg"
        generated_file.parent.mkdir(parents=True)
        generated_file.write_text("old\n")

        result = rt.run_command(
            "python3 -c \"open('.godot/cache.cfg', 'w').write('new')\""
        )
        assert result["ok"] is False
        assert "generated artifacts" in result["error"]
        assert generated_file.read_text() == "old\n"

    def test_blocks_python_pathlib_write_into_vendor_path(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        vendor_file = proj / "addons" / "gut" / "helper.gd"
        vendor_file.parent.mkdir(parents=True)
        vendor_file.write_text("x = 1\n")

        result = rt.run_command(
            "python3 -c \"from pathlib import Path; Path('addons/gut/helper.gd').write_text('x = 2\\n')\""
        )
        assert result["ok"] is False
        assert "vendor code" in result["error"]
        assert vendor_file.read_text() == "x = 1\n"


class TestTaskAuthorityGuards:
    def test_qa_blocks_run_command(self):
        rt.TASK_TYPE = "qa"
        result = rt.execute_tool({"tool": "run_command", "args": {"command": "echo hi"}})
        assert result["ok"] is False
        assert "read-only testers" in result["error"]

    def test_qa_only_allows_qa_report_write(self):
        rt.TASK_TYPE = "qa"
        with patch("swarm.runtime_helpers._lock_project_file", return_value={"ok": True}):
            blocked = rt.execute_tool({"tool": "write_file", "args": {"path": "notes.md", "content": "x"}})
            allowed = rt.execute_tool({"tool": "write_file", "args": {"path": "QA_REPORT.md", "content": "ok"}})
        assert blocked["ok"] is False
        assert "QA_REPORT.md" in blocked["error"]
        assert allowed["ok"] is True

    def test_qa_allows_absolute_project_qa_report_path(self):
        rt.TASK_TYPE = "qa"
        abs_report = rt.WORKSPACE / rt.PROJECT / "QA_REPORT.md"
        with patch("swarm.runtime_helpers._lock_project_file", return_value={"ok": True}):
            result = rt.execute_tool({"tool": "write_file", "args": {"path": str(abs_report), "content": "ok"}})
        assert result["ok"] is True

    def test_qa_create_bug_task_uses_qa_tool_not_harness_alias(self):
        rt.TASK_TYPE = "qa"
        with patch("swarm.tool_dispatch.qa_create_bug_task", return_value={"ok": True, "task_id": "qa-bug-1"}) as create:
            result = rt.execute_tool({
                "tool": "create_bug_task",
                "args": {
                    "description": "Player does not spawn",
                    "evidence_path": "qa_screenshots/spawn.png",
                    "priority": 95,
                    "dependencies": ["qa-1"],
                },
            })
        assert result["ok"] is True
        create.assert_called_once()

    def test_qa_requeue_self_uses_qa_tool_not_harness_alias(self):
        rt.TASK_TYPE = "qa"
        with patch("swarm.tool_dispatch.qa_requeue_self", return_value={"ok": True, "task_id": "qa-rerun"}) as requeue:
            result = rt.execute_tool({"tool": "requeue_self", "args": {"bug_task_ids": ["qa-bug-1"]}})
        assert result["ok"] is True
        requeue.assert_called_once_with(["qa-bug-1"])

    def test_harness_launch_game_dispatch_imports_extra_arg_parser(self):
        rt.TASK_TYPE = "harness_qa"
        with patch("swarm.tool_dispatch.harness_launch_game", return_value={"ok": True}) as launch:
            result = rt.execute_tool({"tool": "harness_launch_game", "args": {}})
        assert result["ok"] is True
        launch.assert_called_once()

    def test_triage_only_allows_triage_report_write(self):
        rt.TASK_TYPE = "triage"
        with patch("swarm.runtime_helpers._lock_project_file", return_value={"ok": True}):
            blocked = rt.execute_tool({"tool": "write_file", "args": {"path": "foo.md", "content": "x"}})
            allowed = rt.execute_tool({"tool": "write_file", "args": {"path": "TRIAGE_REPORT.md", "content": "ok"}})
        assert blocked["ok"] is False
        assert "TRIAGE_REPORT.md" in blocked["error"]
        assert allowed["ok"] is True

    def test_research_cannot_patch_code(self):
        rt.TASK_TYPE = "research"
        result = rt.execute_tool({"tool": "patch_file", "args": {"path": "main.py", "old": "a", "new": "b"}})
        assert result["ok"] is False
        assert "must not implement code changes" in result["error"]

    def test_research_finding_path_is_constrained(self):
        rt.TASK_TYPE = "research"
        with patch("swarm.runtime_helpers._lock_project_file", return_value={"ok": True}):
            blocked = rt.execute_tool({"tool": "write_file", "args": {"path": "notes.md", "content": "x"}})
            allowed = rt.execute_tool({"tool": "write_file", "args": {"path": "research/findings.md", "content": "ok"}})
        assert blocked["ok"] is False
        assert "research/*.md" in blocked["error"]
        assert allowed["ok"] is True

    def test_project_plan_blocks_run_command(self):
        rt.TASK_TYPE = "project_plan"
        result = rt.execute_tool({"tool": "run_command", "args": {"command": "git log --oneline -5"}})
        assert result["ok"] is False
        assert "create_tasks_file_aware" in result["error"]


    def test_plan_blocks_run_command(self):
        rt.TASK_TYPE = "plan"
        result = rt.execute_tool({"tool": "run_command", "args": {"command": "git log --oneline -5"}})
        assert result["ok"] is False
        assert "read-only" in result["error"]

    def test_python_plan_blocks_run_command(self):
        rt.TASK_TYPE = "python_plan"
        result = rt.execute_tool({"tool": "run_command", "args": {"command": "echo hello > /tmp/test.txt"}})
        assert result["ok"] is False
        assert "read-only" in result["error"]

    def test_recovery_task_cannot_spawn_child_work(self):
        rt.TASK_TYPE = "bug"
        rt.TASK_METADATA = {"is_recovery_task": True}
        result = rt.execute_tool({"tool": "create_task", "args": {"description": "follow-up"}})
        assert result["ok"] is False
        assert "cannot spawn arbitrary child work" in result["error"]

    def test_qa_cannot_delegate_helper(self):
        rt.TASK_TYPE = "qa"
        result = rt.execute_tool({"tool": "delegate_helper", "args": {"question": "inspect"}})
        assert result["ok"] is False
        assert "does not support transient helper delegation" in result["error"]

    def test_feature_can_delegate_helper(self):
        rt.TASK_TYPE = "feature"
        with patch("swarm.tool_dispatch.delegate_helper", return_value={"ok": True, "answer": "fine"}):
            result = rt.execute_tool({"tool": "delegate_helper", "args": {"question": "inspect"}})
        assert result["ok"] is True

    def test_audit_cannot_delegate_task_batch(self):
        rt.TASK_TYPE = "audit"
        result = rt.execute_tool({"tool": "delegate_task_batch", "args": {"children": [{"description": "part"}]}})
        assert result["ok"] is False
        assert "does not support structured child-task delegation" in result["error"]

    def test_feature_can_delegate_task_batch(self):
        rt.TASK_TYPE = "feature"
        with patch("swarm.tool_dispatch.delegate_task_batch", return_value={"ok": True, "task_ids": ["child-1"]}):
            result = rt.execute_tool({"tool": "delegate_task_batch", "args": {"children": [{"description": "part"}]}})
        assert result["ok"] is True

    def test_delegated_child_cannot_delegate_task_batch(self):
        rt.TASK_TYPE = "feature"
        rt.TASK_METADATA = {"delegation_batch_id": "delegation-123"}
        rt._sync_core_globals()
        result = rt.execute_tool({"tool": "delegate_task_batch", "args": {"children": [{"description": "part"}]}})
        assert result["ok"] is False
        assert "nested structured child-task delegation" in result["error"]


class TestCatastrophicCommandBlock:
    """run_command must hard-block irreversible/destructive shell operations."""

    def _run(self, cmd):
        import swarm.tools.core as core
        return core.run_command(cmd)

    # --- recursive rm ---
    def test_blocks_rm_rf(self):
        result = self._run("rm -rf /tmp/some-dir")
        assert result["ok"] is False
        assert "blocked" in result["error"]

    def test_blocks_rm_fr(self):
        result = self._run("rm -fr src/")
        assert result["ok"] is False

    def test_blocks_rm_r(self):
        result = self._run("rm -r old_files/")
        assert result["ok"] is False

    def test_allows_rm_single_file(self):
        # Single-file rm (no -r flag) must still be allowed
        import unittest.mock as mock
        with mock.patch("swarm.tools.shell.run", return_value=(0, "", "")):
            result = self._run("rm /tmp/swarm_temp.py")
        assert result["ok"] is True

    # --- find --delete ---
    def test_blocks_find_delete(self):
        result = self._run("find . -name '*.pyc' --delete")
        assert result["ok"] is False

    def test_blocks_find_dash_delete(self):
        result = self._run("find . -name '*.pyc' -delete")
        assert result["ok"] is False

    # --- destructive git ---
    def test_blocks_git_push_force(self):
        result = self._run("git push origin main --force")
        assert result["ok"] is False

    def test_blocks_git_push_f(self):
        result = self._run("git push -f origin main")
        assert result["ok"] is False

    def test_blocks_git_push_force_with_lease(self):
        result = self._run("git push --force-with-lease")
        assert result["ok"] is False

    def test_blocks_git_reset_hard(self):
        result = self._run("git reset --hard HEAD~1")
        assert result["ok"] is False

    def test_blocks_git_branch_force_delete(self):
        result = self._run("git branch -D old-feature")
        assert result["ok"] is False

    def test_blocks_git_clean_f(self):
        result = self._run("git clean -fd")
        assert result["ok"] is False

    def test_blocks_git_filter_branch(self):
        result = self._run("git filter-branch --tree-filter 'rm -f secrets.txt'")
        assert result["ok"] is False

    def test_allows_git_push_without_force(self):
        import unittest.mock as mock
        with mock.patch("swarm.tools.shell.run", return_value=(0, "", "")):
            result = self._run("git push origin feature-branch")
        assert result["ok"] is True

    def test_allows_git_reset_soft(self):
        import unittest.mock as mock
        with mock.patch("swarm.tools.shell.run", return_value=(0, "", "")):
            result = self._run("git reset --soft HEAD~1")
        assert result["ok"] is True

    # --- process killing ---
    def test_blocks_pkill(self):
        result = self._run("pkill godot")
        assert result["ok"] is False

    def test_blocks_killall(self):
        result = self._run("killall python3")
        assert result["ok"] is False

    # --- disk writes ---
    def test_blocks_dd_if(self):
        result = self._run("dd if=/dev/zero of=/tmp/disk.img bs=1M count=100")
        assert result["ok"] is False

    # --- permission bombs ---
    def test_blocks_chmod_recursive(self):
        result = self._run("chmod -R 777 /tmp/project")
        assert result["ok"] is False

    def test_blocks_chown_recursive(self):
        result = self._run("chown -R user:group /tmp/project")
        assert result["ok"] is False

    # --- swarm db direct access ---
    def test_blocks_direct_db_access(self):
        result = self._run("sqlite3 data/swarm.db 'DROP TABLE tasks'")
        assert result["ok"] is False

    def test_first_edit_requires_broadcast_claim_when_sibling_active(self):
        rt.TASK_TYPE = "feature"
        rt.RUN_BROADCAST_WRITE_COUNT = 0
        with patch("swarm.runtime_helpers._has_active_sibling_tasks", return_value=True):
            result = rt.execute_tool({"tool": "write_file", "args": {"path": "src/shared.py", "content": "x = 1\n"}})
        assert result["ok"] is False
        assert "before your first edit" in result["error"]
        assert "broadcast_write()" in result["error"]

    def test_broadcast_claim_unblocks_first_edit_when_sibling_active(self):
        rt.TASK_TYPE = "feature"
        rt.RUN_BROADCAST_WRITE_COUNT = 0
        with patch("swarm.runtime_helpers._has_active_sibling_tasks", return_value=True), \
             patch("swarm.tool_dispatch.broadcast_write", return_value={"ok": True}), \
             patch("swarm.runtime_helpers._lock_project_file", return_value={"ok": True}):
            claim = rt.execute_tool({"tool": "broadcast_write", "args": {"message": "Claiming src/shared.py"}})
            result = rt.execute_tool({"tool": "write_file", "args": {"path": "src/shared.py", "content": "x = 1\n"}})
        assert claim["ok"] is True
        assert result["ok"] is True

    def test_write_denied_when_file_locked_by_sibling(self):
        rt.TASK_TYPE = "feature"
        rt.RUN_BROADCAST_WRITE_COUNT = 1
        with patch("swarm.runtime_helpers._lock_project_file", return_value={"ok": False, "task_id": "sibling-task"}), \
             patch("swarm.runtime_helpers._spawn_lock_conflict_handoff", return_value={"ok": True, "followup_task_id": "feature-followup", "reparented_dependents": ["downstream-1"]}):
            result = rt.execute_tool({"tool": "write_file", "args": {"path": "src/shared.py", "content": "x = 1\n"}})
        assert result["ok"] is False
        assert "currently locked" in result["error"]
        assert "sibling-task" in result["error"]
        assert result["lock_conflict_handoff_created"] is True
        assert result["followup_task_id"] == "feature-followup"
        assert result["reparented_dependents"] == ["downstream-1"]

    def test_write_acquires_file_lock_before_edit(self, tmp_path):
        rt.TASK_TYPE = "feature"
        rt.RUN_BROADCAST_WRITE_COUNT = 1
        rt.CLAIMED_FILE_PATHS = set()
        with patch("swarm.runtime_helpers._lock_project_file", return_value={"ok": True, "file_path": "src/shared.py"}) as locker:
            result = rt.execute_tool({"tool": "write_file", "args": {"path": "src/shared.py", "content": "x = 1\n"}})
        assert result["ok"] is True
        locker.assert_called_once()

    def test_spawn_lock_conflict_handoff_creates_followup_and_reparents_dependents(self):
        rt.TASK_TYPE = "feature"
        rt.TASK_ID = "task-001"
        rt.TASK_DESC = "Player Control\n\nImplement movement and active operative visuals."
        rt.TASK_PRIORITY = 55
        rt.PROJECT = "test-proj"
        rt.TASK_METADATA = {}
        rt.LOCK_CONFLICT_HANDOFF = None

        patch_calls = []

        def _fake_patch(path, payload):
            patch_calls.append((path, payload))
            return {"ok": True}

        with patch("swarm.runtime_helpers._api_post_json", return_value={"task": {"id": "feature-followup"}, "created": True}) as post_json, \
             patch("swarm.runtime_helpers._api_get_json", side_effect=[
                 {"task": {"id": "task-001", "dependencies": ["root-dep", "other-dep"]}},
                 {"dependents": [{"id": "downstream-1"}]},
                 {"task": {"id": "downstream-1", "dependencies": ["task-001", "other-dep"]}},
             ]), \
             patch("swarm.runtime_helpers._api_patch_json", side_effect=_fake_patch):
            result = rt._spawn_lock_conflict_handoff("src/shared.py", "owner-task")

        assert result["ok"] is True
        assert result["followup_task_id"] == "feature-followup"
        post_json.assert_called_once()
        call_path, create_payload = post_json.call_args[0]
        assert call_path == "/api/projects/test-proj/lock-conflict-handoff"
        assert create_payload["dependencies"] == ["root-dep", "other-dep", "owner-task"]
        assert create_payload["locked_path"] == "src/shared.py"
        assert create_payload["owner_task_id"] == "owner-task"
        assert "ORIGINAL TASK OBJECTIVE (task-001):" in create_payload["description"]
        assert "Player Control" in create_payload["description"]
        assert create_payload["metadata"]["branch_intent_root_task_id"] == "task-001"
        assert create_payload["metadata"]["branch_intent_title"] == "Player Control"
        assert "Implement movement and active operative visuals." in create_payload["metadata"]["branch_intent_full_description"]
        assert any(path == "/api/tasks/downstream-1" and payload["dependencies"] == ["feature-followup", "other-dep"] for path, payload in patch_calls)
        assert any(path == "/api/tasks/task-001" and payload["metadata"]["lock_conflict_handoff_to"] == "feature-followup" for path, payload in patch_calls)

    def test_spawn_lock_conflict_handoff_preserves_local_intent_when_api_task_is_sparse(self):
        rt.TASK_TYPE = "feature"
        rt.TASK_ID = "task-001"
        rt.TASK_DESC = "Player Control\n\nImplement movement and active operative visuals."
        rt.TASK_PRIORITY = 55
        rt.PROJECT = "test-proj"
        rt.TASK_METADATA = {}
        rt.LOCK_CONFLICT_HANDOFF = None

        with patch("swarm.runtime_helpers._api_post_json", return_value={"task": {"id": "feature-followup"}, "created": True}) as post_json, \
             patch("swarm.runtime_helpers._api_get_json", side_effect=[
                 {"task": {"id": "task-001", "dependencies": ["root-dep"]}},
                 {"dependents": []},
             ]), \
             patch("swarm.runtime_helpers._api_patch_json", return_value={"ok": True}):
            result = rt._spawn_lock_conflict_handoff("src/shared.py", "owner-task")

        assert result["ok"] is True
        create_payload = post_json.call_args[0][1]
        assert "ORIGINAL TASK OBJECTIVE (task-001):" in create_payload["description"]
        assert "Player Control" in create_payload["description"]
        assert "Implement movement and active operative visuals." in create_payload["metadata"]["branch_intent_full_description"]


class TestDelegateHelper:
    def test_delegate_helper_reads_scoped_files(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "foo.gd").write_text("extends Node\nvar hp = 5\n")

        rt.WORKSPACE = tmp_path / "workspace"
        rt.PROJECT = "test-proj"
        rt.PROJECT_PATH_OVERRIDE = ""
        rt.TASK_ID = "task-123"
        rt.TASK_TYPE = "feature"
        rt._sync_core_globals()

        with patch("swarm.llm_utils.call_llm", return_value=("Helper answer", {"input": 11, "output": 7}, [])):
            result = rt.delegate_helper("What does foo contain?", ["foo.gd"], "Inspect health state", 4000)

        assert result["ok"] is True
        assert result["answer"] == "Helper answer"
        assert result["files_consulted"] == ["foo.gd"]
        assert result["input_tokens"] == 11
        assert result["output_tokens"] == 7

    def test_delegate_helper_persists_parent_metadata(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "foo.gd").write_text("extends Node\nvar hp = 5\n")

        rt.WORKSPACE = tmp_path / "workspace"
        rt.PROJECT = "test-proj"
        rt.PROJECT_PATH_OVERRIDE = ""
        rt.TASK_ID = "task-123"
        rt.TASK_TYPE = "feature"
        rt._sync_core_globals()

        calls = []

        def fake_urlopen(req, timeout=0):
            payload = json.loads(req.data.decode()) if getattr(req, "data", None) else None
            calls.append((req.get_method(), req.full_url, payload))
            if req.get_method() == "GET":
                return _FakeUrlopenResponse({"task": {"id": "task-123", "metadata": {"existing": True}}})
            if req.get_method() == "PATCH":
                return _FakeUrlopenResponse({"task": {"id": "task-123", "metadata": payload["metadata"]}})
            raise AssertionError(f"Unexpected request: {req.get_method()} {req.full_url}")

        with patch("swarm.llm_utils.call_llm", return_value=("Helper answer", {"input": 11, "output": 7}, [])):
            with patch("swarm.tools.core._ur.urlopen", side_effect=fake_urlopen):
                result = rt.delegate_helper("What does foo contain?", ["foo.gd"], "Inspect health state", 4000)

        assert result["ok"] is True
        patch_call = next(call for call in calls if call[0] == "PATCH")
        helper_entries = patch_call[2]["metadata"]["helper_delegations"]
        assert len(helper_entries) == 1
        assert helper_entries[0]["question"] == "What does foo contain?"
        assert helper_entries[0]["files"] == ["foo.gd"]
        assert patch_call[2]["metadata"]["existing"] is True







class TestCreateSubtask:
    """Tests for the create_subtask tool.

    _ur is a local import inside create_subtask functions, not a module attribute.
    Must patch urllib.request.urlopen (the real urllib module).
    """

    def _setup_rt(self, task_id="parent-task", depth=0):
        """Set up runtime context and sync to core so _read_core() returns valid data."""
        rt.TASK_ID = task_id
        rt.PROJECT = "test-proj"
        rt.TASK_TYPE = "feature"
        rt._sync_core_globals()
        rt.IO_LOG = []
        rt.LLM_RESPONSES = []
        rt.MATCH_STRATEGY = "first"
        rt.FINAL_PLAN = None

    # --- dispatch routing ---

    def test_invalid_type_rejected_at_dispatch(self):
        result = rt.execute_tool({
            "tool": "create_subtask",
            "args": {"description": "x", "type": "bad"},
        })
        assert result["ok"] is False
        assert "Invalid task type" in result["error"]

    def test_dispatch_passes_correct_args(self):
        with patch("swarm.tool_dispatch.create_subtask",
                   return_value={"ok": True, "task_id": "sub-1", "depth": 1}) as cs:
            result = rt.execute_tool({
                "tool": "create_subtask",
                "args": {
                    "description": "my sub",
                    "type": "refactor",
                    "priority": 75,
                    "files_touched": ["src/main.gd"],
                    "depends_on_current": True,
                    "max_depth": 3,
                    "project": "my-proj",
                    "metadata": {"note": "test"},
                },
            })
        assert result["ok"] is True
        assert result["task_id"] == "sub-1"
        cs.assert_called_once()
        args = cs.call_args[0]
        assert args[0] == "my sub"
        assert args[1] == "refactor"
        assert args[2] == 75
        assert args[3] == ["src/main.gd"]
        assert args[4] is True
        assert args[5] == 3
        assert args[6] == "my-proj"
        assert args[7] == {"note": "test"}

    def test_dispatch_rejects_missing_description(self):
        self._setup_rt()

        def fake(url, timeout=None):
            return _FakeUrlopenResponse({
                "tasks": [{"id": "parent-task", "metadata": {"task_depth": 0}}]
            })

        with patch("urllib.request.urlopen", side_effect=fake):
            result = rt.execute_tool({
                "tool": "create_subtask", "args": {}})
        assert result["ok"] is False
        assert "description" in result["error"]

    # --- validation ---

    def test_invalid_task_type_rejected(self):
        self._setup_rt()

        def fake(url, timeout=None):
            return _FakeUrlopenResponse({
                "tasks": [{"id": "parent-task", "metadata": {"task_depth": 0}}]
            })

        with patch("urllib.request.urlopen", side_effect=fake):
            from swarm.tools.tasks import create_subtask
            result = create_subtask("test", task_type="not_a_type")
        assert result["ok"] is False
        assert "Invalid task type" in result["error"]

    def test_unknown_task_id_rejected(self):
        self._setup_rt(task_id="unknown")

        def fake(url, timeout=None):
            return _FakeUrlopenResponse({
                "tasks": [{"id": "unknown", "metadata": {"task_depth": 0}}]
            })

        with patch("urllib.request.urlopen", side_effect=fake):
            from swarm.tools.tasks import create_subtask
            result = create_subtask("sub")
        assert result["ok"] is False
        assert "valid TASK_ID" in result["error"]

    # --- depth enforcement ---

    def test_depth_guard_blocks_at_max_depth(self):
        # Parent is at depth 2, max_depth is 1 (sub-task would be at 3 > 1 → blocked)
        self._setup_rt(task_id="deep-task")

        def fake(url, timeout=None):
            # GET: parent is at depth 2
            # POST: would create new task at depth 3
            if not isinstance(url, str) and url.get_method() == "POST":
                raise AssertionError("POST should not be called when max depth exceeded")
            return _FakeUrlopenResponse({
                "tasks": [{"id": "deep-task", "metadata": {"task_depth": 2}}]
            })

        with patch("urllib.request.urlopen", side_effect=fake):
            from swarm.tools.tasks import create_subtask
            result = create_subtask("sub", max_depth=1)
        assert result["ok"] is False
        assert "max sub-task depth" in result["error"]

    def test_normal_depth_allowed(self):
        self._setup_rt()
        captured = {}

        def fake(url, timeout=None):
            if not isinstance(url, str) and url.get_method() == "POST":
                captured["data"] = json.loads(url.data.decode())
                return _FakeUrlopenResponse({"task": {"id": "new-sub", "metadata": {}}})
            return _FakeUrlopenResponse({
                "tasks": [{"id": "parent-task", "metadata": {"task_depth": 0}}]
            })

        with patch("urllib.request.urlopen", side_effect=fake):
            from swarm.tools.tasks import create_subtask
            result = create_subtask("child")
        assert result["ok"] is True
        assert captured["data"]["dependencies"] == ["parent-task"]
        assert captured["data"]["metadata"]["parent_task_id"] == "parent-task"
        assert captured["data"]["metadata"]["task_depth"] == 1

    # --- file conflict detection ---

    def test_pending_sibling_blocks_same_file(self):
        self._setup_rt()

        def fake(url, timeout=None):
            return _FakeUrlopenResponse({
                "tasks": [
                    {"id": "parent-task", "metadata": {"task_depth": 0}},
                    {"id": "sibling-subtask", "status": "pending",
                     "metadata": {"parent_task_id": "parent-task",
                                  "delegated_files": ["src/shared.gd"]}}
                ]
            })

        with patch("urllib.request.urlopen", side_effect=fake):
            from swarm.tools.tasks import create_subtask
            result = create_subtask("conflicting", files_touched=["src/shared.gd"])
        assert result["ok"] is False
        assert "file conflict detected" in result["error"]
        assert "sibling-subtask" in result["error"]

    def test_in_progress_sibling_blocks_same_file(self):
        self._setup_rt()

        def fake(url, timeout=None):
            return _FakeUrlopenResponse({
                "tasks": [
                    {"id": "parent-task", "metadata": {"task_depth": 0}},
                    {"id": "active-subtask", "status": "in_progress",
                     "metadata": {"parent_task_id": "parent-task",
                                  "delegated_files": ["src/shared.gd"]}}
                ]
            })

        with patch("urllib.request.urlopen", side_effect=fake):
            from swarm.tools.tasks import create_subtask
            result = create_subtask("conflicting", files_touched=["src/shared.gd"])
        assert result["ok"] is False
        assert "file conflict detected" in result["error"]

    def test_non_overlapping_files_allowed(self):
        self._setup_rt()

        def fake(url, timeout=None):
            if not isinstance(url, str) and url.get_method() == "POST":
                return _FakeUrlopenResponse({"task": {"id": "new-subtask", "metadata": {}}})
            return _FakeUrlopenResponse({
                "tasks": [
                    {"id": "parent-task", "metadata": {"task_depth": 0}},
                    {"id": "sibling-subtask", "status": "pending",
                     "metadata": {"parent_task_id": "parent-task",
                                  "delegated_files": ["src/other.gd"]}}
                ]
            })

        with patch("urllib.request.urlopen", side_effect=fake):
            from swarm.tools.tasks import create_subtask
            result = create_subtask("non-conflicting", files_touched=["src/shared.gd"])
        assert result["ok"] is True

    def test_completed_sibling_does_not_block(self):
        self._setup_rt()

        def fake(url, timeout=None):
            if not isinstance(url, str) and url.get_method() == "POST":
                return _FakeUrlopenResponse({"task": {"id": "new-sub", "metadata": {}}})
            return _FakeUrlopenResponse({
                "tasks": [
                    {"id": "parent-task", "metadata": {"task_depth": 0}},
                    {"id": "done-subtask", "status": "completed",
                     "metadata": {"parent_task_id": "parent-task",
                                  "delegated_files": ["src/shared.gd"]}}
                ]
            })

        with patch("urllib.request.urlopen", side_effect=fake):
            from swarm.tools.tasks import create_subtask
            result = create_subtask("after-completion", files_touched=["src/shared.gd"])
        assert result["ok"] is True

    # --- depends_on_current ---

    def test_depends_on_current_true_adds_parent_dep(self):
        self._setup_rt(task_id="my-parent")
        captured = {}

        def fake(url, timeout=None):
            if not isinstance(url, str) and url.get_method() == "POST":
                captured["data"] = json.loads(url.data.decode())
                return _FakeUrlopenResponse({
                    "task": {"id": "created-sub", "metadata": {"task_depth": 2}}
                })
            return _FakeUrlopenResponse({
                "tasks": [{"id": "my-parent", "metadata": {"task_depth": 1}}]
            })

        with patch("urllib.request.urlopen", side_effect=fake):
            from swarm.tools.tasks import create_subtask
            result = create_subtask("child task")
        assert result["ok"] is True
        assert captured["data"]["dependencies"] == ["my-parent"]
        assert captured["data"]["metadata"]["parent_task_id"] == "my-parent"
        assert captured["data"]["metadata"]["task_depth"] == 2

    def test_depends_on_current_false_no_parent_dep(self):
        self._setup_rt(task_id="my-parent")
        captured = {}

        def fake(url, timeout=None):
            if not isinstance(url, str) and url.get_method() == "POST":
                captured["data"] = json.loads(url.data.decode())
                return _FakeUrlopenResponse({"task": {"id": "fire-and-forget", "metadata": {}}})
            return _FakeUrlopenResponse({
                "tasks": [{"id": "my-parent", "metadata": {"task_depth": 1}}]
            })

        with patch("urllib.request.urlopen", side_effect=fake):
            from swarm.tools.tasks import create_subtask
            result = create_subtask("fire and forget", depends_on_current=False)
        assert result["ok"] is True
        assert captured["data"]["dependencies"] == []


class _FakeUrlopenResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class TestDelegateTaskBatch:
    def test_delegate_task_batch_rejects_missing_files(self, tmp_path):
        rt.WORKSPACE = tmp_path / "workspace"
        rt.PROJECT = "test-proj"
        rt.TASK_ID = "parent-1"
        rt._sync_core_globals()
        result = rt.delegate_task_batch([{"description": "part a"}], "integrate", "test-proj")
        assert result["ok"] is False
        assert "must declare files" in result["error"]

    def test_delegate_task_batch_rejects_too_many_children(self, tmp_path):
        rt.WORKSPACE = tmp_path / "workspace"
        rt.PROJECT = "test-proj"
        rt.TASK_ID = "parent-1"
        rt._sync_core_globals()
        result = rt.delegate_task_batch(
            [{"description": f"part {i}", "files": [f"file_{i}.gd"]} for i in range(7)],
            "integrate",
            "test-proj",
        )
        assert result["ok"] is False
        assert "rollout limit of 6 children" in result["error"]

    def test_delegate_task_batch_rejects_overlapping_parallel_writes(self, tmp_path):
        rt.WORKSPACE = tmp_path / "workspace"
        rt.PROJECT = "test-proj"
        rt.TASK_ID = "parent-1"
        rt._sync_core_globals()
        result = rt.delegate_task_batch([
            {"description": "part a", "files": ["shared.gd"]},
            {"description": "part b", "files": ["shared.gd"]},
        ], "integrate", "test-proj")
        assert result["ok"] is False
        assert "overlapping delegated write scope" in result["error"]

    def test_delegate_task_batch_allows_explicit_sequential_overlap(self, tmp_path):
        rt.WORKSPACE = tmp_path / "workspace"
        rt.PROJECT = "test-proj"
        rt.TASK_ID = "parent-1"
        rt._sync_core_globals()

        with patch("swarm.tools.core._ur.urlopen", return_value=_FakeUrlopenResponse({
            "ids": ["child-a", "child-b"],
            "id_map": {"0": "child-a", "1": "child-b"},
        })):
            result = rt.delegate_task_batch([
                {"description": "part a", "files": ["shared.gd"]},
                {"description": "part b", "files": ["shared.gd"], "depends_on_children": [0]},
            ], "integrate", "test-proj")

        assert result["ok"] is True
        assert result["count"] == 2

    def test_delegate_task_batch_integrate_creates_successor_and_patches_parent(self, tmp_path):
        rt.WORKSPACE = tmp_path / "workspace"
        rt.PROJECT = "test-proj"
        rt.TASK_ID = "parent-1"
        rt.TASK_TYPE = "feature"
        rt.TASK_PRIORITY = 77
        rt._sync_core_globals()

        calls = []

        def fake_urlopen(req, timeout=0):
            payload = json.loads(req.data.decode()) if getattr(req, "data", None) else None
            calls.append((req.get_method(), req.full_url, payload))
            if req.get_method() == "POST" and req.full_url.endswith("/api/tasks/batch"):
                return _FakeUrlopenResponse({
                    "ids": ["child-a", "child-b"],
                    "id_map": {"0": "child-a", "1": "child-b"},
                })
            if req.get_method() == "POST" and req.full_url.endswith("/api/tasks"):
                return _FakeUrlopenResponse({"task": {"id": "integration-1"}})
            if req.get_method() == "GET" and req.full_url.endswith("/api/tasks/parent-1"):
                return _FakeUrlopenResponse({"task": {"id": "parent-1", "metadata": {"existing": True}}})
            if req.get_method() == "PATCH" and req.full_url.endswith("/api/tasks/parent-1"):
                return _FakeUrlopenResponse({"task": {"id": "parent-1", "metadata": payload["metadata"]}})
            raise AssertionError(f"Unexpected request: {req.get_method()} {req.full_url}")

        with patch("swarm.tools.core._ur.urlopen", side_effect=fake_urlopen):
            result = rt.delegate_task_batch([
                {"description": "part a", "files": ["a.gd"]},
                {"description": "part b", "files": ["b.gd"], "depends_on_children": [0]},
            ], "integrate", "test-proj")

        assert result["ok"] is True
        assert result["successor_task_id"] == "integration-1"
        assert result["successor_kind"] == "integration"
        assert result["parent_action"] == "complete_parent"

        batch_call = next(call for call in calls if call[0] == "POST" and call[1].endswith("/api/tasks/batch"))
        batch_payload = batch_call[2]
        assert batch_payload["tasks"][0]["dependencies"] == ["parent-1"]

        successor_call = [call for call in calls if call[0] == "POST" and call[1].endswith("/api/tasks")][0]
        assert successor_call[2]["dependencies"] == ["child-a", "child-b"]
        assert successor_call[2]["metadata"]["delegation_successor_kind"] == "integration"

        patch_call = next(call for call in calls if call[0] == "PATCH")
        assert patch_call[2]["metadata"]["delegation_batch_id"] == result["delegation_batch_id"]
        assert patch_call[2]["metadata"]["delegation_successor_task_id"] == "integration-1"
        assert patch_call[2]["metadata"]["existing"] is True

    def test_delegate_task_batch_wait_creates_resume_successor(self, tmp_path):
        rt.WORKSPACE = tmp_path / "workspace"
        rt.PROJECT = "test-proj"
        rt.TASK_ID = "parent-1"
        rt.TASK_TYPE = "bug"
        rt._sync_core_globals()

        calls = []

        def fake_urlopen(req, timeout=0):
            payload = json.loads(req.data.decode()) if getattr(req, "data", None) else None
            calls.append((req.get_method(), req.full_url, payload))
            if req.get_method() == "POST" and req.full_url.endswith("/api/tasks/batch"):
                return _FakeUrlopenResponse({"ids": ["child-a"], "id_map": {"0": "child-a"}})
            if req.get_method() == "POST" and req.full_url.endswith("/api/tasks"):
                return _FakeUrlopenResponse({"task": {"id": "resume-1"}})
            if req.get_method() == "GET":
                return _FakeUrlopenResponse({"task": {"id": "parent-1", "metadata": {}}})
            if req.get_method() == "PATCH":
                return _FakeUrlopenResponse({"task": {"id": "parent-1", "metadata": payload["metadata"]}})
            raise AssertionError(f"Unexpected request: {req.get_method()} {req.full_url}")

        with patch("swarm.tools.core._ur.urlopen", side_effect=fake_urlopen):
            result = rt.delegate_task_batch([
                {"description": "part a", "files": ["bugfix.gd"]},
            ], "wait", "test-proj")

        assert result["ok"] is True
        assert result["successor_task_id"] == "resume-1"
        assert result["successor_kind"] == "resume"
        successor_call = [call for call in calls if call[0] == "POST" and call[1].endswith("/api/tasks")][0]
        assert successor_call[2]["type"] == "bug"
        assert successor_call[2]["metadata"]["delegation_successor_kind"] == "resume"

    def test_delegate_task_batch_replace_has_no_successor(self, tmp_path):
        rt.WORKSPACE = tmp_path / "workspace"
        rt.PROJECT = "test-proj"
        rt.TASK_ID = "parent-1"
        rt.TASK_TYPE = "feature"
        rt._sync_core_globals()

        calls = []

        def fake_urlopen(req, timeout=0):
            payload = json.loads(req.data.decode()) if getattr(req, "data", None) else None
            calls.append((req.get_method(), req.full_url, payload))
            if req.get_method() == "POST" and req.full_url.endswith("/api/tasks/batch"):
                return _FakeUrlopenResponse({"ids": ["child-a"], "id_map": {"0": "child-a"}})
            if req.get_method() == "GET":
                return _FakeUrlopenResponse({"task": {"id": "parent-1", "metadata": {}}})
            if req.get_method() == "PATCH":
                return _FakeUrlopenResponse({"task": {"id": "parent-1", "metadata": payload["metadata"]}})
            raise AssertionError(f"Unexpected request: {req.get_method()} {req.full_url}")

        with patch("swarm.tools.core._ur.urlopen", side_effect=fake_urlopen):
            result = rt.delegate_task_batch([
                {"description": "part a", "files": ["feature_a.gd"]},
            ], "replace", "test-proj")

        assert result["ok"] is True
        assert "successor_task_id" not in result
        assert result["parent_action"] == "complete_parent"
        assert not any(call[0] == "POST" and call[1].endswith("/api/tasks") for call in calls)


# ---------------------------------------------------------------------------
# call_llm()
# ---------------------------------------------------------------------------

class TestCallLlm:
    def _anthropic_sse_lines(self, text):
        """Return SSE lines for a simple text response."""
        import json
        lines = [
            f"data: {json.dumps({'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}})}",
            f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}",
            f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}})}",
            f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}",
            f"data: {json.dumps({'type': 'message_delta', 'delta': {}, 'usage': {'output_tokens': 5}})}",
            f"data: {json.dumps({'type': 'message_stop'})}",
        ]
        return lines

    def _anthropic_resp(self, text, status=200):
        m = MagicMock()
        m.status_code = status
        m.text = text
        if status == 200:
            m.iter_lines.return_value = self._anthropic_sse_lines(text)
        return m

    def _openai_resp(self, text, status=200):
        m = MagicMock()
        m.status_code = status
        m.json.return_value = {"choices": [{"message": {"content": text}}]}
        m.text = text
        return m

    def test_anthropic_format_returns_text(self):
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}):
            with patch("requests.post", return_value=self._anthropic_resp("Hello!")):
                text, tokens, thinking = rt.call_llm("system", [{"role": "user", "content": "hi"}])
        assert text == "Hello!"
        assert isinstance(tokens, dict)

    def test_openai_format_returns_text(self):
        rt.LLM_PROVIDER = "openrouter"
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            with patch("requests.post", return_value=self._openai_resp("OpenAI reply")):
                text, tokens, thinking = rt.call_llm("system", [{"role": "user", "content": "hi"}])
        assert text == "OpenAI reply"
        assert isinstance(tokens, dict)

    def test_loopback_openai_provider_does_not_require_api_key(self):
        rt.LLM_PROVIDER = "shrimp"
        rt.LLM_PROVIDERS["shrimp"] = {
            "base_url": "http://127.0.0.1:8090/v1",
            "model": "MiniMax-M3",
            "api_key_env": "SHRIMP_ROUTER_API_KEY",
            "format": "openai",
            "max_tokens": 8096,
        }
        with patch.dict(os.environ, {"SHRIMP_ROUTER_API_KEY": ""}):
            with patch("requests.post", return_value=self._openai_resp("Shrimp reply")):
                text, tokens, thinking = rt.call_llm("system", [{"role": "user", "content": "hi"}])
        assert text == "Shrimp reply"
        assert isinstance(tokens, dict)

    def test_anthropic_native_format_returns_text(self):
        rt.LLM_PROVIDER = "claude"
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch("requests.post", return_value=self._anthropic_resp("Claude reply")):
                text, tokens, thinking = rt.call_llm("system", [{"role": "user", "content": "hi"}])
        assert text == "Claude reply"
        assert isinstance(tokens, dict)

    def test_missing_api_key_returns_error_string(self):
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "", "MINIMAX-API": ""}):
            text, tokens, thinking = rt.call_llm("system", [{"role": "user", "content": "hi"}])
        assert "MINIMAX_API_KEY" in text or "not set" in text.lower()

    def test_non_200_returns_error_string(self):
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}):
            with patch("requests.post", return_value=self._anthropic_resp("bad", status=400)):
                text, tokens, thinking = rt.call_llm("system", [{"role": "user", "content": "hi"}])
        assert "400" in text

    def test_429_is_retried_and_succeeds(self):
        responses = [
            self._anthropic_resp("", status=429),
            self._anthropic_resp("", status=429),
            self._anthropic_resp("Success after retry"),
        ]
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}):
            with patch("requests.post", side_effect=responses):
                with patch("swarm.llm_utils.time.sleep"):
                    text, tokens, thinking = rt.call_llm("sys", [{"role": "user", "content": "hi"}])
        assert text == "Success after retry"

    def test_network_exception_returns_error_after_retries(self):
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}):
            with patch("requests.post", side_effect=Exception("connection refused")):
                with patch("swarm.llm_utils.time.sleep"):
                    text, tokens, thinking = rt.call_llm("sys", [{"role": "user", "content": "hi"}])
        assert "Error" in text or "error" in text.lower()

    def test_truncated_stream_returns_error(self):
        import json
        # Stream that ends without message_stop or [DONE]
        truncated_lines = [
            f"data: {json.dumps({'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}})}",
            f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'partial'}})}",
            # connection dropped here -- no message_stop
        ]
        m = MagicMock()
        m.status_code = 200
        m.iter_lines.return_value = truncated_lines
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}):
            with patch("requests.post", return_value=m):
                text, tokens, thinking = rt.call_llm("system", [{"role": "user", "content": "hi"}])
        assert "truncated" in text.lower()
        assert tokens == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    def test_minimax_provider_uses_minimax_url(self):
        captured = []

        def capture(url, **kwargs):
            captured.append(url)
            return self._anthropic_resp("ok")

        with patch.dict(os.environ, {"MINIMAX_API_KEY": "key"}):
            with patch("requests.post", side_effect=capture):
                rt.call_llm("sys", [])

        assert any("minimax" in u.lower() for u in captured)

    def test_openai_format_prepends_system_message(self):
        rt.LLM_PROVIDER = "openrouter"
        bodies = []

        def capture(url, **kwargs):
            bodies.append(kwargs.get("json", {}))
            return self._openai_resp("ok")

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "key"}):
            with patch("requests.post", side_effect=capture):
                rt.call_llm("MY SYSTEM PROMPT", [{"role": "user", "content": "hello"}])

        msgs = bodies[0].get("messages", [])
        system_msgs = [m for m in msgs if m.get("role") == "system"]
        assert any("MY SYSTEM PROMPT" in m.get("content", "") for m in system_msgs)

    def test_loopback_provider_sends_shrimp_router_headers(self):
        rt.LLM_PROVIDER = "shrimp"
        rt.TASK_TYPE = "bug"
        rt._ROUTING_LOOP = 51
        rt._ROUTING_COMMITS = 0
        rt.LLM_PROVIDERS["shrimp"] = {
            "base_url": "http://127.0.0.1:8090/v1",
            "model": "MiniMax-M3",
            "api_key_env": "",
            "format": "openai",
            "max_tokens": 8096,
        }
        headers_used = []

        def capture(url, headers=None, **kwargs):
            headers_used.append(headers or {})
            return self._openai_resp("ok")

        with patch.dict(os.environ, {"SHRIMP_ROUTER_HINTS": "1"}):
            with patch("requests.post", side_effect=capture):
                rt.call_llm("sys", [])

        assert headers_used[0]["X-Task-Type"] == "bug"
        assert headers_used[0]["X-Loop-Count"] == "51"
        assert headers_used[0]["X-Has-Commits"] == "false"

    def test_anthropic_native_uses_x_api_key_header(self):
        rt.LLM_PROVIDER = "claude"
        headers_used = []

        def capture(url, headers=None, **kwargs):
            headers_used.append(headers or {})
            return self._anthropic_resp("ok")

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "my-claude-key"}):
            with patch("requests.post", side_effect=capture):
                rt.call_llm("sys", [])

        assert any(h.get("x-api-key") == "my-claude-key" for h in headers_used)


# ---------------------------------------------------------------------------
# main() -- full agent loop
# ---------------------------------------------------------------------------

class TestMainLoop:
    def test_task_complete_exits_zero(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        with patch("swarm.agent_runtime.call_llm", return_value=("TASK_COMPLETE", {"input": 0, "output": 0}, [])):
            code = rt.main()
        assert code == 0

    def test_tool_result_fed_back_into_conversation(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        responses = iter([
            '[TOOL_CALL]{"tool": "list_files", "args": {"path": "."}}[/TOOL_CALL]',
            "TASK_COMPLETE",
        ])
        calls = []

        def fake_llm(sys_p, msgs, **kwargs):
            calls.append(list(msgs))
            return next(responses), {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm):
            rt.main()

        # Second LLM call should have the tool result as the last user message
        second_msgs = calls[1]
        last = second_msgs[-1]
        assert last["role"] == "user"
        assert "list_files" in last["content"]

    def test_no_tool_calls_after_nudge_fails_closed(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        with patch("swarm.agent_runtime.call_llm", return_value=("I'm thinking...", {"input": 0, "output": 0}, [])):
            code = rt.main()
        assert code == 1

    def test_three_consecutive_truncated_tool_calls_fail_closed(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        truncated = '[TOOL_CALL]{"tool": "write_file", "args": {"path": "x"'
        call_count = 0

        def fake_llm(sys_p, msgs, **kwargs):
            nonlocal call_count
            call_count += 1
            return truncated, {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm):
            code = rt.main()

        assert code == 1
        assert call_count == 3

    def test_playthrough_bot_rejects_bare_task_complete(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        rt.TASK_TYPE = "playthrough_bot"
        rt.PLAYTHROUGH_BOT_SYSTEM = "Build bot"
        rt.PLAYTHROUGH_BOT_USER = "Run bot"

        with patch(
            "swarm.agent_runtime.call_llm",
            return_value=("TASK_COMPLETE", {"input": 0, "output": 0}, []),
        ):
            code = rt.main()

        assert code == 1

    def test_playthrough_bot_accepts_successful_self_test(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        rt.TASK_TYPE = "playthrough_bot"
        rt.PLAYTHROUGH_BOT_SYSTEM = "Build bot"
        rt.PLAYTHROUGH_BOT_USER = "Run bot"
        responses = iter([
            '[TOOL_CALL]{"tool": "run_command", "args": '
            '{"command": "python tests/playthrough_bot.py --project-path ."}}[/TOOL_CALL]',
            "TASK_COMPLETE",
        ])

        def fake_llm(sys_p, msgs, **kwargs):
            return next(responses), {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm), patch(
            "swarm.agent_runtime.execute_tool",
            return_value={
                "ok": True,
                "stdout": (
                    "✓ PASSED: terminal state reached\n"
                    'PLAYTHROUGH_RESULT: {"status":"success","outcome":"complete",'
                    '"progress":{"completed":true,"wave":6}}'
                ),
                "stderr": "",
            },
        ):
            code = rt.main()

        assert code == 0

    def test_playthrough_bot_rejects_early_game_over_receipt(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        rt.TASK_TYPE = "playthrough_bot"
        rt.PLAYTHROUGH_BOT_SYSTEM = "Build bot"
        rt.PLAYTHROUGH_BOT_USER = "Run bot"
        rt.MAX_TOOL_LOOPS = 2
        responses = iter([
            '[TOOL_CALL]{"tool": "run_command", "args": '
            '{"command": "python3 tests/playthrough_bot.py --project-path ."}}[/TOOL_CALL]',
            "TASK_COMPLETE",
        ])

        def fake_llm(sys_p, msgs, **kwargs):
            return next(responses), {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm), patch(
            "swarm.agent_runtime.execute_tool",
            return_value={
                "ok": True,
                "stdout": (
                    "✓ PASSED: terminal state reached\n"
                    'PLAYTHROUGH_RESULT: {"status":"success","outcome":"game_over",'
                    '"progress":{"completed":false,"wave":1}}'
                ),
                "stderr": "",
            },
        ):
            code = rt.main()

        assert code == 1

    def test_playthrough_bot_does_not_accept_command_that_only_mentions_path(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        rt.TASK_TYPE = "playthrough_bot"
        rt.PLAYTHROUGH_BOT_SYSTEM = "Build bot"
        rt.PLAYTHROUGH_BOT_USER = "Run bot"
        rt.MAX_TOOL_LOOPS = 2
        responses = iter([
            '[TOOL_CALL]{"tool": "run_command", "args": '
            '{"command": "echo tests/playthrough_bot.py"}}[/TOOL_CALL]',
            "TASK_COMPLETE",
        ])

        def fake_llm(sys_p, msgs, **kwargs):
            return next(responses), {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm), patch(
            "swarm.agent_runtime.execute_tool",
            return_value={"ok": True, "stdout": "tests/playthrough_bot.py", "stderr": ""},
        ):
            code = rt.main()

        assert code == 1

    def test_playthrough_bot_rejects_pipe_masked_failure(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        rt.TASK_TYPE = "playthrough_bot"
        rt.PLAYTHROUGH_BOT_SYSTEM = "Build bot"
        rt.PLAYTHROUGH_BOT_USER = "Run bot"
        rt.MAX_TOOL_LOOPS = 2
        responses = iter([
            '[TOOL_CALL]{"tool": "run_command", "args": '
            '{"command": "timeout 180 python3 tests/playthrough_bot.py | tail -20"}}[/TOOL_CALL]',
            "TASK_COMPLETE",
        ])

        def fake_llm(sys_p, msgs, **kwargs):
            return next(responses), {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm), patch(
            "swarm.agent_runtime.execute_tool",
            return_value={
                "ok": True,
                "stdout": "python3: command not found",
                "stderr": "",
            },
        ):
            code = rt.main()

        assert code == 1

    def test_loop_limit_caps_llm_calls(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        rt.MAX_TOOL_LOOPS = 3
        call_count = [0]

        def fake_llm(sys_p, msgs, **kwargs):
            call_count[0] += 1
            return '[TOOL_CALL]{"tool": "list_files", "args": {"path": "."}}[/TOOL_CALL]', {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm):
            code = rt.main()

        assert call_count[0] == 3
        assert code == 0

    def test_art_pass_loop_limit_auto_commit_does_not_push(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        rt.TASK_TYPE = "art_pass"
        rt.ART_PASS_SYSTEM = "Art pass system"
        rt.ART_PASS_USER = "Art pass user"
        rt.MAX_TOOL_LOOPS = 1
        def fake_llm(sys_p, msgs, **kwargs):
            return '[TOOL_CALL]{"tool": "run_command", "args": {"command": "printf changed > art_note.txt"}}[/TOOL_CALL]', {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm), \
             patch("swarm.agent_runtime.git_push") as git_push_mock:
            code = rt.main()

        assert code == 0
        git_push_mock.assert_not_called()
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=tmp_path / "workspace" / "test-proj",
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        assert log == "art: update art_note.txt"

    def test_python_project_uses_python_feature_prompt(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        (proj / "requirements.txt").write_text("flask\n")
        _init_git(proj)
        rt.TASK_TYPE = "feature"
        captured = []

        def fake_llm(sys_p, msgs, **kwargs):
            captured.append(sys_p)
            return "TASK_COMPLETE", {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm):
            rt.main()

        assert captured[0].endswith(rt.PYTHON_FEATURE_SYSTEM)

    def test_python_project_bug_uses_python_bug_prompt(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        (proj / "requirements.txt").write_text("flask\n")
        _init_git(proj)
        rt.TASK_TYPE = "bug"
        captured = []

        def fake_llm(sys_p, msgs, **kwargs):
            captured.append(sys_p)
            return "TASK_COMPLETE", {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm):
            rt.main()

        assert captured[0].endswith(rt.PYTHON_BUG_SYSTEM)

    def test_godot_project_uses_godot_feature_prompt(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        rt.TASK_TYPE = "feature"
        captured = []

        def fake_llm(sys_p, msgs, **kwargs):
            captured.append(sys_p)
            return "TASK_COMPLETE", {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm):
            rt.main()

        assert captured[0].endswith(rt.FEATURE_SYSTEM)

    def test_bug_task_uses_bug_system_prompt(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        rt.TASK_TYPE = "bug"
        captured = []

        def fake_llm(sys_p, msgs, **kwargs):
            captured.append(sys_p)
            return "TASK_COMPLETE", {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm):
            rt.main()

        assert captured[0].endswith(rt.BUG_SYSTEM)

    def test_polish_task_uses_polish_system_prompt(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        rt.TASK_TYPE = "polish"
        captured = []

        def fake_llm(sys_p, msgs, **kwargs):
            captured.append(sys_p)
            return "TASK_COMPLETE", {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm):
            rt.main()

        assert captured[0].endswith(rt.POLISH_SYSTEM)

    def test_unknown_tool_error_fed_back_to_llm(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        responses = iter([
            '[TOOL_CALL]{"tool": "fake_tool_xyz", "args": {}}[/TOOL_CALL]',
            "TASK_COMPLETE",
        ])
        calls = []

        def fake_llm(sys_p, msgs, **kwargs):
            calls.append(list(msgs))
            return next(responses), {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm):
            rt.main()

        last_user_msg = calls[1][-1]["content"]
        assert "fake_tool_xyz" in last_user_msg or "Unknown tool" in last_user_msg

    def test_write_file_actually_creates_file(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        responses = iter([
            '[TOOL_CALL]{"tool": "write_file", "args": {"path": "result.txt", "content": "written!"}}[/TOOL_CALL]',
            "TASK_COMPLETE",
        ])
        with patch("swarm.agent_runtime.call_llm", side_effect=lambda *a, **kw: (next(responses), {"input": 0, "output": 0}, [])), \
             patch("swarm.runtime_helpers._lock_project_file", return_value={"ok": True}):
            rt.main()

        assert (tmp_path / "workspace" / "test-proj" / "result.txt").read_text() == "written!"

    def test_multiple_tool_calls_in_one_response_all_executed(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        two_calls = (
            '[TOOL_CALL]{"tool": "list_files", "args": {"path": "."}}[/TOOL_CALL]'
            '[TOOL_CALL]{"tool": "list_files", "args": {"path": "."}}[/TOOL_CALL]'
        )
        responses = iter([two_calls, "TASK_COMPLETE"])
        calls = []

        def fake_llm(sys_p, msgs, **kwargs):
            calls.append(list(msgs))
            return next(responses), {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm):
            rt.main()

        # Both tool results joined in one user message
        last_user = calls[1][-1]["content"]
        assert last_user.count("list_files") == 2

    def test_write_file_mention_without_tool_call_sends_retry(self, tmp_path):
        """Truncated response mentioning write_file triggers a retry nudge."""
        _init_git(tmp_path / "workspace" / "test-proj")
        responses = iter([
            # mentions write_file but no valid [TOOL_CALL] block
            "I'll write_file with the new content now...",
            "TASK_COMPLETE",
        ])
        calls = []

        def fake_llm(sys_p, msgs, **kwargs):
            calls.append(list(msgs))
            return next(responses), {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm):
            rt.main()

        # A retry message about smaller files should have been injected
        all_user_msgs = [m["content"] for c in calls for m in c if m["role"] == "user"]
        assert any("smaller files" in msg for msg in all_user_msgs)

    def test_conversation_accumulates_across_loops(self, tmp_path):
        _init_git(tmp_path / "workspace" / "test-proj")
        responses = iter([
            '[TOOL_CALL]{"tool": "list_files", "args": {"path": "."}}[/TOOL_CALL]',
            '[TOOL_CALL]{"tool": "list_files", "args": {"path": "."}}[/TOOL_CALL]',
            "TASK_COMPLETE",
        ])
        calls = []

        def fake_llm(sys_p, msgs, **kwargs):
            calls.append(list(msgs))
            return next(responses), {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm):
            rt.main()

        # Each subsequent call should have a longer conversation
        assert len(calls[0]) < len(calls[1]) < len(calls[2])

    def test_task_complete_in_tool_args_does_not_trigger_early_exit(self, tmp_path):
        """TASK_COMPLETE inside a tool call's args must NOT cause premature exit.

        Regression test: grep -n "TASK_COMPLETE" would previously fire the
        detector because the raw response contained the string.
        """
        _init_git(tmp_path / "workspace" / "test-proj")
        responses = iter([
            # Tool call whose args contain the string "TASK_COMPLETE"
            '[TOOL_CALL]{"tool": "run_command", "args": {"command": "grep -n \\"TASK_COMPLETE\\" main.py"}}[/TOOL_CALL]',
            "TASK_COMPLETE",
            # Reflection loop fires after TASK_COMPLETE -- let it complete cleanly
            "REFLECTION_COMPLETE",
        ])
        calls = []

        def fake_llm(sys_p, msgs, **kwargs):
            calls.append(list(msgs))
            return next(responses), {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm):
            rt.main()

        # Must have run at least TWO main-loop calls: one for the tool call, one
        # for TASK_COMPLETE. A third call is the post-completion reflection loop.
        # The key invariant: TASK_COMPLETE inside tool *args* must NOT exit early
        # (which would produce only 1 call).
        assert len(calls) >= 2, (
            f"Expected at least 2 LLM calls but got {len(calls)}; "
            "TASK_COMPLETE in tool args likely triggered premature exit"
        )
        # Verify the first response was treated as a tool call, not an early exit
        first_response_msgs = calls[0]
        assert any("TASK_COMPLETE" not in str(m) for m in first_response_msgs), (
            "First LLM call should not have exited early on TASK_COMPLETE in tool args"
        )

    def test_pyproject_toml_triggers_python_path(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        (proj / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        _init_git(proj)
        rt.TASK_TYPE = "feature"
        captured = []

        def fake_llm(sys_p, msgs, **kwargs):
            captured.append(sys_p)
            return "TASK_COMPLETE", {"input": 0, "output": 0}, []

        with patch("swarm.agent_runtime.call_llm", side_effect=fake_llm):
            rt.main()

        assert captured[0].endswith(rt.PYTHON_FEATURE_SYSTEM)


# ---------------------------------------------------------------------------
# _safe_cwd()
# ---------------------------------------------------------------------------

class TestSafeCwd:
    """Tests for the _safe_cwd() helper in agent_runtime."""

    def test_returns_explicit_cwd_when_given_and_exists(self, tmp_path):
        existing_dir = tmp_path / "explicit_dir"
        existing_dir.mkdir()
        result = rt._safe_cwd(str(existing_dir))
        assert result == str(existing_dir)

    def test_returns_project_root_when_cwd_not_given_and_root_exists(self, tmp_path):
        ws = tmp_path / "workspace"
        proj_dir = ws / "my-project"
        proj_dir.mkdir(parents=True)
        rt.WORKSPACE = ws
        rt.PROJECT = "my-project"
        rt.PROJECT_PATH_OVERRIDE = ""
        rt._sync_core_globals()
        result = rt._safe_cwd()
        assert result == str(proj_dir)

    def test_falls_back_to_workspace_when_project_dir_missing(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        # Do NOT create the project subdirectory
        rt.WORKSPACE = ws
        rt.PROJECT = "nonexistent-project"
        rt.PROJECT_PATH_OVERRIDE = ""
        rt._sync_core_globals()
        result = rt._safe_cwd()
        assert result == str(ws)

    def test_falls_back_to_getcwd_when_workspace_also_missing(self, tmp_path):
        rt.WORKSPACE = tmp_path / "no-such-workspace"
        rt.PROJECT = "no-such-project"
        rt.PROJECT_PATH_OVERRIDE = ""
        rt._sync_core_globals()
        result = rt._safe_cwd()
        assert result == os.getcwd()

    def test_respects_project_path_override(self, tmp_path):
        override_dir = tmp_path / "override"
        override_dir.mkdir()
        rt.PROJECT_PATH_OVERRIDE = str(override_dir)
        rt.WORKSPACE = tmp_path / "workspace"
        rt.PROJECT = "irrelevant"
        rt._sync_core_globals()
        # _safe_cwd() calls _project_root() which returns the override
        result = rt._safe_cwd()
        assert result == str(override_dir)

    def test_explicit_cwd_takes_priority_over_project_root(self, tmp_path):
        ws = tmp_path / "workspace"
        proj_dir = ws / "test-proj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        explicit = tmp_path / "explicit"
        explicit.mkdir()
        rt.WORKSPACE = ws
        rt.PROJECT = "test-proj"
        rt.PROJECT_PATH_OVERRIDE = ""
        rt._sync_core_globals()
        result = rt._safe_cwd(str(explicit))
        assert result == str(explicit)
