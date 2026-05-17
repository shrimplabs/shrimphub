"""Integration tests for login API endpoints."""
import json
import threading
from pathlib import Path

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
            "login_required": True,  # Enable login for tests
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


class TestLoginAPI:
    def test_login_returns_401_when_login_required_and_invalid_credentials(self, client):
        """Login should return 401 with invalid credentials when login required."""
        r = client.post("/api/login", 
                        json={"username": "wrong", "password": "wrong"},
                        content_type="application/json")
        assert r.status_code == 401
        assert "error" in r.json

    def test_login_returns_400_when_missing_credentials(self, client):
        """Login should return 400 when credentials are missing."""
        r = client.post("/api/login", json={}, content_type="application/json")
        assert r.status_code == 400
        assert "error" in r.json

    def test_login_returns_401_with_default_credentials_when_login_required(self, client):
        """Login should return 401 even with default credentials when login required."""
        # Default credentials are admin/admin
        r = client.post("/api/login",
                        json={"username": "admin", "password": "admin"},
                        content_type="application/json")
        # Should work with default credentials
        assert r.status_code == 200
        assert r.json.get("success") is True
        assert "session_token" in r.json

    def test_session_check_returns_401_when_no_token(self, client):
        """Session check should return 401 when no token provided."""
        r = client.get("/api/session")
        assert r.status_code == 401

    def test_logout_returns_success(self, client):
        """Logout should return success."""
        r = client.post("/api/logout")
        assert r.status_code == 200
        assert r.json.get("success") is True

    def test_full_login_flow(self, client):
        """Test complete login flow: login, check session, logout."""
        # Login
        r = client.post("/api/login",
                        json={"username": "admin", "password": "admin"},
                        content_type="application/json")
        assert r.status_code == 200
        token = r.json.get("session_token")
        assert token is not None

        # Check session with token
        r = client.get("/api/session", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json.get("authenticated") is True
        assert r.json.get("username") == "admin"

        # Logout
        r = client.post("/api/logout", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

        # Session should now be invalid
        r = client.get("/api/session", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
