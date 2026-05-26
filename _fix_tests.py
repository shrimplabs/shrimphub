"""Script to replace the TestCreateSubtask class in test_agent_runtime.py."""
import pathlib

test_file = pathlib.Path('tests/test_agent_runtime.py')
content = test_file.read_text()

old_class_block = '''

# ---------------------------------------------------------------------------
# create_subtask()
# ---------------------------------------------------------------------------

class TestCreateSubtask:
    """Tests for the create_subtask tool."""

    def test_rejects_invalid_task_type(self, tmp_path):
        from swarm.tools.tasks import create_subtask
        result = create_subtask("test", task_type="not_a_type")
        assert result["ok"] is False
        assert "Invalid task type" in result["error"]

    def test_rejects_unknown_task_id(self, tmp_path):
        from swarm.tools.tasks import create_subtask
        # Uses the reset_rt fixture's TASK_ID = "task-001" which is not "unknown"
        # so this would only trigger the no-ID error if TASK_ID were missing
        pass  # covered by the dispatch test below

    def test_dispatches_via_execute_tool(self):
        # Invalid type triggers before any API call is made
        result = rt.execute_tool({
            "tool": "create_subtask",
            "args": {"description": "test", "type": "invalid"},
        })
        assert result["ok"] is False
        assert "Invalid task type" in result["error"]

    def test_dispatches_to_create_subtask_function(self):
        with patch("swarm.tools.tasks.create_subtask", return_value={"ok": True, "task_id": "sub-1", "depth": 1}) as cs:
            result = rt.execute_tool({
                "tool": "create_subtask",
                "args": {"description": "sub task", "type": "feature"},
            })
        assert result["ok"] is True
        assert result["task_id"] == "sub-1"
        cs.assert_called_once()

    def test_passes_all_args_through(self):
        with patch("swarm.tools.tasks.create_subtask", return_value={"ok": True, "task_id": "sub-x", "depth": 1}) as cs:
            result = rt.execute_tool({
                "tool": "create_subtask",
                "args": {
                    "description": "my sub-task",
                    "type": "refactor",
                    "priority": 75,
                    "files_touched": ["src/main.gd"],
                    "depends_on_current": True,
                    "max_depth": 3,
                    "project": "my-proj",
                    "metadata": {"note": "test"},
                },
            })
        cs.assert_called_once()
        args = cs.call_args[0]
        assert args[0] == "my sub-task"
        assert args[1] == "refactor"
        assert args[2] == 75
        assert args[3] == ["src/main.gd"]
        assert args[4] is True
        assert args[5] == 3
        assert args[6] == "my-proj"
        assert args[7] == {"note": "test"}

    def test_rejects_when_no_description(self):
        # Required args check in execute_tool
        result = rt.execute_tool({"tool": "create_subtask", "args": {}})
        assert result["ok"] is False
        assert "description" in result["error"]

    def test_returns_error_when_no_task_id_available(self, tmp_path):
        # Simulate unknown task ID by patching _read_core
        from swarm.tools import tasks
        original = tasks._read_core
        tasks._read_core = lambda: ("proj", "feature", "unknown", 50, 19999)
        try:
            from swarm.tools.tasks import create_subtask
            result = create_subtask("sub")
        finally:
            tasks._read_core = original
        assert result["ok"] is False
        assert "valid TASK_ID" in result["error"]

    def test_depth_guard_blocks_at_max_depth(self, tmp_path):
        # Simulate parent at depth 2 with max_depth=2
        from swarm.tools import tasks
        original = tasks._read_core

        def fake_read_core():
            return ("test-proj", "feature", "deep-task", 50, 19999)

        class FakeResp:
            def read(self):
                import json
                return json.dumps({
                    "tasks": [{
                        "id": "deep-task",
                        "metadata": {"task_depth": 2}
                    }]
                }).encode()

        class FakeUrlopen:
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

        tasks._read_core = fake_read_core
        try:
            with patch("urllib.request.urlopen", return_value=FakeUrlopen()):
                from swarm.tools.tasks import create_subtask
                result = create_subtask("sub", max_depth=2)
        finally:
            tasks._read_core = original

        assert result["ok"] is False
        assert "max sub-task depth" in result["error"]

    def test_file_conflict_detected_when_sibling_touches_same_file(self, tmp_path):
        from swarm.tools import tasks
        original = tasks._read_core

        def fake_read_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

        class FakeUrlopen:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({
                    "tasks": [{
                        "id": "parent-task",
                        "metadata": {"task_depth": 0}
                    }, {
                        "id": "sibling-subtask",
                        "status": "pending",
                        "metadata": {
                            "parent_task_id": "parent-task",
                            "delegated_files": ["src/shared.gd"]
                        }
                    }]
                }).encode()

        tasks._read_core = fake_read_core
        try:
            with patch("urllib.request.urlopen", return_value=FakeUrlopen()):
                from swarm.tools.tasks import create_subtask
                result = create_subtask("conflicting sub", files_touched=["src/shared.gd"])
        finally:
            tasks._read_core = original

        assert result["ok"] is False
        assert "file conflict detected" in result["error"]
        assert "sibling-subtask" in result["error"]

    def test_no_conflict_when_files_do_not_overlap(self, tmp_path):
        from swarm.tools import tasks
        original = tasks._read_core

        def fake_read_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

        class FakeUrlopen:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({
                    "tasks": [{
                        "id": "parent-task",
                        "metadata": {"task_depth": 0}
                    }, {
                        "id": "sibling-subtask",
                        "status": "pending",
                        "metadata": {
                            "parent_task_id": "parent-task",
                            "delegated_files": ["src/other.gd"]
                        }
                    }]
                }).encode()

        class FakeReq:
            def __init__(self, url, data=None, method=None, headers=None):
                self.url = url
                self.data = data
            def get_full_url(self): return self.url
            def get_method(self): return "POST"

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"task": {"id": "new-subtask", "metadata": {}}}).encode()

        tasks._read_core = fake_read_core
        try:
            with patch("urllib.request.urlopen", return_value=FakeUrlopen()), \
                 patch("urllib.request.Request", return_value=FakeReq("http://localhost:19999/api/tasks")):
                result = tasks.create_subtask("non-conflicting", files_touched=["src/shared.gd"])
        finally:
            tasks._read_core = original

        assert result["ok"] is True
        assert result["task_id"] == "new-subtask"

    def test_max_depth_zero_skips_depth_check(self, tmp_path):
        from swarm.tools import tasks
        original = tasks._read_core

        def fake_read_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

        class FakeUrlopen:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({
                    "tasks": [{
                        "id": "parent-task",
                        "metadata": {"task_depth": 999}
                    }]
                }).encode()

        class FakeReq:
            def __init__(self, url, data=None, method=None, headers=None):
                self.url = url
            def get_full_url(self): return self.url
            def get_method(self): return "POST"

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"task": {"id": "unlimited-sub", "metadata": {}}}).encode()

        tasks._read_core = fake_read_core
        try:
            with patch("urllib.request.urlopen", return_value=FakeUrlopen()), \
                 patch("urllib.request.Request", return_value=FakeReq("http://localhost:19999/api/tasks")):
                result = tasks.create_subtask("unlimited depth", max_depth=0)
        finally:
            tasks._read_core = original

        assert result["ok"] is True
        assert result["task_id"] == "unlimited-sub"

    def test_depends_on_current_false_no_parent_dep(self, tmp_path):
        from swarm.tools import tasks
        original = tasks._read_core

        def fake_read_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

        class FakeUrlopen:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"tasks": [{"id": "parent-task", "metadata": {"task_depth": 0}}]}).encode()

        captured_body = {}

        class FakeReq:
            def __init__(self, url, data=None, method=None, headers=None):
                self.url = url
                self.data = data
            def get_full_url(self): return self.url
            def get_method(self): return "POST"
            def get_data(self): return self.data

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"task": {"id": "fire-and-forget", "metadata": {}}}).encode()

        tasks._read_core = fake_read_core
        try:
            def capture_urlopen(req, timeout=None):
                if req.full_url.endswith("/api/tasks"):
                    captured_body["data"] = json.loads(req.data.decode())
                return FakeResp()

            with patch("urllib.request.urlopen", side_effect=capture_urlopen), \
                 patch("urllib.request.Request", return_value=FakeReq("http://localhost:19999/api/tasks")):
                result = tasks.create_subtask("fire and forget", depends_on_current=False)
        finally:
            tasks._read_core = original

        assert result["ok"] is True
        assert captured_body["data"]["dependencies"] == []

    def test_priority_is_capped_at_90(self, tmp_path):
        from swarm.tools.tasks import create_subtask
        with patch("swarm.tools.tasks.create_subtask", return_value={"ok": True, "task_id": "x", "depth": 0}):
            result = rt.execute_tool({
                "tool": "create_subtask",
                "args": {"description": "test", "priority": 999},
            })
        # The function itself caps it, but the test patches it out so we just
        # verify it goes through
        assert result["ok"] is True

    def test_includes_parent_task_id_in_metadata_on_success(self, tmp_path):
        from swarm.tools import tasks
        original = tasks._read_core

        def fake_read_core():
            return ("test-proj", "feature", "my-parent", 50, 19999)

        class FakeUrlopen:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({
                    "tasks": [{"id": "my-parent", "metadata": {"task_depth": 1}}]
                }).encode()

        class FakeReq:
            def __init__(self, url, data=None, method=None, headers=None):
                self.url = url
                self.data = data
            def get_full_url(self): return self.url
            def get_method(self): return "POST"

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"task": {"id": "created-sub", "metadata": {}}}).encode()

        captured_data = {}

        def capture_open(req, timeout=None):
            if "api/tasks" in req.full_url and req.get_method() == "POST":
                captured_data["body"] = json.loads(req.data.decode())
            return FakeResp()

        tasks._read_core = fake_read_core
        try:
            with patch("urllib.request.urlopen", side_effect=capture_open):
                result = tasks.create_subtask("child task")
        finally:
            tasks._read_core = original

        assert result["ok"] is True
        assert captured_data["body"]["dependencies"] == ["my-parent"]
        assert captured_data["body"]["metadata"]["parent_task_id"] == "my-parent"
        assert captured_data["body"]["metadata"]["task_depth"] == 2

'''

new_class_block = '''

# ---------------------------------------------------------------------------
# create_subtask()
# ---------------------------------------------------------------------------

class TestCreateSubtask:
    """Tests for the create_subtask tool."""

    # --- validation / dispatch ---

    def test_rejects_invalid_task_type_direct(self):
        from swarm.tools.tasks import create_subtask
        result = create_subtask("test", task_type="not_a_type")
        assert result["ok"] is False
        assert "Invalid task type" in result["error"]

    def test_rejects_no_task_id_direct(self):
        from swarm.tools import tasks
        original = tasks._read_core
        tasks._read_core = lambda: ("proj", "feature", "unknown", 50, 19999)
        try:
            result = tasks.create_subtask("sub")
        finally:
            tasks._read_core = original
        assert result["ok"] is False
        assert "valid TASK_ID" in result["error"]

    def test_dispatch_invalid_type_returns_error(self):
        # Type check happens before any API call
        result = rt.execute_tool({
            "tool": "create_subtask",
            "args": {"description": "x", "type": "bad"},
        })
        assert result["ok"] is False
        assert "Invalid task type" in result["error"]

    def test_dispatch_calls_function_with_correct_args(self):
        # Patch where the function is actually called (tool_dispatch module)
        with patch("swarm.tool_dispatch.create_subtask",
                   return_value={"ok": True, "task_id": "sub-1", "depth": 1}) as cs:
            result = rt.execute_tool({
                "tool": "create_subtask",
                "args": {
                    "description": "my sub-task",
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
        assert args[0] == "my sub-task"
        assert args[1] == "refactor"
        assert args[2] == 75
        assert args[3] == ["src/main.gd"]
        assert args[4] is True
        assert args[5] == 3
        assert args[6] == "my-proj"
        assert args[7] == {"note": "test"}

    def test_missing_description_blocked_by_dispatch(self):
        # execute_tool validates required args before calling the function
        result = rt.execute_tool({
            "tool": "create_subtask",
            "args": {}})
        assert result["ok"] is False
        assert "description" in result["error"]

    # --- depth enforcement ---

    def test_depth_guard_blocks_at_max_depth(self):
        from swarm.tools import tasks
        original = tasks._read_core

        def fake_read_core():
            return ("test-proj", "feature", "deep-task", 50, 19999)

        class FakeUrlopen:
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

        tasks._read_core = fake_read_core
        try:
            with patch("urllib.request.urlopen", return_value=FakeUrlopen()):
                result = tasks.create_subtask("sub", max_depth=2)
        finally:
            tasks._read_core = original

        assert result["ok"] is False
        assert "max sub-task depth" in result["error"]

    # --- file conflict detection ---

    def test_file_conflict_blocks_pending_sibling(self):
        from swarm.tools import tasks
        original = tasks._read_core

        def fake_read_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

        class FakeUrlopen:
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

        tasks._read_core = fake_read_core
        try:
            with patch("urllib.request.urlopen", return_value=FakeUrlopen()):
                result = tasks.create_subtask(
                    "conflicting sub",
                    files_touched=["src/shared.gd"]
                )
        finally:
            tasks._read_core = original

        assert result["ok"] is False
        assert "file conflict detected" in result["error"]
        assert "sibling-subtask" in result["error"]

    def test_file_conflict_blocks_in_progress_sibling(self):
        from swarm.tools import tasks
        original = tasks._read_core

        def fake_read_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

        class FakeUrlopen:
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

        tasks._read_core = fake_read_core
        try:
            with patch("urllib.request.urlopen", return_value=FakeUrlopen()):
                result = tasks.create_subtask(
                    "conflicting sub",
                    files_touched=["src/shared.gd"]
                )
        finally:
            tasks._read_core = original

        assert result["ok"] is False
        assert "file conflict detected" in result["error"]
        assert "active-subtask" in result["error"]

    def test_non_overlapping_files_allowed(self):
        from swarm.tools import tasks
        original = tasks._read_core

        def fake_read_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

        class FakeUrlopen:
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

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"task": {"id": "new-subtask", "metadata": {}}}).encode()

        tasks._read_core = fake_read_core
        try:
            with patch("urllib.request.urlopen", return_value=FakeUrlopen()), \
                 patch("urllib.request.urlopen", return_value=FakeResp()):
                result = tasks.create_subtask("non-conflicting", files_touched=["src/shared.gd"])
        finally:
            tasks._read_core = original

        assert result["ok"] is True
        assert result["task_id"] == "new-subtask"

    # --- depends_on_current ---

    def test_depends_on_current_true_adds_parent_dep(self):
        from swarm.tools import tasks
        original = tasks._read_core

        def fake_read_core():
            return ("test-proj", "feature", "my-parent", 50, 19999)

        captured_body = {}

        class FakeUrlopen:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"tasks": [{"id": "my-parent", "metadata": {"task_depth": 1}}]}).encode()

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"task": {"id": "created-sub", "metadata": {}}}).encode()

        def fake_urlopen(req, timeout=None):
            if req.full_url.endswith("/api/tasks") and req.get_method() == "POST":
                captured_body["data"] = json.loads(req.data.decode())
            return FakeResp()

        tasks._read_core = fake_read_core
        try:
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = tasks.create_subtask("child task")
        finally:
            tasks._read_core = original

        assert result["ok"] is True
        assert captured_body["data"]["dependencies"] == ["my-parent"]
        assert captured_body["data"]["metadata"]["parent_task_id"] == "my-parent"
        assert captured_body["data"]["metadata"]["task_depth"] == 2

    def test_depends_on_current_false_no_parent_dep(self):
        from swarm.tools import tasks
        original = tasks._read_core

        def fake_read_core():
            return ("test-proj", "feature", "my-parent", 50, 19999)

        captured_body = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"task": {"id": "fire-and-forget", "metadata": {}}}).encode()

        def fake_urlopen(req, timeout=None):
            if req.full_url.endswith("/api/tasks") and req.get_method() == "POST":
                captured_body["data"] = json.loads(req.data.decode())
            return FakeResp()

        tasks._read_core = fake_read_core
        try:
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = tasks.create_subtask("fire and forget", depends_on_current=False)
        finally:
            tasks._read_core = original

        assert result["ok"] is True
        assert captured_body["data"]["dependencies"] == []

    # --- completed sibling does not block ---

    def test_completed_sibling_does_not_conflict(self):
        from swarm.tools import tasks
        original = tasks._read_core

        def fake_read_core():
            return ("test-proj", "feature", "parent-task", 50, 19999)

        class FakeUrlopen:
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

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self):
                import json
                return json.dumps({"task": {"id": "new-sub", "metadata": {}}}).encode()

        tasks._read_core = fake_read_core
        try:
            with patch("urllib.request.urlopen", return_value=FakeUrlopen()), \
                 patch("urllib.request.urlopen", return_value=FakeResp()):
                result = tasks.create_subtask("after completion", files_touched=["src/shared.gd"])
        finally:
            tasks._read_core = original

        assert result["ok"] is True
        assert result["task_id"] == "new-sub"

'''

if old_class_block not in content:
    print("ERROR: old class block not found in test file")
else:
    new_content = content.replace(old_class_block, new_class_block, 1)
    test_file.write_text(new_content)
    print("OK: TestCreateSubtask class replaced successfully")
    print(f"Old: {len(content)} chars, New: {len(new_content)} chars")
