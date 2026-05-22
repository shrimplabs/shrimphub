"""Tests for create_app startup helpers extracted from swarm/api.py.

Covers _init_db, _wire_runtime, and _handle_startup_orphans independently
of the full Flask app construction path.
"""

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from swarm import db
from swarm.api import _init_db, _wire_runtime, _handle_startup_orphans


# ---------------------------------------------------------------------------
# Fixture: isolated DB
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    yield tmp_path
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


# ---------------------------------------------------------------------------
# _init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_swarm_db_file(self, isolated_db):
        data_dir = isolated_db / "data"
        data_dir.mkdir()
        db_path = _init_db(data_dir)
        assert db_path.exists()
        assert db_path.name == "swarm.db"

    def test_returns_db_path(self, isolated_db):
        data_dir = isolated_db / "data"
        data_dir.mkdir()
        result = _init_db(data_dir)
        assert result == data_dir / "swarm.db"

    def test_db_usable_after_init(self, isolated_db):
        data_dir = isolated_db / "data"
        data_dir.mkdir()
        _init_db(data_dir)
        # Should be able to upsert and retrieve a task
        db.task_upsert({
            "id": "test-task-1",
            "project": "proj",
            "type": "feature",
            "description": "test",
            "priority": 50,
            "status": "pending",
            "dependencies": [],
            "metadata": {},
            "attempts": 0,
            "max_attempts": 3,
        })
        task = db.task_get("test-task-1")
        assert task is not None
        assert task["id"] == "test-task-1"

    def test_repair_runs_without_error(self, isolated_db):
        """_init_db should not raise even if repair finds nothing to fix."""
        data_dir = isolated_db / "data"
        data_dir.mkdir()
        # Should not raise
        _init_db(data_dir)


# ---------------------------------------------------------------------------
# _wire_runtime
# ---------------------------------------------------------------------------

class TestWireRuntime:
    def test_sets_orchestrator_workspace(self, isolated_db):
        import swarm.orchestrator as orc
        data_dir = isolated_db / "data"
        data_dir.mkdir()
        _init_db(data_dir)
        from swarm.projects import SQLiteProjectRegistry
        pr = SQLiteProjectRegistry(isolated_db / "ws")
        _wire_runtime(
            {"max_active_agents": 2},
            isolated_db / "ws",
            data_dir,
            pr,
        )
        assert orc.WORKSPACE == isolated_db / "ws"

    def test_sets_orchestrator_data_dir(self, isolated_db):
        import swarm.orchestrator as orc
        data_dir = isolated_db / "data"
        data_dir.mkdir()
        _init_db(data_dir)
        from swarm.projects import SQLiteProjectRegistry
        pr = SQLiteProjectRegistry(isolated_db / "ws")
        _wire_runtime({}, isolated_db / "ws", data_dir, pr)
        assert orc.DATA_DIR == data_dir

    def test_sets_lifecycle_data_dir(self, isolated_db):
        import swarm.agent_lifecycle as lc
        data_dir = isolated_db / "data"
        data_dir.mkdir()
        _init_db(data_dir)
        from swarm.projects import SQLiteProjectRegistry
        pr = SQLiteProjectRegistry(isolated_db / "ws")
        _wire_runtime({}, isolated_db / "ws", data_dir, pr)
        assert lc.DATA_DIR == data_dir

    def test_lock_project_propagates(self, isolated_db):
        import swarm.orchestrator as orc
        import swarm.agent_lifecycle as lc
        data_dir = isolated_db / "data"
        data_dir.mkdir()
        _init_db(data_dir)
        from swarm.projects import SQLiteProjectRegistry
        pr = SQLiteProjectRegistry(isolated_db / "ws")
        _wire_runtime({"lock_project": True}, isolated_db / "ws", data_dir, pr)
        assert orc.LOCK_PROJECT is True
        assert lc.LOCK_PROJECT is True

    def test_returns_generate_task_script_or_none(self, isolated_db):
        data_dir = isolated_db / "data"
        data_dir.mkdir()
        _init_db(data_dir)
        from swarm.projects import SQLiteProjectRegistry
        pr = SQLiteProjectRegistry(isolated_db / "ws")
        gen_fn, runner_mod = _wire_runtime({}, isolated_db / "ws", data_dir, pr)
        # Either a callable (swarm_runner available) or None
        assert gen_fn is None or callable(gen_fn)

    def test_returns_runner_mod_when_available(self, isolated_db):
        data_dir = isolated_db / "data"
        data_dir.mkdir()
        _init_db(data_dir)
        from swarm.projects import SQLiteProjectRegistry
        pr = SQLiteProjectRegistry(isolated_db / "ws")
        gen_fn, runner_mod = _wire_runtime({}, isolated_db / "ws", data_dir, pr)
        if gen_fn is not None:
            assert runner_mod is not None
            assert hasattr(runner_mod, "WORKSPACE")


# ---------------------------------------------------------------------------
# _handle_startup_orphans
# ---------------------------------------------------------------------------

class TestHandleStartupOrphans:
    def _seed_agent(self, agent_id, task_id, spawned_at):
        db.project_upsert({"name": "proj", "status": "active"})
        db.task_upsert({
            "id": task_id, "project": "proj", "type": "feature",
            "description": "t", "priority": 50, "status": "in_progress",
            "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 3,
        })
        db.agent_upsert({
            "id": agent_id, "project": "proj", "task_type": "feature",
            "status": "active", "spawned_at": spawned_at,
            "pid": 99999, "log_path": "", "script_path": "",
            "task_id": task_id,
        })
        db.task_update_status(task_id, "in_progress", agent_id=agent_id)

    def test_old_orphan_marked_failed(self, isolated_db, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _init_db(data_dir)
        old_time = (datetime.now() - timedelta(hours=3)).isoformat()
        self._seed_agent("agent-old", "task-old", old_time)
        _handle_startup_orphans(data_dir, agent_timeout=7200)
        agent = db.agent_get("agent-old")
        assert agent["status"] == "failed"

    def test_old_orphan_task_reset_to_pending(self, isolated_db, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _init_db(data_dir)
        old_time = (datetime.now() - timedelta(hours=3)).isoformat()
        self._seed_agent("agent-old2", "task-old2", old_time)
        _handle_startup_orphans(data_dir, agent_timeout=7200)
        task = db.task_get("task-old2")
        assert task["status"] == "pending"

    def test_recent_orphan_left_alone(self, isolated_db, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _init_db(data_dir)
        recent_time = (datetime.now() - timedelta(minutes=5)).isoformat()
        self._seed_agent("agent-new", "task-new", recent_time)
        _handle_startup_orphans(data_dir, agent_timeout=7200)
        agent = db.agent_get("agent-new")
        assert agent["status"] == "active"

    def test_no_agents_is_no_op(self, isolated_db, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        _init_db(data_dir)
        # Should not raise with an empty agent table
        _handle_startup_orphans(data_dir, agent_timeout=7200)
