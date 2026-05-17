import threading

import pytest

from swarm import db
from swarm.closure.regressions import (
    normalize_regression_fingerprints,
    refresh_project_recurrence_state,
    summarize_project_recurrence,
    upsert_regressions_for_run,
)


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


def _run(run_id="run-1", **kw):
    db.verification_run_upsert({
        "id": run_id,
        "project": "proj",
        "trigger_task_id": "task-1",
        "run_type": "post_task",
        "status": "failed",
        "created_at": "2026-01-01T00:00:00",
        "completed_at": "2026-01-01T00:01:00",
        "results_json": {"boot_ok": False, "tests_ok": None, "smoke_ok": None, "critical_flows": {}, "errors": []},
        "artifacts_json": {"checks": []},
        "fingerprints_json": [],
        "metadata_json": {},
        **kw,
    })


def test_normalize_regression_fingerprints_uses_provided_values_deterministically():
    fingerprints = normalize_regression_fingerprints({
        "fingerprints_json": ["Boot:HTTP:Failed", "Boot:HTTP:Failed", "error:Socket Timeout"],
    })

    assert fingerprints == ["boot:http:failed", "error:socket-timeout"]


def test_normalize_regression_fingerprints_can_derive_from_results():
    fingerprints = normalize_regression_fingerprints({
        "results_json": {
            "boot_ok": False,
            "tests_ok": False,
            "smoke_ok": False,
            "critical_flows": {"Main Flow": False},
            "errors": ["Connection refused on port 5001"],
        }
    })

    assert "boot:failed" in fingerprints
    assert "tests:failed" in fingerprints
    assert "smoke:failed" in fingerprints
    assert "flow:main-flow:failed" in fingerprints
    assert any(item.startswith("error:connection-refused-on-port-") for item in fingerprints)


def test_upsert_regressions_for_run_creates_open_regression_rows():
    db.project_upsert(_project("proj", profile="python"))
    _run(fingerprints_json=["boot:http:failed", "tests:failed"])

    regressions = upsert_regressions_for_run("run-1")

    assert len(regressions) == 2
    assert {item["fingerprint"] for item in regressions} == {"boot:http:failed", "tests:failed"}
    project = db.project_get("proj")
    assert project["open_regression_count"] == 2


def test_upsert_regressions_for_run_increments_occurrences_for_repeats():
    db.project_upsert(_project("proj", profile="python"))
    _run(run_id="run-1", fingerprints_json=["boot:http:failed"])
    first = upsert_regressions_for_run("run-1")

    _run(run_id="run-2", completed_at="2026-01-01T00:02:00", fingerprints_json=["boot:http:failed"])
    second = upsert_regressions_for_run("run-2")

    assert len(first) == 1
    assert len(second) == 1
    assert first[0]["id"] == second[0]["id"]
    assert second[0]["occurrences"] == 2
    assert second[0]["source_run_id"] == "run-2"


def test_upsert_regressions_for_run_ignores_non_failed_runs():
    db.project_upsert(_project("proj", profile="python"))
    _run(run_id="run-1", status="passed", fingerprints_json=["boot:http:failed"])

    regressions = upsert_regressions_for_run("run-1")

    assert regressions == []
    assert db.regression_list_by_project("proj") == []


def test_summarize_project_recurrence_distinguishes_one_off_recurring_and_active_repairs():
    db.project_upsert(_project("proj", profile="python"))
    _run(run_id="run-1", fingerprints_json=["boot:http:failed"])
    [boot] = upsert_regressions_for_run("run-1")

    _run(run_id="run-2", completed_at="2026-01-01T00:02:00", fingerprints_json=["boot:http:failed", "tests:failed"])
    upsert_regressions_for_run("run-2")
    tests_regression = db.regression_find_open("proj", "tests:failed")
    assert tests_regression is not None

    db.task_upsert({
        "id": "closure-repair-1",
        "project": "proj",
        "type": "bug",
        "description": "repair",
        "status": "in_progress",
        "priority": 80,
        "dependencies": [],
        "metadata": {},
    })
    db.regression_update(boot["id"], {"linked_task_id": "closure-repair-1"})

    summary = summarize_project_recurrence("proj", stall_threshold=3)

    assert summary["open_regression_count"] == 2
    assert summary["max_occurrences"] == 2
    assert summary["stall_count"] == 2
    assert summary["top_fingerprints"] == ["boot:http:failed", "tests:failed"]
    assert [item["fingerprint"] for item in summary["one_off_regressions"]] == ["tests:failed"]
    assert [item["fingerprint"] for item in summary["recurring_regressions"]] == ["boot:http:failed"]
    assert [item["fingerprint"] for item in summary["repair_loop_regressions"]] == ["boot:http:failed"]
    assert summary["repair_loop_regressions"][0]["linked_task_status"] == "in_progress"
    assert summary["should_mark_stalled"] is False


def test_refresh_project_recurrence_state_persists_stall_inputs():
    db.project_upsert(_project("proj", profile="python"))
    _run(run_id="run-1", fingerprints_json=["boot:http:failed"])
    upsert_regressions_for_run("run-1")
    _run(run_id="run-2", completed_at="2026-01-01T00:02:00", fingerprints_json=["boot:http:failed"])
    upsert_regressions_for_run("run-2")
    _run(run_id="run-3", completed_at="2026-01-01T00:03:00", fingerprints_json=["boot:http:failed"])
    upsert_regressions_for_run("run-3")

    summary = refresh_project_recurrence_state("proj", stall_threshold=3)
    project = db.project_get("proj")

    assert summary["should_mark_stalled"] is True
    assert [item["fingerprint"] for item in summary["stalled_candidates"]] == ["boot:http:failed"]
    assert project["open_regression_count"] == 1
    assert project["stall_count"] == 3
