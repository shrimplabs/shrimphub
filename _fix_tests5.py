"""Rewrite TestCreateSubtask with correct fake_urlopen handling str + Request."""
import pathlib, importlib

f = pathlib.Path('tests/test_agent_runtime.py')
lines = f.read_text().split('\n')

lines_before = lines[:1608]
while lines_before and lines_before[-1] == '':
    lines_before.pop()

new_class = '''
class TestCreateSubtask:
    """Tests for the create_subtask tool."""

    # All tests use patch.object(urllib.request, "urlopen", ...).
    # fake_urlopen must handle both str URLs (GET) and Request objects (POST).

    def _make_fake_urlopen(self, depth_json, post_json=None):
        """Helper to build a urlopen fake that returns different responses.
        depth_json: JSON string for GET /api/tasks
        post_json: JSON string for POST /api/tasks (or None to raise)
        """
        class FakeDepthResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return depth_json.encode()
        class FakePostResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return post_json.encode() if post_json else b'{}'
        def fake_urlopen(req, timeout=None):
            if isinstance(req, str):
                method, url = "GET", req
            else:
                method = req.get_method()
                url = req.full_url
            if method == "POST":
                return FakePostResp()
            return FakeDepthResp()
        return fake_urlopen

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

        fake_uf = self._make_fake_urlopen(
            depth_json=\'\'\'{"tasks": [{"id": "deep-task", "metadata": {"task_depth": 2}}]}\'\'\'
        )

        _tm._read_core = lambda: ("test-proj", "feature", "deep-task", 50, 19999)
        try:
            with patch.object(urllib.request, "urlopen", side_effect=fake_uf):
                result = _tm.create_subtask("sub", max_depth=2)
        finally:
            _tm._read_core = orig

        assert result["ok"] is False
        assert "max sub-task depth" in result["error"]

    def test_normal_depth_allowed(self):
        from swarm.tools import tasks as _tm
        orig = _tm._read_core

        fake_uf = self._make_fake_urlopen(
            depth_json=\'\'\'{"tasks": [{"id": "parent-task", "metadata": {"task_depth": 0}}]}\'\'\',
            post_json=\'\'\'{"task": {"id": "new-sub", "metadata": {}}}\'\'\'
        )

        _tm._read_core = lambda: ("test-proj", "feature", "parent-task", 50, 19999)
        try:
            with patch.object(urllib.request, "urlopen", side_effect=fake_uf):
                result = _tm.create_subtask("child")
        finally:
            _tm._read_core = orig

        assert result["ok"] is True
        assert "task_id" in result

    # --- file conflict detection ---

    def test_pending_sibling_blocks_same_file(self):
        from swarm.tools import tasks as _tm
        orig = _tm._read_core

        fake_uf = self._make_fake_urlopen(
            depth_json=\'\'\'{"tasks": [
                {"id": "parent-task", "metadata": {"task_depth": 0}},
                {"id": "sibling-subtask", "status": "pending",
                 "metadata": {"parent_task_id": "parent-task",
                              "delegated_files": ["src/shared.gd"]}}
            ]}\'\'\'
        )

        _tm._read_core = lambda: ("test-proj", "feature", "parent-task", 50, 19999)
        try:
            with patch.object(urllib.request, "urlopen", side_effect=fake_uf):
                result = _tm.create_subtask("conflicting", files_touched=["src/shared.gd"])
        finally:
            _tm._read_core = orig

        assert result["ok"] is False
        assert "file conflict detected" in result["error"]
        assert "sibling-subtask" in result["error"]

    def test_in_progress_sibling_blocks_same_file(self):
        from swarm.tools import tasks as _tm
        orig = _tm._read_core

        fake_uf = self._make_fake_urlopen(
            depth_json=\'\'\'{"tasks": [
                {"id": "parent-task", "metadata": {"task_depth": 0}},
                {"id": "active-subtask", "status": "in_progress",
                 "metadata": {"parent_task_id": "parent-task",
                              "delegated_files": ["src/shared.gd"]}}
            ]}\'\'\'
        )

        _tm._read_core = lambda: ("test-proj", "feature", "parent-task", 50, 19999)
        try:
            with patch.object(urllib.request, "urlopen", side_effect=fake_uf):
                result = _tm.create_subtask("conflicting", files_touched=["src/shared.gd"])
        finally:
            _tm._read_core = orig

        assert result["ok"] is False
        assert "file conflict detected" in result["error"]
        assert "active-subtask" in result["error"]

    def test_non_overlapping_files_allowed(self):
        from swarm.tools import tasks as _tm
        orig = _tm._read_core

        fake_uf = self._make_fake_urlopen(
            depth_json=\'\'\'{"tasks": [
                {"id": "parent-task", "metadata": {"task_depth": 0}},
                {"id": "sibling-subtask", "status": "pending",
                 "metadata": {"parent_task_id": "parent-task",
                              "delegated_files": ["src/other.gd"]}}
            ]}\'\'\',
            post_json=\'\'\'{"task": {"id": "new-subtask", "metadata": {}}}\'\'\'
        )

        _tm._read_core = lambda: ("test-proj", "feature", "parent-task", 50, 19999)
        try:
            with patch.object(urllib.request, "urlopen", side_effect=fake_uf):
                result = _tm.create_subtask("non-conflicting", files_touched=["src/shared.gd"])
        finally:
            _tm._read_core = orig

        assert result["ok"] is True
        assert result["task_id"] == "new-subtask"

    def test_completed_sibling_does_not_block(self):
        from swarm.tools import tasks as _tm
        orig = _tm._read_core

        fake_uf = self._make_fake_urlopen(
            depth_json=\'\'\'{"tasks": [
                {"id": "parent-task", "metadata": {"task_depth": 0}},
                {"id": "done-subtask", "status": "completed",
                 "metadata": {"parent_task_id": "parent-task",
                              "delegated_files": ["src/shared.gd"]}}
            ]}\'\'\',
            post_json=\'\'\'{"task": {"id": "new-sub", "metadata": {}}}\'\'\'
        )

        _tm._read_core = lambda: ("test-proj", "feature", "parent-task", 50, 19999)
        try:
            with patch.object(urllib.request, "urlopen", side_effect=fake_uf):
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

        def fake_urlopen(req, timeout=None):
            if isinstance(req, str):
                method = "GET"
            else:
                method = req.get_method()
            if method == "POST":
                captured["data"] = json.loads(req.data.decode())
                class R:
                    def __enter__(self): return self
                    def __exit__(self, *a): pass
                    def read(self): return b'{"task": {"id": "created-sub", "metadata": {}}'
                return R()
            class R2:
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def read(self): return b'{"tasks": [{"id": "my-parent", "metadata": {"task_depth": 1}}]}'
            return R2()

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

        def fake_urlopen(req, timeout=None):
            if isinstance(req, str):
                method = "GET"
            else:
                method = req.get_method()
            if method == "POST":
                captured["data"] = json.loads(req.data.decode())
                class R:
                    def __enter__(self): return self
                    def __exit__(self, *a): pass
                    def read(self): return b'{"task": {"id": "fire-and-forget", "metadata": {}}'
                return R()
            class R2:
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def read(self): return b'{"tasks": [{"id": "my-parent", "metadata": {"task_depth": 1}}]}'
            return R2()

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
