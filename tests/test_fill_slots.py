"""
Tests for fill_slots() and _get_next_task() in swarm/orchestrator.py.

spawn_agent() is mocked to avoid real subprocess creation — scheduling
logic is what's under test here.
"""
import threading
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
    orc.WORKSPACE = tmp_path / "workspace"
    orc.WORKSPACE.mkdir(parents=True, exist_ok=True)
    orc.HISTORY_FILE = data_dir / "agent-history.jsonl"
    orc.MAX_ACTIVE_AGENTS = 5
    orc.LOCK_PROJECT = False
    orc.MANAGED_PROJECTS = []
    orc.PAUSED_PROJECTS = []

    with lifecycle._handle_lock:
        lifecycle._active_handles.clear()

    yield

    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COUNTER = 0

def _task(task_id=None, project="p", priority=50, deps=None, attempts=0,
          max_attempts=3, status="pending", run_after=None):
    global _COUNTER
    _COUNTER += 1
    tid = task_id or f"task-{_COUNTER}"
    t = {
        "id": tid,
        "project": project,
        "type": "feature",
        "description": "desc",
        "priority": priority,
        "status": status,
        "dependencies": deps or [],
        "metadata": {},
        "attempts": attempts,
        "max_attempts": max_attempts,
    }
    if run_after is not None:
        t["run_after"] = run_after
    db.task_upsert(t)
    return t


def _project(name="p", locked=False, managed=True):
    db.project_upsert({"name": name, "status": "active", "managed": managed, "locked": locked})


def _fake_spawn(task, generate_fn):
    """Mock spawn_agent that updates DB state as the real one would."""
    import uuid
    agent_id = str(uuid.uuid4())
    db.agent_upsert({
        "id": agent_id,
        "project": task["project"],
        "task_type": task["type"],
        "status": "active",
        "spawned_at": "2026-01-01T00:00:00",
        "pid": 99999,
        "task_id": task["id"],
    })
    db.task_update_status(task["id"], "in_progress", agent_id=agent_id)
    # Simulate an in-process handle so get_active_count() works
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # still running
    with lifecycle._handle_lock:
        lifecycle._active_handles[agent_id] = {
            "process": mock_proc,
            "project": task["project"],
            "task_id": task["id"],
            "script_path": str(orc.DATA_DIR / f"agent_{agent_id}.py"),
            "log_path": str(orc.DATA_DIR / f"agent_{agent_id}.log"),
        }
    return agent_id


# ---------------------------------------------------------------------------
# _get_next_task() tests (via fill_slots with max_spawn=1)
# ---------------------------------------------------------------------------

class TestGetNextTask:
    def test_returns_highest_priority_task(self, isolated_orc):
        _project()
        _task(task_id="low", priority=10)
        _task(task_id="high", priority=90)

        orc.MAX_ACTIVE_AGENTS = 1
        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            orc.fill_slots(lambda t: "", max_spawn=1)

        assert db.task_get("high")["status"] == "in_progress"
        assert db.task_get("low")["status"] == "pending"

    def test_skips_paused_projects(self, isolated_orc):
        orc.PAUSED_PROJECTS = ["paused"]
        _project("paused")
        _task(project="paused")

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert spawned == []

    def test_skips_tasks_for_unmanaged_projects(self, isolated_orc):
        orc.MANAGED_PROJECTS = ["allowed"]  # compatibility list should not be authoritative
        _project("not-allowed", managed=False)
        _task(project="not-allowed")

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert spawned == []

    def test_picks_task_in_managed_projects(self, isolated_orc):
        orc.MANAGED_PROJECTS = []  # scheduler should use project state
        _project("allowed", managed=True)
        _task(task_id="t1", project="allowed")

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert len(spawned) == 1

    def test_skips_locked_projects(self, isolated_orc):
        _project("locked-proj", locked=True)
        _task(project="locked-proj")

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert spawned == []

    def test_skips_task_with_unmet_dependency(self, isolated_orc):
        _project()
        parent = _task(task_id="parent", priority=50)
        _task(task_id="child", priority=90, deps=["parent"])

        orc.MAX_ACTIVE_AGENTS = 1
        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            orc.fill_slots(lambda t: "", max_spawn=1)

        # Child has higher priority but unmet dep — parent should be picked
        assert db.task_get("parent")["status"] == "in_progress"
        assert db.task_get("child")["status"] == "pending"

    def test_picks_task_with_met_dependency(self, isolated_orc):
        _project()
        # Mark parent as completed
        parent = _task(task_id="parent", status="completed")
        db.task_update_status("parent", "completed")
        child = _task(task_id="child", deps=["parent"], priority=50)

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert len(spawned) == 1
        assert db.task_get("child")["status"] == "in_progress"

    def test_returns_none_when_queue_empty(self, isolated_orc):
        _project()
        # No tasks seeded
        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert spawned == []

    def test_skips_already_in_progress_tasks(self, isolated_orc):
        _project()
        _task(task_id="running", status="in_progress")
        _task(task_id="pending-only", priority=10)

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert len(spawned) == 1
        assert db.task_get("pending-only")["status"] == "in_progress"

    def test_global_qa_lock_blocks_second_qa(self, isolated_orc):
        """A pending QA task must not be picked up while any QA task is active globally."""
        _project("proj-a")
        _project("proj-b")
        # Simulate an active QA on proj-a
        db.task_upsert({
            "id": "qa-active", "project": "proj-a", "type": "qa",
            "description": "qa running", "priority": 75, "status": "in_progress",
            "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 2,
        })
        # Pending QA on a different project
        db.task_upsert({
            "id": "qa-pending", "project": "proj-b", "type": "qa",
            "description": "qa waiting", "priority": 75, "status": "pending",
            "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 2,
        })

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert spawned == []
        assert db.task_get("qa-pending")["status"] == "pending"

    def test_global_qa_lock_allows_qa_when_none_active(self, isolated_orc):
        """A pending QA task is picked up normally when no QA is currently running."""
        _project()
        db.task_upsert({
            "id": "qa-pending", "project": "p", "type": "qa",
            "description": "qa waiting", "priority": 75, "status": "pending",
            "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 2,
        })

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert len(spawned) == 1
        assert db.task_get("qa-pending")["status"] == "in_progress"

    def test_global_qa_lock_does_not_block_non_qa_tasks(self, isolated_orc):
        """A running QA task must not block feature/bug tasks from being picked up."""
        _project()
        db.task_upsert({
            "id": "qa-active", "project": "p", "type": "qa",
            "description": "qa running", "priority": 75, "status": "in_progress",
            "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 2,
        })
        _task(task_id="feat", project="p", priority=80)

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert len(spawned) == 1
        assert db.task_get("feat")["status"] == "in_progress"


# ---------------------------------------------------------------------------
# fill_slots() tests
# ---------------------------------------------------------------------------

class TestFillSlots:
    def test_spawns_pending_tasks(self, isolated_orc):
        _project()
        for i in range(3):
            _task()

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert len(spawned) == 3

    def test_skips_phase_gate_tasks(self, isolated_orc):
        _project()
        gate = _task(task_id="phase-gate")
        db.task_upsert({**gate, "type": "phase_gate"})

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert spawned == []
        assert db.task_get("phase-gate")["status"] == "pending"

    def test_respects_max_active_agents(self, isolated_orc):
        orc.MAX_ACTIVE_AGENTS = 2
        _project()
        for i in range(5):
            _task()

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert len(spawned) <= 2

    def test_respects_max_spawn_parameter(self, isolated_orc):
        _project()
        for i in range(10):
            _task()

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "", max_spawn=3)

        assert len(spawned) == 3

    def test_stops_when_no_tasks_remain(self, isolated_orc):
        _project()
        _task()  # only one

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert len(spawned) == 1

    def test_does_not_spawn_when_already_at_limit(self, isolated_orc):
        orc.MAX_ACTIVE_AGENTS = 1
        _project()
        _task(task_id="running", status="in_progress")

        # Pre-populate handle so get_active_count() sees it
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        with lifecycle._handle_lock:
            lifecycle._active_handles["fake-agent"] = {
                "process": mock_proc,
                "project": "p",
                "task_id": "running",
                "script_path": "",
                "log_path": "",
            }

        _task(task_id="waiting")

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert spawned == []

    def test_returns_spawned_and_skipped_lists(self, isolated_orc):
        _project()
        _task()

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, skipped = orc.fill_slots(lambda t: "")

        assert isinstance(spawned, list)
        assert isinstance(skipped, list)

    def test_generate_script_fn_called_with_task(self, isolated_orc):
        _project()
        t = _task(task_id="specific-task")
        received = []

        def capturing_fn(task):
            received.append(task)
            return ""

        def capturing_spawn(task, gen_fn):
            gen_fn(task)  # invoke so we can verify it was passed through
            return _fake_spawn(task, gen_fn)

        with patch("swarm.orchestrator.spawn_agent", side_effect=capturing_spawn):
            orc.fill_slots(capturing_fn)

        assert len(received) == 1
        assert received[0]["id"] == "specific-task"

    def test_lock_set_when_lock_project_true(self, isolated_orc):
        orc.LOCK_PROJECT = True
        _project("lockable")
        _task(project="lockable")

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            orc.fill_slots(lambda t: "")

        proj = db.project_get("lockable")
        assert proj["locked"] is True

    def test_multiple_projects_each_get_one_slot(self, isolated_orc):
        _project("alpha")
        _project("beta")
        _task(task_id="a1", project="alpha")
        _task(task_id="b1", project="beta")

        with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
            spawned, _ = orc.fill_slots(lambda t: "")

        assert len(spawned) == 2

    def test_idle_fill_slots_triggers_periodic_closure_verification(self, isolated_orc):
        _project("proj")
        db.project_update("proj", {
            "closure_status": "yellow",
            "last_verification_at": None,
            "open_regression_count": 0,
        })

        with patch("swarm.orchestrator._validation.run_closure_verification", return_value={"id": "vr-1"}) as run_closure:
            with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
                spawned, _ = orc.fill_slots(lambda t: "")

        assert spawned == []
        run_closure.assert_called_once_with("proj", run_type="periodic")

    def test_idle_fill_slots_skips_periodic_closure_when_work_was_spawned(self, isolated_orc):
        _project("proj")
        _task(task_id="t1", project="proj")

        with patch("swarm.orchestrator._validation.run_closure_verification") as run_closure:
            with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
                spawned, _ = orc.fill_slots(lambda t: "")

        assert len(spawned) == 1
        run_closure.assert_not_called()

    def test_fill_slots_prefers_closure_repair_before_healthy_feature_and_skips_idle_trigger(self, isolated_orc):
        _project("repair-proj")
        _project("healthy-proj")
        db.project_update("repair-proj", {
            "closure_status": "red",
            "open_regression_count": 2,
        })
        db.task_upsert({
            "id": "closure-repair",
            "project": "repair-proj",
            "type": "bug",
            "description": "repair",
            "priority": 30,
            "status": "pending",
            "dependencies": [],
            "metadata": {"is_closure_repair_task": True},
            "attempts": 0,
            "max_attempts": 3,
        })
        _task(task_id="healthy-feature", project="healthy-proj", priority=90)

        with patch("swarm.orchestrator._validation.run_closure_verification") as run_closure:
            with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
                spawned, _ = orc.fill_slots(lambda t: "", max_spawn=1)

        assert len(spawned) == 1
        assert db.task_get("closure-repair")["status"] == "in_progress"
        assert db.task_get("healthy-feature")["status"] == "pending"
        run_closure.assert_not_called()

    def test_fill_slots_triggers_idle_verification_when_only_frozen_expansion_remains(self, isolated_orc):
        _project("proj")
        db.project_update("proj", {
            "closure_status": "frozen",
            "open_regression_count": 2,
        })
        _task(task_id="frozen-feature", project="proj", priority=100)

        with patch("swarm.orchestrator._validation.run_closure_verification", return_value={"id": "vr-1"}) as run_closure:
            with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
                spawned, _ = orc.fill_slots(lambda t: "")

        assert spawned == []
        run_closure.assert_called_once_with("proj", run_type="periodic")

    def test_fill_slots_allows_stalled_triage_path_without_idle_trigger(self, isolated_orc):
        _project("proj")
        db.project_update("proj", {
            "closure_status": "stalled",
            "open_regression_count": 4,
        })
        db.task_upsert({
            "id": "stall-triage",
            "project": "proj",
            "type": "triage",
            "description": "triage stalled repair loop",
            "priority": 25,
            "status": "pending",
            "dependencies": [],
            "metadata": {},
            "attempts": 0,
            "max_attempts": 2,
        })
        _task(task_id="stalled-feature", project="proj", priority=100)

        with patch("swarm.orchestrator._validation.run_closure_verification") as run_closure:
            with patch("swarm.orchestrator.spawn_agent", side_effect=_fake_spawn):
                spawned, _ = orc.fill_slots(lambda t: "", max_spawn=1)

        assert len(spawned) == 1
        assert db.task_get("stall-triage")["status"] == "in_progress"
        assert db.task_get("stalled-feature")["status"] == "pending"
        run_closure.assert_not_called()


# ---------------------------------------------------------------------------
# run_after scheduling
# ---------------------------------------------------------------------------

class TestRunAfter:
    def test_skips_task_with_future_run_after(self, isolated_orc):
        """Tasks with run_after in the future should not be picked."""
        _project("proj")
        from datetime import datetime, timedelta
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        _task(task_id="future-task", project="proj", run_after=future)

        task = orc._get_next_task()
        assert task is None

    def test_picks_task_with_past_run_after(self, isolated_orc):
        """Tasks with run_after in the past should be picked normally."""
        _project("proj")
        from datetime import datetime, timedelta
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        _task(task_id="past-task", project="proj", run_after=past)

        task = orc._get_next_task()
        assert task is not None
        assert task["id"] == "past-task"

    def test_picks_task_with_no_run_after(self, isolated_orc):
        """Tasks without run_after are always eligible."""
        _project("proj")
        _task(task_id="no-delay-task", project="proj")

        task = orc._get_next_task()
        assert task is not None
        assert task["id"] == "no-delay-task"

    def test_prefers_ready_task_over_delayed(self, isolated_orc):
        """When both delayed and ready tasks exist, picks the ready one."""
        _project("proj")
        from datetime import datetime, timedelta
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        _task(task_id="delayed", project="proj", priority=100, run_after=future)
        _task(task_id="ready", project="proj", priority=50)

        task = orc._get_next_task()
        assert task["id"] == "ready"

    def test_unhealthy_project_prefers_repair_task_over_feature_expansion(self, isolated_orc):
        _project("proj")
        db.project_update("proj", {
            "closure_status": "red",
            "open_regression_count": 2,
        })
        _task(task_id="feature-task", project="proj", priority=100)
        db.task_upsert({
            "id": "repair-task",
            "project": "proj",
            "type": "bug",
            "description": "closure repair",
            "priority": 40,
            "status": "pending",
            "dependencies": [],
            "metadata": {"is_closure_repair_task": True},
            "attempts": 0,
            "max_attempts": 3,
        })

        task = orc._get_next_task()
        assert task is not None
        assert task["id"] == "repair-task"

    def test_unhealthy_project_feature_is_deprioritized_below_healthy_project_work(self, isolated_orc):
        _project("unhealthy")
        _project("healthy")
        db.project_update("unhealthy", {
            "closure_status": "yellow",
            "open_regression_count": 1,
        })
        db.project_update("healthy", {
            "closure_status": "green",
            "open_regression_count": 0,
        })
        _task(task_id="unhealthy-feature", project="unhealthy", priority=100)
        _task(task_id="healthy-feature", project="healthy", priority=50)

        task = orc._get_next_task()
        assert task is not None
        assert task["id"] == "healthy-feature"

    def test_blocked_repair_task_does_not_override_dependency_readiness(self, isolated_orc):
        _project("unhealthy")
        _project("healthy")
        db.project_update("unhealthy", {
            "closure_status": "red",
            "open_regression_count": 3,
        })
        _task(task_id="repair-parent", project="unhealthy", priority=95)
        db.task_upsert({
            "id": "blocked-repair",
            "project": "unhealthy",
            "type": "bug",
            "description": "blocked repair",
            "priority": 90,
            "status": "pending",
            "dependencies": ["repair-parent"],
            "metadata": {"is_closure_repair_task": True},
            "attempts": 0,
            "max_attempts": 3,
        })
        _task(task_id="healthy-feature", project="healthy", priority=10)

        task = orc._get_next_task()
        assert task is not None
        assert task["id"] == "healthy-feature"

    def test_frozen_project_blocks_feature_expansion_but_allows_repair(self, isolated_orc):
        _project("proj")
        db.project_update("proj", {
            "closure_status": "frozen",
            "open_regression_count": 2,
        })
        _task(task_id="frozen-feature", project="proj", priority=100)
        db.task_upsert({
            "id": "frozen-repair",
            "project": "proj",
            "type": "bug",
            "description": "repair",
            "priority": 20,
            "status": "pending",
            "dependencies": [],
            "metadata": {"is_closure_repair_task": True},
            "attempts": 0,
            "max_attempts": 3,
        })

        task = orc._get_next_task()
        assert task is not None
        assert task["id"] == "frozen-repair"

    def test_stalled_project_blocks_expansion_when_no_repair_path_exists(self, isolated_orc):
        _project("proj")
        db.project_update("proj", {
            "closure_status": "stalled",
            "open_regression_count": 3,
        })
        _task(task_id="stalled-feature", project="proj", priority=100)

        task = orc._get_next_task()
        assert task is None
        blocked = db.task_get("stalled-feature")["metadata"].get("scheduler_blocked")
        assert blocked["reason"] == "closure_expansion_gate"
        assert blocked["closure_status"] == "stalled"
        assert blocked["open_regression_count"] == 3

    def test_closure_scheduler_block_annotation_clears_when_project_recovers(self, isolated_orc):
        _project("proj")
        db.project_update("proj", {
            "closure_status": "frozen",
            "open_regression_count": 2,
        })
        _task(task_id="frozen-feature", project="proj", priority=100)

        assert orc._get_next_task() is None
        assert db.task_get("frozen-feature")["metadata"].get("scheduler_blocked")

        db.project_update("proj", {
            "closure_status": "green",
            "open_regression_count": 0,
        })

        task = orc._get_next_task()
        assert task is not None
        assert task["id"] == "frozen-feature"
        assert "scheduler_blocked" not in db.task_get("frozen-feature")["metadata"]

    def test_stalled_project_allows_feature_typed_recovery_task(self, isolated_orc):
        _project("proj")
        db.project_update("proj", {
            "closure_status": "stalled",
            "open_regression_count": 3,
        })
        db.task_record_completed("completed-parent", project="proj")
        db.task_upsert({
            "id": "feature-recovery",
            "project": "proj",
            "type": "feature",
            "description": "recovery for failed feature branch",
            "priority": 50,
            "status": "pending",
            "dependencies": ["completed-parent"],
            "metadata": {"is_recovery_task": True},
            "attempts": 0,
            "max_attempts": 3,
        })

        task = orc._get_next_task()
        assert task is not None
        assert task["id"] == "feature-recovery"
