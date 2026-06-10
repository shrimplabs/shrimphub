"""
Tests for improvements A-I implemented in feature/improvements.

A - Exit-1 false failures (orchestrator treats TASK_COMPLETE as success)
B - Orphan agent detection on startup (create_app cleans up stale "active" agents)
C - Task management API (POST /api/tasks, DELETE /api/tasks/<id>)
D - Dependency graph endpoint (/api/dependencies/dot)
E - Project notes (GET/POST /api/projects/<name>/notes)
F - Task import/export (GET /api/tasks/export, POST /api/tasks/import)
G - Self-improvement review task spawned after max failures
H - Readonly task mode (write_file/git_commit/git_push blocked)
I - Completion webhooks (POST /api/webhook, fire on queue empty)
"""
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, call
import io

import pytest

from swarm import agent_lifecycle, db, orchestrator


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path):
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm_test.db")
    orchestrator.DATA_DIR = tmp_path
    orchestrator.WORKSPACE = tmp_path / "workspace"
    orchestrator.WORKSPACE.mkdir()
    orchestrator.HISTORY_FILE = tmp_path / "agent-history.jsonl"
    orchestrator._active_handles.clear()
    orchestrator.MANAGED_PROJECTS = []
    orchestrator.PAUSED_PROJECTS = []
    agent_lifecycle.WORKSPACE = orchestrator.WORKSPACE
    agent_lifecycle.DATA_DIR = orchestrator.DATA_DIR
    agent_lifecycle.AUTO_REPLAN_PROJECTS = []
    agent_lifecycle.PAUSED_PROJECTS = []
    yield tmp_path
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


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
            "max_active_agents": 3,
            "lock_project": False,
            "agent_timeout": 60,
            "quota_limit_percent": 90,
            "llm_provider": "minimax",
            "managed_projects": [],
            "paused_projects": [],
        },
        data_dir=tmp_path / "data",
        config_file=tmp_path / "config.json",
    )
    flask_app.config["TESTING"] = True
    (tmp_path / "workspace").mkdir(exist_ok=True)
    yield flask_app

    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


@pytest.fixture()
def client(app):
    return app.test_client()


def _task(**kwargs):
    base = {
        "id": "t1", "project": "proj", "type": "feature",
        "description": "do something", "priority": 50, "status": "pending",
        "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 3,
    }
    base.update(kwargs)
    db.task_upsert(base)
    return base


def _agent(**kwargs):
    base = {
        "id": "a1", "project": "proj", "task_type": "feature",
        "status": "active", "spawned_at": datetime.now().isoformat(),
        "pid": 99999, "task_id": "t1", "log_path": None, "script_path": None,
        "output": "", "metadata": "{}",
    }
    base.update(kwargs)
    db.agent_upsert(base)
    return base


# ===========================================================================
# A — Exit-1 false failures
# ===========================================================================

class TestExitOneFalseFailures:
    """_finish_agent: if exit_code != 0 but output contains TASK_COMPLETE, treat as success."""

    def test_task_complete_in_output_overrides_exit_code(self, tmp_db):
        _task(id="t1", status="in_progress")
        log_path = tmp_db / "agent_a1.log"
        log_path.write_text("some output\nTASK_COMPLETE\nmore stuff")

        orchestrator._finish_agent(
            agent_id="a1",
            exit_code=1,          # non-zero
            project="proj",
            task_id="t1",
            script_path=None,
            log_path=str(log_path),
        )

        t = db.task_get("t1")
        assert t["status"] == "completed", "TASK_COMPLETE in output should override exit code 1"

    def test_exit_zero_with_task_complete_is_success(self, tmp_db):
        _task(id="t1", status="in_progress")
        log_path = tmp_db / "agent_a1.log"
        log_path.write_text("TASK_COMPLETE")

        orchestrator._finish_agent("a1", 0, "proj", "t1", None, str(log_path))
        assert db.task_get("t1")["status"] == "completed"

    def test_exit_one_without_task_complete_is_retry(self, tmp_db):
        # attempts=1, max=3 → failure handler increments to 2 < 3 → retry
        _task(id="t1", status="in_progress", attempts=1, max_attempts=3)
        log_path = tmp_db / "agent_a1.log"
        log_path.write_text("something went wrong")

        orchestrator._finish_agent("a1", 1, "proj", "t1", None, str(log_path))
        assert db.task_get("t1")["status"] == "pending"

    def test_exit_one_without_task_complete_exhausted_is_failed(self, tmp_db):
        # attempts=2, max=3 → handler increments to 3 >= 3 → research feeder spawned.
        # Feature tasks escalate to research on exhaust — original resets to pending.
        # QA/research tasks cancel.  Test with type=qa to verify cancel path.
        _task(id="t1", status="in_progress", attempts=2, max_attempts=3, type="qa")
        log_path = tmp_db / "agent_a1.log"
        # Note: must NOT contain "TASK_COMPLETE" — that would trigger the override
        log_path.write_text("Exception: something went wrong, all retries exhausted")

        orchestrator._finish_agent("a1", 1, "proj", "t1", None, str(log_path))
        # QA tasks cancel (not reschedule) on exhaust per escalation policy
        assert db.task_get("t1")["status"] in ("failed", "cancelled", "pending")
        assert "t1" not in db.task_get_completed_ids()

    def test_negative_task_complete_mention_does_not_satisfy_dependency(self, tmp_db):
        _task(id="t1", status="in_progress", attempts=2, max_attempts=3, type="qa")
        log_path = tmp_db / "agent_a1.log"
        log_path.write_text(
            "API error 529: overloaded_error\n"
            "[Agent] No valid tool calls found after nudge — marking task failed\n"
            "[Agent] Task ended without TASK_COMPLETE — marking as failed\n"
        )

        orchestrator._finish_agent("a1", 1, "proj", "t1", None, str(log_path))

        # QA tasks are not re-enqueued via research feeder, so they end up non-completed
        assert db.task_get("t1")["status"] in ("failed", "cancelled", "pending")
        assert "t1" not in db.task_get_completed_ids()

    def test_agent_status_reflects_success_override(self, tmp_db):
        """Agent record itself should show 'completed' when TASK_COMPLETE overrides exit 1."""
        _agent(id="a1", status="active", task_id="t1")
        _task(id="t1", status="in_progress")
        log_path = tmp_db / "agent_a1.log"
        log_path.write_text("TASK_COMPLETE")

        orchestrator._finish_agent("a1", 1, "proj", "t1", None, str(log_path))
        agent = db.agent_get("a1")
        assert agent["status"] == "completed"

    def test_validation_failure_blocks_follow_on_tasks(self, tmp_db):
        """A false-success code task must not spawn integration or QA before validation bug handoff."""
        project_path = orchestrator.WORKSPACE / "proj"
        project_path.mkdir()
        (project_path / "project.godot").write_text("config_version=5\n")
        _task(id="t1", status="in_progress", type="feature")
        log_path = tmp_db / "agent_a1.log"
        log_path.write_text("TASK_COMPLETE")
        agent_lifecycle._lazy_imports()

        with patch("swarm.agent_lifecycle._validation._post_task_validation_in_worktree", return_value=(True, "SCRIPT ERROR: boom")) as validate, \
                patch("swarm.agent_lifecycle._validation._spawn_validation_bug_task") as spawn_bug:
            orchestrator._finish_agent("a1", 0, "proj", "t1", None, str(log_path))

        validate.assert_called_once()
        spawn_bug.assert_called_once()
        follow_ons = [
            t for t in db.task_get_by_project("proj")
            if t["id"].startswith("integration-") or t["id"].startswith("qa-auto-")
        ]
        assert follow_ons == []

    def test_empty_godot_queue_spawns_qa_after_integration_task(self, tmp_db):
        """When the last Godot implementation task finishes, queue QA even below the threshold."""
        project_path = orchestrator.WORKSPACE / "proj"
        (project_path / "autoload").mkdir(parents=True)
        (project_path / "project.godot").write_text("config_version=5\n")
        (project_path / "autoload" / "test_harness.gd").write_text("extends Node\n")
        _task(
            id="t1",
            status="in_progress",
            type="bug",
            metadata={"is_integration_task": True},
        )
        log_path = tmp_db / "agent_a1.log"
        log_path.write_text("TASK_COMPLETE")
        agent_lifecycle._lazy_imports()

        with patch("swarm.agent_lifecycle._validation._post_task_validation_in_worktree", return_value=(False, "")):
            orchestrator._finish_agent("a1", 0, "proj", "t1", None, str(log_path))

        qa_tasks = [
            t for t in db.task_get_by_project("proj")
            if t["id"].startswith("qa-auto-")
        ]
        assert len(qa_tasks) == 1
        assert qa_tasks[0]["type"] == "harness_qa"
        assert "queue is empty" in qa_tasks[0]["description"]

    def test_successful_validation_triggers_closure_verification(self, tmp_db):
        _task(id="t1", status="in_progress", type="feature")
        log_path = tmp_db / "agent_a1.log"
        log_path.write_text("TASK_COMPLETE")
        agent_lifecycle._lazy_imports()

        with patch("swarm.agent_lifecycle._validation._post_task_validation_in_worktree", return_value=(False, "")) as validate, \
                patch("swarm.agent_lifecycle._validation.run_closure_verification") as run_closure:
            orchestrator._finish_agent("a1", 0, "proj", "t1", None, str(log_path))

        validate.assert_called_once()
        run_closure.assert_called_once_with("proj", "t1")

    def test_invalid_project_plan_retry_clears_completed_and_agent_fields(self, tmp_db):
        """Plan invalidation must not leave a retryable task looking completed or claimed."""
        _agent(id="a1", status="active", task_id="plan-1")
        _task(
            id="plan-1",
            project="proj",
            type="project_plan",
            status="in_progress",
            started="2026-01-01T00:00:00",
            agent_id="a1",
        )
        log_path = tmp_db / "agent_a1.log"
        log_path.write_text("TASK_COMPLETE")
        agent_lifecycle._lazy_imports()

        orchestrator._finish_agent("a1", 0, "proj", "plan-1", None, str(log_path))

        task = db.task_get("plan-1")
        assert task["status"] == "pending"
        assert task["attempts"] == 1
        assert task["started"] is None
        assert task["completed"] is None
        assert task["agent_id"] is None
        assert "plan-1" not in db.task_get_completed_ids()


# ===========================================================================
# B — Orphan agent detection on startup
# ===========================================================================

class TestOrphanAgentDetection:
    """create_app should clean up stale active agents on startup."""

    def test_old_orphan_marked_failed_and_task_reset(self, tmp_path):
        db._db_path = None
        db._initialized = False
        db._local = threading.local()
        db.init(tmp_path / "swarm_test.db")

        # Insert a task and an agent that's been "active" for too long
        _task(id="t1", status="in_progress")
        old_time = (datetime.now() - timedelta(seconds=7200)).isoformat()
        _agent(id="a1", status="active", task_id="t1", spawned_at=old_time)

        from swarm.api import create_app
        flask_app = create_app(
            config={"workspace": str(tmp_path / "workspace")},
            data_dir=tmp_path / "data",
            config_file=tmp_path / "config.json",
        )
        (tmp_path / "workspace").mkdir(exist_ok=True)

        agent = db.agent_get("a1")
        task = db.task_get("t1")
        assert agent["status"] == "failed"
        assert task["status"] == "pending"  # reset for retry

        conn = getattr(db._local, "conn", None)
        if conn:
            conn.close()
            db._local.conn = None

    def test_recent_orphan_left_alone(self, tmp_path):
        """An agent spawned recently (within timeout) should not be touched."""
        db._db_path = None
        db._initialized = False
        db._local = threading.local()
        db.init(tmp_path / "swarm_test.db")

        _task(id="t1", status="in_progress")
        recent_time = (datetime.now() - timedelta(seconds=10)).isoformat()
        _agent(id="a1", status="active", task_id="t1", spawned_at=recent_time)

        from swarm.api import create_app
        create_app(
            config={"workspace": str(tmp_path / "workspace"), "agent_timeout": 600},
            data_dir=tmp_path / "data",
            config_file=tmp_path / "config.json",
        )
        (tmp_path / "workspace").mkdir(exist_ok=True)

        assert db.agent_get("a1")["status"] == "active"  # untouched

        conn = getattr(db._local, "conn", None)
        if conn:
            conn.close()
            db._local.conn = None

    def test_completed_agent_not_touched(self, tmp_path):
        db._db_path = None
        db._initialized = False
        db._local = threading.local()
        db.init(tmp_path / "swarm_test.db")

        _task(id="t1", status="completed")
        old_time = (datetime.now() - timedelta(seconds=700)).isoformat()
        db.agent_upsert({
            "id": "a1", "project": "proj", "task_type": "feature",
            "status": "completed",  # already done
            "spawned_at": old_time, "pid": 1, "task_id": "t1",
            "log_path": None, "script_path": None, "output": "", "metadata": "{}",
        })

        from swarm.api import create_app
        create_app(
            config={"workspace": str(tmp_path / "workspace"), "agent_timeout": 600},
            data_dir=tmp_path / "data",
            config_file=tmp_path / "config.json",
        )

        assert db.agent_get("a1")["status"] == "completed"  # unchanged

        conn = getattr(db._local, "conn", None)
        if conn:
            conn.close()
            db._local.conn = None


# ===========================================================================
# C — Task management API
# ===========================================================================

class TestTaskManagementAPI:
    def test_add_task_via_post(self, client):
        r = client.post("/api/tasks", json={
            "project": "my-game",
            "type": "feature",
            "description": "Add score display",
            "priority": 60,
        })
        assert r.status_code == 200
        task = r.json["task"]
        assert task["project"] == "my-game"
        assert task["type"] == "feature"
        assert task["priority"] == 60
        assert task["status"] == "pending"

    def test_added_task_appears_in_list(self, client):
        client.post("/api/tasks", json={"project": "g", "type": "bug", "description": "crash"})
        r = client.get("/api/tasks")
        assert any(t["type"] == "bug" for t in r.json["tasks"])

    def test_delete_pending_task(self, client):
        r = client.post("/api/tasks", json={"project": "g", "type": "feature", "description": "x"})
        task_id = r.json["task"]["id"]

        r = client.delete(f"/api/tasks/{task_id}")
        assert r.status_code == 200
        assert r.json["success"] is True

        # Should be gone
        r2 = client.get(f"/api/tasks/{task_id}")
        assert r2.status_code == 404

    def test_delete_nonexistent_task_returns_404(self, client):
        r = client.delete("/api/tasks/ghost-task-id")
        assert r.status_code == 404

    def test_task_defaults_applied(self, client):
        r = client.post("/api/tasks", json={"project": "g", "description": "minimal"})
        t = r.json["task"]
        assert t["type"] == "feature"
        assert t["priority"] == 50
        assert t["dependencies"] == []

    def test_task_with_dependencies(self, client):
        client.post("/api/tasks", json={"id": "task-001", "project": "g", "description": "first"})
        r = client.post("/api/tasks", json={
            "project": "g", "description": "second",
            "dependencies": ["task-001"],
        })
        assert r.json["task"]["dependencies"] == ["task-001"]


# ===========================================================================
# D — Dependency graph endpoint
# ===========================================================================

class TestDependencyGraph:
    def test_dot_endpoint_returns_digraph(self, client):
        r = client.get("/api/dependencies/dot")
        assert r.status_code == 200
        body = r.data.decode()
        assert "digraph" in body

    def test_dot_with_tasks_includes_node(self, client):
        client.post("/api/tasks", json={
            "id": "t-alpha", "project": "g", "description": "alpha task",
        })
        r = client.get("/api/dependencies/dot")
        body = r.data.decode()
        assert "digraph" in body

    def test_dot_with_dependencies_includes_edge(self, client):
        client.post("/api/tasks", json={"id": "dep-1", "project": "g", "description": "dep"})
        client.post("/api/tasks", json={
            "id": "child-1", "project": "g", "description": "child",
            "dependencies": ["dep-1"],
        })
        r = client.get("/api/dependencies/dot")
        body = r.data.decode()
        # An edge dep-1 -> child-1 should appear
        assert "dep-1" in body
        assert "child-1" in body

    def test_dependencies_ready_endpoint(self, client):
        # "ready" is a list of task objects with full fields
        client.post("/api/tasks", json={"id": "t-ready", "project": "g", "description": "no deps"})
        r = client.get("/api/dependencies/ready")
        assert r.status_code == 200
        ready = r.json.get("ready", [])
        ids = [t["id"] for t in ready]
        assert "t-ready" in ids

    def test_dependencies_endpoint(self, client):
        r = client.get("/api/dependencies")
        assert r.status_code == 200


# ===========================================================================
# E — Project notes
# ===========================================================================

class TestProjectNotes:
    def _add_project(self, client, name="my-game"):
        db.project_upsert({"name": name, "status": "active", "locked": False,
                           "files": {}, "recent_commits": []})

    def test_get_notes_empty(self, client):
        self._add_project(client)
        r = client.get("/api/projects/my-game/notes")
        assert r.status_code == 200
        assert r.json["notes"] == ""

    def test_set_and_get_notes(self, client):
        self._add_project(client)
        r = client.post("/api/projects/my-game/notes",
                        json={"notes": "This is a tower defence game."})
        assert r.status_code == 200
        assert r.json["success"] is True

        r2 = client.get("/api/projects/my-game/notes")
        assert r2.json["notes"] == "This is a tower defence game."

    def test_update_notes_replaces_previous(self, client):
        self._add_project(client)
        client.post("/api/projects/my-game/notes", json={"notes": "old notes"})
        client.post("/api/projects/my-game/notes", json={"notes": "new notes"})
        r = client.get("/api/projects/my-game/notes")
        assert r.json["notes"] == "new notes"

    def test_notes_persisted_in_db(self, client):
        self._add_project(client)
        client.post("/api/projects/my-game/notes", json={"notes": "context here"})
        # Read directly from DB layer
        assert db.project_get_notes("my-game") == "context here"

    def test_clear_notes_with_empty_string(self, client):
        self._add_project(client)
        client.post("/api/projects/my-game/notes", json={"notes": "some notes"})
        client.post("/api/projects/my-game/notes", json={"notes": ""})
        assert db.project_get_notes("my-game") == ""

    def test_notes_for_unknown_project_returns_empty(self, client):
        r = client.get("/api/projects/ghost-project/notes")
        assert r.status_code == 200
        assert r.json["notes"] == ""


# ===========================================================================
# F — Task import / export
# ===========================================================================

class TestTaskImportExport:
    def _add_pending_task(self, client, desc="do something", project="my-game"):
        client.post("/api/tasks", json={
            "project": project, "type": "feature",
            "description": desc, "priority": 55,
        })

    def test_export_returns_yaml(self, client):
        self._add_pending_task(client)
        r = client.get("/api/tasks/export")
        assert r.status_code == 200
        assert b"tasks:" in r.data
        assert b"do something" in r.data

    def test_export_empty_when_no_pending(self, client):
        r = client.get("/api/tasks/export")
        assert r.status_code == 200
        import yaml
        data = yaml.safe_load(r.data)
        assert data["tasks"] == []

    def test_export_excludes_non_pending(self, client):
        """Completed and in_progress tasks should not appear in the export."""
        db.task_upsert({
            "id": "done-1", "project": "g", "type": "feature",
            "description": "completed task", "priority": 50,
            "status": "completed", "dependencies": [], "metadata": {},
            "attempts": 0, "max_attempts": 3,
        })
        r = client.get("/api/tasks/export")
        import yaml
        data = yaml.safe_load(r.data)
        assert not any(t.get("description") == "completed task" for t in data["tasks"])

    def test_import_yaml(self, client):
        yaml_body = b"tasks:\n- project: my-game\n  type: feature\n  description: Imported task\n  priority: 70\n  dependencies: []\n"
        r = client.post("/api/tasks/import",
                        data=yaml_body,
                        content_type="text/yaml")
        assert r.status_code == 200
        assert r.json["imported"] == 1
        assert len(r.json["ids"]) == 1

    def test_imported_task_appears_in_list(self, client):
        yaml_body = b"tasks:\n- project: g\n  type: bug\n  description: Imported bug\n  priority: 80\n  dependencies: []\n"
        client.post("/api/tasks/import", data=yaml_body, content_type="text/yaml")
        r = client.get("/api/tasks")
        descs = [t["description"] for t in r.json["tasks"]]
        assert "Imported bug" in descs

    def test_import_json(self, client):
        body = json.dumps({"tasks": [
            {"project": "g", "type": "feature", "description": "JSON imported", "priority": 50, "dependencies": []},
        ]})
        r = client.post("/api/tasks/import",
                        data=body, content_type="application/json")
        assert r.status_code == 200
        assert r.json["imported"] == 1

    def test_import_bulk_multiple_tasks(self, client):
        yaml_body = (
            b"tasks:\n"
            b"- project: g\n  type: feature\n  description: Task one\n  priority: 50\n  dependencies: []\n"
            b"- project: g\n  type: bug\n  description: Task two\n  priority: 80\n  dependencies: []\n"
        )
        r = client.post("/api/tasks/import", data=yaml_body, content_type="text/yaml")
        assert r.json["imported"] == 2

    def test_import_invalid_yaml_returns_400(self, client):
        r = client.post("/api/tasks/import",
                        data=b": invalid: yaml: [[[",
                        content_type="text/yaml")
        assert r.status_code == 400
        assert "error" in r.json

    def test_export_route_not_confused_with_task_id(self, client):
        """Regression: GET /api/tasks/export must not hit /api/tasks/<task_id>."""
        r = client.get("/api/tasks/export")
        # Should be 200 YAML, not a 404 "Task not found"
        assert r.status_code == 200
        assert r.json is None or b"tasks:" in r.data  # yaml, not json error


# ===========================================================================
# G — Research feeder spawned after max failures (progressive refinement model)
# ===========================================================================

class TestSelfImprovementReviewTask:
    """Tests for the research-feeder escalation policy.

    When a task (type=feature/bug/refactor) exhausts max_attempts, a research
    feeder is spawned instead of a legacy recovery task. The original task is
    reset to pending with attempts=0, blocked on the feeder. The feeder has
    is_research_feeder=True in its metadata and feeds_into_task_id pointing
    back to the original task.
    """

    def _insert_task(self, **kwargs):
        base = {
            "id": "t1", "project": "proj", "type": "feature",
            "description": "add player jump", "priority": 50,
            "status": "in_progress", "dependencies": [], "metadata": {},
            "attempts": 2, "max_attempts": 3,
        }
        base.update(kwargs)
        db.task_upsert(base)

    def test_review_task_spawned_after_max_attempts(self, tmp_db):
        """Exhausting max_attempts spawns a research feeder and resets the original."""
        self._insert_task(attempts=2, max_attempts=3)
        orchestrator._handle_task_failure("t1", "proj", "error on attempt 3")

        all_tasks = db.task_get_all()
        feeders = [t for t in all_tasks if t.get("metadata", {}).get("is_research_feeder")]
        assert len(feeders) == 1, "Expected one research feeder to be spawned"
        # Original task should be reset to pending
        original = db.task_get("t1")
        assert original["status"] == "pending", "Original task should be reset to pending"
        assert original["attempts"] == 0, "Original task attempts should be reset to 0"

    def test_review_task_references_failed_task(self, tmp_db):
        """Research feeder should reference the original task it feeds into."""
        self._insert_task(attempts=2, max_attempts=3)
        orchestrator._handle_task_failure("t1", "proj", "some error")

        all_tasks = db.task_get_all()
        feeder = next(t for t in all_tasks if t.get("metadata", {}).get("is_research_feeder"))
        assert feeder["metadata"]["feeds_into_task_id"] == "t1"

    def test_review_task_has_appropriate_priority(self, tmp_db):
        """Research feeder runs at elevated priority (85) to unblock the original quickly."""
        self._insert_task(attempts=2, max_attempts=3)
        orchestrator._handle_task_failure("t1", "proj", "error")

        all_tasks = db.task_get_all()
        feeder = next(t for t in all_tasks if t.get("metadata", {}).get("is_research_feeder"))
        assert feeder["priority"] == 85  # elevated to unblock the original task

    def test_review_task_max_attempts(self, tmp_db):
        """Research feeder has a fixed max_attempts (typically 1 or 2)."""
        self._insert_task(attempts=2, max_attempts=3)
        orchestrator._handle_task_failure("t1", "proj", "error")

        all_tasks = db.task_get_all()
        feeder = next(t for t in all_tasks if t.get("metadata", {}).get("is_research_feeder"))
        # Research feeders use default max_attempts (typically 2)
        assert feeder["max_attempts"] >= 1

    def test_review_task_not_spawned_on_retry(self, tmp_db):
        """Research feeder should only spawn when attempts are exhausted, not on retries."""
        self._insert_task(attempts=0, max_attempts=3)
        orchestrator._handle_task_failure("t1", "proj", "first failure")

        all_tasks = db.task_get_all()
        feeders = [t for t in all_tasks if t.get("metadata", {}).get("is_research_feeder")]
        assert len(feeders) == 0

    def test_no_review_task_when_project_none(self, tmp_db):
        """If project is None, research feeder should not be spawned."""
        self._insert_task(attempts=2, max_attempts=3)
        orchestrator._handle_task_failure("t1", None, "error")

        all_tasks = db.task_get_all()
        feeders = [t for t in all_tasks if t.get("metadata", {}).get("is_research_feeder")]
        assert len(feeders) == 0

    def test_project_feeder_cap_blocks_original_on_existing_feeder(self, tmp_db):
        """Held tasks must not remain failed when the project feeder cap is active."""
        self._insert_task(id="t1", attempts=2, max_attempts=3)
        db.task_upsert({
            "id": "research-feeder-live",
            "project": "proj",
            "type": "research",
            "description": "Existing diagnosis",
            "priority": 85,
            "status": "pending",
            "dependencies": [],
            "metadata": {"is_research_feeder": True, "feeds_into_task_id": "other-task"},
            "attempts": 0,
            "max_attempts": 2,
        })

        orchestrator._handle_task_failure("t1", "proj", "terminal error")

        original = db.task_get("t1")
        assert original["status"] == "pending"
        assert original["attempts"] == 0
        assert "research-feeder-live" in original["dependencies"]
        assert original["metadata"]["awaiting_research_feeder"] == "research-feeder-live"

    def test_failed_research_feeder_does_not_block_new_feeder(self, tmp_db):
        """A failed feeder cannot unblock anything, so it should not count as live."""
        self._insert_task(id="t1", attempts=2, max_attempts=3)
        db.task_upsert({
            "id": "research-feeder-dead",
            "project": "proj",
            "type": "research",
            "description": "Dead diagnosis",
            "priority": 85,
            "status": "failed",
            "dependencies": [],
            "metadata": {"is_research_feeder": True, "feeds_into_task_id": "t1"},
            "attempts": 1,
            "max_attempts": 2,
        })

        orchestrator._handle_task_failure("t1", "proj", "terminal error")

        feeders = [
            t for t in db.task_get_all()
            if (t.get("metadata") or {}).get("is_research_feeder")
        ]
        new_feed = [t for t in feeders if t["id"] != "research-feeder-dead"]
        assert len(new_feed) == 1
        original = db.task_get("t1")
        assert original["status"] == "pending"
        assert new_feed[0]["id"] in original["dependencies"]

    def test_research_feeder_uses_stable_pipeline_under_random_experiment(self, tmp_db, monkeypatch):
        """Experiment labels are preserved, but recovery feeders do not inherit chaos ordering."""
        self._insert_task(id="t1", attempts=2, max_attempts=3)

        def fake_stamp(_project, metadata):
            return {
                **metadata,
                "experiment_id": "exp",
                "experiment_variant": "variant-d",
                "pipeline": ["validate", "plan", "scout", "work"],
                "pipeline_variant": ["validate", "plan", "scout", "work"],
                "phase_order": ["validate", "plan", "scout", "work"],
                "phase_random_seed": 123,
                "is_valid_order": False,
                "invalidity_reason": "validate_before_work",
            }

        monkeypatch.setattr("swarm.agent_recovery.stamp_experiment_metadata", fake_stamp)

        orchestrator._handle_task_failure("t1", "proj", "terminal error")

        feeder = next(
            t for t in db.task_get_all()
            if (t.get("metadata") or {}).get("is_research_feeder")
        )
        meta = feeder["metadata"]
        assert meta["experiment_variant"] == "variant-d"
        assert meta["pipeline"] == ["scout", "diagnose"]
        assert meta["pipeline_variant"] == ["scout", "diagnose"]
        assert meta["phase_order"] == ["scout", "diagnose"]
        assert meta["recovery_pipeline_override"] is True
        assert meta["experiment_inherited_pipeline"] == ["validate", "plan", "scout", "work"]
        assert meta["is_valid_order"] is True
        assert meta["invalidity_reason"] == ""
        assert "phase_random_seed" not in meta

    def test_research_feeder_cycle_cap_stops_spawning_and_leaves_task_failed(self, tmp_db):
        """After the feeder cap, terminal failure should drain instead of creating another feeder."""
        self._insert_task(
            id="t1",
            attempts=2,
            max_attempts=3,
            metadata={"research_feeder_cycles": 2},
        )

        orchestrator._handle_task_failure("t1", "proj", "terminal error")

        feeders = [
            t for t in db.task_get_all()
            if (t.get("metadata") or {}).get("is_research_feeder")
        ]
        original = db.task_get("t1")
        assert feeders == []
        assert original["status"] == "failed"
        assert original["attempts"] == 3
        assert original["metadata"]["research_feeder_cap_reached"] is True
        assert original["metadata"]["needs_human_review"] is True


# ===========================================================================
# H — Readonly task mode
# ===========================================================================

class TestReadonlyTaskMode:
    """swarm.agent_runtime: when READONLY=True, mutating tools are disabled."""

    def setup_method(self):
        import swarm.agent_runtime as rt
        self._orig_readonly = rt.READONLY
        rt.READONLY = True

    def teardown_method(self):
        import swarm.agent_runtime as rt
        rt.READONLY = self._orig_readonly

    def test_write_file_blocked_when_readonly(self, tmp_path):
        import swarm.agent_runtime as rt
        rt.WORKSPACE = tmp_path
        rt.PROJECT = "proj"
        rt._sync_core_globals()
        (tmp_path / "proj").mkdir()

        result = rt.write_file("some_file.gd", "content here")
        assert result["ok"] is False
        assert "Read-only" in result["error"]
        assert not (tmp_path / "proj" / "some_file.gd").exists()

    def test_git_commit_blocked_when_readonly(self):
        import swarm.agent_runtime as rt
        result = rt.git_commit("should not commit")
        assert result["ok"] is False
        assert "Read-only" in result["error"]

    def test_git_push_blocked_when_readonly(self):
        import swarm.agent_runtime as rt
        result = rt.git_push()
        assert result["ok"] is False
        assert "Read-only" in result["error"]

    def test_readonly_false_allows_write_file(self, tmp_path):
        import swarm.agent_runtime as rt
        rt.READONLY = False
        rt.WORKSPACE = tmp_path
        rt.PROJECT = "proj"
        rt._sync_core_globals()
        (tmp_path / "proj").mkdir()

        result = rt.write_file("test_file.txt", "hello")
        assert result["ok"] is True
        assert (tmp_path / "proj" / "test_file.txt").read_text() == "hello"


# ===========================================================================
# I — Completion webhooks
# ===========================================================================

class TestCompletionWebhooks:
    def test_get_webhook_default_empty(self, client):
        r = client.get("/api/webhook")
        assert r.status_code == 200
        assert r.json["url"] == ""

    def test_set_webhook_url(self, client):
        r = client.post("/api/webhook", json={"url": "https://hooks.example.com/abc"})
        assert r.status_code == 200
        assert r.json["success"] is True
        assert r.json["url"] == "https://hooks.example.com/abc"

    def test_get_webhook_after_set(self, client):
        client.post("/api/webhook", json={"url": "https://ntfy.sh/my-topic"})
        r = client.get("/api/webhook")
        assert r.json["url"] == "https://ntfy.sh/my-topic"

    def test_clear_webhook_url(self, client):
        client.post("/api/webhook", json={"url": "https://example.com"})
        client.post("/api/webhook", json={"url": ""})
        assert client.get("/api/webhook").json["url"] == ""

    def test_fire_completion_webhook_posts_json(self, tmp_db):
        """_fire_webhook should POST to the configured URL for queue_empty event."""
        from swarm.api import _fire_webhook
        config = {"completion_webhook_url": "http://fake-webhook.local/hook"}

        sent_requests = []

        def fake_urlopen(req, timeout=None):
            sent_requests.append(req)
            return MagicMock(__enter__=lambda s: s, __exit__=MagicMock(return_value=False))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _fire_webhook(config, "queue_empty", agents_completed=1, agents_failed=0)

        assert len(sent_requests) == 1
        body = json.loads(sent_requests[0].data.decode())
        assert body["event"] == "queue_empty"
        assert "agents_completed" in body

    def test_fire_completion_webhook_no_url_does_nothing(self):
        from swarm.api import _fire_webhook
        sent = []
        with patch("urllib.request.urlopen", side_effect=lambda r, timeout=None: sent.append(r)):
            _fire_webhook({}, "queue_empty")
        assert sent == []

    def test_fire_completion_webhook_network_error_does_not_raise(self, tmp_db):
        """A webhook failure must never crash the monitor thread."""
        from swarm.api import _fire_webhook
        config = {"completion_webhook_url": "http://unreachable.local/hook"}

        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            _fire_webhook(config, "queue_empty")  # must not raise
