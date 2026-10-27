"""Integration tests for hierarchical tasks via the API and agent tools.

Tests parent_task_id / task_depth storage, depth guard enforcement,
list_subtasks(), and dependency resolution with completed parents.
"""
import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from swarm import db


@pytest.fixture()
def app(tmp_path):
    """Create a fresh Flask test app with an isolated DB."""
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm_test.db")

    from swarm.api import create_app
    flask_app = create_app(
        config={
            "workspace": str(tmp_path / "workspace"),
            "max_active_agents": 3,
            "lock_project": False,
            "agent_timeout": 60,
            "quota_limit_percent": 90,
            "llm_provider": "minimax",
            "task_selection_strategy": "priority",
            "managed_projects": [],
            "paused_projects": [],
        },
        data_dir=tmp_path / "data",
        config_file=tmp_path / "config.json",
    )
    flask_app.config["TESTING"] = True
    yield flask_app

    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Helpers for agent_runtime tool tests
# ---------------------------------------------------------------------------

def _mock_response(body=None):
    """Build a mock urllib response (context manager)."""
    m = MagicMock()
    m.status_code = 200
    m.read.return_value = json.dumps(
        body if body is not None else {"task": {"id": "test-task"}}
    ).encode()
    m.__enter__ = MagicMock(return_value=m)
    m.__exit__ = MagicMock(return_value=False)
    return m


def _mock_urlopen_list_tasks(tasks):
    """Return a mock urlopen that always returns the given task list."""
    return _mock_response(body={"tasks": tasks})


# ---------------------------------------------------------------------------
# 1. parent_task_id and task_depth stored in DB via API
# ---------------------------------------------------------------------------

class TestParentChildStorage:
    def test_create_task_stores_parent_id(self, client):
        """POST /api/tasks with metadata.parent_task_id stores it correctly."""
        r_parent = client.post("/api/tasks", json={
            "project": "my-game",
            "type": "feature",
            "description": "Parent task",
            "priority": 50,
        }, content_type="application/json")
        assert r_parent.status_code == 200
        parent_id = r_parent.json["task"]["id"]

        r_child = client.post("/api/tasks", json={
            "project": "my-game",
            "type": "feature",
            "description": "Child task",
            "priority": 50,
            "metadata": {
                "parent_task_id": parent_id,
                "task_depth": 1,
            },
        }, content_type="application/json")
        assert r_child.status_code == 200
        child_task = r_child.json["task"]

        r_list = client.get("/api/tasks")
        assert r_list.status_code == 200
        child_from_db = next(
            t for t in r_list.json["tasks"] if t["id"] == child_task["id"]
        )
        assert child_from_db["metadata"]["parent_task_id"] == parent_id
        assert child_from_db["metadata"]["task_depth"] == 1


# ---------------------------------------------------------------------------
# 2. Depth guard is enforced in the agent tool (not the API)
# ---------------------------------------------------------------------------

class TestDepthGuard:
    def test_task_depth_guard_via_api_allows_depth_3(self, client):
        """The API itself allows any task_depth — the guard lives in the agent tool."""
        r = client.post("/api/tasks", json={
            "project": "p",
            "type": "feature",
            "description": "Deep task",
            "metadata": {"task_depth": 3},
        }, content_type="application/json")
        assert r.status_code == 200

    def test_agent_tool_blocks_depth_2_parent(self):
        """create_task() rejects a child when parent is at depth 2."""
        import swarm.agent_runtime as rt

        def capture_request(url, timeout=10):
            return _mock_response(body={
                "tasks": [{"id": "depth2-task", "metadata": {"task_depth": 2}}]
            })

        with patch.object(rt, "PROJECT", "p"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=capture_request):
                    result = rt.create_task(
                        "Should be rejected",
                        "feature",
                        parent_task_id="depth2-task",
                    )

        assert result["ok"] is False
        assert "max sub-task depth (2) reached" in result["error"]

    def test_agent_tool_allows_child_of_depth_1_parent(self):
        """create_task() allows child when parent is at depth 1 (child depth = 2)."""
        import swarm.agent_runtime as rt

        call_count = [0]

        def capture_request(url, timeout=10):
            call_count[0] += 1
            if call_count[0] == 1:
                return _mock_response(body={
                    "tasks": [{"id": "depth1-task", "metadata": {"task_depth": 1}}]
                })
            return _mock_response(body={"task": {"id": "new-child-task"}})

        with patch.object(rt, "PROJECT", "p"):
            with patch.object(rt, "API_PORT", 8080):
                with patch("urllib.request.urlopen", side_effect=capture_request):
                    result = rt.create_task(
                        "Child at depth 2",
                        "feature",
                        parent_task_id="depth1-task",
                    )

        assert result["ok"] is True


# ---------------------------------------------------------------------------
# 3. list_subtasks() tool
# ---------------------------------------------------------------------------

class TestListSubtasks:
    def test_list_subtasks_returns_only_children(self):
        """list_subtasks(parent_id) returns only tasks with matching parent_task_id."""
        import swarm.agent_runtime as rt

        tasks = [
            {"id": "child-1", "type": "feature", "status": "pending",
             "description": "Child A", "metadata": {"parent_task_id": "parent-abc"}},
            {"id": "child-2", "type": "bug",      "status": "pending",
             "description": "Child B", "metadata": {"parent_task_id": "parent-abc"}},
            {"id": "orphan",  "type": "polish",   "status": "pending",
             "description": "No parent", "metadata": {}},
            {"id": "other",   "type": "feature",   "status": "pending",
             "description": "Other child", "metadata": {"parent_task_id": "other-parent"}},
        ]

        import swarm.tools.core as _core
        with patch.object(_core, "API_PORT", 8080):
            with patch("urllib.request.urlopen", return_value=_mock_response(body={"tasks": tasks})):
                result = rt.list_subtasks(parent_task_id="parent-abc")

        assert result["ok"] is True
        subtask_ids = [s["id"] for s in result["subtasks"]]
        assert "child-1" in subtask_ids
        assert "child-2" in subtask_ids
        assert "orphan" not in subtask_ids
        assert "other" not in subtask_ids
        assert result["parent_task_id"] == "parent-abc"

    def test_list_subtasks_empty_when_no_children(self):
        """list_subtasks returns an empty list for a task with no children."""
        import swarm.agent_runtime as rt
        import swarm.tools.core as _core

        with patch.object(_core, "API_PORT", 8080):
            with patch("urllib.request.urlopen", return_value=_mock_response(body={
                "tasks": [{"id": "orphan", "type": "feature",
                           "status": "pending",
                           "description": "Lone task", "metadata": {}}]
            })):
                result = rt.list_subtasks(parent_task_id="orphan")

        assert result["ok"] is True
        assert result["subtasks"] == []

    def test_list_subtasks_defaults_to_current_task_id(self):
        """list_subtasks() with no args uses the current TASK_ID."""
        import swarm.agent_runtime as rt
        import swarm.tools.core as _core

        with patch.object(_core, "API_PORT", 8080), patch.object(_core, "TASK_ID", "my-agent-task-42"), \
             patch("urllib.request.urlopen", return_value=_mock_response(body={"tasks": []})):
            result = rt.list_subtasks()

        assert result["ok"] is True
        assert result["parent_task_id"] == "my-agent-task-42"


# ---------------------------------------------------------------------------
# 4. Dependency resolution: parent completes, child becomes runnable
# ---------------------------------------------------------------------------

class TestParentDependencyResolution:
    def test_parent_completes_before_subtask(self, client):
        """When parent task completes, a child depending on it becomes runnable."""
        r_parent = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "Parent task", "priority": 50,
        }, content_type="application/json")
        parent_id = r_parent.json["task"]["id"]

        r_child = client.post("/api/tasks", json={
            "project": "p",
            "type": "feature",
            "description": "Child task",
            "priority": 50,
            "dependencies": [parent_id],
        }, content_type="application/json")
        child_id = r_child.json["task"]["id"]

        r_ready = client.get("/api/dependencies/ready")
        ready = r_ready.json.get("ready", [])
        ready_ids = [t["id"] for t in ready]
        assert parent_id in ready_ids
        assert child_id not in ready_ids

        client.put(f"/api/tasks/{parent_id}",
                   json={"status": "completed"},
                   content_type="application/json")

        r_ready2 = client.get("/api/dependencies/ready")
        ready2 = r_ready2.json.get("ready", [])
        ready_ids2 = [t["id"] for t in ready2]
        assert child_id in ready_ids2

    def test_child_blocked_when_parent_still_pending(self, client):
        """Child with parent dependency is NOT ready while parent is pending."""
        r_parent = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "Pending parent", "priority": 50,
        }, content_type="application/json")
        parent_id = r_parent.json["task"]["id"]

        r_child = client.post("/api/tasks", json={
            "project": "p",
            "type": "feature",
            "description": "Blocked child",
            "priority": 60,
            "dependencies": [parent_id],
        }, content_type="application/json")
        child_id = r_child.json["task"]["id"]

        r_ready = client.get("/api/dependencies/ready")
        ready = r_ready.json.get("ready", [])
        ready_ids = [t["id"] for t in ready]
        assert parent_id in ready_ids
        assert child_id not in ready_ids
