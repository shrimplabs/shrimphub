"""Tests for the project-debug chat endpoint (eftp.1 — session persistence)."""
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from swarm import db


@pytest.fixture()
def app(tmp_path):
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm_test.db")

    from swarm.api import create_app
    flask_app = create_app(
        config={
            "workspace": str(tmp_path / "workspace"),
            "max_active_agents": 1,
            "lock_project": False,
            "disable_monitor": True,
            "disable_remote_repo": True,
            "managed_projects": [],
            "paused_projects": [],
        },
        data_dir=tmp_path / "data",
        config_file=tmp_path / "config.json",
    )
    flask_app.config["TESTING"] = True
    flask_app.config["DATA_DIR"] = str(tmp_path / "data")
    yield flask_app

    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


@pytest.fixture()
def client(app):
    return app.test_client()


def _mock_llm(response="Hello from LLM"):
    return patch(
        "swarm.api_chat._chat_call_llm",
        return_value=response,
    )


class TestProjectDebugChatBasic:
    def test_requires_project(self, client):
        with _mock_llm():
            r = client.post("/api/project-debug", json={"message": "hi"})
        assert r.status_code == 400
        assert b"project" in r.data

    def test_requires_message(self, client):
        with _mock_llm():
            r = client.post("/api/project-debug", json={"project": "my-proj"})
        assert r.status_code == 400
        assert b"message" in r.data

    def test_returns_response_and_session_id(self, client):
        with _mock_llm("got it"):
            r = client.post("/api/project-debug", json={
                "project": "my-proj", "message": "hello",
            })
        assert r.status_code == 200
        data = r.get_json()
        assert data["response"] == "got it"
        assert data["session_id"]
        assert isinstance(data["tool_calls"], list)

    def test_returns_empty_tool_calls(self, client):
        with _mock_llm():
            r = client.post("/api/project-debug", json={
                "project": "my-proj", "message": "hello",
            })
        assert r.get_json()["tool_calls"] == []


class TestProjectDebugChatSessionPersistence:
    def test_same_session_id_loads_history(self, client):
        """Second message in same session should include prior exchange in context."""
        calls = []

        def fake_llm(system_prompt, messages, config, **kw):
            calls.append(messages[:])
            return f"reply-{len(calls)}"

        with patch("swarm.api_chat._chat_call_llm", side_effect=fake_llm):
            r1 = client.post("/api/project-debug", json={
                "project": "p1", "message": "first message",
            })
            sid = r1.get_json()["session_id"]

            r2 = client.post("/api/project-debug", json={
                "project": "p1", "message": "second message", "session_id": sid,
            })

        assert r2.status_code == 200
        # Second call's messages should contain both the first user message and assistant reply
        second_call_messages = calls[1]
        roles = [m["role"] for m in second_call_messages]
        assert roles == ["user", "assistant", "user"]
        assert second_call_messages[0]["content"] == "first message"
        assert second_call_messages[1]["content"] == "reply-1"
        assert second_call_messages[2]["content"] == "second message"

    def test_new_session_id_starts_fresh(self, client):
        calls = []

        def fake_llm(system_prompt, messages, config, **kw):
            calls.append(messages[:])
            return "reply"

        with patch("swarm.api_chat._chat_call_llm", side_effect=fake_llm):
            client.post("/api/project-debug", json={
                "project": "p1", "message": "first message",
            })
            client.post("/api/project-debug", json={
                "project": "p1", "message": "fresh start",
                # no session_id → new session
            })

        assert len(calls[1]) == 1
        assert calls[1][0]["content"] == "fresh start"

    def test_session_stored_on_disk(self, app, client):
        with _mock_llm("saved"):
            r = client.post("/api/project-debug", json={
                "project": "my-proj", "message": "persist me",
            })
        sid = r.get_json()["session_id"]
        data_dir = Path(app.config["DATA_DIR"])
        session_file = data_dir / "chat_sessions" / "my-proj" / f"{sid}.jsonl"
        assert session_file.exists()
        lines = [json.loads(l) for l in session_file.read_text().splitlines() if l.strip()]
        assert lines[0] == {"role": "user", "content": "persist me"}
        assert lines[1] == {"role": "assistant", "content": "saved"}

    def test_unknown_session_id_starts_fresh(self, client):
        calls = []

        def fake_llm(system_prompt, messages, config, **kw):
            calls.append(messages[:])
            return "ok"

        with patch("swarm.api_chat._chat_call_llm", side_effect=fake_llm):
            r = client.post("/api/project-debug", json={
                "project": "p1", "message": "hello",
                "session_id": "nonexistent-session-id",
            })

        assert r.status_code == 200
        assert len(calls[0]) == 1  # only the current message, no history loaded

    def test_expired_session_starts_fresh(self, app, client):
        """Session files older than 7 days are discarded."""
        data_dir = Path(app.config["DATA_DIR"])
        old_sid = "old-session-id"
        session_dir = data_dir / "chat_sessions" / "proj"
        session_dir.mkdir(parents=True)
        old_file = session_dir / f"{old_sid}.jsonl"
        old_file.write_text(
            json.dumps({"role": "user", "content": "ancient message"}) + "\n"
        )
        # Back-date the file to 8 days ago
        old_mtime = time.time() - 8 * 86400
        import os
        os.utime(old_file, (old_mtime, old_mtime))

        calls = []

        def fake_llm(system_prompt, messages, config, **kw):
            calls.append(messages[:])
            return "fresh"

        with patch("swarm.api_chat._chat_call_llm", side_effect=fake_llm):
            client.post("/api/project-debug", json={
                "project": "proj", "message": "new message", "session_id": old_sid,
            })

        assert len(calls[0]) == 1  # expired history not loaded
        # File may be re-created for the new session but ancient message must be gone
        if old_file.exists():
            lines = [json.loads(l) for l in old_file.read_text().splitlines() if l.strip()]
            assert all(m["content"] != "ancient message" for m in lines)


class TestProjectDebugChatContext:
    """US-002: Project context auto-loaded on new session."""

    def test_context_included_in_system_prompt_on_new_session(self, app, client, tmp_path):
        """System prompt should include git log / tasks when project workspace exists."""
        workspace = Path(app.config.get("DATA_DIR", "data")).parent / "workspace"
        project_dir = workspace / "ctx-proj"
        project_dir.mkdir(parents=True)
        # Create a fake AGENT_KNOWLEDGE.md
        (project_dir / "AGENT_KNOWLEDGE.md").write_text("## Key facts\n- thing A works\n")

        captured = {}

        def fake_llm(system_prompt, messages, config, **kw):
            captured["system"] = system_prompt
            return "ok"

        with patch("swarm.api_chat._chat_call_llm", side_effect=fake_llm):
            client.post("/api/project-debug", json={
                "project": "ctx-proj", "message": "hello",
            })

        assert "AGENT_KNOWLEDGE" in captured.get("system", "")
        assert "thing A works" in captured.get("system", "")

    def test_context_not_loaded_on_existing_session(self, app, client):
        """Follow-up messages in an existing session must not re-load context."""
        workspace = Path(app.config.get("DATA_DIR", "data")).parent / "workspace"
        project_dir = workspace / "ctx-proj2"
        project_dir.mkdir(parents=True)
        (project_dir / "AGENT_KNOWLEDGE.md").write_text("## Knowledge\n- fact\n")

        system_prompts = []

        def fake_llm(system_prompt, messages, config, **kw):
            system_prompts.append(system_prompt)
            return "reply"

        with patch("swarm.api_chat._chat_call_llm", side_effect=fake_llm):
            r1 = client.post("/api/project-debug", json={
                "project": "ctx-proj2", "message": "first",
            })
            sid = r1.get_json()["session_id"]
            client.post("/api/project-debug", json={
                "project": "ctx-proj2", "message": "second", "session_id": sid,
            })

        # First call has context, second does not
        assert "PROJECT CONTEXT" in system_prompts[0]
        assert "PROJECT CONTEXT" not in system_prompts[1]

    def test_missing_workspace_does_not_error(self, client):
        """If the project workspace doesn't exist, returns 200 without context."""
        with _mock_llm("ok"):
            r = client.post("/api/project-debug", json={
                "project": "nonexistent-project-xyz", "message": "hello",
            })
        assert r.status_code == 200

    def test_tasks_included_in_context(self, app, client):
        """Pending tasks for the project appear in the system prompt."""
        workspace = Path(app.config.get("DATA_DIR", "data")).parent / "workspace"
        project_dir = workspace / "task-ctx-proj"
        project_dir.mkdir(parents=True)

        # Seed a task
        from swarm import db as _db
        _db.task_upsert({
            "id": "test-task-abc123",
            "project": "task-ctx-proj",
            "type": "bug",
            "description": "Fix the widget rendering issue",
            "priority": 80,
            "status": "pending",
            "attempts": 0,
            "max_attempts": 3,
            "metadata": {},
            "dependencies": [],
        })

        captured = {}

        def fake_llm(system_prompt, messages, config, **kw):
            captured["system"] = system_prompt
            return "ok"

        with patch("swarm.api_chat._chat_call_llm", side_effect=fake_llm):
            client.post("/api/project-debug", json={
                "project": "task-ctx-proj", "message": "what's pending?",
            })

        assert "widget rendering" in captured.get("system", "")


class TestProjectDebugTools:
    """US-003: File and shell tools scoped to project workspace."""

    def _project_root(self, app, name="tool-proj"):
        workspace = Path(app.config.get("DATA_DIR", "data")).parent / "workspace"
        d = workspace / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_read_file_tool(self, app):
        from swarm.api_chat import _execute_debug_tool
        root = self._project_root(app, "rftool")
        (root / "hello.txt").write_text("hello world")
        result = _execute_debug_tool("read_file", {"path": "hello.txt"}, root)
        assert "hello world" in result

    def test_read_file_rejects_path_traversal(self, app):
        from swarm.api_chat import _execute_debug_tool
        root = self._project_root(app, "rftool2")
        result = _execute_debug_tool("read_file", {"path": "../../etc/passwd"}, root)
        assert "outside" in result.lower() or "error" in result.lower()

    def test_write_file_tool(self, app):
        from swarm.api_chat import _execute_debug_tool
        root = self._project_root(app, "wftool")
        _execute_debug_tool("write_file", {"path": "out.txt", "content": "written"}, root)
        assert (root / "out.txt").read_text() == "written"

    def test_write_file_rejects_path_traversal(self, app):
        from swarm.api_chat import _execute_debug_tool
        root = self._project_root(app, "wftool2")
        result = _execute_debug_tool("write_file", {"path": "../escape.txt", "content": "x"}, root)
        assert "outside" in result.lower() or "error" in result.lower()
        assert not (root.parent / "escape.txt").exists()

    def test_list_dir_tool(self, app):
        from swarm.api_chat import _execute_debug_tool
        root = self._project_root(app, "ldtool")
        (root / "a.gd").write_text("")
        (root / "sub").mkdir(exist_ok=True)
        result = _execute_debug_tool("list_dir", {"path": ""}, root)
        assert "a.gd" in result
        assert "/sub" in result

    def test_run_command_tool(self, app):
        from swarm.api_chat import _execute_debug_tool
        root = self._project_root(app, "rctool")
        result = _execute_debug_tool("run_command", {"command": "echo hello"}, root)
        assert "hello" in result

    def test_run_command_blocked(self, app):
        from swarm.api_chat import _execute_debug_tool
        root = self._project_root(app, "rctool2")
        result = _execute_debug_tool("run_command", {"command": "rm -rf /"}, root)
        assert "blocked" in result

    def test_tool_calls_returned_in_response(self, app, client):
        """Endpoint returns tool_calls list with tool/args/result."""
        root = self._project_root(app, "tc-proj")
        (root / "notes.txt").write_text("important notes")

        call_count = [0]

        def fake_llm(system_prompt, messages, config, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return '[TOOL_CALL]{"tool": "read_file", "args": {"path": "notes.txt"}}[/TOOL_CALL]'
            return "I read the file."

        with patch("swarm.api_chat._chat_call_llm", side_effect=fake_llm):
            r = client.post("/api/project-debug", json={
                "project": "tc-proj", "message": "read notes",
            })

        data = r.get_json()
        assert r.status_code == 200
        assert len(data["tool_calls"]) == 1
        tc = data["tool_calls"][0]
        assert tc["tool"] == "read_file"
        assert tc["args"] == {"path": "notes.txt"}
        assert "important notes" in tc["result"]

    def test_path_traversal_blocked_in_endpoint(self, app, client):
        """Path traversal attempt via endpoint returns error in tool result, not 500."""
        self._project_root(app, "trav-proj")

        call_count = [0]

        def fake_llm(system_prompt, messages, config, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return '[TOOL_CALL]{"tool": "read_file", "args": {"path": "../../etc/passwd"}}[/TOOL_CALL]'
            return "could not read"

        with patch("swarm.api_chat._chat_call_llm", side_effect=fake_llm):
            r = client.post("/api/project-debug", json={
                "project": "trav-proj", "message": "read passwd",
            })

        assert r.status_code == 200
        # Tool call logged with error result
        tc = r.get_json()["tool_calls"][0]
        assert "outside" in tc["result"].lower() or "error" in tc["result"].lower()

    def test_tools_description_in_system_prompt(self, app, client):
        """System prompt includes tool descriptions."""
        self._project_root(app, "td-proj")
        captured = {}

        def fake_llm(system_prompt, messages, config, **kw):
            captured["system"] = system_prompt
            return "ok"

        with patch("swarm.api_chat._chat_call_llm", side_effect=fake_llm):
            client.post("/api/project-debug", json={
                "project": "td-proj", "message": "hi",
            })

        assert "read_file" in captured["system"]
        assert "run_command" in captured["system"]
        assert "git_commit" in captured["system"]


class TestProjectDebugGraphTools:
    """US-004: Graph primitives exposed to the LLM."""

    def _seed_tasks(self, project="graph-proj"):
        from swarm import db as _db
        tasks = [
            {"id": f"{project}-t1", "project": project, "type": "feature",
             "description": "Build the thing", "priority": 50, "status": "pending",
             "attempts": 0, "max_attempts": 3, "metadata": {}, "dependencies": []},
            {"id": f"{project}-t2", "project": project, "type": "bug",
             "description": "Fix the widget", "priority": 80, "status": "pending",
             "attempts": 0, "max_attempts": 3, "metadata": {}, "dependencies": [f"{project}-t1"]},
        ]
        for t in tasks:
            _db.task_upsert(t)
        return tasks

    def test_list_tasks_tool(self, app):
        from swarm.api_chat import _execute_graph_tool
        from swarm import db as _db
        self._seed_tasks("lt-proj")
        result = _execute_graph_tool("list_tasks", {}, "lt-proj", _db)
        assert "Build the thing" in result
        assert "Fix the widget" in result

    def test_list_tasks_filters_by_status(self, app):
        from swarm.api_chat import _execute_graph_tool
        from swarm import db as _db
        self._seed_tasks("lts-proj")
        result = _execute_graph_tool("list_tasks", {"status": "pending"}, "lts-proj", _db)
        assert "Build the thing" in result

    def test_create_task_tool(self, app):
        from swarm.api_chat import _execute_graph_tool
        from swarm import db as _db
        result = _execute_graph_tool("create_task", {
            "type": "bug", "description": "Fix crash on startup", "priority": 80,
        }, "ct-proj", _db)
        assert "Created task" in result
        task_id = result.split("Created task ")[-1].strip()
        task = _db.task_get(task_id)
        assert task is not None
        assert task["description"] == "Fix crash on startup"

    def test_get_critical_path_tool(self, app):
        from swarm.api_chat import _execute_graph_tool
        from swarm import db as _db
        self._seed_tasks("cp-proj")
        result = _execute_graph_tool("get_critical_path", {}, "cp-proj", _db)
        assert "Critical path" in result
        assert "cp-proj-t" in result

    def test_get_subgraph_tool(self, app):
        from swarm.api_chat import _execute_graph_tool
        from swarm import db as _db
        self._seed_tasks("sg-proj")
        result = _execute_graph_tool("get_subgraph", {
            "root_id": "sg-proj-t1", "direction": "downstream", "depth": 3,
        }, "sg-proj", _db)
        assert "sg-proj-t" in result

    def test_graph_tools_called_via_endpoint(self, app, client):
        """list_tasks tool call works end-to-end through the endpoint."""
        workspace = Path(app.config.get("DATA_DIR", "data")).parent / "workspace"
        (workspace / "gtest-proj").mkdir(parents=True, exist_ok=True)
        self._seed_tasks("gtest-proj")

        call_count = [0]

        def fake_llm(system_prompt, messages, config, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return '[TOOL_CALL]{"tool": "list_tasks", "args": {"status": "pending"}}[/TOOL_CALL]'
            return "Here are the tasks."

        with patch("swarm.api_chat._chat_call_llm", side_effect=fake_llm):
            r = client.post("/api/project-debug", json={
                "project": "gtest-proj", "message": "what tasks are pending?",
            })

        assert r.status_code == 200
        data = r.get_json()
        assert data["tool_calls"][0]["tool"] == "list_tasks"
        assert "Build the thing" in data["tool_calls"][0]["result"]

    def test_graph_tool_descriptions_in_system_prompt(self, app, client):
        workspace = Path(app.config.get("DATA_DIR", "data")).parent / "workspace"
        (workspace / "gd-proj").mkdir(parents=True, exist_ok=True)
        captured = {}

        def fake_llm(system_prompt, messages, config, **kw):
            captured["system"] = system_prompt
            return "ok"

        with patch("swarm.api_chat._chat_call_llm", side_effect=fake_llm):
            client.post("/api/project-debug", json={
                "project": "gd-proj", "message": "hi",
            })

        assert "get_critical_path" in captured["system"]
        assert "get_subgraph" in captured["system"]
        assert "bulk_deps" in captured["system"]


class TestProjectDebugEmergencyStop:
    """US-007: Emergency stop and session rollback."""

    def test_stop_returns_immediately_without_llm(self, client):
        """Stop endpoint never calls the LLM."""
        with _mock_llm("first reply"):
            r1 = client.post("/api/project-debug", json={
                "project": "stop-proj", "message": "hello",
            })
        sid = r1.get_json()["session_id"]

        with patch("swarm.api_chat._chat_call_llm") as mock_llm:
            r2 = client.post(f"/api/project-debug/{sid}/stop", json={"project": "stop-proj"})
            mock_llm.assert_not_called()

        assert r2.status_code == 200
        data = r2.get_json()
        assert data["stopped"] is True
        assert "Stopped by user" in data["response"] or "interrupted" in data["response"].lower()

    def test_stop_appends_to_session_history(self, app, client):
        """Stop message is persisted so the next message has correct context."""
        with _mock_llm("going somewhere"):
            r1 = client.post("/api/project-debug", json={
                "project": "stop-proj2", "message": "do the wrong thing",
            })
        sid = r1.get_json()["session_id"]
        client.post(f"/api/project-debug/{sid}/stop", json={"project": "stop-proj2"})

        data_dir = Path(app.config["DATA_DIR"])
        history = []
        f = data_dir / "chat_sessions" / "stop-proj2" / f"{sid}.jsonl"
        for line in f.read_text().splitlines():
            if line.strip():
                history.append(json.loads(line))

        roles = [m["role"] for m in history]
        assert roles[-1] == "assistant"
        assert "interrupted" in history[-1]["content"].lower() or "stopped" in history[-1]["content"].lower()

    def test_stop_requires_project(self, client):
        r = client.post("/api/project-debug/some-session/stop", json={})
        assert r.status_code == 400

    def test_rollback_removes_last_exchange(self, app, client):
        """Rollback removes the last user+assistant pair."""
        with _mock_llm("bad response"):
            r1 = client.post("/api/project-debug", json={
                "project": "rb-proj", "message": "first message",
            })
        sid = r1.get_json()["session_id"]

        r2 = client.delete(f"/api/project-debug/{sid}/last", json={"project": "rb-proj"})
        assert r2.status_code == 200
        data = r2.get_json()
        assert data["rolled_back"] is True
        assert data["turns_removed"] == 2  # user + assistant

        # Session should now be empty
        data_dir = Path(app.config["DATA_DIR"])
        f = data_dir / "chat_sessions" / "rb-proj" / f"{sid}.jsonl"
        history = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        assert history == []

    def test_rollback_on_empty_history_is_safe(self, client):
        """Rollback on a session with no history returns rolled_back=False."""
        # Create a fresh session by making a chat then rolling back to empty
        with _mock_llm("hi"):
            r1 = client.post("/api/project-debug", json={
                "project": "rb-empty", "message": "hi",
            })
        sid = r1.get_json()["session_id"]
        # First rollback empties it
        client.delete(f"/api/project-debug/{sid}/last", json={"project": "rb-empty"})
        # Second rollback on empty
        r2 = client.delete(f"/api/project-debug/{sid}/last", json={"project": "rb-empty"})
        assert r2.status_code == 200
        assert r2.get_json()["rolled_back"] is False
        assert r2.get_json()["turns_removed"] == 0

    def test_rollback_requires_project(self, client):
        r = client.delete("/api/project-debug/some-session/last", json={})
        assert r.status_code == 400

    def test_after_stop_next_message_continues_session(self, client):
        """After a stop, the user can send a new message and get a real LLM response."""
        with _mock_llm("first"):
            r1 = client.post("/api/project-debug", json={
                "project": "cont-proj", "message": "start",
            })
        sid = r1.get_json()["session_id"]
        client.post(f"/api/project-debug/{sid}/stop", json={"project": "cont-proj"})

        with _mock_llm("redirected response"):
            r2 = client.post("/api/project-debug", json={
                "project": "cont-proj", "message": "actually do this instead",
                "session_id": sid,
            })

        assert r2.status_code == 200
        assert r2.get_json()["response"] == "redirected response"


class TestProjectDebugChatDelete:
    def test_delete_session(self, app, client):
        with _mock_llm("hi"):
            r = client.post("/api/project-debug", json={
                "project": "my-proj", "message": "hi",
            })
        sid = r.get_json()["session_id"]
        data_dir = Path(app.config["DATA_DIR"])
        assert (data_dir / "chat_sessions" / "my-proj" / f"{sid}.jsonl").exists()

        r2 = client.delete(f"/api/project-debug/{sid}", json={"project": "my-proj"})
        assert r2.status_code == 200
        assert r2.get_json()["deleted"] is True
        assert not (data_dir / "chat_sessions" / "my-proj" / f"{sid}.jsonl").exists()

    def test_delete_nonexistent_session(self, client):
        r = client.delete("/api/project-debug/no-such-id", json={"project": "p"})
        assert r.status_code == 200
        assert r.get_json()["deleted"] is False

    def test_delete_requires_project(self, client):
        r = client.delete("/api/project-debug/some-id", json={})
        assert r.status_code == 400
