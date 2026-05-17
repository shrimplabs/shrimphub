"""Tests for _execute_chat_actions — tested via the /api/chat endpoint."""
import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from swarm import db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_llm_response(text):
    """Return a mock requests.Response yielding the given text as LLM reply.

    api_chat._chat_call_llm uses streaming (iter_lines) for anthropic/minimax
    format, so the mock must return proper SSE lines — not just json().
    """
    import json as _json
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"content": [{"type": "text", "text": text}]}
    # SSE lines consumed by _parse_sse_stream
    mock_resp.iter_lines.return_value = [
        f"data: {_json.dumps({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': text}})}",
        f"data: {_json.dumps({'type': 'message_stop'})}",
    ]
    return mock_resp


def _action_text(action_dict):
    """Wrap a dict in [ACTION]...[/ACTION] tags."""
    return f"[ACTION]{json.dumps(action_dict)}[/ACTION]"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app(tmp_path):
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    # Use the same data_dir as create_app() so both init() calls hit the same DB
    data_dir = tmp_path / "data"
    db.init(data_dir / "swarm.db")

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
        data_dir=data_dir,
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


def _post_chat(client, message):
    return client.post(
        "/api/chat",
        json={"message": message},
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestChatActions:

    def test_create_task_action_creates_pending_task(self, client):
        # Seed a project so the action has something to attach to
        db.project_upsert({"name": "p1"})

        action = {
            "type": "create_task",
            "project": "p1",
            "task_type": "bug",
            "description": "fix the crash",
            "priority": 80,
        }
        llm_text = _action_text(action) + " Done."

        with patch("swarm.api_chat.requests.post", return_value=_mock_llm_response(llm_text)):
            r = _post_chat(client, "create a bug task")

        assert r.status_code == 200

        tasks = db.task_get_all()
        bug_tasks = [t for t in tasks if t["project"] == "p1" and t["type"] == "bug"]
        assert len(bug_tasks) == 1
        assert bug_tasks[0]["status"] == "pending"
        assert bug_tasks[0]["description"] == "fix the crash"

    def test_kill_agent_action_marks_agent_killed(self, client):
        # Seed a task and an active agent linked to it
        db.task_upsert({
            "id": "task-001", "project": "p1", "type": "bug",
            "description": "fix it", "priority": 80,
            "status": "in_progress", "attempts": 0, "max_attempts": 3,
            "dependencies": [], "metadata": {},
        })
        db.agent_upsert({
            "id": "test-agent-001",
            "project": "p1",
            "task_type": "bug",
            "status": "active",
            "spawned_at": "2026-01-01T00:00:00",
            "task_id": "task-001",
            "pid": 99999,
        })

        action = {"type": "kill_agent", "agent_id": "test-agent-001"}
        llm_text = _action_text(action)

        # Provide a fake process handle so kill() succeeds
        fake_process = MagicMock()
        fake_handle = {"process": fake_process}

        import swarm.orchestrator as orch
        original_handles = orch._active_handles.copy()
        orch._active_handles["test-agent-001"] = fake_handle

        try:
            with patch("swarm.api_chat.requests.post", return_value=_mock_llm_response(llm_text)):
                r = _post_chat(client, "kill the agent")

            assert r.status_code == 200
            fake_process.kill.assert_called_once()

            agent = db.agent_get("test-agent-001")
            assert agent["status"] == "killed"

            task = db.task_get("task-001")
            assert task["status"] == "pending"
        finally:
            orch._active_handles.clear()
            orch._active_handles.update(original_handles)

    def test_set_auto_mode_action_disables_auto(self, client):
        import swarm.orchestrator as orch
        orch.AUTO_MODE = True  # start enabled

        action = {"type": "set_auto_mode", "enabled": False}
        llm_text = _action_text(action)

        with patch("swarm.api_chat.requests.post", return_value=_mock_llm_response(llm_text)):
            r = _post_chat(client, "disable auto mode")

        assert r.status_code == 200
        assert orch.AUTO_MODE is False

    def test_set_auto_mode_action_enables_auto(self, client):
        import swarm.orchestrator as orch
        orch.AUTO_MODE = False  # start disabled

        action = {"type": "set_auto_mode", "enabled": True}
        llm_text = _action_text(action)

        with patch("swarm.api_chat.requests.post", return_value=_mock_llm_response(llm_text)):
            r = _post_chat(client, "enable auto mode")

        assert r.status_code == 200
        assert orch.AUTO_MODE is True

    def test_actions_taken_returned_in_response(self, client):
        db.project_upsert({"name": "p2"})

        action = {
            "type": "create_task",
            "project": "p2",
            "task_type": "feature",
            "description": "add login",
            "priority": 50,
        }
        llm_text = _action_text(action)

        with patch("swarm.api_chat.requests.post", return_value=_mock_llm_response(llm_text)):
            r = _post_chat(client, "add a feature task")

        assert r.status_code == 200
        data = r.get_json()
        assert "actions_taken" in data
        assert len(data["actions_taken"]) > 0

    def test_unknown_action_type_does_not_crash(self, client):
        action = {"type": "nonexistent_action", "foo": "bar"}
        llm_text = _action_text(action)

        with patch("swarm.api_chat.requests.post", return_value=_mock_llm_response(llm_text)):
            r = _post_chat(client, "do something unknown")

        assert r.status_code == 200
        data = r.get_json()
        assert "response" in data

    def test_kill_all_agents_resets_tasks_to_pending(self, client):
        # Seed two tasks and two active agents
        for i in (1, 2):
            db.task_upsert({
                "id": f"task-00{i}", "project": "p1", "type": "bug",
                "description": f"fix {i}", "priority": 80,
                "status": "in_progress", "attempts": 0, "max_attempts": 3,
                "dependencies": [], "metadata": {},
            })
            db.agent_upsert({
                "id": f"agent-00{i}",
                "project": "p1",
                "task_type": "bug",
                "status": "active",
                "spawned_at": "2026-01-01T00:00:00",
                "task_id": f"task-00{i}",
                "pid": 99990 + i,
            })

        action = {"type": "kill_all_agents"}
        llm_text = _action_text(action)

        import swarm.orchestrator as orch
        original_handles = orch._active_handles.copy()

        # Provide fake process handles for both agents
        for i in (1, 2):
            fake_proc = MagicMock()
            orch._active_handles[f"agent-00{i}"] = {"process": fake_proc}

        try:
            with patch("swarm.api_chat.requests.post", return_value=_mock_llm_response(llm_text)):
                r = _post_chat(client, "kill all agents")

            assert r.status_code == 200

            for i in (1, 2):
                agent = db.agent_get(f"agent-00{i}")
                assert agent["status"] == "killed", f"agent-00{i} should be killed"

                task = db.task_get(f"task-00{i}")
                assert task["status"] == "pending", f"task-00{i} should be pending"
        finally:
            orch._active_handles.clear()
            orch._active_handles.update(original_handles)

    def test_invalid_action_json_does_not_crash(self, client):
        # LLM emits a malformed [ACTION] block (not valid JSON)
        llm_text = "[ACTION]not-valid-json[/ACTION] Done."

        with patch("swarm.api_chat.requests.post", return_value=_mock_llm_response(llm_text)):
            r = _post_chat(client, "do something")

        assert r.status_code == 200
        # Response should mention the parse error
        data = r.get_json()
        assert "response" in data

    def test_create_task_missing_description_does_not_create(self, client):
        db.project_upsert({"name": "p3"})

        # Missing description field
        action = {"type": "create_task", "project": "p3", "task_type": "bug"}
        llm_text = _action_text(action)

        with patch("swarm.api_chat.requests.post", return_value=_mock_llm_response(llm_text)):
            r = _post_chat(client, "create task without description")

        assert r.status_code == 200

        tasks = db.task_get_all()
        p3_tasks = [t for t in tasks if t["project"] == "p3"]
        assert len(p3_tasks) == 0

    def test_kill_agent_not_found_does_not_crash(self, client):
        action = {"type": "kill_agent", "agent_id": "nonexistent-agent-id"}
        llm_text = _action_text(action)

        with patch("swarm.api_chat.requests.post", return_value=_mock_llm_response(llm_text)):
            r = _post_chat(client, "kill a ghost agent")

        assert r.status_code == 200
        data = r.get_json()
        assert "response" in data
