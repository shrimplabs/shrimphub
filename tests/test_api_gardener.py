"""Integration tests for swarm/api_gardener.py -- Gardener routes."""

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
            "gardener_enabled": True,
            "gardener_schedule": 3600,
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
# Gardener routes
# ---------------------------------------------------------------------------

class TestGardenerRun:
    def test_run_creates_gardener_task(self, client, app):
        """POST /api/gardener/run creates a task of type=gardener."""
        r = client.post("/api/gardener/run", content_type="application/json")
        assert r.status_code == 200
        data = r.json
        assert "task_id" in data
        assert data["status"] == "created"
        assert data["task_id"].startswith("gardener-")

        # Verify the task exists in DB
        task = db.task_get(data["task_id"])
        assert task is not None
        assert task["type"] == "gardener"
        assert task["project"] == "swarm-controller"
        assert task["priority"] == 60


class TestGardenerStatus:
    def test_status_returns_expected_shape(self, client, app):
        """GET /api/gardener/status returns last_run_ts, last_report, knowledge_count, enabled."""
        r = client.get("/api/gardener/status")
        assert r.status_code == 200
        data = r.json
        assert "last_run_ts" in data
        assert "last_report" in data
        assert "knowledge_count" in data
        assert "enabled" in data
        # enabled matches gardener_enabled from the app fixture
        assert data["enabled"] is True

    def test_status_knowledge_count_int(self, client, app):
        """knowledge_count is an integer (never a string or null when absent)."""
        r = client.get("/api/gardener/status")
        assert r.status_code == 200
        assert isinstance(r.json["knowledge_count"], int)



class TestGardenerKnowledge:
    def test_knowledge_returns_entries_list(self, client, app):
        """GET /api/gardener/knowledge returns {"entries": [...]}."""
        r = client.get("/api/gardener/knowledge")
        assert r.status_code == 200
        data = r.json
        assert "entries" in data
        assert isinstance(data["entries"], list)

    def test_knowledge_empty_when_no_jsonl(self, client, app):
        """When swarm_knowledge.jsonl does not exist, returns empty list."""
        data_dir = Path(app.config["DATA_DIR"])
        jsonl = data_dir / "swarm_knowledge.jsonl"
        if jsonl.exists():
            jsonl.unlink()
        r = client.get("/api/gardener/knowledge")
        assert r.status_code == 200
        assert r.json["entries"] == []


class TestGardenerConfig:
    def test_gardener_config_returns_defaults(self, client, app):
        """GET /api/gardener/config returns expected keys."""
        r = client.get("/api/gardener/config")
        assert r.status_code == 200
        data = r.json
        for key in ("gardener_enabled", "gardener_schedule",
                    "gardener_max_tasks_per_run", "gardener_skip_projects"):
            assert key in data
        assert data["gardener_enabled"] is True
        assert data["gardener_schedule"] == 3600
        assert data["gardener_max_tasks_per_run"] == 10
        assert isinstance(data["gardener_skip_projects"], list)

    def test_gardener_config_update(self, client, app, tmp_path):
        """POST /api/gardener/config updates config and persists to config.json."""
        r = client.post("/api/gardener/config",
                        json={"gardener_enabled": False},
                        content_type="application/json")
        assert r.status_code == 200
        assert r.json["gardener_enabled"] is False

        # Verify persistence
        cfg_file = tmp_path / "config.json"
        assert cfg_file.exists()
        cfg = json.loads(cfg_file.read_text())
        assert cfg["gardener_enabled"] is False
