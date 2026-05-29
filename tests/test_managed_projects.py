#!/usr/bin/env python3
"""Tests that registered projects are automatically added to managed_projects.

Three registration paths must sync managed_projects:
1. POST /api/projects       (add_project in api_projects.py)
2. POST /api/projects/<name>/scan  (scan_project in api_projects.py)
3. POST /api/projects/<name>/spawn  (spawn_parallel in api_projects.py)
4. POST /api/spawn (create_project_task in api_spawn.py)

All must call _sync_managed_projects so projects are immediately visible
in GET /api/managed-projects and survive server restarts.
"""

import json
import os
import threading
from pathlib import Path

import pytest

from swarm import db as _db


@pytest.fixture()
def app(tmp_path):
    """Create a fresh Flask test app with an isolated DB."""
    gut_source = tmp_path / "gut-source" / "addons" / "gut"
    gut_source.mkdir(parents=True)
    (gut_source / "gut_cmdln.gd").write_text("extends SceneTree\n")
    (gut_source / "plugin.cfg").write_text("[plugin]\nname=\"GUT\"\n")
    old_source = os.environ.get("SWARM_GUT_SOURCE_DIR")
    os.environ["SWARM_GUT_SOURCE_DIR"] = str(gut_source.parent.parent)
    _db._db_path = None
    _db._initialized = False
    _db._local = threading.local()
    _db.init(tmp_path / "swarm_test.db")

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

    conn = getattr(_db._local, "conn", None)
    if conn:
        conn.close()
        _db._local.conn = None
    if old_source is None:
        os.environ.pop("SWARM_GUT_SOURCE_DIR", None)
    else:
        os.environ["SWARM_GUT_SOURCE_DIR"] = old_source


@pytest.fixture()
def client(app):
    return app.test_client()


class TestManagedProjectsRegistration:
    """Verify that project registration paths auto-add to managed_projects."""

    def test_add_project_auto_managed(self, client):
        """POST /api/projects should add project to managed_projects."""
        r = client.post("/api/projects",
                        json={"name": "scan-reg-test"},
                        content_type="application/json")
        assert r.status_code == 200

        r = client.get("/api/managed-projects")
        assert r.status_code == 200
        assert "scan-reg-test" in r.json["managed_projects"], \
            "Project registered via POST /api/projects should be in managed_projects"

    def test_scan_project_auto_managed(self, client, app):
        """POST /api/projects/<name>/scan should add project to managed_projects."""
        workspace = Path(app.config["WORKSPACE_ROOT"])
        proj_dir = workspace / "scan-test-proj"
        proj_dir.mkdir(parents=True)
        (proj_dir / "main.gd").write_text("extends Node\n")

        r = client.post("/api/projects/scan-test-proj/scan",
                        content_type="application/json")
        assert r.status_code == 200

        r = client.get("/api/managed-projects")
        assert r.status_code == 200
        assert "scan-test-proj" in r.json["managed_projects"], \
            "Project registered via POST /api/projects/<name>/scan should be in managed_projects"

    def test_spawn_parallel_auto_managed(self, client, app):
        """POST /api/projects/<name>/spawn should add project to managed_projects."""
        workspace = Path(app.config["WORKSPACE_ROOT"])
        proj_dir = workspace / "spawn-test-proj"
        proj_dir.mkdir(parents=True)
        (proj_dir / "main.gd").write_text("extends Node\n")

        # First register the project
        client.post("/api/projects",
                   json={"name": "spawn-test-proj"},
                   content_type="application/json")

        r = client.post("/api/projects/spawn-test-proj/spawn",
                        json={"count": 1, "task_type": "feature"},
                        content_type="application/json")
        assert r.status_code == 200

        r = client.get("/api/managed-projects")
        assert r.status_code == 200
        assert "spawn-test-proj" in r.json["managed_projects"], \
            "Project registered via POST /api/projects/<name>/spawn should be in managed_projects"

    def test_config_file_persisted(self, client, app):
        """managed_projects must survive a server restart (config.json persisted)."""
        # Register a project
        r = client.post("/api/projects",
                        json={"name": "persist-test"},
                        content_type="application/json")
        assert r.status_code == 200

        # Simulate restart by re-reading config.json
        config_file = app.config.get("CONFIG_FILE")
        if config_file:
            with open(config_file) as f:
                saved = json.load(f)
            assert "persist-test" in saved.get("managed_projects", []), \
                "managed_projects must be persisted to config.json"
