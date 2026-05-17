import threading

import pytest

from swarm import db
from swarm.closure.runs import (
    complete_verification_run,
    create_verification_run,
    describe_verification_guard,
    list_verification_runs,
    load_verification_run,
    mark_verification_run_started,
    normalize_verification_results,
    normalize_verification_run,
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


def _project(name="p1", **kw):
    return {"name": name, "status": "active", "locked": False, "files": {}, "recent_commits": [], "file_locks": {}, **kw}


def test_normalize_verification_results_preserves_core_shape_and_bounds_noise():
    normalized = normalize_verification_results({
        "boot_ok": True,
        "tests_ok": False,
        "smoke_ok": True,
        "critical_flows": {"main-flow": True, "bad": "nope"},
        "errors": ["first", "", "second"],
        "nested": {"message": "x" * 3000},
    })

    assert normalized["boot_ok"] is True
    assert normalized["tests_ok"] is False
    assert normalized["smoke_ok"] is True
    assert normalized["critical_flows"] == {"main-flow": True}
    assert normalized["errors"] == ["first", "second"]
    assert len(normalized["nested"]["message"]) == 2000


def test_normalize_verification_run_requires_project_and_sets_defaults():
    with pytest.raises(ValueError):
        normalize_verification_run({"run_type": "post_task"})

    normalized = normalize_verification_run({"project": "proj"})
    assert normalized["project"] == "proj"
    assert normalized["run_type"] == "post_task"
    assert normalized["status"] == "pending"
    assert normalized["results_json"]["critical_flows"] == {}
    assert normalized["fingerprints_json"] == []


def test_create_and_load_verification_run_round_trip():
    db.project_upsert(_project("proj", profile="python"))
    run = create_verification_run(
        "proj",
        trigger_task_id="task-1",
        status="running",
        results={"boot_ok": True},
        artifacts={"log": "path/to/log"},
        fingerprints=["boot:ok", "boot:ok"],
        metadata={"source": "manual"},
    )

    assert run["status"] == "running"
    assert run["started_at"] is not None
    assert run["results_json"]["boot_ok"] is True
    assert run["artifacts_json"] == {"log": "path/to/log"}
    assert run["fingerprints_json"] == ["boot:ok"]
    assert run["metadata_json"] == {"source": "manual"}

    loaded = load_verification_run(run["id"])
    assert loaded is not None
    assert loaded["id"] == run["id"]


def test_mark_started_and_complete_verification_run_updates_project_summary():
    db.project_upsert(_project("proj", profile="python"))
    run = create_verification_run("proj", status="pending")

    started = mark_verification_run_started(run["id"])
    assert started["status"] == "running"
    assert started["started_at"] is not None

    finished = complete_verification_run(
        run["id"],
        status="failed",
        results={"boot_ok": False, "errors": ["connection refused"]},
        artifacts={"trace": {"path": "/tmp/trace.zip"}},
        fingerprints=["boot:refused"],
    )
    assert finished["status"] == "failed"
    assert finished["completed_at"] is not None
    assert finished["results_json"]["boot_ok"] is False
    assert finished["fingerprints_json"] == ["boot:refused"]

    project = db.project_get("proj")
    assert project["last_verification_status"] == "failed"
    assert project["last_verification_at"] is not None


def test_list_verification_runs_orders_newest_first():
    db.project_upsert(_project("proj", profile="python"))
    first = create_verification_run("proj", run_id="run-1", status="pending")
    second = create_verification_run("proj", run_id="run-2", status="pending")

    listed = list_verification_runs("proj")
    assert [row["id"] for row in listed] == ["run-2", "run-1"]
    assert first["id"] == "run-1"
    assert second["id"] == "run-2"


def test_complete_verification_run_rejects_non_terminal_status():
    db.project_upsert(_project("proj", profile="python"))
    run = create_verification_run("proj", status="running")

    with pytest.raises(ValueError):
        complete_verification_run(run["id"], status="running")


def test_verification_run_contract_keeps_expected_result_keys_for_partial_inputs():
    db.project_upsert(_project("proj", profile="python"))
    run = create_verification_run(
        "proj",
        results={"boot_ok": True},
        artifacts={"report": {"path": "/tmp/report.json"}},
    )

    assert run["results_json"] == {
        "boot_ok": True,
        "tests_ok": None,
        "smoke_ok": None,
        "critical_flows": {},
        "errors": [],
    }
    assert run["artifacts_json"] == {"report": {"path": "/tmp/report.json"}}
    assert run["fingerprints_json"] == []
    assert run["metadata_json"] == {}


def test_verification_run_contract_bounds_artifact_references_and_metadata():
    db.project_upsert(_project("proj", profile="python"))
    run = create_verification_run(
        "proj",
        artifacts={"trace": {"path": "/tmp/" + ("x" * 3000)}},
        metadata={"note": "y" * 3000},
    )

    assert len(run["artifacts_json"]["trace"]["path"]) == 2000
    assert len(run["metadata_json"]["note"]) == 2000


def test_describe_verification_guard_reuses_active_project_run():
    db.project_upsert(_project("proj", profile="python"))
    active = create_verification_run("proj", trigger_task_id="task-1", status="running")

    guard = describe_verification_guard("proj", trigger_task_id="task-2")

    assert guard["duplicate_run_id"] == active["id"]
    assert guard["active_run_count"] == 1
    assert guard["throttled"] is False


def test_describe_verification_guard_time_throttles_exact_duplicate_trigger_only():
    db.project_upsert(_project("proj", profile="python"))
    latest = create_verification_run("proj", trigger_task_id="task-1", status="passed")

    duplicate_guard = describe_verification_guard("proj", trigger_task_id="task-1")
    unrelated_guard = describe_verification_guard("proj", trigger_task_id="task-2")

    assert duplicate_guard["latest_matching_run_id"] == latest["id"]
    assert duplicate_guard["throttled"] is True
    assert duplicate_guard["throttle_reason"] == "recent_duplicate"
    assert unrelated_guard["latest_matching_run_id"] is None
    assert unrelated_guard["throttled"] is False


def test_describe_verification_guard_enforces_recent_run_budget():
    db.project_upsert(_project("proj", profile="python"))
    for index in range(6):
        create_verification_run("proj", trigger_task_id=f"task-{index}", run_id=f"run-{index}", status="passed")

    guard = describe_verification_guard("proj", trigger_task_id="task-new", throttle_seconds=0, max_runs_per_window=6)

    assert guard["recent_run_count"] == 6
    assert guard["throttled"] is True
    assert guard["throttle_reason"] == "run_budget_exhausted"
