"""Integration tests for swarm/api_archaeologist.py -- Archaeologist routes."""

import json
import os
import threading

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
            "managed_projects": ["test-project"],
            "paused_projects": [],
            "archaeologist_enabled": True,
            "archaeologist_stall_threshold_hours": 72,
            "archaeologist_max_concurrent": 2,
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
# Archaeologist routes
# ---------------------------------------------------------------------------

class TestArchaeologistStatus:
    def test_status_returns_expected_keys(self, client, app):
        """GET /api/archaeologist/status returns expected shape."""
        r = client.get("/api/archaeologist/status")
        assert r.status_code == 200
        data = r.json
        assert "archaeologist_enabled" in data
        assert "archaeologist_stall_threshold_hours" in data
        assert "archaeologist_max_concurrent" in data
        assert "running_tasks" in data
        assert data["archaeologist_enabled"] is True
        assert data["archaeologist_stall_threshold_hours"] == 72
        assert data["archaeologist_max_concurrent"] == 2

    def test_status_running_tasks_list(self, client, app):
        """running_tasks is always a list."""
        r = client.get("/api/archaeologist/status")
        assert r.status_code == 200
        assert isinstance(r.json["running_tasks"], list)


class TestArchaeologistInvestigate:
    def test_investigate_creates_archaeologist_task(self, client, app):
        """POST /api/archaeologist/investigate/<project> creates a task of type=archaeologist."""
        r = client.post("/api/archaeologist/investigate/test-project",
                        json={"stall_reason": "test stall"},
                        content_type="application/json")
        assert r.status_code == 200
        data = r.json
        assert "task_id" in data
        assert data["status"] == "created"
        assert data["project"] == "test-project"
        assert data["task_id"].startswith("archaeologist-test-project")

        # Verify task in DB
        task = db.task_get(data["task_id"])
        assert task is not None
        assert task["type"] == "archaeologist"
        assert task["project"] == "swarm-controller"
        assert task["priority"] == 55
        # Metadata carries stalled_project
        assert task["metadata"]["stalled_project"] == "test-project"

    def test_investigate_dedupes_same_project(self, client, app):
        """Two investigate calls for the same project return 409."""
        r1 = client.post("/api/archaeologist/investigate/test-project",
                         json={"stall_reason": "test"},
                         content_type="application/json")
        assert r1.status_code == 200

        r2 = client.post("/api/archaeologist/investigate/test-project",
                         json={"stall_reason": "test2"},
                         content_type="application/json")
        assert r2.status_code == 409
        assert "already running" in r2.json["error"]


class TestArchaeologistRun:
    def test_run_scans_and_spawns(self, client, app):
        """POST /api/archaeologist/run scans tasks and spawns for stalled projects."""
        # test-project is in managed_projects but has no tasks -> not stalled
        r = client.post("/api/archaeologist/run", content_type="application/json")
        assert r.status_code == 200
        data = r.json
        assert "scanned" in data
        assert "stalled_found" in data
        assert "spawned" in data
        assert "projects" in data
        assert data["spawned"] == 0

    def test_run_respects_max_concurrent(self, client, app):
        """When max concurrent is reached, run returns 0 spawned."""
        # First manually create 2 archaeologist tasks to hit max_concurrent=2
        for i in range(2):
            r = client.post(f"/api/archaeologist/investigate/test-project{i}",
                            json={"stall_reason": f"stall{i}"},
                            content_type="application/json")
            assert r.status_code == 200

        # Now auto-scan should respect max and spawn 0 (already at cap)
        r = client.post("/api/archaeologist/run", content_type="application/json")
        assert r.status_code == 200
        assert r.json["spawned"] == 0


class TestArchaeologistConfig:
    def test_config_get_returns_expected_keys(self, client, app):
        """GET /api/archaeologist/config returns expected keys."""
        r = client.get("/api/archaeologist/config")
        assert r.status_code == 200
        data = r.json
        assert "archaeologist_enabled" in data
        assert "archaeologist_stall_threshold_hours" in data
        assert "archaeologist_max_concurrent" in data
        assert data["archaeologist_enabled"] is True
        assert data["archaeologist_stall_threshold_hours"] == 72

    def test_config_update(self, client, app, tmp_path):
        """POST /api/archaeologist/config updates config and persists."""
        r = client.post("/api/archaeologist/config",
                        json={"archaeologist_enabled": False},
                        content_type="application/json")
        assert r.status_code == 200
        assert r.json["archaeologist_enabled"] is False

        cfg_file = tmp_path / "config.json"
        assert cfg_file.exists()
        cfg = json.loads(cfg_file.read_text())
        assert cfg["archaeologist_enabled"] is False
