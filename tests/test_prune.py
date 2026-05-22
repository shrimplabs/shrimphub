"""
Tests for prune_history() and check_quota_limit() in swarm/orchestrator.py.
"""
import json
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from swarm import db
import swarm.orchestrator as orc
import swarm.agent_lifecycle as lifecycle


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_orc(tmp_path):
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm.db")

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    orc.DATA_DIR = data_dir
    orc.HISTORY_FILE = data_dir / "agent-history.jsonl"

    with lifecycle._handle_lock:
        lifecycle._active_handles.clear()

    yield tmp_path

    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_agent(agent_id="agent-001", status="completed", project="p",
                task_type="feature", exit_code=0):
    db.agent_upsert({
        "id": agent_id,
        "project": project,
        "task_type": task_type,
        "status": status,
        "spawned_at": "2026-01-01T00:00:00",
        "pid": 12345,
        "exit_code": exit_code,
        "output": "done",
        "task_id": f"task-for-{agent_id}",
    })


def _seed_task(task_id="task-001", status="completed", project="p"):
    db.task_upsert({
        "id": task_id,
        "project": project,
        "type": "feature",
        "description": "desc",
        "priority": 50,
        "status": status,
        "dependencies": [],
        "metadata": {},
        "attempts": 0,
        "max_attempts": 3,
    })


# ---------------------------------------------------------------------------
# prune_history() tests
# ---------------------------------------------------------------------------

class TestPruneHistory:
    def test_completed_agent_moved_to_jsonl(self, isolated_orc):
        _seed_agent("a1", status="completed")
        orc.prune_history()

        lines = orc.HISTORY_FILE.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["id"] == "a1"

    def test_completed_agent_removed_from_db(self, isolated_orc):
        _seed_agent("a1", status="completed")
        orc.prune_history()
        assert db.agent_get("a1") is None

    def test_failed_agent_moved_to_jsonl(self, isolated_orc):
        _seed_agent("a2", status="failed", exit_code=1)
        orc.prune_history()

        lines = orc.HISTORY_FILE.read_text().strip().splitlines()
        assert any(json.loads(l)["id"] == "a2" for l in lines)

    def test_active_agent_not_pruned(self, isolated_orc):
        _seed_agent("a3", status="active")
        orc.prune_history()
        assert db.agent_get("a3") is not None
        assert not orc.HISTORY_FILE.exists()

    def test_completed_task_removed_from_db(self, isolated_orc):
        _seed_task("t1", status="completed")
        _seed_agent("a1", status="completed")
        orc.prune_history()
        assert db.task_get("t1") is None

    def test_failed_task_removed_from_db(self, isolated_orc):
        _seed_task("t2", status="failed")
        _seed_agent("a1", status="completed")
        orc.prune_history()
        assert db.task_get("t2") is None

    def test_pending_task_not_removed(self, isolated_orc):
        _seed_task("t3", status="pending")
        _seed_agent("a1", status="completed")
        orc.prune_history()
        assert db.task_get("t3") is not None

    def test_in_progress_task_not_removed(self, isolated_orc):
        _seed_task("t4", status="in_progress")
        _seed_agent("a1", status="completed")
        orc.prune_history()
        assert db.task_get("t4") is not None

    def test_multiple_agents_all_appended(self, isolated_orc):
        _seed_agent("a1", status="completed")
        _seed_agent("a2", status="failed")
        orc.prune_history()

        lines = orc.HISTORY_FILE.read_text().strip().splitlines()
        ids = {json.loads(l)["id"] for l in lines}
        assert ids == {"a1", "a2"}

    def test_jsonl_entries_are_valid_json(self, isolated_orc):
        _seed_agent("a1", status="completed")
        orc.prune_history()

        for line in orc.HISTORY_FILE.read_text().strip().splitlines():
            obj = json.loads(line)  # must not raise
            assert isinstance(obj, dict)

    def test_calling_twice_does_not_duplicate_entries(self, isolated_orc):
        _seed_agent("a1", status="completed")
        orc.prune_history()

        # Second call: nothing in DB to prune
        _seed_agent("a2", status="completed")
        orc.prune_history()

        lines = orc.HISTORY_FILE.read_text().strip().splitlines()
        ids = [json.loads(l)["id"] for l in lines]
        assert ids.count("a1") == 1
        assert ids.count("a2") == 1

    def test_no_finished_agents_creates_no_file(self, isolated_orc):
        # All active
        _seed_agent("a1", status="active")
        orc.prune_history()
        assert not orc.HISTORY_FILE.exists()

    def test_history_dir_created_if_missing(self, isolated_orc, tmp_path):
        # Point HISTORY_FILE to a nested dir that doesn't exist
        orc.HISTORY_FILE = tmp_path / "deep" / "nested" / "history.jsonl"
        _seed_agent("a1", status="completed")
        orc.prune_history()
        assert orc.HISTORY_FILE.exists()

    def test_head_task_id_updated_to_latest_completed_task(self, isolated_orc):
        # Seed a project and two completed tasks with different completed times
        db.project_upsert({"name": "testproj", "head_task_id": None})
        db.task_upsert({
            "id": "old-task", "project": "testproj", "type": "feature",
            "description": "old", "priority": 50, "status": "completed",
            "completed": "2026-01-01T10:00:00",
            "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 3,
        })
        db.task_upsert({
            "id": "new-task", "project": "testproj", "type": "feature",
            "description": "new", "priority": 50, "status": "completed",
            "completed": "2026-01-02T10:00:00",
            "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 3,
        })
        _seed_agent("a1", status="completed")
        orc.prune_history()
        proj = db.project_get("testproj")
        assert proj["head_task_id"] == "new-task"

    def test_head_task_id_not_updated_if_already_correct(self, isolated_orc):
        db.project_upsert({"name": "testproj", "head_task_id": "existing-head"})
        db.task_upsert({
            "id": "existing-head", "project": "testproj", "type": "feature",
            "description": "desc", "priority": 50, "status": "completed",
            "completed": "2026-01-01T10:00:00",
            "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 3,
        })
        _seed_agent("a1", status="completed")
        orc.prune_history()
        proj = db.project_get("testproj")
        assert proj["head_task_id"] == "existing-head"

    def test_head_task_id_updated_per_project(self, isolated_orc):
        db.project_upsert({"name": "proj-a", "head_task_id": None})
        db.project_upsert({"name": "proj-b", "head_task_id": None})
        db.task_upsert({
            "id": "task-a", "project": "proj-a", "type": "feature",
            "description": "a", "priority": 50, "status": "completed",
            "completed": "2026-01-01T10:00:00",
            "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 3,
        })
        db.task_upsert({
            "id": "task-b", "project": "proj-b", "type": "feature",
            "description": "b", "priority": 50, "status": "completed",
            "completed": "2026-01-02T10:00:00",
            "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 3,
        })
        _seed_agent("a1", status="completed")
        orc.prune_history()
        assert db.project_get("proj-a")["head_task_id"] == "task-a"
        assert db.project_get("proj-b")["head_task_id"] == "task-b"

    def test_head_task_id_not_updated_if_no_completed_tasks(self, isolated_orc):
        db.project_upsert({"name": "testproj", "head_task_id": "keep-this"})
        _seed_task("pending-task", status="pending", project="testproj")
        _seed_agent("a1", status="completed")
        orc.prune_history()
        proj = db.project_get("testproj")
        assert proj["head_task_id"] == "keep-this"


# ---------------------------------------------------------------------------
# check_quota_limit() tests
# ---------------------------------------------------------------------------

class TestCheckQuotaLimit:
    def test_returns_safe_defaults_when_no_api_key(self, isolated_orc):
        orc.MINIMAX_API_KEY = ""
        over, pct_used, pct_remaining, used, total = orc.check_quota_limit()
        assert over is False
        assert pct_used == 0.0
        assert pct_remaining == 100.0

    def test_never_raises(self, isolated_orc):
        orc.MINIMAX_API_KEY = "fake-key"
        with patch("requests.get", side_effect=Exception("network error")):
            result = orc.check_quota_limit()
        assert result is not None
        assert len(result) == 5

    def test_over_limit_when_above_threshold(self, isolated_orc):
        orc.MINIMAX_API_KEY = "fake-key"
        orc.QUOTA_LIMIT_PERCENT = 90.0
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model_remains": [{
                "current_interval_usage_count": 50,   # 950 used of 1000 = 95%
                "current_interval_total_count": 1000,
            }]
        }
        with patch("requests.get", return_value=mock_resp):
            over, pct_used, pct_remaining, used, total = orc.check_quota_limit()

        assert over is True
        assert abs(pct_used - 95.0) < 0.1
        assert used == 950
        assert total == 1000

    def test_not_over_limit_when_below_threshold(self, isolated_orc):
        orc.MINIMAX_API_KEY = "fake-key"
        orc.QUOTA_LIMIT_PERCENT = 90.0
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model_remains": [{
                "current_interval_usage_count": 500,  # 500 used of 1000 = 50%
                "current_interval_total_count": 1000,
            }]
        }
        with patch("requests.get", return_value=mock_resp):
            over, pct_used, _, used, total = orc.check_quota_limit()

        assert over is False
        assert abs(pct_used - 50.0) < 0.1

    def test_empty_model_remains_returns_defaults(self, isolated_orc):
        orc.MINIMAX_API_KEY = "fake-key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"model_remains": []}
        with patch("requests.get", return_value=mock_resp):
            over, pct_used, pct_remaining, used, total = orc.check_quota_limit()

        assert over is False
        assert pct_used == 0.0

    def test_non_200_response_returns_safe_defaults(self, isolated_orc):
        orc.MINIMAX_API_KEY = "fake-key"
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("requests.get", return_value=mock_resp):
            over, pct_used, pct_remaining, used, total = orc.check_quota_limit()

        assert over is False
