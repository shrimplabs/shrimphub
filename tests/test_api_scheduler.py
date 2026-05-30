"""Tests for the Scheduler API routes."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def app():
    """Create a Flask app with scheduler routes registered."""
    from swarm.api import create_app

    import swarm.api_scheduler as _sched_mod

    # Cancel any pre-existing timer FIRST -- it holds a closure over a previous
    # app's data_dir and config dict. If left running, its periodic _fire() call
    # can race with the new test's first POST, call _run_scheduler_task() using
    # the new test's fresh data_dir, and insert a stale task into the new DB,
    # causing the test to see a 409 instead of the expected 200 on its first
    # POST /api/scheduler/run.
    with _sched_mod._scheduler_lock:
        if _sched_mod._scheduler_timer is not None:
            _sched_mod._scheduler_timer.cancel()
            _sched_mod._scheduler_timer = None
        _sched_mod._last_scheduler_run_ts = 0.0

    # Clean up any stale scheduler tasks that may have been left behind by
    # previous test classes or a prior test's teardown (e.g. if the process
    # was killed before cleanup ran).
    try:
        from swarm import db as _db
        for t in _db.task_get_all():
            if t.get("type") == "scheduler" and t.get("status") in (
                "pending", "in_progress"
            ):
                _db.task_delete(t["id"])
    except Exception:
        pass

    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "data"
        data_dir.mkdir()
        workspace = Path(tmpdir) / "workspace"
        workspace.mkdir()
        config_file = Path(tmpdir) / "config.json"
        config_file.write_text(json.dumps({
            "workspace": str(workspace),
            "meta_mode_enabled": True,
            "scheduler_enabled": True,
            "scheduler_interval_minutes": 15,
            "scheduler_allow_pause": True,
            "scheduler_allow_agent_ceiling_adjust": True,
            "scheduler_off_peak_hours": [0, 6],
        }) + "\n")

        app = create_app(
            workspace=workspace,
            data_dir=data_dir,
            config_file=config_file,
            config={
                "meta_mode_enabled": True,
                "scheduler_enabled": True,
                "scheduler_interval_minutes": 15,
                "scheduler_allow_pause": True,
                "scheduler_allow_agent_ceiling_adjust": True,
                "scheduler_off_peak_hours": [0, 6],
            },
        )
        # Clean up any stale scheduler tasks BEFORE the app is used.
        # This prevents bleed-over from previous test class runs (e.g.
        # TestSchedulerRunCreates runs before TestSchedulerRunPrevents and
        # leaves a pending scheduler task that causes the first POST in
        # TestSchedulerRunPrevents to return 409 instead of 200).
        with _sched_mod._scheduler_lock:
            if _sched_mod._scheduler_timer is not None:
                _sched_mod._scheduler_timer.cancel()
                _sched_mod._scheduler_timer = None
        try:
            from swarm import db as _db
            for t in _db.task_get_all():
                if t.get("type") == "scheduler" and t.get("status") in (
                    "pending", "in_progress"
                ):
                    _db.task_delete(t["id"])
        except Exception:
            pass

        app.config["TESTING"] = True
        try:
            yield app
        finally:
            # After each test: cancel the timer and clean up scheduler tasks from the DB.
            # Without this, a pending scheduler task from one test bleeds into the next
            # test's _is_scheduler_running() check, causing spurious 409 responses.
            with _sched_mod._scheduler_lock:
                if _sched_mod._scheduler_timer is not None:
                    _sched_mod._scheduler_timer.cancel()
                    _sched_mod._scheduler_timer = None
            try:
                from swarm import db as _db
                for t in _db.task_get_all():
                    if t.get("type") == "meta_scheduler" and t.get("status") in (
                        "pending", "in_progress"
                    ):
                        _db.task_delete(t["id"])
            except Exception:
                pass


@pytest.fixture
def client(app):
    return app.test_client()


class TestSchedulerConfig:
    """GET /api/scheduler/config -- return current scheduler configuration."""

    def test_returns_default_config(self, client):
        resp = client.get("/api/scheduler/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["scheduler_enabled"] is True
        assert data["scheduler_interval_minutes"] == 15
        assert data["scheduler_allow_pause"] is True
        assert data["scheduler_allow_agent_ceiling_adjust"] is True
        assert data["scheduler_off_peak_hours"] == [0, 6]

    def test_returns_disabled_config(self, client):
        resp = client.get("/api/scheduler/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "scheduler_enabled" in data
        assert "scheduler_interval_minutes" in data


class TestSchedulerStatus:
    """GET /api/scheduler/status -- return scheduler status."""

    def test_returns_status(self, client):
        resp = client.get("/api/scheduler/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "scheduler_enabled" in data
        assert "last_run_ts" in data


class TestSchedulerRunCreates:
    """POST /api/scheduler/run -- trigger a scheduler task immediately.
    Isolated class so DB state from TestSchedulerRunPrevents doesn't leak."""

    def test_creates_scheduler_task(self, client):
        resp = client.post("/api/scheduler/run")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "task_id" in data
        assert data["status"] == "created"


class TestSchedulerRunPrevents:
    """POST /api/scheduler/run -- prevents duplicate scheduler tasks.
    Isolated class so the DB is fresh (same-app fixture issue: pytest shares
    the same app across tests in the same class, so this gets its own class)."""

    def test_prevents_duplicate_scheduler_tasks(self, client):
        # Create first task
        resp1 = client.post("/api/scheduler/run")
        assert resp1.status_code == 200
        # Second task should be blocked (already pending/in_progress)
        resp2 = client.post("/api/scheduler/run")
        assert resp2.status_code == 409
        data = resp2.get_json()
        assert "already" in data["error"].lower()


class TestSchedulerConfigUpdate:
    """POST /api/scheduler/config -- update scheduler configuration."""

    def test_can_disable_scheduler(self, client):
        resp = client.post(
            "/api/scheduler/config",
            json={"scheduler_enabled": False},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["scheduler_enabled"] is False

    def test_can_update_interval(self, client):
        resp = client.post(
            "/api/scheduler/config",
            json={"scheduler_interval_minutes": 30},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["scheduler_interval_minutes"] == 30

    def test_can_update_off_peak_hours(self, client):
        resp = client.post(
            "/api/scheduler/config",
            json={"scheduler_off_peak_hours": [22, 6]},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["scheduler_off_peak_hours"] == [22, 6]


class TestSchedulerOrchestratorGlobals:
    """Verify scheduler globals are wired into orchestrator from config."""

    def test_scheduler_globals_synced_from_config(self, client):
        # The fixture sets scheduler_enabled=True and interval=15
        from swarm import orchestrator
        assert orchestrator.SCHEDULER_ENABLED is True
        assert orchestrator.SCHEDULER_INTERVAL_MINUTES == 15  # from fixture config
        assert orchestrator.SCHEDULER_ALLOW_PAUSE is True
        assert orchestrator.SCHEDULER_ALLOW_AGENT_CEILING_ADJUST is True
        assert orchestrator.SCHEDULER_OFF_PEAK_HOURS == [0, 6]


class TestSchedulerMetaModeIntegration:
    """GET /api/meta-mode includes scheduler status."""

    def test_scheduler_in_meta_mode_response(self, client):
        resp = client.get("/api/meta-mode")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "agents" in data
        assert "scheduler" in data["agents"]
        sched = data["agents"]["scheduler"]
        assert "enabled" in sched
        assert "interval_minutes" in sched
        assert "allow_pause" in sched
        assert "allow_agent_ceiling_adjust" in sched
        assert "off_peak_hours" in sched
