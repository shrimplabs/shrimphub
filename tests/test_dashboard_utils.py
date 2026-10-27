"""Tests for dashboard-utils.js extraction (lfmn.9).

Covers:
- Flask serves the new dashboard-utils.js route
- The file contains the extracted escapeHtml and projectLargestFileSize functions
- dashboard.js no longer defines escapeHtml or projectLargestFileSize
- dashboard.html loads dashboard-utils.js before dashboard.js
"""

import os
import threading
from pathlib import Path

import pytest

from swarm import db


@pytest.fixture()
def app(tmp_path):
    """Minimal Flask app for static file serving tests."""
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
    yield flask_app

    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


@pytest.fixture()
def client(app):
    return app.test_client()


class TestDashboardUtilsRoute:
    def test_dashboard_utils_js_is_served(self, client):
        r = client.get("/dashboard-utils.js")
        assert r.status_code == 200
        assert b"escapeHtml" in r.data

    def test_dashboard_utils_js_content_type(self, client):
        r = client.get("/dashboard-utils.js")
        assert "javascript" in r.content_type

    def test_dashboard_utils_contains_escape_html(self, client):
        r = client.get("/dashboard-utils.js")
        assert b"function escapeHtml" in r.data

    def test_dashboard_utils_contains_project_largest_file_size(self, client):
        r = client.get("/dashboard-utils.js")
        assert b"function projectLargestFileSize" in r.data


class TestDashboardJsNoLongerDefines:
    """Verify the extracted functions are no longer defined in dashboard.js."""

    def _dashboard_js_content(self):
        root = Path(__file__).parent.parent
        return (root / "dashboard.js").read_text(encoding="utf-8")

    def test_dashboard_js_does_not_define_escape_html(self):
        content = self._dashboard_js_content()
        assert "function escapeHtml" not in content

    def test_dashboard_js_does_not_define_project_largest_file_size(self):
        content = self._dashboard_js_content()
        assert "function projectLargestFileSize" not in content

    def test_dashboard_modules_still_use_escape_html(self):
        # dashboard.js is now just the init block; escapeHtml calls live in the
        # split modules. Verify at least one module still calls it.
        root = Path(__file__).parent.parent
        all_js = "".join(
            p.read_text(encoding="utf-8")
            for p in root.glob("dashboard*.js")
            if p.name != "dashboard-utils.js"
        )
        assert "escapeHtml(" in all_js


class TestDashboardHtmlLoadOrder:
    """Verify dashboard-utils.js is loaded before dashboard.js in dashboard.html."""

    def _html_content(self):
        root = Path(__file__).parent.parent
        return (root / "dashboard.html").read_text(encoding="utf-8")

    def test_dashboard_utils_is_loaded(self):
        html = self._html_content()
        assert "dashboard-utils.js" in html

    def test_dashboard_utils_loaded_before_dashboard_js(self):
        html = self._html_content()
        utils_pos = html.index("dashboard-utils.js")
        main_pos = html.index('src="dashboard.js')
        assert utils_pos < main_pos, "dashboard-utils.js must be loaded before dashboard.js"

    def test_dashboard_utils_loaded_before_closure_js(self):
        html = self._html_content()
        utils_pos = html.index("dashboard-utils.js")
        closure_pos = html.index("dashboard_closure.js")
        assert utils_pos < closure_pos, "dashboard-utils.js must be loaded before dashboard_closure.js"
