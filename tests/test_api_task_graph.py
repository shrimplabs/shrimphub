"""API tests for task graph operations.

Extracted from tests/test_api.py. Covers:
- TestInsertTaskAfter  — POST /api/tasks/<id>/insert-after
- TestGetTaskDependents — GET /api/tasks/<id>/dependents
- TestDependencies     — GET /api/dependencies/* endpoints
"""

import json
import os
import threading
from pathlib import Path

import pytest

from swarm import db


@pytest.fixture()
def app(tmp_path):
    """Create a fresh Flask test app with an isolated DB."""
    gut_source = tmp_path / "gut-source" / "addons" / "gut"
    gut_source.mkdir(parents=True)
    (gut_source / "gut_cmdln.gd").write_text("extends SceneTree\n")
    (gut_source / "plugin.cfg").write_text("[plugin]\nname=\"GUT\"\n")
    old_source = os.environ.get("SWARM_GUT_SOURCE_DIR")
    os.environ["SWARM_GUT_SOURCE_DIR"] = str(gut_source.parent.parent)
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
            "disable_monitor": True,
            "disable_remote_repo": True,
            "project_creation_retry_rounds": 0,
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
    flask_app.config["DATA_DIR"] = str(tmp_path / "data")
    flask_app.config["PROJECT_CREATION_RETRY_ROUNDS_OVERRIDE"] = 0
    yield flask_app

    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None
    if old_source is None:
        os.environ.pop("SWARM_GUT_SOURCE_DIR", None)
    else:
        os.environ["SWARM_GUT_SOURCE_DIR"] = old_source


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Insert-after (dependency chain surgery)
# ---------------------------------------------------------------------------

class TestInsertTaskAfter:
    def test_insert_after_basic(self, client):
        """New task gets task_id as its dependency; source is unaffected."""
        r = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "original task"
        })
        task_id = r.json["task"]["id"]

        r2 = client.post(f"/api/tasks/{task_id}/insert-after", json={
            "type": "polish",
            "description": "inserted task",
            "priority": 70,
        })
        assert r2.status_code == 200
        inserted = r2.json["inserted_task"]
        assert inserted["dependencies"] == [task_id]
        assert inserted["type"] == "polish"
        assert inserted["description"] == "inserted task"
        assert inserted["priority"] == 70
        assert r2.json["reparented"] == []

    def test_insert_after_reparents_dependents(self, client):
        """Tasks that depended on task_id now depend on the new task instead."""
        r1 = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "source"
        })
        src_id = r1.json["task"]["id"]

        r2 = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "dependent",
            "dependencies": [src_id],
        })
        dep_id = r2.json["task"]["id"]

        r3 = client.post(f"/api/tasks/{src_id}/insert-after", json={
            "type": "polish", "description": "inserted"
        })
        assert r3.status_code == 200
        new_id = r3.json["inserted_task"]["id"]
        assert r3.json["reparented"] == [dep_id]

        # Dependent now points to new task, not source
        r4 = client.get(f"/api/tasks/{dep_id}")
        assert new_id in r4.json["task"]["dependencies"]
        assert src_id not in r4.json["task"]["dependencies"]

    def test_insert_after_multiple_dependents(self, client):
        """All dependents are reparented; new task is their only parent."""
        src_id = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "source"
        }).json["task"]["id"]

        dep_ids = []
        for i in range(3):
            r = client.post("/api/tasks", json={
                "project": "p", "type": "bug", "description": f"dep {i}",
                "dependencies": [src_id],
            })
            dep_ids.append(r.json["task"]["id"])

        r = client.post(f"/api/tasks/{src_id}/insert-after", json={
            "description": "middle"
        })
        assert r.status_code == 200
        assert set(r.json["reparented"]) == set(dep_ids)

        # Each dep now has the new task as its sole dependency
        new_id = r.json["inserted_task"]["id"]
        for did in dep_ids:
            deps = client.get(f"/api/tasks/{did}").json["task"]["dependencies"]
            assert deps == [new_id]

    def test_insert_after_task_not_found(self, client):
        r = client.post("/api/tasks/ghost/insert-after", json={"description": "nope"})
        assert r.status_code == 404
        assert "not found" in r.json["error"].lower()

    def test_insert_after_inherits_project_and_priority(self, client):
        """Source project and priority are inherited when not overridden."""
        r1 = client.post("/api/tasks", json={
            "project": "acme", "type": "feature", "description": "source",
            "priority": 85,
        })
        src_id = r1.json["task"]["id"]

        r2 = client.post(f"/api/tasks/{src_id}/insert-after", json={
            "description": "inserted"
        })
        assert r2.status_code == 200
        inserted = r2.json["inserted_task"]
        assert inserted["project"] == "acme"
        assert inserted["priority"] == 85


# ---------------------------------------------------------------------------
# Dependents
# ---------------------------------------------------------------------------

class TestGetTaskDependents:
    def test_dependents_returns_active_tasks(self, client):
        """Pending/in-progress tasks that depend on task_id are returned."""
        parent = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "parent"
        }).json["task"]["id"]

        dep1 = client.post("/api/tasks", json={
            "project": "p", "type": "bug", "description": "dep1",
            "dependencies": [parent],
        }).json["task"]["id"]

        dep2 = client.post("/api/tasks", json={
            "project": "q", "type": "polish", "description": "dep2",
            "dependencies": [parent],
        }).json["task"]["id"]

        r = client.get(f"/api/tasks/{parent}/dependents")
        assert r.status_code == 200
        assert r.json["task_id"] == parent
        ids = {d["id"] for d in r.json["dependents"]}
        assert ids == {dep1, dep2}

    def test_dependents_excludes_completed_tasks(self, client):
        """Completed/failed/cancelled tasks are not returned."""
        parent = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "parent"
        }).json["task"]["id"]

        dep = client.post("/api/tasks", json={
            "project": "p", "type": "bug", "description": "dep",
            "dependencies": [parent],
        }).json["task"]["id"]

        client.put(f"/api/tasks/{dep}", json={"status": "completed"})

        r = client.get(f"/api/tasks/{parent}/dependents")
        assert r.status_code == 200
        ids = {d["id"] for d in r.json["dependents"]}
        assert dep not in ids

    def test_dependents_excludes_self(self, client):
        """A task that lists itself in dependencies is not returned."""
        parent = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "parent"
        }).json["task"]["id"]

        client.put(f"/api/tasks/{parent}", json={"dependencies": [parent]})

        r = client.get(f"/api/tasks/{parent}/dependents")
        assert r.status_code == 200
        ids = {d["id"] for d in r.json["dependents"]}
        assert parent not in ids

    def test_dependents_404_for_missing_task(self, client):
        r = client.get("/api/tasks/ghost/dependents")
        assert r.status_code == 404

    def test_dependents_empty_when_no_dependents(self, client):
        orphan = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "orphan"
        }).json["task"]["id"]

        r = client.get(f"/api/tasks/{orphan}/dependents")
        assert r.status_code == 200
        assert r.json["dependents"] == []


# ---------------------------------------------------------------------------
# Dependencies endpoints
# ---------------------------------------------------------------------------

class TestDependencies:
    def test_dependency_stats(self, client):
        r = client.get("/api/dependencies")
        assert r.status_code == 200

    def test_ready_tasks_empty(self, client):
        r = client.get("/api/dependencies/ready")
        assert r.status_code == 200

    def test_execution_order_empty(self, client):
        r = client.get("/api/dependencies/execution-order")
        assert r.status_code == 200

    def test_dot_graph(self, client):
        r = client.get("/api/dependencies/dot")
        assert r.status_code == 200
        assert "dot" in r.json

    def test_dot_graph_escapes_malformed_dependency_ids(self, client):
        # Create a real dependency task whose ID includes quotes, then reference it.
        dep_id = '<id>"'
        dep_resp = client.post("/api/tasks", json={
            "id": dep_id,
            "project": "p",
            "type": "feature",
            "description": "quoted dependency task",
            "priority": 40,
        }, content_type="application/json")
        assert dep_resp.status_code in (200, 201)

        r = client.post("/api/tasks", json={
            "id": "quoted-dep-task",
            "project": "p",
            "type": "feature",
            "description": "task with malformed dep id",
            "dependencies": [dep_id],
            "priority": 50,
        }, content_type="application/json")
        assert r.status_code in (200, 201)

        dot_resp = client.get("/api/dependencies/dot?project=p")
        assert dot_resp.status_code == 200
        dot = dot_resp.json.get("dot", "")
        # Broken form would be: "<id>"" -> "quoted-dep-task";
        assert '"<id>"" -> "quoted-dep-task"' not in dot
        # Escaped form should be present.
        assert '"<id>\\"" -> "quoted-dep-task"' in dot

    def test_ready_tasks_with_deps(self, client):
        # Create parent and child tasks
        r1 = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "parent", "priority": 50
        }, content_type="application/json")
        parent_id = r1.json["task"]["id"]

        client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "child",
            "dependencies": [parent_id], "priority": 50
        }, content_type="application/json")

        r = client.get("/api/dependencies/ready")
        # Response is {"ready": [...]} — list of task objects with id/project/type/description/priority/dependencies
        ready = r.json.get("ready", [])
        ready_ids = [t["id"] for t in ready]
        # Only parent should be ready (child has unmet dep)
        assert parent_id in ready_ids
        # Verify full object shape
        for t in ready:
            assert {"id", "project", "type", "description", "priority", "dependencies"} == set(t.keys())

    def test_ready_tasks_honor_archived_completed_dependencies(self, client):
        from swarm import db

        db.task_record_completed("archived-parent", project="p")
        client.post("/api/tasks", json={
            "id": "child-after-archive",
            "project": "p",
            "type": "feature",
            "description": "child",
            "dependencies": ["archived-parent"],
            "priority": 50,
        }, content_type="application/json")

        r = client.get("/api/dependencies/ready?project=p")
        assert r.status_code == 200
        ready_ids = [t["id"] for t in r.json.get("ready", [])]
        assert "child-after-archive" in ready_ids

    def test_ready_tasks_honor_task_history_completed_dependencies(self, client, app):
        history_file = Path(app.config["DATA_DIR"]) / "task-history.jsonl"
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history_file.write_text(
            json.dumps({
                "id": "history-parent",
                "project": "p",
                "status": "completed",
                "completed": "2026-05-06T20:00:00",
            }) + "\n",
            encoding="utf-8",
        )
        # Seed completed_task_ids so the validator accepts it as a known completed dep
        db.task_record_completed("history-parent", "p")
        client.post("/api/tasks", json={
            "id": "child-after-history",
            "project": "p",
            "type": "feature",
            "description": "child",
            "dependencies": ["history-parent"],
            "priority": 50,
        }, content_type="application/json")

        r = client.get("/api/dependencies/ready?project=p")
        assert r.status_code == 200
        ready_ids = [t["id"] for t in r.json.get("ready", [])]
        assert "child-after-history" in ready_ids
