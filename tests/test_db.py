"""Tests for swarm/db.py — SQLite storage layer."""
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from swarm import db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Give each test a fresh in-memory-like DB by pointing to a temp file."""
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm_test.db")
    yield
    # Teardown: close connection
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


# ---------------------------------------------------------------------------
# Schema / init
# ---------------------------------------------------------------------------

def test_schema_creates_tables(tmp_path):
    db2 = __import__("swarm.db", fromlist=["db"])
    conn = db._connect()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "tasks" in tables
    assert "projects" in tables
    assert "agents" in tables
    assert "verification_runs" in tables
    assert "regressions" in tables


def test_evolve_schema_is_idempotent():
    """Calling _evolve_schema twice must not raise."""
    conn = db._connect()
    db._evolve_schema(conn)
    db._evolve_schema(conn)


def test_init_safe_to_call_twice(tmp_path):
    path = tmp_path / "twice.db"
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(path)
    db.init(path)  # second call must not raise or duplicate tables


def test_init_migrates_legacy_project_schema(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'feature',
            description TEXT DEFAULT '',
            priority INTEGER DEFAULT 50,
            status TEXT DEFAULT 'pending',
            created TEXT,
            started TEXT,
            completed TEXT,
            agent_id TEXT,
            dependencies TEXT DEFAULT '[]',
            metadata TEXT DEFAULT '{}'
        );
        CREATE TABLE projects (
            name TEXT PRIMARY KEY,
            status TEXT DEFAULT 'active',
            locked INTEGER DEFAULT 0,
            locked_at TEXT,
            unlocked_at TEXT,
            last_update TEXT,
            files TEXT DEFAULT '{}',
            sprint INTEGER DEFAULT 0,
            recent_commits TEXT DEFAULT '[]',
            last_commit TEXT,
            last_commit_msg TEXT,
            file_locks TEXT DEFAULT '{}',
            profile TEXT
        );
        CREATE TABLE agents (
            id TEXT PRIMARY KEY,
            project TEXT,
            task_type TEXT,
            status TEXT DEFAULT 'spawning',
            spawned_at TEXT,
            completed_at TEXT,
            pid INTEGER,
            exit_code INTEGER,
            task_id TEXT,
            log_path TEXT,
            script_path TEXT,
            output TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}'
        );
        CREATE TABLE plans (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            created_at TEXT,
            task_ids TEXT DEFAULT '[]',
            task_graph TEXT DEFAULT '{}'
        );
        CREATE TABLE completed_task_ids (
            id TEXT PRIMARY KEY,
            project TEXT,
            completed_at TEXT
        );
    """)
    conn.execute(
        """
        INSERT INTO projects
        (name, status, locked, files, recent_commits, file_locks, profile)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("legacy-proj", "active", 0, "{}", "[]", "{}", "python"),
    )
    conn.commit()
    conn.close()

    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(path)

    migrated = db.project_get("legacy-proj")
    assert migrated is not None
    assert migrated["closure_mode"] == "build"
    assert migrated["closure_status"] == "yellow"
    assert migrated["closure_spec"] == {}
    assert migrated["open_regression_count"] == 0
    assert migrated["stall_count"] == 0

    conn = db._connect()
    project_cols = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    assert "closure_mode" in project_cols
    assert "closure_status" in project_cols
    assert "closure_spec" in project_cols
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "verification_runs" in tables
    assert "regressions" in tables


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------

def _task(id="t1", project="proj", **kw):
    return {
        "id": id, "project": project, "type": "feature",
        "description": "test", "priority": 50, "status": "pending",
        "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 3,
        **kw,
    }


def test_task_upsert_and_get():
    db.task_upsert(_task("t1"))
    t = db.task_get("t1")
    assert t is not None
    assert t["id"] == "t1"
    assert t["project"] == "proj"
    assert t["dependencies"] == []
    assert t["metadata"] == {}


def test_task_get_missing_returns_none():
    assert db.task_get("nonexistent") is None


def test_task_upsert_updates_existing():
    db.task_upsert(_task("t1", priority=50))
    db.task_upsert(_task("t1", priority=99))
    assert db.task_get("t1")["priority"] == 99


def test_task_get_all_returns_all():
    db.task_upsert(_task("t1"))
    db.task_upsert(_task("t2"))
    ids = {t["id"] for t in db.task_get_all()}
    assert ids == {"t1", "t2"}


def test_task_get_by_status():
    db.task_upsert(_task("t1", status="pending"))
    db.task_upsert(_task("t2", status="completed"))
    pending = db.task_get_by_status("pending")
    assert len(pending) == 1
    assert pending[0]["id"] == "t1"


def test_task_get_by_project():
    db.task_upsert(_task("t1", project="alpha"))
    db.task_upsert(_task("t2", project="beta"))
    results = db.task_get_by_project("alpha")
    assert len(results) == 1
    assert results[0]["id"] == "t1"


def test_task_delete():
    db.task_upsert(_task("t1"))
    assert db.task_delete("t1") is True
    assert db.task_get("t1") is None


def test_task_delete_nonexistent_returns_false():
    assert db.task_delete("ghost") is False


def test_task_update_status():
    db.task_upsert(_task("t1"))
    db.task_update_status("t1", "completed", completed="2026-01-01T00:00:00")
    t = db.task_get("t1")
    assert t["status"] == "completed"
    assert t["completed"] == "2026-01-01T00:00:00"


def test_task_update_status_pending_clears_lifecycle_columns():
    db.task_upsert(_task(
        "t1",
        status="completed",
        started="2026-01-01T00:00:00",
        completed="2026-01-01T00:05:00",
        agent_id="agent-1",
    ))

    db.task_update_status("t1", "pending", attempts=1)

    t = db.task_get("t1")
    assert t["status"] == "pending"
    assert t["started"] is None
    assert t["completed"] is None
    assert t["agent_id"] is None
    assert t["attempts"] == 1
    assert "t1" not in db.task_get_completed_ids()


def test_task_update_pending_clears_lifecycle_columns():
    db.task_upsert(_task(
        "t1",
        status="completed",
        started="2026-01-01T00:00:00",
        completed="2026-01-01T00:05:00",
        agent_id="agent-1",
    ))

    db.task_update("t1", {"status": "pending"})

    t = db.task_get("t1")
    assert t["status"] == "pending"
    assert t["started"] is None
    assert t["completed"] is None
    assert t["agent_id"] is None
    assert "t1" not in db.task_get_completed_ids()


def test_task_update_status_completed_sets_completed_timestamp():
    db.task_upsert(_task("t1"))

    db.task_update_status("t1", "completed")

    t = db.task_get("t1")
    assert t["status"] == "completed"
    assert t["completed"]
    assert "t1" in db.task_get_completed_ids()


def test_repair_malformed_task_rows_clears_pending_lifecycle_columns():
    db.task_upsert(_task(
        "t1",
        status="pending",
        started="2026-01-01T00:00:00",
        completed="2026-01-01T00:05:00",
        agent_id="agent-1",
    ))

    repaired = db.repair_malformed_task_rows()

    assert repaired == ["t1"]
    t = db.task_get("t1")
    assert t["status"] == "pending"
    assert t["started"] is None
    assert t["completed"] is None
    assert t["agent_id"] is None
    assert "t1" not in db.task_get_completed_ids()


def test_task_update_status_records_completed_archive():
    db.task_upsert(_task("t1"))
    db.task_update_status("t1", "completed", completed="2026-01-01T00:00:00")
    assert "t1" in db.task_get_completed_ids()


def test_task_upsert_completed_records_completed_archive():
    db.task_upsert(_task("t1", status="completed", completed="2026-01-01T00:00:00"))
    assert "t1" in db.task_get_completed_ids()


def test_backfill_completed_task_ids_archives_existing_completed_rows():
    conn = db._connect()
    conn.execute(
        """
        INSERT INTO tasks
        (id, project, type, description, priority, status, created, completed, dependencies, metadata, attempts, max_attempts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-completed",
            "proj",
            "feature",
            "legacy",
            50,
            "completed",
            "2026-01-01T00:00:00",
            "2026-01-01T01:00:00",
            "[]",
            "{}",
            0,
            3,
        ),
    )
    conn.commit()

    inserted = db.backfill_completed_task_ids()
    assert inserted == ["legacy-completed"]
    assert "legacy-completed" in db.task_get_completed_ids()


def test_task_dependencies_serialised():
    db.task_upsert(_task("t1", dependencies=["dep-a", "dep-b"]))
    t = db.task_get("t1")
    assert t["dependencies"] == ["dep-a", "dep-b"]


def test_task_metadata_serialised():
    meta = {"last_failure": "some error", "failure_attempt": 1}
    db.task_upsert(_task("t1", metadata=meta))
    t = db.task_get("t1")
    assert t["metadata"] == meta


def test_task_attempts_fields():
    db.task_upsert(_task("t1", attempts=2, max_attempts=5))
    t = db.task_get("t1")
    assert t["attempts"] == 2
    assert t["max_attempts"] == 5


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------

def _project(name="p1", **kw):
    return {"name": name, "status": "active", "locked": False,
            "files": {}, "recent_commits": [], "file_locks": {}, **kw}


def test_project_upsert_and_get():
    db.project_upsert(_project("p1"))
    p = db.project_get("p1")
    assert p is not None
    assert p["name"] == "p1"
    assert p["locked"] is False


def test_project_get_missing_returns_none():
    assert db.project_get("ghost") is None


def test_project_get_all():
    db.project_upsert(_project("p1"))
    db.project_upsert(_project("p2"))
    all_p = db.project_get_all()
    assert "p1" in all_p
    assert "p2" in all_p


def test_project_set_locked():
    db.project_upsert(_project("p1"))
    db.project_set_locked("p1", True)
    assert db.project_get("p1")["locked"] is True
    db.project_set_locked("p1", False)
    assert db.project_get("p1")["locked"] is False


def test_project_files_serialised():
    db.project_upsert(_project("p1", files={"main.py": 100}))
    assert db.project_get("p1")["files"] == {"main.py": 100}


def test_project_closure_fields_default_and_update():
    db.project_upsert(_project("p1"))

    project = db.project_get("p1")
    assert project["closure_mode"] == "build"
    assert project["closure_status"] == "yellow"
    assert project["closure_spec"] == {}
    assert project["open_regression_count"] == 0
    assert project["stall_count"] == 0

    db.project_update("p1", {
        "closure_mode": "stabilize",
        "closure_status": "red",
        "closure_spec": {"boot": {"command": "pytest"}},
        "last_verification_at": "2026-01-01T00:00:00",
        "last_verification_status": "failed",
        "open_regression_count": 2,
        "stall_count": 1,
    })
    updated = db.project_get("p1")
    assert updated["closure_mode"] == "stabilize"
    assert updated["closure_status"] == "red"
    assert updated["closure_spec"] == {"boot": {"command": "pytest"}}
    assert updated["last_verification_at"] == "2026-01-01T00:00:00"
    assert updated["last_verification_status"] == "failed"
    assert updated["open_regression_count"] == 2
    assert updated["stall_count"] == 1


def test_project_upsert_preserves_existing_closure_fields():
    db.project_upsert(_project("p1"))
    db.project_update("p1", {
        "closure_mode": "ship",
        "closure_status": "green",
        "closure_spec": {"gates": {"boot_ok": True}},
        "open_regression_count": 0,
    })

    db.project_upsert({"name": "p1", "status": "paused", "locked": False, "files": {}, "recent_commits": [], "file_locks": {}})
    updated = db.project_get("p1")
    assert updated["status"] == "paused"
    assert updated["closure_mode"] == "ship"
    assert updated["closure_status"] == "green"
    assert updated["closure_spec"] == {"gates": {"boot_ok": True}}


# ---------------------------------------------------------------------------
# Agent CRUD
# ---------------------------------------------------------------------------

def _agent(id="a1", **kw):
    return {"id": id, "project": "proj", "task_type": "feature",
            "status": "active", "metadata": {}, **kw}


def test_agent_upsert_and_get():
    db.agent_upsert(_agent("a1"))
    a = db.agent_get("a1")
    assert a is not None
    assert a["id"] == "a1"


def test_agent_get_missing_returns_none():
    assert db.agent_get("ghost") is None


def test_agent_get_active():
    db.agent_upsert(_agent("a1", status="active"))
    db.agent_upsert(_agent("a2", status="completed"))
    active = db.agent_get_active()
    assert len(active) == 1
    assert active[0]["id"] == "a1"


def test_agent_update_status():
    db.agent_upsert(_agent("a1"))
    db.agent_update_status("a1", "completed", exit_code=0)
    a = db.agent_get("a1")
    assert a["status"] == "completed"
    assert a["exit_code"] == 0


def test_agent_metadata_serialised():
    db.agent_upsert(_agent("a1", metadata={"diff_stat": "main.py | 5 ++"}))
    a = db.agent_get("a1")
    assert a["metadata"]["diff_stat"] == "main.py | 5 ++"


def test_verification_run_crud_round_trip():
    db.project_upsert(_project("p1"))
    db.verification_run_upsert({
        "id": "run-1",
        "project": "p1",
        "trigger_task_id": "task-1",
        "run_type": "post_task",
        "status": "running",
        "created_at": "2026-01-01T00:00:00",
        "results_json": {"boot_ok": False},
        "artifacts_json": {"log": "path/to/log"},
        "fingerprints_json": ["boot:refused"],
        "metadata_json": {"attempt": 1},
    })

    run = db.verification_run_get("run-1")
    assert run is not None
    assert run["results_json"] == {"boot_ok": False}
    assert run["artifacts_json"] == {"log": "path/to/log"}
    assert run["fingerprints_json"] == ["boot:refused"]
    assert run["metadata_json"] == {"attempt": 1}

    db.verification_run_update("run-1", {
        "status": "failed",
        "completed_at": "2026-01-01T00:01:00",
        "results_json": {"boot_ok": False, "tests_ok": True},
    })
    updated = db.verification_run_get("run-1")
    assert updated["status"] == "failed"
    assert updated["completed_at"] == "2026-01-01T00:01:00"
    assert updated["results_json"] == {"boot_ok": False, "tests_ok": True}

    listed = db.verification_run_list_by_project("p1", limit=1)
    assert [row["id"] for row in listed] == ["run-1"]


def test_regression_crud_round_trip():
    db.project_upsert(_project("p1"))
    db.regression_upsert({
        "id": "reg-1",
        "project": "p1",
        "fingerprint": "boot:refused",
        "status": "open",
        "severity": "high",
        "first_seen_at": "2026-01-01T00:00:00",
        "last_seen_at": "2026-01-01T00:00:00",
        "occurrences": 1,
        "source_run_id": "run-1",
        "linked_task_id": "task-1",
        "details_json": {"message": "connection refused"},
    })

    regression = db.regression_get("reg-1")
    assert regression is not None
    assert regression["details_json"] == {"message": "connection refused"}
    assert regression["occurrences"] == 1

    found = db.regression_find_open("p1", "boot:refused")
    assert found is not None
    assert found["id"] == "reg-1"

    db.regression_update("reg-1", {
        "occurrences": 2,
        "last_seen_at": "2026-01-01T00:05:00",
        "details_json": {"message": "still failing"},
    })
    updated = db.regression_get("reg-1")
    assert updated["occurrences"] == 2
    assert updated["details_json"] == {"message": "still failing"}

    listed = db.regression_list_by_project("p1", status="open")
    assert [row["id"] for row in listed] == ["reg-1"]


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

def test_concurrent_task_writes():
    """Multiple threads writing distinct tasks should all succeed."""
    errors = []

    def write(i):
        try:
            db.task_upsert(_task(f"t{i}", project=f"proj{i}"))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(db.task_get_all()) == 20
