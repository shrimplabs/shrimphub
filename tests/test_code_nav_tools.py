"""
Tests for code navigation tools: get_file_outline, read_file_range, patch_file.
"""
import os
import pytest
from pathlib import Path

import swarm.agent_runtime as rt


@pytest.fixture(autouse=True)
def reset_rt(tmp_path):
    """Reset module globals before each test."""
    proj_dir = tmp_path / "workspace" / "test-proj"
    proj_dir.mkdir(parents=True)

    rt.WORKSPACE = tmp_path / "workspace"
    rt.PROJECT = "test-proj"
    rt.TASK_TYPE = "feature"
    rt.TASK_DESC = "Test task"
    rt.TASK_ID = "task-001"
    rt.READONLY = False
    rt.IGNORE_DIRS = {"addons", ".git", ".godot"}
    rt.IGNORE_EXTENSIONS = set()
    rt.CLAIMED_FILE_PATHS = set()  # clear stale file claims from other test modules

    rt._sync_core_globals()

    yield tmp_path


class TestGetFileOutline:
    """Tests for get_file_outline function."""

    def test_returns_functions_and_classes_for_python(self, tmp_path):
        """get_file_outline returns correct functions with line numbers for a Python file."""
        proj = tmp_path / "workspace" / "test-proj"
        py_file = proj / "example.py"
        py_file.write_text('''
class MyClass:
    def method(self):
        pass

def top_level_func():
    pass

async def async_func():
    pass
''')

        result = rt.get_file_outline("example.py")
        assert result["ok"] is True
        outline = result["outline"]
        
        # Should find: class MyClass, method, top_level_func, async_func
        names = [item["name"] for item in outline]
        assert "MyClass" in names
        assert "method" in names
        assert "top_level_func" in names
        assert "async_func" in names
        
        # Check line numbers exist
        for item in outline:
            assert "line" in item
            assert "end_line" in item
            assert item["line"] is not None

    def test_handles_gdscript(self, tmp_path):
        """get_file_outline handles GDScript files."""
        proj = tmp_path / "workspace" / "test-proj"
        gd_file = proj / "example.gd"
        gd_file.write_text('''
extends Node

class MyClass:
    var value: int
    
    func _ready():
        pass

func _process(delta):
    pass
''')

        result = rt.get_file_outline("example.gd")
        assert result["ok"] is True
        outline = result["outline"]
        
        names = [item["name"] for item in outline]
        assert "MyClass" in names
        assert "_ready" in names
        assert "_process" in names

    def test_unsupported_file_type(self, tmp_path):
        """get_file_outline returns error for unsupported file types."""
        proj = tmp_path / "workspace" / "test-proj"
        txt_file = proj / "example.txt"
        txt_file.write_text("some text")

        result = rt.get_file_outline("example.txt")
        assert result["ok"] is False
        assert "unsupported" in result["error"]

    def test_file_not_found(self, tmp_path):
        """get_file_outline returns error when file doesn't exist."""
        result = rt.get_file_outline("nonexistent.py")
        assert result["ok"] is False
        assert "not found" in result["error"]


class TestReadFileRange:
    """Tests for read_file_range function."""

    def test_returns_correct_lines(self, tmp_path):
        """read_file_range returns correct lines."""
        proj = tmp_path / "workspace" / "test-proj"
        py_file = proj / "lines.py"
        py_file.write_text("\n".join(f"line {i}" for i in range(1, 21)))

        result = rt.read_file_range("lines.py", 5, 10)
        assert result["ok"] is True
        assert result["start_line"] == 5
        assert result["end_line"] == 10
        assert result["total_lines"] == 20
        assert "line 5" in result["content"]
        assert "line 10" in result["content"]
        assert "line 4" not in result["content"]

    def test_truncates_at_300_lines(self, tmp_path):
        """read_file_range truncates at 300 lines."""
        proj = tmp_path / "workspace" / "test-proj"
        py_file = proj / "long.py"
        py_file.write_text("\n".join(f"line {i}" for i in range(1, 501)))

        result = rt.read_file_range("long.py", 1, 500)
        assert result["ok"] is True
        assert result["truncated"] is True
        assert result["end_line"] == 301  # 1 + 300
        assert result["total_lines"] == 500

    def test_file_not_found(self, tmp_path):
        """read_file_range returns error when file doesn't exist."""
        result = rt.read_file_range("nonexistent.py", 1, 10)
        assert result["ok"] is False
        assert "not found" in result["error"]


class TestPatchFile:
    """Tests for patch_file function."""

    def test_replaces_single_match(self, tmp_path):
        """patch_file replaces single match."""
        proj = tmp_path / "workspace" / "test-proj"
        py_file = proj / "patch.py"
        py_file.write_text("timeout = 120\nother = 50")

        result = rt.patch_file("patch.py", "timeout = 120", "timeout = 300")
        assert result["ok"] is True
        assert result["replaced"] == 1
        
        content = py_file.read_text()
        assert "timeout = 300" in content
        assert "timeout = 120" not in content

    def test_errors_on_no_match(self, tmp_path):
        """patch_file errors when string not found."""
        proj = tmp_path / "workspace" / "test-proj"
        py_file = proj / "patch.py"
        py_file.write_text("timeout = 120")

        result = rt.patch_file("patch.py", "nonexistent", "something")
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_errors_on_multiple_matches(self, tmp_path):
        """patch_file errors when multiple matches found."""
        proj = tmp_path / "workspace" / "test-proj"
        py_file = proj / "patch.py"
        py_file.write_text("timeout = 120\ntimeout = 120")

        result = rt.patch_file("patch.py", "timeout = 120", "timeout = 300")
        assert result["ok"] is False
        assert "ambiguous" in result["error"]
        assert "2" in result["error"]  # Should mention count

    def test_readonly_blocks_patch(self, tmp_path):
        """patch_file is disabled in readonly mode."""
        rt.READONLY = True
        rt._sync_core_globals()
        proj = tmp_path / "workspace" / "test-proj"
        py_file = proj / "patch.py"
        py_file.write_text("timeout = 120")

        result = rt.patch_file("patch.py", "timeout = 120", "timeout = 300")
        assert result["ok"] is False
        assert "Read-only" in result["error"]

    def test_file_not_found(self, tmp_path):
        """patch_file returns error when file doesn't exist."""
        result = rt.patch_file("nonexistent.py", "old", "new")
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_blocks_vendor_addons_patch(self, tmp_path):
        """patch_file refuses to modify vendored addon code."""
        proj = tmp_path / "workspace" / "test-proj"
        vendor_file = proj / "addons" / "gut" / "helper.gd"
        vendor_file.parent.mkdir(parents=True)
        vendor_file.write_text("x = 1")

        result = rt.patch_file("addons/gut/helper.gd", "x = 1", "x = 2")
        assert result["ok"] is False
        assert "vendor code" in result["error"]
        assert vendor_file.read_text() == "x = 1"


class TestWriteAppendVendorProtection:
    def test_write_file_blocks_vendor_addons(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        (proj / "addons" / "gut").mkdir(parents=True)

        result = rt.write_file("addons/gut/new_file.gd", "extends Node")
        assert result["ok"] is False
        assert "vendor code" in result["error"]
        assert not (proj / "addons" / "gut" / "new_file.gd").exists()

    def test_append_file_blocks_vendor_addons(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        vendor_file = proj / "addons" / "gut" / "keep.txt"
        vendor_file.parent.mkdir(parents=True)
        vendor_file.write_text("hello")

        result = rt.append_file("addons/gut/keep.txt", "\nworld")
        assert result["ok"] is False
        assert "vendor code" in result["error"]
        assert vendor_file.read_text() == "hello"

    def test_write_file_blocks_generated_godot_cache(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        (proj / ".godot").mkdir(parents=True)

        result = rt.write_file(".godot/editor_layout.cfg", "layout")
        assert result["ok"] is False
        assert "generated artifacts" in result["error"]
        assert not (proj / ".godot" / "editor_layout.cfg").exists()

    def test_append_file_blocks_generated_uid_files(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        uid_file = proj / "scripts" / "player.gd.uid"
        uid_file.parent.mkdir(parents=True)
        uid_file.write_text("uid://abc")

        result = rt.append_file("scripts/player.gd.uid", "\nuid://def")
        assert result["ok"] is False
        assert "generated artifacts" in result["error"]
        assert uid_file.read_text() == "uid://abc"


class TestDispatchTable:
    """Tests that new tools are properly registered in dispatch table."""

    def test_execute_tool_dispatches_get_file_outline(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        py_file = proj / "test.py"
        py_file.write_text("def foo(): pass")

        result = rt.execute_tool({"tool": "get_file_outline", "args": {"path": "test.py"}})
        assert result["ok"] is True
        assert "outline" in result

    def test_execute_tool_dispatches_read_file_range(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        py_file = proj / "test.py"
        py_file.write_text("\n".join(f"line {i}" for i in range(1, 11)))

        result = rt.execute_tool({"tool": "read_file_range", "args": {"path": "test.py", "start_line": 1, "end_line": 5}})
        assert result["ok"] is True
        assert "line 1" in result["content"]

    def test_execute_tool_dispatches_patch_file(self, tmp_path):
        proj = tmp_path / "workspace" / "test-proj"
        proj.mkdir(parents=True, exist_ok=True)
        py_file = proj / "test.py"
        py_file.write_text("x = 1")

        # execute_tool reads from swarm.tools.core globals, not agent_runtime —
        # sync them so patch_file resolves paths under the right project root.
        import swarm.tools.core as _core
        _core.WORKSPACE = tmp_path / "workspace"
        _core.PROJECT = "test-proj"

        # Pre-claim the file so execute_tool skips the live-API lock check.
        # (The lock API may be unreachable when API_PORT was changed by another
        # test module's fixture; this is a unit test of dispatch, not locking.)
        rt.CLAIMED_FILE_PATHS.add("test.py")

        result = rt.execute_tool({"tool": "patch_file", "args": {"path": "test.py", "old": "x = 1", "new": "x = 2"}})
        assert result["ok"] is True
        assert py_file.read_text() == "x = 2"
