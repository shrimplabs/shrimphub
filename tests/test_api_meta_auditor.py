"""Integration tests for swarm/api_meta_auditor.py -- Auditor routes."""

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
            "managed_projects": ["test-project"],
            "paused_projects": [],
            "meta_auditor_enabled": True,
            "meta_auditor_interval_days": 7,
            "meta_auditor_max_tasks": 20,
            "meta_mode_enabled": True,
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
# Auditor routes
# ---------------------------------------------------------------------------
class TestAuditorStatus:
    def test_status_returns_expected_keys(self, client, app):
        """GET /api/meta-auditor/status returns expected shape."""
        r = client.get("/api/meta-auditor/status")
        assert r.status_code == 200
        data = r.json
        assert "last_run_ts" in data
        assert "last_report" in data
        assert "enabled" in data
        assert "interval_days" in data
        assert "max_tasks" in data
        assert data["enabled"] is True
        assert data["interval_days"] == 7
        assert data["max_tasks"] == 20

    def test_status_returns_null_report_initially(self, client, app):
        """last_report is null when no AUDIT_REPORT.md exists."""
        r = client.get("/api/meta-auditor/status")
        assert r.status_code == 200
        assert r.json["last_report"] is None

    def test_status_returns_report_when_file_exists(self, client, app, tmp_path):
        """last_report contains report content when AUDIT_REPORT.md exists."""
        # Write a report file in the data directory
        data_dir = Path(app.config["DATA_DIR"])
        data_dir.mkdir(parents=True, exist_ok=True)
        report = data_dir / "AUDIT_REPORT.md"
        report.write_text("# Audit Report\n\nTest findings.\n")

        r = client.get("/api/meta-auditor/status")
        assert r.status_code == 200
        assert "Test findings" in r.json["last_report"]


class TestAuditorRun:
    def test_run_creates_auditor_task(self, client, app):
        """POST /api/meta-auditor/run creates a task of type=meta_auditor."""
        r = client.post("/api/meta-auditor/run",
                        content_type="application/json")
        assert r.status_code == 200
        data = r.json
        assert "task_id" in data
        assert data["status"] == "created"
        assert data["task_id"].startswith("meta-auditor-")

        # Verify task in DB
        task = db.task_get(data["task_id"])
        assert task is not None
        assert task["type"] == "meta_auditor"
        assert task["project"] == "swarm-controller"
        assert task["priority"] == 60
        assert task["status"] == "pending"

    def test_run_updates_last_run_ts(self, client, app):
        """POST /api/meta-auditor/run updates the last_run_ts in config."""
        r = client.post("/api/meta-auditor/run",
                        content_type="application/json")
        assert r.status_code == 200

        # Check the status reflects the new timestamp
        r2 = client.get("/api/meta-auditor/status")
        assert r2.status_code == 200
        assert r2.json["last_run_ts"] > 0

    def test_run_stores_managed_projects_in_metadata(self, client, app):
        """The created auditor task includes managed_projects in its metadata."""
        r = client.post("/api/meta-auditor/run",
                        content_type="application/json")
        assert r.status_code == 200
        task_id = r.json["task_id"]

        task = db.task_get(task_id)
        assert "managed_projects" in task["metadata"]
        assert "test-project" in task["metadata"]["managed_projects"]


class TestAuditorConfig:
    def test_config_get_returns_expected_keys(self, client, app):
        """GET /api/meta-auditor/config returns expected keys."""
        r = client.get("/api/meta-auditor/config")
        assert r.status_code == 200
        data = r.json
        assert "meta_auditor_enabled" in data
        assert "meta_auditor_interval_days" in data
        assert "meta_auditor_max_tasks" in data
        assert data["meta_auditor_enabled"] is True
        assert data["meta_auditor_interval_days"] == 7
        assert data["meta_auditor_max_tasks"] == 20

    def test_config_update_enabled(self, client, app, tmp_path):
        """POST /api/meta-auditor/config updates enabled flag and persists."""
        r = client.post("/api/meta-auditor/config",
                        json={"meta_auditor_enabled": False},
                        content_type="application/json")
        assert r.status_code == 200
        assert r.json["meta_auditor_enabled"] is False

        cfg_file = tmp_path / "config.json"
        assert cfg_file.exists()
        cfg = json.loads(cfg_file.read_text())
        assert cfg["meta_auditor_enabled"] is False

    def test_config_update_interval(self, client, app, tmp_path):
        """POST /api/meta-auditor/config updates interval_days and persists."""
        r = client.post("/api/meta-auditor/config",
                        json={"meta_auditor_interval_days": 14},
                        content_type="application/json")
        assert r.status_code == 200
        assert r.json["meta_auditor_interval_days"] == 14

        cfg_file = tmp_path / "config.json"
        cfg = json.loads(cfg_file.read_text())
        assert cfg["meta_auditor_interval_days"] == 14

    def test_config_update_max_tasks(self, client, app, tmp_path):
        """POST /api/meta-auditor/config updates max_tasks and persists."""
        r = client.post("/api/meta-auditor/config",
                        json={"meta_auditor_max_tasks": 5},
                        content_type="application/json")
        assert r.status_code == 200
        assert r.json["meta_auditor_max_tasks"] == 5

        cfg_file = tmp_path / "config.json"
        cfg = json.loads(cfg_file.read_text())
        assert cfg["meta_auditor_max_tasks"] == 5


class TestAuditorStateFile:
    def test_state_file_persisted_on_run(self, client, app, tmp_path):
        """Running the auditor creates an auditor_state.json file."""
        r = client.post("/api/meta-auditor/run",
                        content_type="application/json")
        assert r.status_code == 200

        data_dir = Path(app.config["DATA_DIR"])
        state_file = data_dir / "auditor_state.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert "_meta_auditor_last_run_ts" in state
        assert state["_meta_auditor_last_run_ts"] > 0
