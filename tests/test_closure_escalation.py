import threading

import pytest

from swarm import db
from swarm.closure.escalation import build_stalled_project_bundle


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm_test.db")
    yield
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


def _project(name="proj", **kw):
    return {"name": name, "status": "active", "locked": False, "files": {}, "recent_commits": [], "file_locks": {}, **kw}


def test_build_stalled_project_bundle_collects_closure_state_runs_regressions_and_repairs():
    db.project_upsert(_project("proj", profile="python"))
    db.project_update("proj", {
        "closure_mode": "stabilize",
        "closure_status": "stalled",
        "closure_spec": {"autonomy": {"stall_threshold": 3}},
        "open_regression_count": 2,
        "stall_count": 3,
        "last_verification_at": "2026-01-01T00:01:00",
        "last_verification_status": "failed",
    })
    db.verification_run_upsert({
        "id": "run-1",
        "project": "proj",
        "trigger_task_id": "task-1",
        "run_type": "post_task",
        "status": "failed",
        "created_at": "2026-01-01T00:00:00",
        "completed_at": "2026-01-01T00:01:00",
        "results_json": {"boot_ok": False, "tests_ok": True, "smoke_ok": None, "critical_flows": {}, "errors": ["boom"]},
        "artifacts_json": {"checks": []},
        "fingerprints_json": ["boot:failed"],
        "metadata_json": {},
    })
    db.regression_upsert({
        "id": "reg-1",
        "project": "proj",
        "fingerprint": "boot:failed",
        "status": "open",
        "severity": "high",
        "first_seen_at": "2026-01-01T00:00:00",
        "last_seen_at": "2026-01-01T00:01:00",
        "occurrences": 3,
        "source_run_id": "run-1",
        "linked_task_id": "repair-1",
        "details_json": {"results": {"boot_ok": False}},
    })
    db.task_upsert({
        "id": "repair-1",
        "project": "proj",
        "type": "bug",
        "description": "repair",
        "priority": 80,
        "status": "completed",
        "created": "2026-01-01T00:02:00",
        "completed": "2026-01-01T00:03:00",
        "dependencies": [],
        "metadata": {"is_closure_repair_task": True, "source_regression_id": "reg-1"},
    })
    db.task_upsert({
        "id": "triage-1",
        "project": "proj",
        "type": "triage",
        "description": "triage loop",
        "priority": 70,
        "status": "pending",
        "created": "2026-01-01T00:04:00",
        "dependencies": [],
        "metadata": {},
    })

    bundle = build_stalled_project_bundle("proj")

    assert bundle["closure_state"]["status"] == "stalled"
    assert bundle["latest_verification_run"]["id"] == "run-1"
    assert bundle["recurrence_summary"]["should_mark_stalled"] is True
    assert bundle["open_regressions"][0]["fingerprint"] == "boot:failed"
    assert [task["id"] for task in bundle["repair_history"]] == ["triage-1", "repair-1"]
    assert bundle["operator_guidance"]
