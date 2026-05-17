import threading

from swarm import db
from swarm.projects import Project, SQLiteProjectRegistry


def test_project_dataclass_round_trip_preserves_closure_fields():
    project = Project.from_dict({
        "name": "proj",
        "status": "active",
        "managed": True,
        "locked": False,
        "files": {},
        "recent_commits": [],
        "file_locks": {},
        "profile": "python",
        "closure_mode": "stabilize",
        "closure_status": "red",
        "closure_spec": {"boot": {"command": "pytest"}},
        "last_verification_at": "2026-01-01T00:00:00",
        "last_verification_status": "failed",
        "open_regression_count": 2,
        "stall_count": 1,
    })

    data = project.to_dict()
    assert data["closure_mode"] == "stabilize"
    assert data["closure_status"] == "red"
    assert data["closure_spec"] == {"boot": {"command": "pytest"}}
    assert data["last_verification_status"] == "failed"
    assert data["open_regression_count"] == 2
    assert data["stall_count"] == 1


def test_sqlite_project_registry_exposes_closure_fields(tmp_path):
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "registry.db")

    db.project_upsert({"name": "proj", "status": "active", "locked": False, "files": {}, "recent_commits": [], "file_locks": {}, "profile": "python"})
    db.project_update("proj", {
        "closure_mode": "ship",
        "closure_status": "green",
        "closure_spec": {"gates": {"max_open_regressions": 0}},
        "last_verification_status": "passed",
        "open_regression_count": 0,
        "stall_count": 0,
    })

    registry = SQLiteProjectRegistry(workspace=tmp_path)
    project = registry.get("proj")
    assert project is not None
    assert project.closure_mode == "ship"
    assert project.closure_status == "green"
    assert project.closure_spec == {"gates": {"max_open_regressions": 0}}
    assert project.last_verification_status == "passed"

    all_projects = registry.get_all()
    assert all_projects["proj"].closure_mode == "ship"
