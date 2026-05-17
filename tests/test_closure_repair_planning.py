import threading

import pytest

from swarm import db
from swarm.closure.regressions import upsert_regressions_for_run
from swarm.closure.repair_planning import ensure_repair_task_for_regression, plan_repair_tasks_for_run


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


def _seed_failed_run(run_id="run-1", trigger_task_id="task-1", fingerprint="boot:http:failed"):
    db.project_upsert(_project("proj", profile="python", head_task_id="proj-head"))
    db.task_upsert({
        "id": "proj-head",
        "project": "proj",
        "type": "feature",
        "description": "head",
        "status": "completed",
        "priority": 50,
        "dependencies": [],
        "metadata": {},
    })
    db.task_upsert({
        "id": trigger_task_id,
        "project": "proj",
        "type": "feature",
        "description": "original feature",
        "status": "completed",
        "priority": 50,
        "dependencies": ["proj-head"],
        "metadata": {},
    })
    db.verification_run_upsert({
        "id": run_id,
        "project": "proj",
        "trigger_task_id": trigger_task_id,
        "run_type": "post_task",
        "status": "failed",
        "created_at": "2026-01-01T00:00:00",
        "completed_at": "2026-01-01T00:01:00",
        "results_json": {
            "boot_ok": False,
            "tests_ok": True,
            "smoke_ok": None,
            "critical_flows": {},
            "errors": ["unexpected status 503"],
        },
        "artifacts_json": {
            "checks": [
                {"category": "boot", "outcome": {"status": "failed", "check_type": "http", "error": "unexpected status 503"}}
            ]
        },
        "fingerprints_json": [fingerprint],
        "metadata_json": {},
    })
    upsert_regressions_for_run(run_id)


def test_ensure_repair_task_for_regression_creates_bounded_bug_task():
    _seed_failed_run()
    regression = db.regression_list_by_project("proj", status="open")[0]

    task = ensure_repair_task_for_regression(regression["id"])

    assert task is not None
    assert task["type"] == "bug"
    assert task["status"] == "pending"
    assert task["metadata"]["is_closure_repair_task"] is True
    assert task["metadata"]["source_verification_run_id"] == "run-1"
    assert task["metadata"]["source_regression_id"] == regression["id"]
    assert task["metadata"]["closure_fingerprint"] == "boot:http:failed"
    assert "CLOSURE REPAIR TASK" in task["description"]
    assert "Fingerprint: boot:http:failed" in task["description"]
    assert "unexpected status 503" in task["description"]
    updated_regression = db.regression_get(regression["id"])
    assert updated_regression["linked_task_id"] == task["id"]


def test_ensure_repair_task_for_regression_reuses_open_linked_task():
    _seed_failed_run()
    regression = db.regression_list_by_project("proj", status="open")[0]
    first = ensure_repair_task_for_regression(regression["id"])
    second = ensure_repair_task_for_regression(regression["id"])

    assert first["id"] == second["id"]
    tasks = [task for task in db.task_get_by_project("proj") if task["metadata"].get("is_closure_repair_task")]
    assert len(tasks) == 1


def test_plan_repair_tasks_for_run_only_uses_regressions_for_that_run():
    _seed_failed_run(run_id="run-1", fingerprint="boot:http:failed")
    _seed_failed_run(run_id="run-2", trigger_task_id="task-2", fingerprint="tests:failed")

    tasks = plan_repair_tasks_for_run("run-2")

    assert len(tasks) == 1
    assert tasks[0]["metadata"]["closure_fingerprint"] == "tests:failed"


def test_closed_linked_task_allows_new_repair_task_generation():
    _seed_failed_run()
    regression = db.regression_list_by_project("proj", status="open")[0]
    first = ensure_repair_task_for_regression(regression["id"])
    db.task_update_status(first["id"], "completed", completed="2026-01-01T00:05:00")

    second = ensure_repair_task_for_regression(regression["id"])

    assert second is not None
    assert second["id"] != first["id"]
    assert second["metadata"]["closure_occurrences"] >= 1


def test_repair_description_includes_round_number():
    _seed_failed_run()
    regression = db.regression_list_by_project("proj", status="open")[0]
    task = ensure_repair_task_for_regression(regression["id"])
    assert "round 1" in task["description"].lower()


def test_repair_description_includes_check_stderr():
    db.project_upsert(_project("proj2", profile="python", head_task_id="proj2-head"))
    db.task_upsert({"id": "proj2-head", "project": "proj2", "type": "feature",
                    "description": "head", "status": "completed", "priority": 50,
                    "dependencies": [], "metadata": {}})
    db.task_upsert({"id": "task-x", "project": "proj2", "type": "feature",
                    "description": "feat", "status": "completed", "priority": 50,
                    "dependencies": ["proj2-head"], "metadata": {}})
    db.verification_run_upsert({
        "id": "run-x", "project": "proj2", "trigger_task_id": "task-x",
        "run_type": "post_task", "status": "failed",
        "created_at": "2026-01-01T00:00:00", "completed_at": "2026-01-01T00:01:00",
        "results_json": {"boot_ok": None, "tests_ok": None, "smoke_ok": False,
                         "critical_flows": {}, "errors": ["command timed out after 60s"]},
        "artifacts_json": {"checks": [{"category": "smoke", "id": "vertical-slice",
                                        "outcome": {"status": "timeout", "check_type": "command",
                                                    "command": "godot --headless -s test.gd",
                                                    "error": "command timed out after 60s",
                                                    "stderr": "SCRIPT ERROR: Identifier not found: SignalBus"}}]},
        "fingerprints_json": ["smoke:vertical-slice:timeout"],
        "metadata_json": {},
    })
    upsert_regressions_for_run("run-x")
    regression = db.regression_list_by_project("proj2", status="open")[0]
    task = ensure_repair_task_for_regression(regression["id"])
    assert "SignalBus" in task["description"]
    assert "command timed out after 60s" in task["description"]


def _seed_n_repair_rounds(n: int, budget: int = 3):
    """Seed a project with a budget and N completed repair task rounds."""
    spec = {
        "autonomy": {"repair_budget": budget, "stall_threshold": 3, "feature_freeze_on_red": True},
        "verification": {"smoke_checks": []},
        "critical_flows": [],
        "gates": {"boot_ok": False, "tests_ok": False, "critical_flow_count": 0, "max_open_regressions": 0},
    }
    db.project_upsert(_project("budgeted", profile="python", head_task_id="budgeted-head",
                               closure_spec=spec))
    db.task_upsert({"id": "budgeted-head", "project": "budgeted", "type": "feature",
                    "description": "head", "status": "completed", "priority": 50,
                    "dependencies": [], "metadata": {}})
    db.task_upsert({"id": "trigger-b", "project": "budgeted", "type": "feature",
                    "description": "feat", "status": "completed", "priority": 50,
                    "dependencies": ["budgeted-head"], "metadata": {}})
    db.verification_run_upsert({
        "id": "run-b", "project": "budgeted", "trigger_task_id": "trigger-b",
        "run_type": "post_task", "status": "failed",
        "created_at": "2026-01-01T00:00:00", "completed_at": "2026-01-01T00:01:00",
        "results_json": {"boot_ok": False, "tests_ok": None, "smoke_ok": None,
                         "critical_flows": {}, "errors": ["boom"]},
        "artifacts_json": {},
        "fingerprints_json": ["boot:http:failed"],
        "metadata_json": {},
    })
    upsert_regressions_for_run("run-b")
    regression = db.regression_list_by_project("budgeted", status="open")[0]
    reg_id = regression["id"]

    # Simulate N completed repair rounds
    from swarm.closure.repair_planning import _repair_task_id
    for i in range(1, n + 1):
        task_id = _repair_task_id("budgeted", "boot:http:failed", i)
        db.task_upsert({
            "id": task_id, "project": "budgeted", "type": "bug",
            "description": f"repair round {i}", "status": "completed", "priority": 75,
            "dependencies": [], "metadata": {
                "is_closure_repair_task": True,
                "source_regression_id": reg_id,
                "closure_repair_round": i,
            },
        })
    return reg_id


def test_repair_budget_exhausted_spawns_triage_task():
    reg_id = _seed_n_repair_rounds(n=3, budget=3)
    regression = db.regression_get(reg_id)

    task = ensure_repair_task_for_regression(reg_id)

    assert task is not None
    assert task["metadata"].get("is_closure_triage_task") is True
    assert task["metadata"].get("repair_budget_exhausted") is True
    assert task["metadata"].get("prior_repair_rounds") == 3
    assert "AUTONOMOUS CLOSURE TRIAGE" in task["description"]
    assert task["priority"] == 90
    # Regression should be marked stalled
    updated = db.regression_get(reg_id)
    assert updated["status"] == "stalled"


def test_repair_within_budget_spawns_normal_repair_task():
    reg_id = _seed_n_repair_rounds(n=2, budget=5)

    task = ensure_repair_task_for_regression(reg_id)

    assert task is not None
    assert task["metadata"].get("is_closure_triage_task") is not True
    assert "CLOSURE REPAIR TASK" in task["description"]
    assert task["metadata"]["closure_repair_round"] == 3


def test_triage_task_description_contains_diagnostic_steps():
    reg_id = _seed_n_repair_rounds(n=5, budget=5)

    task = ensure_repair_task_for_regression(reg_id)

    assert "Step 1" in task["description"]
    assert "Step 2" in task["description"]
    assert "Step 3" in task["description"]
    assert "Step 4" in task["description"]
    assert "closure/spec" in task["description"]
    assert "closure/verify" in task["description"]


def test_triage_task_description_includes_prior_round_count():
    reg_id = _seed_n_repair_rounds(n=4, budget=4)

    task = ensure_repair_task_for_regression(reg_id)

    assert "4 repair agents" in task["description"] or "4 rounds" in task["description"] or "Prior repair attempts (4" in task["description"]
