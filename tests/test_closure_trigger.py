import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from swarm import db, orchestrator, validation


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm_test.db")
    orchestrator.WORKSPACE = tmp_path / "workspace"
    orchestrator.WORKSPACE.mkdir()
    yield
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


def _project(name="proj", **kw):
    return {"name": name, "status": "active", "locked": False, "files": {}, "recent_commits": [], "file_locks": {}, **kw}


def _seed_project_tasks(project="proj", trigger_task_id="task-1"):
    db.task_upsert({
        "id": "proj-head",
        "project": project,
        "type": "feature",
        "description": "head",
        "status": "completed",
        "priority": 50,
        "dependencies": [],
        "metadata": {},
    })
    db.task_upsert({
        "id": trigger_task_id,
        "project": project,
        "type": "feature",
        "description": "feature",
        "status": "completed",
        "priority": 50,
        "dependencies": ["proj-head"],
        "metadata": {},
    })


def test_run_closure_verification_persists_run_and_updates_project_status(tmp_path):
    project_path = orchestrator.WORKSPACE / "proj"
    project_path.mkdir(parents=True)
    db.project_upsert(_project("proj", profile="python", head_task_id="proj-head"))
    _seed_project_tasks()
    db.project_update("proj", {
        "closure_spec": {
            "boot": {"ready_check": {"type": "command", "command": "echo boot"}},
            "verification": {
                "unit_test_command": "echo tests",
                "smoke_checks": [{"id": "main-flow", "type": "command", "command": "echo smoke"}],
            },
        }
    })

    with patch("swarm.validation.execute_ready_check", return_value={"ok": True, "status": "passed", "check_type": "command", "error": "", "stdout": "boot", "stderr": "", "timed_out": False, "exit_code": 0, "response_status": None, "body_preview": ""}), \
         patch("swarm.validation.execute_check", return_value={"ok": True, "status": "passed", "check_type": "command", "error": "", "stdout": "ok", "stderr": "", "timed_out": False, "exit_code": 0, "response_status": None, "body_preview": ""}):
        run = validation.run_closure_verification("proj", "task-1", project_path=project_path)

    assert run is not None
    assert run["status"] == "passed"
    assert run["results_json"]["boot_ok"] is True
    assert run["results_json"]["tests_ok"] is True
    assert run["results_json"]["critical_flows"]["main-flow"] is True
    project = db.project_get("proj")
    assert project["last_verification_status"] == "passed"
    assert project["closure_status"] == "green"


def test_run_closure_verification_fails_green_when_smoke_check_fails(tmp_path):
    project_path = orchestrator.WORKSPACE / "proj"
    project_path.mkdir(parents=True)
    db.project_upsert(_project("proj", profile="python", head_task_id="proj-head"))
    _seed_project_tasks()
    db.project_update("proj", {
        "closure_spec": {
            "boot": {"ready_check": {"type": "command", "command": "echo boot"}},
            "verification": {
                "unit_test_command": "echo tests",
                "smoke_checks": [{"id": "main-flow", "type": "command", "command": "echo smoke"}],
            },
        }
    })

    def fake_execute(check, **kwargs):
        if check.get("category") == "tests" or check.get("id") == "unit-tests":
            return {"ok": True, "status": "passed", "check_type": "command", "error": "", "stdout": "tests", "stderr": "", "timed_out": False, "exit_code": 0, "response_status": None, "body_preview": ""}
        return {"ok": False, "status": "failed", "check_type": "command", "error": "smoke failed", "stdout": "", "stderr": "", "timed_out": False, "exit_code": 1, "response_status": None, "body_preview": ""}

    with patch("swarm.validation.execute_ready_check", return_value={"ok": True, "status": "passed", "check_type": "command", "error": "", "stdout": "boot", "stderr": "", "timed_out": False, "exit_code": 0, "response_status": None, "body_preview": ""}), \
         patch("swarm.validation.execute_check", side_effect=fake_execute):
        run = validation.run_closure_verification("proj", "task-1", project_path=project_path)

    assert run is not None
    assert run["status"] == "failed"
    assert run["results_json"]["smoke_ok"] is False
    project = db.project_get("proj")
    assert project["closure_status"] in {"red", "frozen"}


def test_project_closure_checks_adds_godot_main_scene_runtime_smoke():
    checks = validation._project_closure_checks({
        "profile": "godot",
        "boot": {"ready_check": {"type": "command", "command": "godot --headless --path . --quit"}},
        "verification": {"unit_test_command": "gut --run --exit", "smoke_checks": [{"id": "gut", "type": "command", "command": "gut --run --exit"}]},
        "critical_flows": [{"id": "main-flow", "description": "main"}],
    })
    smoke_types = [check.get("type") for check in checks if check.get("category") == "smoke"]
    assert "godot_scene" in smoke_types


def test_run_closure_verification_records_failures_and_fingerprints(tmp_path):
    project_path = orchestrator.WORKSPACE / "proj"
    project_path.mkdir(parents=True)
    db.project_upsert(_project("proj", profile="python", head_task_id="proj-head"))
    _seed_project_tasks()
    db.project_update("proj", {
        "closure_spec": {
            "boot": {"ready_check": {"type": "http", "url": "http://127.0.0.1:5001/api/health"}},
        }
    })

    with patch("swarm.validation.execute_ready_check", return_value={"ok": False, "status": "failed", "check_type": "http", "error": "unexpected status 503", "stdout": "", "stderr": "", "timed_out": False, "exit_code": None, "response_status": 503, "body_preview": "down"}):
        run = validation.run_closure_verification("proj", "task-1", project_path=project_path)

    assert run is not None
    assert run["status"] == "failed"
    assert run["fingerprints_json"] == ["boot:http:failed"]
    assert "unexpected status 503" in run["results_json"]["errors"]
    project = db.project_get("proj")
    assert project["closure_status"] in {"red", "frozen"}
    assert project["open_regression_count"] == 1
    assert project["stall_count"] == 1
    regressions = db.regression_list_by_project("proj", status="open")
    assert len(regressions) == 1
    assert regressions[0]["fingerprint"] == "boot:http:failed"
    repair_tasks = [task for task in db.task_get_by_project("proj") if task["metadata"].get("is_closure_repair_task")]
    assert len(repair_tasks) == 1
    assert repair_tasks[0]["metadata"]["source_verification_run_id"] == run["id"]
    assert repair_tasks[0]["metadata"]["closure_fingerprint"] == "boot:http:failed"


def test_run_closure_verification_with_default_spec_still_produces_structured_run(tmp_path):
    project_path = orchestrator.WORKSPACE / "proj"
    project_path.mkdir(parents=True)
    db.project_upsert(_project("proj", profile="python"))

    run = validation.run_closure_verification("proj", "task-1", project_path=project_path)

    assert run is not None
    assert run["results_json"] == {
        "boot_ok": None,
        "tests_ok": None,
        "smoke_ok": None,
        "critical_flows": {},
        "errors": [],
    }
    assert run["artifacts_json"]["checks"] == []
    assert run["fingerprints_json"] == []
    assert run["status"] == "passed"
    project = db.project_get("proj")
    assert project["closure_status"] == "yellow"


def test_failed_verification_reuses_open_regression_and_repair_task(tmp_path):
    project_path = orchestrator.WORKSPACE / "proj"
    project_path.mkdir(parents=True)
    db.project_upsert(_project("proj", profile="python", head_task_id="proj-head"))
    _seed_project_tasks()
    db.project_update("proj", {
        "closure_spec": {
            "boot": {"ready_check": {"type": "http", "url": "http://127.0.0.1:5001/api/health"}},
        }
    })

    failure = {
        "ok": False,
        "status": "failed",
        "check_type": "http",
        "error": "unexpected status 503",
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "exit_code": None,
        "response_status": 503,
        "body_preview": "down",
    }
    guard_open = {
        "project": "proj",
        "run_type": "post_task",
        "trigger_task_id": "task-1",
        "active_run_count": 0,
        "recent_run_count": 0,
        "duplicate_run_id": None,
        "latest_run_id": None,
        "latest_matching_run_id": None,
        "throttled": False,
        "throttle_reason": None,
        "window_seconds": 300,
        "throttle_seconds": 15,
        "max_runs_per_window": 6,
    }
    with patch("swarm.validation.describe_verification_guard", return_value=guard_open), \
         patch("swarm.validation.execute_ready_check", return_value=failure):
        first = validation.run_closure_verification("proj", "task-1", project_path=project_path)
    with patch("swarm.validation.describe_verification_guard", return_value=guard_open), \
         patch("swarm.validation.execute_ready_check", return_value=failure):
        second = validation.run_closure_verification("proj", "task-1", project_path=project_path)

    assert first is not None and second is not None
    regressions = db.regression_list_by_project("proj", status="open")
    assert len(regressions) == 1
    assert regressions[0]["occurrences"] == 2
    repair_tasks = [task for task in db.task_get_by_project("proj") if task["metadata"].get("is_closure_repair_task")]
    assert len(repair_tasks) == 1
    assert repair_tasks[0]["metadata"]["source_regression_id"] == regressions[0]["id"]


def test_run_closure_verification_reuses_inflight_project_run_without_executing_checks(tmp_path):
    project_path = orchestrator.WORKSPACE / "proj"
    project_path.mkdir(parents=True)
    db.project_upsert(_project("proj", profile="python", head_task_id="proj-head"))
    _seed_project_tasks()
    db.project_update("proj", {
        "closure_spec": {
            "boot": {"ready_check": {"type": "command", "command": "echo boot"}},
        }
    })
    existing = db.verification_run_upsert({
        "id": "run-active",
        "project": "proj",
        "trigger_task_id": "task-older",
        "run_type": "post_task",
        "status": "running",
        "created_at": "2026-01-01T00:00:00",
        "started_at": "2026-01-01T00:00:01",
        "results_json": {"boot_ok": None, "tests_ok": None, "smoke_ok": None, "critical_flows": {}, "errors": []},
        "artifacts_json": {"checks": []},
        "fingerprints_json": [],
        "metadata_json": {},
    })
    assert existing == "run-active"

    with patch("swarm.validation.execute_ready_check", side_effect=AssertionError("guard should skip duplicate execution")):
        run = validation.run_closure_verification("proj", "task-new", project_path=project_path)

    assert run is not None
    assert run["id"] == "run-active"
    assert len(db.verification_run_list_by_project("proj")) == 1


def test_run_closure_verification_respects_recent_run_budget(tmp_path):
    project_path = orchestrator.WORKSPACE / "proj"
    project_path.mkdir(parents=True)
    db.project_upsert(_project("proj", profile="python", head_task_id="proj-head"))
    _seed_project_tasks()
    db.project_update("proj", {
        "closure_spec": {
            "boot": {"ready_check": {"type": "command", "command": "echo boot"}},
        }
    })
    for index in range(6):
        db.verification_run_upsert({
            "id": f"run-{index}",
            "project": "proj",
            "trigger_task_id": f"task-{index}",
            "run_type": "post_task",
            "status": "passed",
            "created_at": "2026-01-01T00:00:00",
            "completed_at": "2026-01-01T00:00:01",
            "results_json": {"boot_ok": True, "tests_ok": True, "smoke_ok": True, "critical_flows": {}, "errors": []},
            "artifacts_json": {"checks": []},
            "fingerprints_json": [],
            "metadata_json": {},
        })

    with patch("swarm.validation.describe_verification_guard", return_value={
        "project": "proj",
        "run_type": "post_task",
        "trigger_task_id": "task-new",
        "active_run_count": 0,
        "recent_run_count": 6,
        "duplicate_run_id": None,
        "latest_run_id": "run-5",
        "latest_matching_run_id": None,
        "throttled": True,
        "throttle_reason": "run_budget_exhausted",
        "window_seconds": 300,
        "throttle_seconds": 15,
        "max_runs_per_window": 6,
    }), patch("swarm.validation.execute_ready_check", side_effect=AssertionError("budget guard should skip execution")):
        run = validation.run_closure_verification("proj", "task-new", project_path=project_path)

    assert run is not None
    assert run["id"] == "run-5"
    assert len(db.verification_run_list_by_project("proj")) == 6
