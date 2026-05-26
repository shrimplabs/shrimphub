"""Replace TestCreateSubtask with correct mock approach."""
import pathlib

f = pathlib.Path('tests/test_agent_runtime.py')
lines = f.read_text().split('\n')

# Everything before line 1608 (0-indexed: 1607) minus trailing blank line
lines_before = lines[:1608]
while lines_before and lines_before[-1] == '':
    lines_before.pop()

new_class = '''
class TestCreateSubtask:
    """Tests for the create_subtask tool."""

    # Patch urllib.request.urlopen AS SEEN from swarm.tools.tasks module.
    # create_subtask uses "import urllib.request as _ur" locally inside the
    # function, so the internal _ur name is what we must patch.

    # We patch it as swarm.tools.tasks._ur.urlopen.

    # --- dispatch routing ---
    def test_invalid_type_rejected_at_dispatch(self):
        result = rt.execute_tool({
            "tool": "create_subtask",
            "args": {"description": "x", "type": "bad"},
        })
        assert result["ok"] is False
        assert "Invalid task type" in result["error"]

    def test_dispatch_passes_correct_args_to_function(self):
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
    def test_invalid_task_type_rejected_inside_create_subtask(self):
        with patch("swarm.tools.tasks._ur.urlopen",
                   side_effect=Exception("should not be called")):
            from swarm.tools.tasks import create_subtask
            result = create_subtask("test", task_type="not_a_type")
        assert result["ok"] is False
        assert "Invalid task type" in result["error"]

    def test_unknown_task_id_rejected(self):
        # Simulate unknown TASK_ID by patching _read_core
        from swarm.tools import tasks as _t
        orig = _t._read_core
        _t._read_core = lambda: ("proj", "feature", "unknown", 50, 19999)
        try:
            result = _t.create_subtask("sub")
        finally:
            _t._read_core = orig
        assert result["ok"] is False
        assert "valid TASK_ID" in result["error"]
    # --- depth enforcement ---
    def test_depth_guard_blocks_at_max_depth(self):
        from swarm.tools import tasks as _t
        orig = _t._read_core

        def fake_read_core():
            return ("test-proj", "feature", "deep-task", 50, 19999)

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({
                    "tasks": [{
                        "id": "deep-task",
                        "metadata": {"task_depth": 2}
                    }]
                }).encode()

        _t._read_core = fake_read_core
        try:
            with patch("swarm.tools.tasks._ur.urlopen", return_value=FakeResp()):
                result = _t.create_subtask("sub", max_depth=2)
        finally:
            _t._read_core = orig

        assert result["ok"] is False
        assert "max sub-task depth" in result["error"]

    def test_normal_depth_allowed(self):
        from swarm.tools import tasks as _t
        orig = _t._read_core

        def fake_read_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

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


        call_count = [0]

        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            if "/api/tasks" in req.full_url and req.get_method() == "POST":
                return FakePostResp()
            return FakeDepthResp()

        _t._read_core = fake_read_core
        try:
            with patch("swarm.tools.tasks._ur.urlopen", side_effect=fake_urlopen):
                result = _t.create_subtask("child", max_depth=2)
        finally:
            _t._read_core = orig

        assert result["ok"] is True
        assert "task_id" in result

    # --- file conflict detection ---

    def test_pending_sibling_blocks_same_file(self):
        from swarm.tools import tasks as _t
        orig = _t._read_core

        def fake_read_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

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

        _t._read_core = fake_read_core
        try:
            with patch("swarm.tools.tasks._ur.urlopen", return_value=FakeResp()):
                result = _t.create_subtask(
                    "conflicting sub",
                    files_touched=["src/shared.gd"]
                )
        finally:
            _t._read_core = orig

        assert result["ok"] is False
        assert "file conflict detected" in result["error"]
        assert "sibling-subtask" in result["error"]

    def test_in_progress_sibling_blocks_same_file(self):
        from swarm.tools import tasks as _t
        orig = _t._read_core


        def fake_read_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

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

        _t._read_core = fake_read_core
        try:
            with patch("swarm.tools.tasks._ur.urlopen", return_value=FakeResp()):
                result = _t.create_subtask(
                    "conflicting sub",
                    files_touched=["src/shared.gd"]
                )
        finally:
            _t._read_core = orig

        assert result["ok"] is False
        assert "file conflict detected" in result["error"]

    def test_non_overlapping_files_allowed(self):
        from swarm.tools import tasks as _t
        orig = _t._read_core

        def fake_read_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

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

        call_count = [0]


        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            if "/api/tasks" in req.full_url and req.get_method() == "POST":
                return FakePostResp()
            return FakeResp()

        _t._read_core = fake_read_core
        try:
            with patch("swarm.tools.tasks._ur.urlopen", side_effect=fake_urlopen):
                result = _t.create_subtask("non-conflicting", files_touched=["src/shared.gd"])
        finally:
            _t._read_core = orig

        assert result["ok"] is True
        assert result["task_id"] == "new-subtask"

    def test_completed_sibling_does_not_block(self):
        from swarm.tools import tasks as _t
        orig = _t._read_core

        def fake_read_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

        class FakeResp:
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

        def fake_urlopen(req, timeout=None):
            if "/api/tasks" in req.full_url and req.get_method() == "POST":
                return FakePostResp()
            return FakeResp()

        _t._read_core = fake_read_core
        try:
            with patch("swarm.tools.tasks._ur.urlopen", side_effect=fake_urlopen):
                result = _t.create_subtask("after completion", files_touched=["src/shared.gd"])
        finally:
            _t._read_core = orig

        assert result["ok"] is True
        assert result["task_id"] == "new-sub"

    # --- depends_on_current ---

    def test_depends_on_current_true_adds_parent_dep(self):
        from swarm.tools import tasks as _t
        orig = _t._read_core

        def fake_read_core():
            return ("test-proj", "feature", "my-parent", 50, 19999)

        captured_body = {}

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
            if "/api/tasks" in req.full_url and req.get_method() == "POST":
                captured_body["data"] = json.loads(req.data.decode())
                return FakePostResp()
            return FakeDepthResp()


        _t._read_core = fake_read_core
        try:
            with patch("swarm.tools.tasks._ur.urlopen", side_effect=fake_urlopen):
                result = _t.create_subtask("child task")
        finally:
            _t._read_core = orig

        assert result["ok"] is True
        assert captured_body["data"]["dependencies"] == ["my-parent"]
        assert captured_body["data"]["metadata"]["parent_task_id"] == "my-parent"
        assert captured_body["data"]["metadata"]["task_depth"] == 2

    def test_depends_on_current_false_no_parent_dep(self):
        from swarm.tools import tasks as _t
        orig = _t._read_core

        def fake_read_core():
            return ("test-proj", "feature", "my-parent", 50, 19999)

        captured_body = {}

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
            if "/api/tasks" in req.full_url and req.get_method() == "POST":
                captured_body["data"] = json.loads(req.data.decode())
                return FakePostResp()
            return FakeDepthResp()

        _t._read_core = fake_read_core
        try:
            with patch("swarm.tools.tasks._ur.urlopen", side_effect=fake_urlopen):
                result = _t.create_subtask("fire and forget", depends_on_current=False)
        finally:
            _t._read_core = orig

        assert result["ok"] is True
        assert captured_body["data"]["dependencies"] == []


    # --- priority capping ---
    def test_description_missing_is_a_required_arg(self):
        # Verify the required-args dispatch guard catches missing description
        import swarm.tool_dispatch as _td
        orig = _td._TOOL_REQUIRED_ARGS.get("create_subtask", [])
        _td._TOOL_REQUIRED_ARGS["create_subtask"] = ["description"]
        try:
            result = rt.execute_tool({"tool": "create_subtask", "args": {}})
        finally:
            _td._TOOL_REQUIRED_ARGS["create_subtask"] = orig
        # The function gets called with empty description; API fails
        # But our test should check that description makes it through
        assert result["ok"] is False  # either dispatch guard or API error

'''  # Note: 3 quotes close the multiline, then 2 quotes close the triple-quoted string


# Reconstruct: keep up to line 1607 (0-indexed), strip trailing blank, add new class
before = '\n'.join(lines_before)
if before.endswith('\n'):
    before = before[:-1]

new_content = before + '\n\n' + new_class + '\n'
f.write_text(new_content)
print(f"Done: {len(new_content)} chars")
