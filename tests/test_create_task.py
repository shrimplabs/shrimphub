"""
Tests for create_task tool in swarm/agent_runtime.py.

Covers:
  - create_task posts to the correct API endpoint with correct payload
  - Auto-sets project from current agent context
  - Rejects invalid task types
  - Caps priority at 90
  - Validates dependencies exist
  - Returns error gracefully when API is unreachable
  - Auto-generates task ID: {type}-{timestamp}-agent
"""
import json
from unittest.mock import MagicMock, patch

import pytest

import swarm.agent_runtime as rt


@pytest.fixture(autouse=True)
def reset_rt(tmp_path):
    proj_dir = tmp_path / "workspace" / "test-proj"
    proj_dir.mkdir(parents=True)

    rt.WORKSPACE = tmp_path / "workspace"
    rt.PROJECT = "test-proj"
    rt.TASK_TYPE = "feature"
    rt.TASK_ID = "task-001"
    rt.API_PORT = 8080

    rt._sync_core_globals()
    rt._sync_knowledge_globals()

    yield


def _mock_response(status=200, body=None):
    """Create a mock HTTP response."""
    m = MagicMock()
    m.status_code = status
    if body is None:
        body = {"task": {"id": "test-task-123"}}
    m.read.return_value = json.dumps(body).encode()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=False)
    return m


def _extract_body(url):
    """Extract JSON body from urllib Request object."""
    if hasattr(url, "data") and url.data:
        return json.loads(url.data)
    return {}


class TestCreateTask:
    """Tests for the create_task function."""

    def test_posts_to_correct_endpoint(self):
        """create_task posts to http://localhost:PORT/api/tasks."""
        captured = []

        def capture_request(url, **kwargs):
            url_str = url.get_full_url() if hasattr(url, "get_full_url") else str(url)
            captured.append(url_str)
            return _mock_response()

        with patch.object(rt, "PROJECT", "test-proj"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=capture_request):
                    result = rt.create_task("Test task", "feature", 50)

        assert result["ok"] is True
        assert any("/api/tasks" in str(c) for c in captured)

    def test_auto_sets_project_from_context(self):
        """create_task uses current PROJECT when project param not provided."""
        captured_body = []

        def capture_request(url, **kwargs):
            captured_body.append(_extract_body(url))
            return _mock_response()

        with patch.object(rt, "PROJECT", "test-proj"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=capture_request):
                    result = rt.create_task("Test task", "feature")

        assert result["ok"] is True
        assert captured_body[0]["project"] == "test-proj"

    def test_rejects_invalid_task_type(self):
        """create_task returns error for invalid task types."""
        result = rt.create_task("Test task", "invalid_type")

        assert result["ok"] is False
        assert "Invalid task type" in result["error"]

    def test_rejects_invalid_task_type_arbitrary(self):
        """create_task rejects arbitrary strings as task type."""
        result = rt.create_task("Test task", "random")

        assert result["ok"] is False
        assert "Invalid task type" in result["error"]

    def test_accepts_valid_task_types(self):
        """create_task accepts: feature, bug, polish, refactor, qa."""
        valid_types = ["feature", "bug", "polish", "refactor", "qa"]

        for task_type in valid_types:
            with patch.object(rt, "PROJECT", "test-proj"):
                with patch.object(rt, "API_PORT", 8080):
                    with patch("urllib.request.urlopen", return_value=_mock_response()):
                        result = rt.create_task("Test task", task_type)
                        assert result["ok"] is True, f"Failed for type: {task_type}"

    def test_caps_priority_at_90(self):
        """create_task caps priority at 90."""
        captured_body = []

        def capture_request(url, **kwargs):
            captured_body.append(_extract_body(url))
            return _mock_response()

        with patch.object(rt, "PROJECT", "test-proj"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=capture_request):
                    result = rt.create_task("Test task", "feature", priority=100)

        assert result["ok"] is True
        assert captured_body[0]["priority"] == 90

    def test_priority_not_capped_if_below_90(self):
        """create_task preserves priority when below 90."""
        captured_body = []

        def capture_request(url, **kwargs):
            captured_body.append(_extract_body(url))
            return _mock_response()

        with patch.object(rt, "PROJECT", "test-proj"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=capture_request):
                    result = rt.create_task("Test task", "feature", priority=70)

        assert result["ok"] is True
        assert captured_body[0]["priority"] == 70

    def test_returns_error_when_api_unreachable(self):
        """create_task returns error when API is unreachable."""
        import urllib.error

        with patch.object(rt, "PROJECT", "test-proj"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
                    result = rt.create_task("Test task", "feature")

        assert result["ok"] is False
        assert "error" in result

    def test_validates_dependencies_exist(self):
        """create_task validates that dependency task IDs exist."""
        call_count = [0]

        def mock_list_tasks(url, timeout=10):
            m = MagicMock()
            m.status_code = 200
            m.read.return_value = json.dumps({
                "tasks": [
                    {"id": "existing-task-1", "project": "test-proj"},
                    {"id": "existing-task-2", "project": "test-proj"},
                ]
            }).encode()
            m.__enter__ = MagicMock(return_value=m)
            m.__exit__ = MagicMock(return_value=False)
            return m

        def capture_request(url, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_list_tasks(url)
            return _mock_response()

        with patch.object(rt, "PROJECT", "test-proj"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=capture_request):
                    result = rt.create_task(
                        "Test task", "feature", dependencies=["existing-task-1", "missing-task"]
                    )

        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_accepts_valid_dependencies(self):
        """create_task accepts valid dependency task IDs."""
        call_count = [0]

        def mock_list_tasks(url, timeout=10):
            m = MagicMock()
            m.status_code = 200
            m.read.return_value = json.dumps({
                "tasks": [{"id": "existing-task-1", "project": "test-proj"}]
            }).encode()
            m.__enter__ = MagicMock(return_value=m)
            m.__exit__ = MagicMock(return_value=False)
            return m

        def capture_request(url, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_list_tasks(url)
            return _mock_response()

        with patch.object(rt, "PROJECT", "test-proj"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=capture_request):
                    result = rt.create_task(
                        "Test task", "feature", dependencies=["existing-task-1"]
                    )

        assert result["ok"] is True

    def test_auto_generates_task_id_format(self):
        """create_task auto-generates task ID in format {type}-{timestamp}-agent."""
        captured_body = []

        def capture_request(url, **kwargs):
            captured_body.append(_extract_body(url))
            return _mock_response()

        with patch.object(rt, "PROJECT", "test-proj"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=capture_request):
                    result = rt.create_task("Test task", "bug")

        assert result["ok"] is True
        task_id = captured_body[0]["id"]
        assert task_id.startswith("bug-")
        assert task_id.endswith("-agent")
        parts = task_id.split("-")
        assert len(parts) == 3
        assert parts[1].isdigit()

    def test_never_raises_always_returns_dict(self):
        """create_task never raises, always returns a dict with ok/error."""
        with patch.object(rt, "PROJECT", "test-proj"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=Exception("Unexpected")):
                    result = rt.create_task("Test task", "feature")

        assert isinstance(result, dict)
        assert result.get("ok") is False
        assert "error" in result


class TestCreateTaskParentTaskId:
    """Tests for parent_task_id and task_depth support in create_task."""

    def test_stores_parent_task_id_and_depth_1_in_metadata(self):
        """create_task with parent_task_id stores parent_task_id and task_depth=1."""
        captured_body = []

        def capture_request(url, **kwargs):
            captured_body.append(_extract_body(url))
            # Return a task with depth=0 (root task)
            return _mock_response(body={"tasks": [{"id": "root-task-001", "metadata": {"task_depth": 0}}]})

        with patch.object(rt, "PROJECT", "test-proj"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=capture_request):
                    result = rt.create_task(
                        "Sub task", "feature", parent_task_id="root-task-001"
                    )

        assert result["ok"] is True
        meta = captured_body[1].get("metadata", {})
        assert meta.get("parent_task_id") == "root-task-001"
        assert meta.get("task_depth") == 1

    def test_depth_2_when_parent_has_depth_1(self):
        """Depth is calculated from parent metadata, not hardcoded."""
        captured_body = []

        def capture_request(url, **kwargs):
            captured_body.append(_extract_body(url))
            # Return a parent task with depth=1
            return _mock_response(
                body={"tasks": [{"id": "depth1-task", "metadata": {"task_depth": 1}}]}
            )

        with patch.object(rt, "PROJECT", "test-proj"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=capture_request):
                    result = rt.create_task(
                        "Grandchild task", "feature", parent_task_id="depth1-task"
                    )

        assert result["ok"] is True
        meta = captured_body[1].get("metadata", {})
        assert meta.get("task_depth") == 2

    def test_max_depth_guard_blocks_depth_2_parent(self):
        """A depth-2 task cannot spawn further sub-tasks."""
        call_count = [0]

        def capture_request(url, **kwargs):
            call_count[0] += 1
            # Return a parent task with depth=2 (max reached)
            return _mock_response(
                body={"tasks": [{"id": "depth2-task", "metadata": {"task_depth": 2}}]}
            )

        with patch.object(rt, "PROJECT", "test-proj"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=capture_request):
                    result = rt.create_task(
                        "Should be rejected", "feature", parent_task_id="depth2-task"
                    )

        assert result["ok"] is False
        assert "max sub-task depth (2) reached" in result["error"]

    def test_rejects_unknown_parent_task_id(self):
        """create_task returns error when parent_task_id does not exist."""
        def capture_request(url, **kwargs):
            # Return no matching task
            return _mock_response(body={"tasks": []})

        with patch.object(rt, "PROJECT", "test-proj"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=capture_request):
                    result = rt.create_task(
                        "Sub task", "feature", parent_task_id="nonexistent-task"
                    )

        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    def test_metadata_param_passed_through(self):
        """Extra metadata param is merged into task metadata."""
        captured_body = []

        def capture_request(url, **kwargs):
            captured_body.append(_extract_body(url))
            return _mock_response(body={"tasks": [{"id": "root-task", "metadata": {"task_depth": 0}}]})

        with patch.object(rt, "PROJECT", "test-proj"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=capture_request):
                    result = rt.create_task(
                        "Task with extra metadata",
                        "feature",
                        parent_task_id="root-task",
                        metadata={"my_key": "my_value"},
                    )

        assert result["ok"] is True
        meta = captured_body[1].get("metadata", {})
        assert meta.get("my_key") == "my_value"
        assert meta.get("parent_task_id") == "root-task"
        assert meta.get("task_depth") == 1


class TestListSubtasks:
    """Tests for list_subtasks function."""

    def test_returns_subtasks_filtered_by_parent_id(self):
        """list_subtasks returns only tasks whose parent_task_id matches."""
        def capture_request(url, **kwargs):
            m = MagicMock()
            m.status_code = 200
            m.read.return_value = json.dumps({
                "tasks": [
                    {"id": "child-1", "type": "feature", "status": "todo", "description": "First child task", "metadata": {"parent_task_id": "parent-abc"}},
                    {"id": "child-2", "type": "bug", "status": "done", "description": "Second child task", "metadata": {"parent_task_id": "parent-abc"}},
                    {"id": "unrelated", "type": "feature", "status": "todo", "description": "Not a child", "metadata": {"parent_task_id": "other-parent"}},
                    {"id": "orphan", "type": "polish", "status": "todo", "description": "No parent", "metadata": {}},
                ]
            }).encode()
            m.__enter__ = MagicMock(return_value=m)
            m.__exit__ = MagicMock(return_value=False)
            return m

        with patch.object(rt, "API_PORT", 8080):
            with patch("urllib.request.urlopen", side_effect=capture_request):
                result = rt.list_subtasks(parent_task_id="parent-abc")

        assert result["ok"] is True
        assert result["parent_task_id"] == "parent-abc"
        subtask_ids = [s["id"] for s in result["subtasks"]]
        assert "child-1" in subtask_ids
        assert "child-2" in subtask_ids
        assert "unrelated" not in subtask_ids
        assert "orphan" not in subtask_ids

    def test_defaults_to_current_task_id(self):
        """list_subtasks() with no args uses current TASK_ID."""
        captured_parent = []

        def capture_request(url, **kwargs):
            captured_parent.append(url if isinstance(url, str) else url.get_full_url())
            m = MagicMock()
            m.status_code = 200
            m.read.return_value = json.dumps({"tasks": []}).encode()
            m.__enter__ = MagicMock(return_value=m)
            m.__exit__ = MagicMock(return_value=False)
            return m

        import swarm.tools.core as _core
        with patch.object(rt, "API_PORT", 8080), patch.object(rt, "TASK_ID", "my-current-task-42"), \
             patch.object(_core, "API_PORT", 8080), patch.object(_core, "TASK_ID", "my-current-task-42"), \
             patch("urllib.request.urlopen", side_effect=capture_request):
            result = rt.list_subtasks()

        assert result["ok"] is True
        assert result["parent_task_id"] == "my-current-task-42"

    def test_returns_error_when_api_unreachable(self):
        """list_subtasks returns error when API is unreachable."""
        import urllib.error

        with patch.object(rt, "API_PORT", 8080):
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
                result = rt.list_subtasks(parent_task_id="some-parent")

        assert result["ok"] is False
        assert "error" in result

    def test_truncates_description_at_80_chars(self):
        """list_subtasks truncates description to 80 chars."""
        long_desc = "x" * 200

        def capture_request(url, **kwargs):
            m = MagicMock()
            m.status_code = 200
            m.read.return_value = json.dumps({
                "tasks": [
                    {"id": "child", "type": "feature", "status": "todo", "description": long_desc, "metadata": {"parent_task_id": "parent-abc"}},
                ]
            }).encode()
            m.__enter__ = MagicMock(return_value=m)
            m.__exit__ = MagicMock(return_value=False)
            return m

        with patch.object(rt, "API_PORT", 8080):
            with patch("urllib.request.urlopen", side_effect=capture_request):
                result = rt.list_subtasks(parent_task_id="parent-abc")

        assert result["ok"] is True
        assert len(result["subtasks"][0]["description"]) == 80
        assert result["subtasks"][0]["description"].endswith("x")
