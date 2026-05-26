"""Rewrite TestCreateSubtask with patch.object(urllib.request, 'urlopen')."""
import pathlib, importlib

f = pathlib.Path('tests/test_agent_runtime.py')
lines = f.read_text().split('\n')

# Lines before TestCreateSubtask (line 1608)
lines_before = lines[:1608]
while lines_before and lines_before[-1] == '':
    lines_before.pop()

new_class = '''
class TestCreateSubtask:
    """Tests for the create_subtask tool."""

    # All tests use patch.object(urllib.request, "urlopen", ...) which patches
    # the module attribute directly. This works even when swarm.tools.tasks
    # was already imported before the patch was applied, because _ur.urlopen
    # resolves via the module object reference.

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

    # --- validation (no API needed) ---

    def test_invalid_task_type_rejected(self):
        with patch.object(urllib.request, "urlopen",
                          side_effect=Exception("should not be called")):
            from swarm.tools.tasks import create_subtask
            result = create_subtask("test", task_type="not_a_type")
        assert result["ok"] is False
        assert "Invalid task type" in result["error"]

    def test_unknown_task_id_rejected(self):
        from swarm.tools import tasks as _tm
        orig = _tm._read_core
        try:
            _tm._read_core = lambda: ("proj", "feature", "unknown", 50, 19999)
            with patch.object(urllib.request, "urlopen",
                              side_effect=Exception("should not be called")):
                result = _tm.create_subtask("sub")
        finally:
            _tm._read_core = orig
        assert result["ok"] is False
        assert "valid TASK_ID" in result["error"]

    # --- depth enforcement ---

    def test_depth_guard_blocks_at_max_depth(self):
        from swarm.tools import tasks as _tm
        orig = _tm._read_core

        class FakeDepthResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({
                    "tasks": [{"id": "deep-task", "metadata": {"task_depth": 2}}]
                }).encode()

        def fake_urlopen(req, timeout=None):
            return FakeDepthResp()

        _tm._read_core = lambda: ("test-proj", "feature", "deep-task", 50, 19999)
        try:
            with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
                result = _tm.create_subtask("sub", max_depth=2)
        finally:
            _tm._read_core = orig

        assert result["ok"] is False
        assert "max sub-task depth" in result["error"]

    def test_normal_depth_allowed(self):
        from swarm.tools import tasks as _tm
        orig = _tm._read_core

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

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "POST":
                return FakePostResp()
            return FakeDepthResp()

        _tm._read_core = lambda: ("test-proj", "feature", "parent-task", 50, 19999)
        try:
            with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
                result = _tm.create_subtask("child")
        finally:
            _tm._read_core = orig

        assert result["ok"] is True
        assert "task_id" in result

    # --- file conflict detection ---

    def test_pending_sibling_blocks_same_file(self):
        from swarm.tools import tasks as _tm
        orig = _tm._read_core

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

        def fake_urlopen(req, timeout=None):
            return FakeResp()

        _tm._read_core = lambda: ("test-proj", "feature", "parent-task", 50, 19999)
        try:
            with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
                result = _tm.create_subtask("conflicting", files_touched=["src/shared.gd"])
        finally:
            _tm._read_core = orig

        assert result["ok"] is False
        assert "file conflict detected" in result["error"]
        assert "sibling-subtask" in result["error"]

    def test_in_progress_sibling_blocks_same_file(self):
        from swarm.tools import tasks as _tm
        orig = _tm._read_core

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

        def fake_urlopen(req, timeout=None):
            return FakeResp()

        _tm._read_core = lambda: ("test-proj", "feature", "parent-task", 50, 19999)
        try:
            with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
                result = _tm.create_subtask("conflicting", files_touched=["src/shared.gd"])
        finally:
            _tm._read_core = orig

        assert result["ok"] is False
        assert "file conflict detected" in result["error"]
        assert "active-subtask" in result["error"]

    def test_non_overlapping_files_allowed(self):
        from swarm.tools import tasks as _tm
        orig = _tm._read_core

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

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "POST":
                return FakePostResp()
            return FakeDepthResp()

        _tm._read_core = lambda: ("test-proj", "feature", "parent-task", 50, 19999)
        try:
            with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
                result = _tm.create_subtask("non-conflicting", files_touched=["src/shared.gd"])
        finally:
            _tm._read_core = orig

        assert result["ok"] is True
        assert result["task_id"] == "new-subtask"

    def test_completed_sibling_does_not_block(self):
        from swarm.tools import tasks as _tm
        orig = _tm._read_core

        class FakeDepthResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({
                    "tasks": [
                        {"id": "parent-task", "metadata": {"task_depth": 0}},
                        {
                            "id": "done-subtask",
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

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "POST":
                return FakePostResp()
            return FakeDepthResp()

        _tm._read_core = lambda: ("test-proj", "feature", "parent-task", 50, 19999)
        try:
            with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
                result = _tm.create_subtask("after-completion", files_touched=["src/shared.gd"])
        finally:
            _tm._read_core = orig

        assert result["ok"] is True
        assert result["task_id"] == "new-sub"

    # --- depends_on_current ---
    def test_depends_on_current_true_adds_parent_dep(self):
        from swarm.tools import tasks as _tm
        orig = _tm._read_core
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

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "POST":
                captured["data"] = json.loads(req.data.decode())
                return FakePostResp()
            return FakeDepthResp()

        _tm._read_core = lambda: ("test-proj", "feature", "my-parent", 50, 19999)
        try:
            with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
                result = _tm.create_subtask("child task")
        finally:
            _tm._read_core = orig

        assert result["ok"] is True
        assert captured["data"]["dependencies"] == ["my-parent"]
        assert captured["data"]["metadata"]["parent_task_id"] == "my-parent"
        assert captured["data"]["metadata"]["task_depth"] == 2

    def test_depends_on_current_false_no_parent_dep(self):
        from swarm.tools import tasks as _tm
        orig = _tm._read_core
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

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "POST":
                captured["data"] = json.loads(req.data.decode())
                return FakePostResp()
            return FakeDepthResp()

        _tm._read_core = lambda: ("test-proj", "feature", "my-parent", 50, 19999)
        try:
            with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
                result = _tm.create_subtask("fire and forget", depends_on_current=False)
        finally:
            _tm._read_core = orig

        assert result["ok"] is True
        assert captured["data"]["dependencies"] == []

'''

before = '\n'.join(lines_before)
if before.endswith('\n'):
    before = before[:-1]
new_content = before + '\n\n' + new_class.rstrip() + '\n\n'
f.write_text(new_content)
print(f"Done: {len(new_content)} chars")
