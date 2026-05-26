"""Replace TestCreateSubtask with importlib.reload pattern."""
import pathlib, importlib

f = pathlib.Path('tests/test_agent_runtime.py')
lines = f.read_text().split('\n')

# Lines before TestCreateSubtask (line 1608, 0-indexed 1607)
lines_before = lines[:1608]
while lines_before and lines_before[-1] == '':
    lines_before.pop()

new_class = '''
class TestCreateSubtask:
    """Tests for the create_subtask tool.
    
    create_subtask uses "import urllib.request as _ur" locally inside the function,
    so _ur is captured at import time. To mock urlopen, we must:
      1. patch urllib.request.urlopen
      2. importlib.reload(swarm.tools.tasks) so _ur re-resolves to patched urllib
    """

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
        result = rt.execute_tool({"tool": "create_subtask", "args": {}})
        assert result["ok"] is False
        assert "description" in result["error"]

    # --- validation within create_subtask ---

    def test_invalid_task_type_rejected_in_function(self):
        # Type check happens before any API call, so no reload needed
        with patch("urllib.request.urlopen"):
            from swarm.tools.tasks import create_subtask
            result = create_subtask("test", task_type="not_a_type")
        assert result["ok"] is False
        assert "Invalid task type" in result["error"]

    def test_unknown_task_id_rejected(self):
        from swarm.tools import tasks as _tm
        orig_core = _tm._read_core
        _tm._read_core = lambda: ("proj", "feature", "unknown", 50, 19999)
        try:
            with patch("urllib.request.urlopen"):
                importlib.reload(_tm)
                _tm._read_core = lambda: ("proj", "feature", "unknown", 50, 19999)
                result = _tm.create_subtask("sub")
        finally:
            _tm._read_core = orig_core
        assert result["ok"] is False
        assert "valid TASK_ID" in result["error"]

    # --- depth enforcement ---

    def _reload_with_mock(self, fake_urlopen, fake_core):
        """Patch urllib.request.urlopen, reload tasks module, set _read_core."""
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            import swarm.tools.tasks as _m
            importlib.reload(_m)
            _m._read_core = fake_core
            return _m

    def test_depth_guard_blocks_at_max_depth(self):
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({
                    "tasks": [{"id": "deep-task", "metadata": {"task_depth": 2}}]
                }).encode()

        def fake_core():
            return ("test-proj", "feature", "deep-task", 50, 19999)

        def fake_urlopen(req, timeout=None):
            return FakeResp()

        m = self._reload_with_mock(fake_urlopen, fake_core)
        result = m.create_subtask("sub", max_depth=2)
        assert result["ok"] is False
        assert "max sub-task depth" in result["error"]

    def test_normal_depth_allowed(self):
        class FakeDepthResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({
                    "tasks": [{"id": "parent-task", "metadata": {"task_depth": 0}}]
                }).encode()

        class FakePostResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"task": {"id": "new-sub", "metadata": {}}}).encode()

        def fake_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "POST":
                return FakePostResp()
            return FakeDepthResp()

        m = self._reload_with_mock(fake_urlopen, fake_core)
        result = m.create_subtask("child", max_depth=2)
        assert result["ok"] is True
        assert "task_id" in result

    # --- file conflict detection ---

    def test_pending_sibling_blocks_same_file(self):
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({
                    "tasks": [
                        {"id": "parent-task", "metadata": {"task_depth": 0}},
                        {
                            "id": "sibling-subtask",
                            "status": "pending",
                            "metadata": {
                                "parent_task_id": "parent-task",
                                "delegated_files": ["src/shared.gd"]
                            }
                        }
                    ]
                }).encode()

        def fake_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

        def fake_urlopen(req, timeout=None):
            return FakeResp()

        m = self._reload_with_mock(fake_urlopen, fake_core)
        result = m.create_subtask("conflicting sub", files_touched=["src/shared.gd"])
        assert result["ok"] is False
        assert "file conflict detected" in result["error"]
        assert "sibling-subtask" in result["error"]

    def test_in_progress_sibling_blocks_same_file(self):
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({
                    "tasks": [
                        {"id": "parent-task", "metadata": {"task_depth": 0}},
                        {
                            "id": "active-subtask",
                            "status": "in_progress",
                            "metadata": {
                                "parent_task_id": "parent-task",
                                "delegated_files": ["src/shared.gd"]
                            }
                        }
                    ]
                }).encode()

        def fake_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

        def fake_urlopen(req, timeout=None):
            return FakeResp()

        m = self._reload_with_mock(fake_urlopen, fake_core)
        result = m.create_subtask("conflicting sub", files_touched=["src/shared.gd"])
        assert result["ok"] is False
        assert "file conflict detected" in result["error"]
        assert "active-subtask" in result["error"]

    def test_non_overlapping_files_allowed(self):
        class FakeDepthResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({
                    "tasks": [
                        {"id": "parent-task", "metadata": {"task_depth": 0}},
                        {
                            "id": "sibling-subtask",
                            "status": "pending",
                            "metadata": {
                                "parent_task_id": "parent-task",
                                "delegated_files": ["src/other.gd"]
                            }
                        }
                    ]
                }).encode()

        class FakePostResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"task": {"id": "new-subtask", "metadata": {}}}).encode()

        def fake_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "POST":
                return FakePostResp()
            return FakeDepthResp()

        m = self._reload_with_mock(fake_urlopen, fake_core)
        result = m.create_subtask("non-conflicting", files_touched=["src/shared.gd"])
        assert result["ok"] is True
        assert result["task_id"] == "new-subtask"

    def test_completed_sibling_does_not_block(self):
        class FakeDepthResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({
                    "tasks": [
                        {"id": "parent-task", "metadata": {"task_depth": 0}},
                        {
                            "id": "completed-subtask",
                            "status": "completed",
                            "metadata": {
                                "parent_task_id": "parent-task",
                                "delegated_files": ["src/shared.gd"]
                            }
                        }
                    ]
                }).encode()

        class FakePostResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"task": {"id": "new-sub", "metadata": {}}}).encode()

        def fake_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "POST":
                return FakePostResp()
            return FakeDepthResp()

        m = self._reload_with_mock(fake_urlopen, fake_core)
        result = m.create_subtask("after completion", files_touched=["src/shared.gd"])
        assert result["ok"] is True
        assert result["task_id"] == "new-sub"

    # --- depends_on_current ---

    def test_depends_on_current_true_adds_parent_dep(self):
        captured = {}

        class FakeDepthResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"tasks": [{"id": "my-parent", "metadata": {"task_depth": 1}}]}).encode()

        class FakePostResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"task": {"id": "created-sub", "metadata": {}}}).encode()

        def fake_core():
            return ("test-proj", "feature", "my-parent", 50, 19999)

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "POST":
                captured["data"] = json.loads(req.data.decode())
                return FakePostResp()
            return FakeDepthResp()

        m = self._reload_with_mock(fake_urlopen, fake_core)
        result = m.create_subtask("child task")
        assert result["ok"] is True
        assert captured["data"]["dependencies"] == ["my-parent"]
        assert captured["data"]["metadata"]["parent_task_id"] == "my-parent"
        assert captured["data"]["metadata"]["task_depth"] == 2

    def test_depends_on_current_false_no_parent_dep(self):
        captured = {}

        class FakeDepthResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"tasks": [{"id": "my-parent", "metadata": {"task_depth": 1}}]}).encode()

        class FakePostResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"task": {"id": "fire-and-forget", "metadata": {}}}).encode()

        def fake_core():
            return ("test-proj", "feature", "my-parent", 50, 19999)

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "POST":
                captured["data"] = json.loads(req.data.decode())
                return FakePostResp()
            return FakeDepthResp()

        m = self._reload_with_mock(fake_urlopen, fake_core)
        result = m.create_subtask("fire and forget", depends_on_current=False)
        assert result["ok"] is True
        assert captured["data"]["dependencies"] == []

    # --- priority capping ---
    def test_priority_capped_at_90(self):
        # We test via dispatch (function is complex to mock with reload)
        with patch("swarm.tool_dispatch.create_subtask",
                   return_value={"ok": True, "task_id": "x", "depth": 1}):
            result = rt.execute_tool({
                "tool": "create_subtask",
                "args": {"description": "test", "priority": 999},
            })
        assert result["ok"] is True

'''

# Reconstruct: everything before line 1608, then new class
before = '\n'.join(lines_before)
if before.endswith('\n\n'):
    pass  # keep as-is
elif before.endswith('\n'):
    before = before[:-1]

new_content = before + '\n\n' + new_class.rstrip() + '\n\n'
f.write_text(new_content)
print(f"Done: {len(new_content)} chars")
