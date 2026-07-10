import threading

import pytest

from swarm import db
import swarm.agent_lifecycle as lifecycle
from swarm.agent_auto_tasks import auto_spawn_playthrough_task


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db_path = tmp_path / "data" / "swarm.db"
    db_path.parent.mkdir(parents=True)
    db.init(db_path)
    lifecycle.DATA_DIR = tmp_path / "data"
    lifecycle.WORKSPACE = tmp_path / "ws"
    lifecycle.WORKSPACE.mkdir()
    lifecycle._configured = True
    yield tmp_path
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None
    lifecycle._configured = False


def _seed_task(task_id, task_type="harness_qa", status="completed"):
    db.project_upsert({"name": "proj", "status": "active"})
    db.task_upsert({
        "id": task_id,
        "project": "proj",
        "type": task_type,
        "description": "done",
        "priority": 50,
        "status": status,
        "dependencies": [],
        "metadata": {},
        "attempts": 0,
        "max_attempts": 2,
    })


def _make_godot_project():
    project_path = lifecycle.WORKSPACE / "proj"
    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / "project.godot").write_text("[application]\n")


def test_auto_spawn_playthrough_after_qa_when_enabled():
    _make_godot_project()
    _seed_task("qa-done")

    auto_spawn_playthrough_task(
        project="proj",
        task_id="qa-done",
        task_type_finished="harness_qa",
        workspace=lifecycle.WORKSPACE,
        validation_failed=False,
        spawned_continuation=False,
        is_recovery_task=False,
        enabled=True,
    )

    tasks = db.task_get_by_project("proj")
    playthrough = [t for t in tasks if t["type"] == "playthrough_bot"]
    assert len(playthrough) == 1
    assert playthrough[0]["status"] == "pending"
    assert playthrough[0]["metadata"]["playthrough_auto"] is True
    assert "qa-done" in playthrough[0]["dependencies"]


def test_auto_spawn_playthrough_is_config_gated():
    _make_godot_project()
    _seed_task("qa-done")

    auto_spawn_playthrough_task(
        project="proj",
        task_id="qa-done",
        task_type_finished="harness_qa",
        workspace=lifecycle.WORKSPACE,
        validation_failed=False,
        spawned_continuation=False,
        is_recovery_task=False,
        enabled=False,
    )

    assert [t for t in db.task_get_by_project("proj") if t["type"] == "playthrough_bot"] == []


def test_auto_spawn_playthrough_does_not_duplicate_active_task():
    _make_godot_project()
    _seed_task("qa-done")
    _seed_task("play-existing", task_type="playthrough_bot", status="pending")

    auto_spawn_playthrough_task(
        project="proj",
        task_id="qa-done",
        task_type_finished="harness_qa",
        workspace=lifecycle.WORKSPACE,
        validation_failed=False,
        spawned_continuation=False,
        is_recovery_task=False,
        enabled=True,
    )

    playthrough = [t for t in db.task_get_by_project("proj") if t["type"] == "playthrough_bot"]
    assert [t["id"] for t in playthrough] == ["play-existing"]
