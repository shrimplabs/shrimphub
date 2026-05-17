import pytest

from swarm import db
from swarm.task_mutations import reparent_dependents, replace_task_dependencies, reset_task_to_pending


@pytest.fixture()
def isolated_db(tmp_path):
    db_path = tmp_path / "swarm.db"
    db._local = type("Local", (), {})()
    db._db_path = None
    db._initialized = False
    db.init(db_path)
    yield db


def test_replace_task_dependencies_dedupes_and_drops(isolated_db):
    isolated_db.task_upsert({
        "id": "task-a",
        "project": "proj",
        "type": "feature",
        "description": "a",
        "status": "pending",
        "dependencies": ["old", "keep", "old", "drop-me"],
        "metadata": {},
    })

    new_deps = replace_task_dependencies(
        isolated_db,
        "task-a",
        {"old": "new"},
        drop=["drop-me"],
    )

    assert new_deps == ["new", "keep"]
    assert isolated_db.task_get("task-a")["dependencies"] == ["new", "keep"]


def test_reset_task_to_pending_clears_failure_metadata(isolated_db):
    isolated_db.task_upsert({
        "id": "task-b",
        "project": "proj",
        "type": "bug",
        "description": "b",
        "status": "failed",
        "attempts": 2,
        "agent_id": "agent-1",
        "dependencies": [],
        "metadata": {"last_failure": "boom", "failure_attempt": 2, "keep": True},
    })

    reset_task_to_pending(isolated_db, "task-b")
    task = isolated_db.task_get("task-b")

    assert task["status"] == "pending"
    assert task["attempts"] == 0
    assert task["agent_id"] is None
    assert "last_failure" not in task["metadata"]
    assert "failure_attempt" not in task["metadata"]
    assert task["metadata"]["keep"] is True


def test_reparent_dependents_scopes_by_project(isolated_db):
    isolated_db.task_upsert({
        "id": "dep-1",
        "project": "proj",
        "type": "feature",
        "description": "dep",
        "status": "pending",
        "dependencies": ["old-root"],
        "metadata": {},
    })
    isolated_db.task_upsert({
        "id": "dep-2",
        "project": "other",
        "type": "feature",
        "description": "dep",
        "status": "pending",
        "dependencies": ["old-root"],
        "metadata": {},
    })

    updated = reparent_dependents(isolated_db, "old-root", "new-root", project="proj")

    assert updated == ["dep-1"]
    assert isolated_db.task_get("dep-1")["dependencies"] == ["new-root"]
    assert isolated_db.task_get("dep-2")["dependencies"] == ["old-root"]
