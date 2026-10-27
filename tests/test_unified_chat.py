"""Tests for the unified chat endpoint (POST /api/unified-chat).

US-001: Unified /api/unified-chat backend endpoint
"""

import json
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture()
def app(tmp_path):
    from swarm.api import create_app

    flask_app = create_app(
        config={
            "workspace": str(tmp_path / "workspace"),
            "managed_projects": [],
            "llm_provider": "claude",
            "max_active_agents": 2,
        },
        data_dir=tmp_path / "data",
        config_file=tmp_path / "config.json",
    )
    flask_app.config["DATA_DIR"] = str(tmp_path / "data")
    flask_app.config["TESTING"] = True
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


def _mock_llm(reply="Hello from unified chat."):
    return patch(
        "swarm.api_chat._chat_call_llm",
        return_value=reply,
    )


# ---------------------------------------------------------------------------
# Routing and scope
# ---------------------------------------------------------------------------


class TestUnifiedChatRouting:
    def test_global_scope_no_project(self, client):
        with _mock_llm():
            resp = client.post(
                "/api/unified-chat",
                json={"message": "hello"},
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["scope"] == "global"
        assert "session_id" in data
        assert "reply" in data

    def test_project_scope_with_project(self, client):
        with _mock_llm():
            resp = client.post(
                "/api/unified-chat",
                json={"message": "hello", "project": "my-project"},
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["scope"] == "my-project"

    def test_message_required(self, client):
        resp = client.post("/api/unified-chat", json={})
        assert resp.status_code == 400
        assert "message" in resp.get_json().get("error", "")

    def test_empty_message_rejected(self, client):
        resp = client.post("/api/unified-chat", json={"message": "  "})
        assert resp.status_code == 400

    def test_session_id_returned_and_reused(self, client):
        session_id = str(uuid.uuid4())
        with _mock_llm("First reply"):
            r1 = client.post(
                "/api/unified-chat",
                json={"message": "hi", "session_id": session_id},
            )
        assert r1.get_json()["session_id"] == session_id

        with _mock_llm("Second reply"):
            r2 = client.post(
                "/api/unified-chat",
                json={"message": "follow up", "session_id": session_id},
            )
        assert r2.get_json()["session_id"] == session_id

    def test_auto_generated_session_id(self, client):
        with _mock_llm():
            r1 = client.post("/api/unified-chat", json={"message": "hi"})
            r2 = client.post("/api/unified-chat", json={"message": "hi"})
        assert r1.get_json()["session_id"] != r2.get_json()["session_id"]


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------


class TestUnifiedChatSession:
    def test_global_session_persisted(self, app, client):
        data_dir = Path(app.config["DATA_DIR"])
        session_id = str(uuid.uuid4())

        with _mock_llm("Remembered"):
            client.post(
                "/api/unified-chat",
                json={"message": "remember this", "session_id": session_id},
            )

        session_file = data_dir / "chat_sessions" / "_global" / f"{session_id}.jsonl"
        assert session_file.exists(), "Global session file should be created"
        lines = [json.loads(l) for l in session_file.read_text().splitlines() if l.strip()]
        roles = [m["role"] for m in lines]
        assert "user" in roles
        assert "assistant" in roles

    def test_project_session_persisted_under_project_dir(self, app, client):
        data_dir = Path(app.config["DATA_DIR"])
        session_id = str(uuid.uuid4())

        with _mock_llm("Project reply"):
            client.post(
                "/api/unified-chat",
                json={"message": "hello", "session_id": session_id, "project": "test-proj"},
            )

        session_file = data_dir / "chat_sessions" / "test-proj" / f"{session_id}.jsonl"
        assert session_file.exists(), "Project session file should be under project directory"

    def test_global_and_project_sessions_are_separate(self, app, client):
        data_dir = Path(app.config["DATA_DIR"])
        session_id = str(uuid.uuid4())

        with _mock_llm("global"):
            client.post("/api/unified-chat", json={"message": "hi", "session_id": session_id})
        with _mock_llm("project"):
            client.post("/api/unified-chat", json={"message": "hi", "session_id": session_id, "project": "proj-a"})

        global_file = data_dir / "chat_sessions" / "_global" / f"{session_id}.jsonl"
        project_file = data_dir / "chat_sessions" / "proj-a" / f"{session_id}.jsonl"
        assert global_file.exists()
        assert project_file.exists()

    def test_delete_session(self, app, client):
        data_dir = Path(app.config["DATA_DIR"])
        session_id = str(uuid.uuid4())

        with _mock_llm():
            client.post("/api/unified-chat", json={"message": "hi", "session_id": session_id})

        session_file = data_dir / "chat_sessions" / "_global" / f"{session_id}.jsonl"
        assert session_file.exists()

        resp = client.delete(f"/api/unified-chat/{session_id}", json={})
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] is True
        assert not session_file.exists()

    def test_delete_project_session(self, app, client):
        data_dir = Path(app.config["DATA_DIR"])
        session_id = str(uuid.uuid4())

        with _mock_llm():
            client.post(
                "/api/unified-chat",
                json={"message": "hi", "session_id": session_id, "project": "proj-b"},
            )

        session_file = data_dir / "chat_sessions" / "proj-b" / f"{session_id}.jsonl"
        assert session_file.exists()

        resp = client.delete(f"/api/unified-chat/{session_id}", json={"project": "proj-b"})
        assert resp.status_code == 200
        assert not session_file.exists()

    def test_rollback_last_exchange(self, app, client):
        data_dir = Path(app.config["DATA_DIR"])
        session_id = str(uuid.uuid4())

        with _mock_llm("First"):
            client.post("/api/unified-chat", json={"message": "msg1", "session_id": session_id})
        with _mock_llm("Second"):
            client.post("/api/unified-chat", json={"message": "msg2", "session_id": session_id})

        resp = client.delete(f"/api/unified-chat/{session_id}/last", json={})
        assert resp.status_code == 200
        d = resp.get_json()
        assert d["rolled_back"] is True
        assert d["turns_removed"] >= 2  # user + assistant

        # Session should now have only one user+assistant pair
        session_file = data_dir / "chat_sessions" / "_global" / f"{session_id}.jsonl"
        lines = [json.loads(l) for l in session_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 2  # one user + one assistant

    def test_history_used_in_followup(self, app, client):
        """Second message should include prior conversation in context."""
        session_id = str(uuid.uuid4())
        captured_messages = []

        def capture_llm(system_prompt, messages, config):
            captured_messages.extend(messages)
            return "OK"

        with patch("swarm.api_chat._chat_call_llm", side_effect=capture_llm):
            client.post("/api/unified-chat", json={"message": "first", "session_id": session_id})
            captured_messages.clear()
            client.post("/api/unified-chat", json={"message": "second", "session_id": session_id})

        # On the second call, there should be prior messages in history
        assert any(m.get("content") == "first" for m in captured_messages)


# ---------------------------------------------------------------------------
# Tool calls in responses
# ---------------------------------------------------------------------------


class TestUnifiedChatToolCalls:
    def test_tool_calls_returned(self, client):
        llm_reply = '[TOOL_CALL]{"tool": "list_tasks", "args": {"status": "pending"}}[/TOOL_CALL]'
        with patch("swarm.api_chat._chat_call_llm", side_effect=[llm_reply, "Here are your tasks."]):
            resp = client.post("/api/unified-chat", json={"message": "what tasks are pending?"})

        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data["tool_calls"], list)
        assert len(data["tool_calls"]) >= 1
        assert data["tool_calls"][0]["tool"] == "list_tasks"

    def test_file_tool_blocked_in_global_scope(self, client):
        llm_reply = '[TOOL_CALL]{"tool": "read_file", "args": {"path": "README.md"}}[/TOOL_CALL]'
        captured = []

        def capture_llm(system_prompt, messages, config):
            # Return tool call on first call, then capture tool results and return final
            if not captured:
                captured.append(True)
                return llm_reply
            # The tool result message should mention the error
            for m in messages:
                if "TOOL_RESULT" in m.get("content", ""):
                    captured.append(m["content"])
            return "Done."

        with patch("swarm.api_chat._chat_call_llm", side_effect=capture_llm):
            resp = client.post("/api/unified-chat", json={"message": "read readme"})

        assert resp.status_code == 200
        # File tool should have been blocked
        assert any(
            "only available in project scope" in str(c) or "Error" in str(c)
            for c in captured[1:]
        )


# ---------------------------------------------------------------------------
# Stop endpoint
# ---------------------------------------------------------------------------


class TestUnifiedChatStop:
    def test_stop_returns_200(self, client):
        session_id = str(uuid.uuid4())
        resp = client.post(f"/api/unified-chat/{session_id}/stop", json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["stopped"] is True
        assert data["session_id"] == session_id

    def test_stop_event_halts_tool_loop(self, app):
        """Setting stop event before loop starts causes the loop to exit early."""
        from swarm.api_chat import _run_unified_tool_loop, _UNIFIED_STOP_EVENTS
        import threading

        session_id = str(uuid.uuid4())
        event = threading.Event()
        _UNIFIED_STOP_EVENTS[session_id] = event
        event.set()  # Pre-set: loop should not call LLM at all

        call_count = [0]

        def counting_llm(system_prompt, messages, config):
            call_count[0] += 1
            return "Should not be called"

        with app.app_context():
            with patch("swarm.api_chat._chat_call_llm", side_effect=counting_llm):
                result, tool_calls = _run_unified_tool_loop(
                    "system",
                    [{"role": "user", "content": "hello"}],
                    {},
                    scope="_global",
                    workspace=Path("/tmp"),
                    data_dir=Path(app.config["DATA_DIR"]),
                    db=MagicMock(),
                    session_id=session_id,
                )
        # Loop should have exited immediately without calling LLM
        assert call_count[0] == 0
        assert "Stopped" in result

    def test_stop_event_cleared_after_response(self, app, client):
        """Stop event should be cleared after a chat response completes."""
        from swarm.api_chat import _UNIFIED_STOP_EVENTS

        session_id = str(uuid.uuid4())
        with _mock_llm("OK"):
            client.post("/api/unified-chat", json={"message": "hi", "session_id": session_id})

        # Event should exist but not be set after response
        event = _UNIFIED_STOP_EVENTS.get(session_id)
        if event:
            assert not event.is_set()


# ---------------------------------------------------------------------------
# US-002: Two-tier memory injection
# ---------------------------------------------------------------------------


class TestMemoryInjection:
    def test_write_swarm_memory(self, app, client):
        data_dir = Path(app.config["DATA_DIR"])
        llm_reply = '[TOOL_CALL]{"tool": "write_swarm_memory", "args": {"content": "Remember: max agents is 5"}}[/TOOL_CALL]'

        with patch("swarm.api_chat._chat_call_llm", side_effect=[llm_reply, "Done."]):
            resp = client.post("/api/unified-chat", json={"message": "save this"})

        assert resp.status_code == 200
        swarm_mem = data_dir / "SWARM_KNOWLEDGE.md"
        assert swarm_mem.exists()
        assert "max agents is 5" in swarm_mem.read_text()
        # write_swarm_memory must NOT touch the auto-generated gardener_patterns.md
        gardener_mem = data_dir / "gardener_patterns.md"
        assert not gardener_mem.exists()

    def test_write_project_memory(self, app, client):
        data_dir = Path(app.config["DATA_DIR"])
        llm_reply = '[TOOL_CALL]{"tool": "write_project_memory", "args": {"project": "proj-x", "content": "Uses GDScript only"}}[/TOOL_CALL]'

        with patch("swarm.api_chat._chat_call_llm", side_effect=[llm_reply, "Done."]):
            resp = client.post("/api/unified-chat", json={"message": "save project note"})

        assert resp.status_code == 200
        proj_mem = data_dir / "project_knowledge" / "proj-x.md"
        assert proj_mem.exists()
        assert "GDScript only" in proj_mem.read_text()

    def test_swarm_memory_injected_into_system_prompt(self, app, client):
        data_dir = Path(app.config["DATA_DIR"])
        swarm_mem = data_dir / "SWARM_KNOWLEDGE.md"
        swarm_mem.parent.mkdir(parents=True, exist_ok=True)
        swarm_mem.write_text("Swarm fact: we use MiniMax by default")

        captured_prompts = []

        def capture_llm(system_prompt, messages, config):
            captured_prompts.append(system_prompt)
            return "OK"

        with patch("swarm.api_chat._chat_call_llm", side_effect=capture_llm):
            client.post("/api/unified-chat", json={"message": "what do you know?"})

        assert any("MiniMax by default" in p for p in captured_prompts)

    def test_gardener_patterns_injected_into_system_prompt(self, app, client):
        """data/gardener_patterns.md is the gardener auto-generated view; it should
        be injected as a separate '## Gardener Patterns' memory section.
        """
        data_dir = Path(app.config["DATA_DIR"])
        gardener_mem = data_dir / "gardener_patterns.md"
        gardener_mem.parent.mkdir(parents=True, exist_ok=True)
        gardener_mem.write_text("Gardener pattern: GUT false positive -- ignore stderr noise")

        captured_prompts = []

        def capture_llm(system_prompt, messages, config):
            captured_prompts.append(system_prompt)
            return "OK"

        with patch("swarm.api_chat._chat_call_llm", side_effect=capture_llm):
            client.post("/api/unified-chat", json={"message": "what do you know?"})

        assert any("GUT false positive" in p for p in captured_prompts)
        assert any("## Gardener Patterns" in p for p in captured_prompts)

    def test_project_memory_injected_into_system_prompt(self, app, client):
        data_dir = Path(app.config["DATA_DIR"])
        proj_mem_dir = data_dir / "project_knowledge"
        proj_mem_dir.mkdir(parents=True, exist_ok=True)
        (proj_mem_dir / "my-proj.md").write_text("Project fact: uses Godot 4.3")

        captured_prompts = []

        def capture_llm(system_prompt, messages, config):
            captured_prompts.append(system_prompt)
            return "OK"

        with patch("swarm.api_chat._chat_call_llm", side_effect=capture_llm):
            client.post(
                "/api/unified-chat",
                json={"message": "tell me about the project", "project": "my-proj"},
            )

        assert any("Godot 4.3" in p for p in captured_prompts)

    def test_missing_memory_files_create_empty(self, app, client):
        data_dir = Path(app.config["DATA_DIR"])
        # Memory files should NOT be auto-created on read — only on write
        # But reading a non-existent file should not crash the chat
        with _mock_llm("OK"):
            resp = client.post("/api/unified-chat", json={"message": "hi"})
        assert resp.status_code == 200

    def test_write_project_memory_requires_project(self, app, client):
        llm_reply = '[TOOL_CALL]{"tool": "write_project_memory", "args": {"content": "something"}}[/TOOL_CALL]'
        with patch("swarm.api_chat._chat_call_llm", side_effect=[llm_reply, "Done."]):
            resp = client.post("/api/unified-chat", json={"message": "save without project"})
        assert resp.status_code == 200
        data = resp.get_json()
        # Tool result should contain the error
        tool_calls = data.get("tool_calls", [])
        assert any("Error" in str(tc.get("result", "")) for tc in tool_calls)


# ---------------------------------------------------------------------------
# US-003: Catastrophic action prevention
# ---------------------------------------------------------------------------


class TestCatastrophicActionPrevention:
    def _create_task(self, app):
        """Helper: insert a real task into the DB and return its ID."""
        with app.app_context():
            from flask import current_app
            db = current_app.extensions.get("swarm_db")
            if db is None:
                # fall back to module-level db from create_app
                import swarm.db as _db_mod
                db = _db_mod._thread_local  # not ideal but ok for test
            return None  # tasks created via API in this test

    def test_delete_task_issues_challenge(self, app, client):
        """delete_task without confirm_token returns a confirmation challenge."""
        # First create a task via the API
        with _mock_llm():
            client.post("/api/unified-chat", json={"message": "warmup"})

        # Directly invoke the tool function
        from swarm.api_chat import _execute_unified_tool, _UNIFIED_GLOBAL_SCOPE
        from pathlib import Path

        # Mock db.task_get to return a fake task
        fake_db = MagicMock()
        fake_db.task_get.return_value = {"id": "bug-abc", "project": "proj", "description": "Fix crash"}

        result, needs_confirm = _execute_unified_tool(
            "delete_task",
            {"task_id": "bug-abc"},
            _UNIFIED_GLOBAL_SCOPE,
            Path("/tmp"),
            Path(app.config["DATA_DIR"]),
            fake_db,
            {},
        )
        challenge = json.loads(result)
        assert challenge["requires_confirmation"] is True
        assert "confirm_token" in challenge
        assert challenge["action"].startswith("delete_task")

    def test_delete_task_with_valid_token_executes(self, app, client):
        from swarm.api_chat import _execute_unified_tool, _UNIFIED_GLOBAL_SCOPE, _issue_confirm_challenge, _validate_confirm_token

        fake_db = MagicMock()
        fake_db.task_get.return_value = {"id": "bug-abc", "project": "proj", "description": "Fix crash"}

        # Issue a token
        challenge_json = _issue_confirm_challenge("delete_task bug-abc", {"task_id": "bug-abc"})
        token = json.loads(challenge_json)["confirm_token"]

        # Execute with valid token
        result, needs_confirm = _execute_unified_tool(
            "delete_task",
            {"task_id": "bug-abc", "confirm_token": token},
            _UNIFIED_GLOBAL_SCOPE,
            Path(app.config.get("WORKSPACE", "/tmp")),
            Path(app.config["DATA_DIR"]),
            fake_db,
            {},
        )
        assert "Deleted" in result
        fake_db.task_delete.assert_called_once_with("bug-abc")

    def test_expired_token_rejected(self, app):
        from swarm.api_chat import _issue_confirm_challenge, _validate_confirm_token, _UNIFIED_CONFIRM_TOKENS, _UNIFIED_CONFIRM_LOCK
        import time

        challenge_json = _issue_confirm_challenge("delete_task test", {"task_id": "test"})
        token = json.loads(challenge_json)["confirm_token"]

        # Artificially expire the token
        with _UNIFIED_CONFIRM_LOCK:
            _UNIFIED_CONFIRM_TOKENS[token]["expires_at"] = time.time() - 1

        valid, err = _validate_confirm_token(token)
        assert not valid
        assert "expired" in err

    def test_token_is_single_use(self, app):
        from swarm.api_chat import _issue_confirm_challenge, _validate_confirm_token

        challenge_json = _issue_confirm_challenge("delete_task test", {"task_id": "test"})
        token = json.loads(challenge_json)["confirm_token"]

        valid1, _ = _validate_confirm_token(token)
        assert valid1  # First use valid

        valid2, err = _validate_confirm_token(token)
        assert not valid2  # Second use invalid
        assert "not found" in err or "already used" in err

    def test_hard_blocked_command_rejected(self, app):
        from swarm.api_chat import _execute_unified_tool, _UNIFIED_GLOBAL_SCOPE

        fake_db = MagicMock()
        result, _ = _execute_unified_tool(
            "run_command",
            {"command": "rm -rf /"},
            "my-project",
            Path("/tmp"),
            Path(app.config["DATA_DIR"]),
            fake_db,
            {},
        )
        assert "hard-blocked" in result or "blocked" in result.lower()

    def test_hard_blocked_command_case_insensitive(self, app):
        from swarm.api_chat import _execute_unified_tool

        fake_db = MagicMock()
        result, _ = _execute_unified_tool(
            "run_command",
            {"command": "DROP TABLE tasks"},
            "my-project",
            Path("/tmp"),
            Path(app.config["DATA_DIR"]),
            fake_db,
            {},
        )
        assert "hard-blocked" in result or "blocked" in result.lower()


# ---------------------------------------------------------------------------
# US-004: Session compaction
# ---------------------------------------------------------------------------


class TestSessionCompaction:
    def test_compaction_triggered_for_long_session(self, app):
        """Session with many long messages triggers compaction."""
        from swarm.api_chat import _compact_unified_session, _COMPACT_TOKEN_THRESHOLD, _COMPACT_MARKER

        # Create messages that exceed the threshold (2 chars/token approx)
        # Need > 80k estimated tokens = > 160k chars
        long_content = "x" * 2000  # 1000 estimated tokens each
        messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": long_content}
            for i in range(100)  # 100 * 1000 tokens = 100k >> 80k threshold
        ]
        assert sum(len(m["content"]) for m in messages) // 2 > _COMPACT_TOKEN_THRESHOLD

        compaction_calls = []

        def mock_llm(system_prompt, messages_arg, config):
            compaction_calls.append(True)
            return "Summary of earlier conversation"

        with patch("swarm.api_chat._chat_call_llm", side_effect=mock_llm):
            result = _compact_unified_session(messages, {})

        # Compaction should have reduced message count
        assert len(result) < len(messages)
        # Should contain the compaction marker
        assert any(_COMPACT_MARKER in m.get("content", "") for m in result)

    def test_compaction_preserves_tail(self, app):
        """Compaction preserves the last N messages verbatim."""
        from swarm.api_chat import _compact_unified_session, _COMPACT_KEEP_TAIL

        long_content = "x" * 2000
        messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg-{i} " + long_content}
            for i in range(100)
        ]
        tail_messages = messages[-_COMPACT_KEEP_TAIL:]

        def mock_llm(system_prompt, messages_arg, config):
            return "Summary"

        with patch("swarm.api_chat._chat_call_llm", side_effect=mock_llm):
            result = _compact_unified_session(messages, {})

        result_tail = result[-_COMPACT_KEEP_TAIL:]
        for expected, actual in zip(tail_messages, result_tail):
            assert expected["content"] == actual["content"]

    def test_short_session_not_compacted(self, app):
        """Session under threshold is returned unchanged."""
        from swarm.api_chat import _compact_unified_session

        messages = [
            {"role": "user", "content": "short message"},
            {"role": "assistant", "content": "short reply"},
        ]
        result = _compact_unified_session(messages, {})
        assert result == messages

    def test_compaction_failure_returns_original(self, app):
        """If LLM call for summary fails, original messages returned unchanged."""
        from swarm.api_chat import _compact_unified_session

        long_content = "x" * 2000
        messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": long_content}
            for i in range(100)
        ]

        def failing_llm(*args, **kwargs):
            raise RuntimeError("LLM unavailable")

        with patch("swarm.api_chat._chat_call_llm", side_effect=failing_llm):
            result = _compact_unified_session(messages, {})

        assert result == messages


# ---------------------------------------------------------------------------
# Backward compatibility — old endpoints still work
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_api_chat_still_works(self, client):
        with patch("swarm.api_chat._chat_call_llm", return_value="Manager reply"):
            resp = client.post("/api/chat", json={"message": "hello", "history": []})
        assert resp.status_code == 200
        assert "response" in resp.get_json()

    def test_api_project_debug_still_works(self, client):
        with patch("swarm.api_chat._chat_call_llm", return_value="Debug reply"):
            resp = client.post(
                "/api/project-debug",
                json={"project": "my-project", "message": "hello"},
            )
        assert resp.status_code == 200
        assert "response" in resp.get_json()
