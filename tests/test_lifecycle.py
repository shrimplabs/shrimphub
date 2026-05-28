"""
Integration tests for the agent spawn → subprocess → monitor → completion lifecycle.

Real subprocesses are used (trivial scripts, no LLM).  prune_history() is patched
out in most tests so DB state can be inspected directly after check_agent_status().
"""
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from swarm import db
import swarm.orchestrator as orc
import swarm.agent_lifecycle as lifecycle


# ---------------------------------------------------------------------------
# Trivial agent scripts — real subprocesses, no LLM
# ---------------------------------------------------------------------------

def _exit0_script():
    """Script that writes .exit file and exits 0."""
    return f"""\
import sys
from pathlib import Path
Path(__file__).with_suffix(".exit").write_text("0")
sys.exit(0)
"""

def _exit1_script():
    """Script that writes .exit file and exits 1."""
    return f"""\
import sys
from pathlib import Path
Path(__file__).with_suffix(".exit").write_text("1")
sys.exit(1)
"""


# ---------------------------------------------------------------------------
# Shared fixture: isolated DB + orchestrator globals
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_orc(tmp_path):
    """Reset DB and all orchestrator module-level state before each test."""
    # Isolate DB
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm.db")

    # Isolate orchestrator globals
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    orc.DATA_DIR = data_dir
    orc.WORKSPACE = workspace
    orc.HISTORY_FILE = data_dir / "agent-history.jsonl"
    orc.MAX_ACTIVE_AGENTS = 5
    orc.LOCK_PROJECT = False
    orc.MANAGED_PROJECTS = []
    orc.PAUSED_PROJECTS = []

    # Configure agent_lifecycle explicitly so spawn_agent / _finish_agent use the
    # isolated tmp dirs rather than falling back to orchestrator globals.
    lifecycle.configure(
        workspace=workspace,
        data_dir=data_dir,
    )

    # Clear in-process handle registry
    with lifecycle._handle_lock:
        lifecycle._active_handles.clear()

    yield tmp_path

    # Kill any stray subprocesses
    with lifecycle._handle_lock:
        for data in list(lifecycle._active_handles.values()):
            try:
                data["process"].kill()
                data["process"].wait(timeout=2)
            except Exception:
                pass
        lifecycle._active_handles.clear()

    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_task(task_id="t-001", project="test-proj", priority=50,
               max_attempts=3, attempts=0, deps=None):
    db.project_upsert({"name": project, "status": "active"})
    task = {
        "id": task_id,
        "project": project,
        "type": "feature",
        "description": "Test task",
        "priority": priority,
        "status": "pending",
        "dependencies": deps or [],
        "metadata": {},
        "attempts": attempts,
        "max_attempts": max_attempts,
    }
    db.task_upsert(task)
    return task


def _wait_for_subprocess(agent_id, timeout=5.0) -> bool:
    """Poll until the agent's subprocess exits or timeout is reached."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with lifecycle._handle_lock:
            handle = lifecycle._active_handles.get(agent_id)
        if handle and handle["process"].poll() is not None:
            return True
        time.sleep(0.05)
    return False


def _check_agent_status_sync():
    """Call check_agent_status() and join all finish threads before returning.

    Tests need synchronous completion of _finish_agent (DB updates, status
    changes) before asserting on task/agent state.  The production monitor
    loop discards the threads so it is never blocked.
    """
    threads = orc.check_agent_status()
    for t in threads:
        t.join(timeout=10)


# ---------------------------------------------------------------------------
# spawn_agent() unit tests
# ---------------------------------------------------------------------------

class TestSpawnAgent:
    def test_returns_uuid_string(self, isolated_orc):
        task = _seed_task()
        agent_id = orc.spawn_agent(task, lambda t: _exit0_script())
        assert agent_id is not None
        assert len(agent_id) == 36  # UUID format

    def test_agent_record_written_as_active(self, isolated_orc):
        task = _seed_task()
        agent_id = orc.spawn_agent(task, lambda t: _exit0_script())
        agent = db.agent_get(agent_id)
        assert agent is not None
        assert agent["status"] == "active"
        assert agent["project"] == "test-proj"

    def test_task_transitions_to_in_progress(self, isolated_orc):
        task = _seed_task()
        orc.spawn_agent(task, lambda t: _exit0_script())
        updated = db.task_get(task["id"])
        assert updated["status"] == "in_progress"

    def test_task_agent_id_set(self, isolated_orc):
        task = _seed_task()
        agent_id = orc.spawn_agent(task, lambda t: _exit0_script())
        updated = db.task_get(task["id"])
        assert updated["agent_id"] == agent_id

    def test_script_file_written_to_data_dir(self, isolated_orc):
        task = _seed_task()
        agent_id = orc.spawn_agent(task, lambda t: _exit0_script())
        with lifecycle._handle_lock:
            handle = lifecycle._active_handles.get(agent_id)
        script_path = Path(handle["script_path"])
        assert script_path.exists()
        assert script_path.parent == orc.DATA_DIR

    def test_refuses_spawn_when_task_already_in_progress(self, isolated_orc):
        task = _seed_task()
        first_agent = orc.spawn_agent(task, lambda t: _exit0_script())
        assert first_agent is not None

        second_agent = orc.spawn_agent(db.task_get(task["id"]), lambda t: _exit0_script())
        assert second_agent is None

    def test_refuses_spawn_when_active_agent_already_claims_task(self, isolated_orc):
        task = _seed_task(task_id="dup-001")
        db.agent_upsert({
            "id": "agent-existing",
            "project": "test-proj",
            "task_type": "feature",
            "status": "active",
            "spawned_at": "2026-04-01T10:00:00",
            "completed_at": None,
            "pid": None,
            "exit_code": None,
            "task_id": "dup-001",
            "log_path": "",
            "script_path": "",
            "output": "",
            "metadata": {},
            "input_tokens": 0,
            "output_tokens": 0,
        })

        agent_id = orc.spawn_agent(task, lambda t: _exit0_script())
        assert agent_id is None

    def test_agent_added_to_active_handles(self, isolated_orc):
        task = _seed_task()
        agent_id = orc.spawn_agent(task, lambda t: _exit0_script())
        with lifecycle._handle_lock:
            assert agent_id in lifecycle._active_handles

    def test_returns_none_when_popen_fails(self, isolated_orc):
        task = _seed_task()
        with patch("subprocess.Popen", side_effect=OSError("No such executable")):
            agent_id = orc.spawn_agent(task, lambda t: _exit0_script())
        assert agent_id is None

    def test_task_stays_pending_when_popen_fails(self, isolated_orc):
        task = _seed_task()
        with patch("subprocess.Popen", side_effect=OSError("No such executable")):
            orc.spawn_agent(task, lambda t: _exit0_script())
        updated = db.task_get(task["id"])
        assert updated["status"] == "pending"


# ---------------------------------------------------------------------------
# Full lifecycle: spawn → subprocess exits → check_agent_status()
# ---------------------------------------------------------------------------

class TestLifecycleSuccess:
    def test_handle_cleared_after_check(self, isolated_orc):
        task = _seed_task()
        agent_id = orc.spawn_agent(task, lambda t: _exit0_script())
        assert _wait_for_subprocess(agent_id), "Subprocess did not exit"

        with patch("swarm.agent_lifecycle.prune_history"):
            _check_agent_status_sync()

        with lifecycle._handle_lock:
            assert agent_id not in lifecycle._active_handles

    def test_agent_marked_completed(self, isolated_orc):
        task = _seed_task()
        agent_id = orc.spawn_agent(task, lambda t: _exit0_script())
        assert _wait_for_subprocess(agent_id)

        with patch("swarm.agent_lifecycle.prune_history"):
            _check_agent_status_sync()

        agent = db.agent_get(agent_id)
        assert agent["status"] == "completed"
        assert agent["exit_code"] == 0

    def test_task_marked_completed(self, isolated_orc):
        task = _seed_task()
        agent_id = orc.spawn_agent(task, lambda t: _exit0_script())
        assert _wait_for_subprocess(agent_id)

        with patch("swarm.agent_lifecycle.prune_history"):
            _check_agent_status_sync()

        updated = db.task_get(task["id"])
        assert updated["status"] == "completed"

    def test_script_file_deleted(self, isolated_orc):
        task = _seed_task()
        agent_id = orc.spawn_agent(task, lambda t: _exit0_script())
        with lifecycle._handle_lock:
            script_path = Path(lifecycle._active_handles[agent_id]["script_path"])
        assert _wait_for_subprocess(agent_id)

        with patch("swarm.agent_lifecycle.prune_history"):
            _check_agent_status_sync()

        assert not script_path.exists()

    def test_lock_released_on_success(self, isolated_orc):
        orc.LOCK_PROJECT = True
        lifecycle.LOCK_PROJECT = True
        task = _seed_task()
        agent_id = orc.spawn_agent(task, lambda t: _exit0_script())
        # fill_slots() normally sets the lock; simulate that here
        db.project_set_locked("test-proj", True)
        assert db.project_get("test-proj")["locked"] is True
        assert _wait_for_subprocess(agent_id)

        with patch("swarm.agent_lifecycle.prune_history"):
            _check_agent_status_sync()

        assert db.project_get("test-proj")["locked"] is False


class TestLifecycleFailure:
    def test_failed_task_retried_when_attempts_remain(self, isolated_orc):
        task = _seed_task(max_attempts=3, attempts=0)
        agent_id = orc.spawn_agent(task, lambda t: _exit1_script())
        assert _wait_for_subprocess(agent_id)

        with patch("swarm.agent_lifecycle.prune_history"):
            _check_agent_status_sync()

        updated = db.task_get(task["id"])
        assert updated["status"] == "pending"
        assert updated["attempts"] == 1

    def test_retry_increments_attempts(self, isolated_orc):
        task = _seed_task(max_attempts=3, attempts=1)  # already 1 attempt
        agent_id = orc.spawn_agent(task, lambda t: _exit1_script())
        assert _wait_for_subprocess(agent_id)

        with patch("swarm.agent_lifecycle.prune_history"):
            _check_agent_status_sync()

        updated = db.task_get(task["id"])
        assert updated["attempts"] == 2

    def test_task_marked_failed_after_max_attempts(self, isolated_orc):
        # max_attempts=1, type=qa: QA tasks cancel on exhaust (no research feeder).
        # Feature tasks would instead spawn a research feeder and reset to pending.
        db.project_upsert({"name": "test-proj", "status": "active"})
        task = {
            "id": "t-001", "project": "test-proj", "type": "qa",
            "description": "Test QA task", "priority": 50,
            "status": "pending", "dependencies": [], "metadata": {},
            "attempts": 0, "max_attempts": 1,
        }
        db.task_upsert(task)
        agent_id = orc.spawn_agent(task, lambda t: _exit1_script())
        assert _wait_for_subprocess(agent_id)

        with patch("swarm.agent_lifecycle.prune_history"):
            _check_agent_status_sync()

        updated = db.task_get(task["id"])
        # QA escalation policy: on_exhaust=cancel → task ends in failed or cancelled
        assert updated["status"] in ("failed", "cancelled")

    def test_lock_released_on_failure(self, isolated_orc):
        orc.LOCK_PROJECT = True
        lifecycle.LOCK_PROJECT = True
        task = _seed_task(max_attempts=1)
        agent_id = orc.spawn_agent(task, lambda t: _exit1_script())
        assert _wait_for_subprocess(agent_id)

        with patch("swarm.agent_lifecycle.prune_history"):
            _check_agent_status_sync()

        assert db.project_get("test-proj")["locked"] is False

    def test_retry_resets_agent_id_on_task(self, isolated_orc):
        task = _seed_task(max_attempts=3)
        agent_id = orc.spawn_agent(task, lambda t: _exit1_script())
        assert _wait_for_subprocess(agent_id)

        with patch("swarm.agent_lifecycle.prune_history"):
            _check_agent_status_sync()

        updated = db.task_get(task["id"])
        assert updated["agent_id"] is None  # cleared for next pick-up

    def test_retry_does_not_spawn_recovery_task_before_terminal_failure(self, isolated_orc):
        task = _seed_task(task_id="retry-no-recovery", max_attempts=3, attempts=0)
        db.project_upsert({"name": "test-proj", "status": "active", "head_task_id": "retry-no-recovery"})

        lifecycle._handle_task_failure("retry-no-recovery", "test-proj", "boom")

        updated = db.task_get("retry-no-recovery")
        recovery_tasks = [
            t for t in db.task_get_all()
            if (t.get("metadata") or {}).get("is_recovery_task")
        ]
        assert updated["status"] == "pending"
        assert updated["attempts"] == 1
        assert recovery_tasks == []

    def test_watchdog_resets_in_progress_task_with_mismatched_agent_id(self, isolated_orc):
        task = _seed_task(task_id="mismatch-001")
        db.task_update("mismatch-001", {"status": "in_progress", "agent_id": "wrong-agent"})
        db.agent_upsert({
            "id": "agent-real",
            "project": "test-proj",
            "task_type": "feature",
            "status": "active",
            "spawned_at": "2026-04-01T10:00:00",
            "completed_at": None,
            "pid": 999999,
            "exit_code": None,
            "task_id": "mismatch-001",
            "log_path": "",
            "script_path": "",
            "output": "",
            "metadata": {},
            "input_tokens": 0,
            "output_tokens": 0,
        })

        with patch("swarm.agent_lifecycle.prune_history"):
            _check_agent_status_sync()

        updated = db.task_get("mismatch-001")
        assert updated["status"] == "pending"
        assert updated["agent_id"] is None

    def test_stale_active_agent_with_wrong_project_is_failed(self, isolated_orc):
        _seed_task(task_id="wrong-proj-task", project="test-proj")
        db.task_update("wrong-proj-task", {"status": "in_progress", "agent_id": "agent-bad"})
        db.agent_upsert({
            "id": "agent-bad",
            "project": "other-proj",
            "task_type": "feature",
            "status": "active",
            "spawned_at": "2026-04-01T10:00:00",
            "completed_at": None,
            "pid": None,
            "exit_code": None,
            "task_id": "wrong-proj-task",
            "log_path": "",
            "script_path": "",
            "output": "",
            "metadata": {},
            "input_tokens": 0,
            "output_tokens": 0,
        })

        with patch("swarm.agent_lifecycle.prune_history"):
            _check_agent_status_sync()

        agent = db.agent_get("agent-bad")
        task = db.task_get("wrong-proj-task")
        assert agent["status"] == "failed"
        assert task["status"] == "pending"
        assert task["agent_id"] is None


class TestRecoveryPrompt:
    def test_recovery_task_uses_full_failure_output_not_tail_only(self, isolated_orc):
        failed_task = _seed_task(task_id="f-001", max_attempts=1)
        db.task_update("f-001", {"status": "failed"})

        long_output = "BEGIN-MARKER\n" + ("z" * 1500) + "\nEND-MARKER"
        lifecycle._spawn_review_task(db.task_get("f-001"), attempts=1, last_output=long_output)

        recovery_tasks = [
            t for t in db.task_get_all()
            if (t.get("metadata") or {}).get("is_recovery_task")
        ]
        assert len(recovery_tasks) == 1
        desc = recovery_tasks[0]["description"]
        assert "RECOVERY TASK: Complete the work that failed 1 times." in desc
        assert "BEGIN-MARKER" in desc
        assert "END-MARKER" in desc

        meta = recovery_tasks[0]["metadata"]
        assert "error_log" not in meta
        assert meta["error_log_chars"] >= len(long_output)
        assert "BEGIN-MARKER" in meta["error_log_excerpt"]
        assert "END-MARKER" in meta["error_log_excerpt"]

    def test_qa_recovery_prompt_does_not_ask_for_code_fixes(self, isolated_orc):
        failed_task = _seed_task(task_id="qa-failed", max_attempts=1)
        db.task_update("qa-failed", {"status": "failed", "type": "qa"})

        lifecycle._spawn_review_task(db.task_get("qa-failed"), attempts=1, last_output="create_bug_task failed")

        recovery_tasks = [
            t for t in db.task_get_all()
            if (t.get("metadata") or {}).get("is_recovery_task")
        ]
        assert len(recovery_tasks) == 1
        desc = recovery_tasks[0]["description"]
        assert "Complete the QA run" in desc
        assert "create bug tasks" in desc
        assert "Do NOT implement project code fixes" in desc
        assert "ACTUALLY COMPLETE the original task" not in desc

    def test_recovery_task_becomes_project_head_when_failed_task_was_head(self, isolated_orc):
        failed_task = _seed_task(task_id="f-head", max_attempts=1, project="head-proj")
        db.project_upsert({"name": "head-proj", "status": "active", "head_task_id": "f-head"})
        db.task_update("f-head", {"status": "failed"})

        lifecycle._spawn_review_task(db.task_get("f-head"), attempts=1, last_output="boom")

        recovery_tasks = [
            t for t in db.task_get_all()
            if (t.get("metadata") or {}).get("is_recovery_task")
        ]
        assert len(recovery_tasks) == 1
        assert db.project_get("head-proj")["head_task_id"] == recovery_tasks[0]["id"]

    def test_recovery_task_failure_does_not_spawn_nested_recovery(self, isolated_orc):
        failed_task = _seed_task(task_id="recovery-root", max_attempts=1, project="recovery-proj")
        db.task_update("recovery-root", {"status": "failed"})
        lifecycle._spawn_review_task(db.task_get("recovery-root"), attempts=1, last_output="boom")
        recovery = next(
            t for t in db.task_get_all()
            if (t.get("metadata") or {}).get("is_recovery_task")
        )
        db.task_update(recovery["id"], {"status": "in_progress", "max_attempts": 1})

        lifecycle._handle_task_failure(recovery["id"], "recovery-proj", "still boom")

        all_recovery_tasks = [
            t for t in db.task_get_all()
            if (t.get("metadata") or {}).get("is_recovery_task")
        ]
        failed_recovery = db.task_get(recovery["id"])
        assert failed_recovery["status"] == "failed"
        assert len(all_recovery_tasks) == 1
        continuation = db.task_get(f"bug-{recovery['id']}")
        assert continuation is not None
        assert continuation["status"] == "pending"
        assert continuation["metadata"]["continuation_reason"] == "terminal_recovery_failure"

    def test_recovery_spawner_reuses_existing_live_branch_tail(self, isolated_orc):
        failed_task = _seed_task(task_id="recover-branch", max_attempts=1, project="branch-proj")
        db.task_upsert({
            "id": "dependent-1",
            "project": "branch-proj",
            "type": "feature",
            "description": "downstream",
            "priority": 50,
            "status": "pending",
            "dependencies": ["recover-branch"],
            "metadata": {},
        })
        db.task_update("recover-branch", {"status": "failed"})

        lifecycle._spawn_review_task(db.task_get("recover-branch"), attempts=1, last_output="boom")
        recovery = next(
            t for t in db.task_get_all()
            if (t.get("metadata") or {}).get("is_recovery_task")
        )

        db.task_upsert({
            "id": "dependent-2",
            "project": "branch-proj",
            "type": "feature",
            "description": "another downstream",
            "priority": 50,
            "status": "pending",
            "dependencies": ["recover-branch"],
            "metadata": {},
        })
        lifecycle._spawn_review_task(db.task_get("recover-branch"), attempts=1, last_output="boom again")

        recovery_tasks = [
            t for t in db.task_get_all()
            if (t.get("metadata") or {}).get("is_recovery_task")
            and t.get("status") in ("pending", "in_progress")
        ]
        assert len(recovery_tasks) == 1
        assert recovery_tasks[0]["id"] == recovery["id"]
        assert db.task_get("dependent-1")["dependencies"] == [recovery["id"]]
        assert db.task_get("dependent-2")["dependencies"] == [recovery["id"]]

    def test_recovery_spawner_cancels_stale_pending_duplicates_for_branch(self, isolated_orc):
        failed_task = _seed_task(task_id="recover-dedupe", max_attempts=1, project="branch-proj")
        db.task_update("recover-dedupe", {"status": "failed"})
        db.task_upsert({
            "id": "recovery-old",
            "project": "branch-proj",
            "type": "feature",
            "description": "old recovery",
            "priority": 50,
            "status": "pending",
            "dependencies": ["recover-dedupe"],
            "metadata": {
                "is_recovery_task": True,
                "failed_task_id": "recover-dedupe",
                "recovery_root_task_id": "recover-dedupe",
            },
            "created": "2026-04-03T10:00:00",
        })
        db.task_upsert({
            "id": "recovery-new",
            "project": "branch-proj",
            "type": "feature",
            "description": "new recovery",
            "priority": 50,
            "status": "pending",
            "dependencies": ["recover-dedupe"],
            "metadata": {
                "is_recovery_task": True,
                "failed_task_id": "recover-dedupe",
                "recovery_root_task_id": "recover-dedupe",
            },
            "created": "2026-04-03T11:00:00",
        })

        lifecycle._spawn_review_task(db.task_get("recover-dedupe"), attempts=1, last_output="boom")

        assert db.task_get("recovery-old")["status"] == "pending"
        assert db.task_get("recovery-new")["status"] == "cancelled"

    def test_terminal_recovery_continuation_reparents_dependents_and_head(self, isolated_orc):
        failed_task = _seed_task(task_id="recover-terminal", max_attempts=1, project="term-proj")
        db.task_update("recover-terminal", {
            "description": "Mission Objective Flow\n\nImplement objective pickup and extraction success flow.",
        })
        db.task_update("recover-terminal", {"status": "failed"})
        lifecycle._spawn_review_task(db.task_get("recover-terminal"), attempts=1, last_output="boom")
        recovery = next(
            t for t in db.task_get_all()
            if (t.get("metadata") or {}).get("is_recovery_task")
        )
        db.project_upsert({"name": "term-proj", "status": "active", "head_task_id": recovery["id"]})
        db.task_upsert({
            "id": "downstream-1",
            "project": "term-proj",
            "type": "feature",
            "description": "downstream",
            "priority": 50,
            "status": "pending",
            "dependencies": [recovery["id"]],
            "metadata": {},
        })
        db.task_update(recovery["id"], {"status": "in_progress", "max_attempts": 1})

        lifecycle._handle_task_failure(recovery["id"], "term-proj", "still broken\nTraceback: x")

        continuation_id = f"bug-{recovery['id']}"
        continuation = db.task_get(continuation_id)
        assert continuation is not None
        assert continuation["status"] == "pending"
        assert continuation["dependencies"] == ["term-proj-genesis"]
        assert recovery["id"] not in continuation["dependencies"]
        assert continuation["metadata"]["recovery_root_task_id"] == "recover-terminal"
        assert continuation["metadata"]["branch_intent_root_task_id"] == "recover-terminal"
        assert continuation["metadata"]["dropped_terminal_dependency"] == recovery["id"]
        assert "Mission Objective Flow" in continuation["description"]
        assert "ORIGINAL TASK OBJECTIVE" in continuation["description"]
        assert continuation["metadata"]["error_log_chars"] >= len("still broken\nTraceback: x")
        assert db.task_get("downstream-1")["dependencies"] == [continuation_id]
        assert db.project_get("term-proj")["head_task_id"] == continuation_id

        db.task_delete(recovery["id"])

        assert db.task_get(continuation_id)["dependencies"] == ["term-proj-genesis"]

    def test_review_task_inherits_failed_task_dependencies(self, isolated_orc):
        _seed_task(task_id="dep-a", project="inherit-proj")
        failed_task = _seed_task(
            task_id="recover-deps",
            max_attempts=1,
            project="inherit-proj",
            deps=["dep-a"],
        )
        db.task_update("recover-deps", {"status": "failed"})

        lifecycle._spawn_review_task(db.task_get("recover-deps"), attempts=1, last_output="boom")

        recovery = next(
            t for t in db.task_get_all()
            if (t.get("metadata") or {}).get("is_recovery_task")
        )
        assert recovery["dependencies"] == ["dep-a"]


class TestPlannerLifecycle:
    def test_project_plan_completion_advances_head_to_generated_tail(self, isolated_orc):
        db.project_upsert({"name": "plan-proj", "status": "active", "head_task_id": "planner-head"})
        db.task_upsert({
            "id": "planner-head",
            "project": "plan-proj",
            "type": "project_plan",
            "description": "planner",
            "priority": 100,
            "status": "in_progress",
            "attempts": 0,
            "max_attempts": 2,
            "dependencies": ["plan-proj-genesis"],
            "metadata": {},
        })
        db.task_upsert({
            "id": "generated-a",
            "project": "plan-proj",
            "type": "feature",
            "description": "first generated task",
            "priority": 50,
            "status": "pending",
            "dependencies": ["planner-head"],
            "metadata": {"parent_task_id": "planner-head"},
        })
        db.task_upsert({
            "id": "generated-b",
            "project": "plan-proj",
            "type": "bug",
            "description": "second generated task",
            "priority": 50,
            "status": "pending",
            "dependencies": ["planner-head"],
            "metadata": {"parent_task_id": "planner-head"},
        })
        db.agent_upsert({
            "id": "agent-plan-head",
            "project": "plan-proj",
            "task_type": "project_plan",
            "status": "active",
            "spawned_at": "2026-04-03T10:00:00",
            "completed_at": None,
            "pid": None,
            "exit_code": None,
            "task_id": "planner-head",
            "log_path": "",
            "script_path": "",
            "output": "",
            "metadata": {},
            "input_tokens": 0,
            "output_tokens": 0,
        })

        with patch("swarm.agent_lifecycle.prune_history"), \
             patch("swarm.agent_lifecycle._fire_task_webhook"), \
             patch("swarm.learnings.extract_learnings_async", return_value=None):
            lifecycle._finish_agent("agent-plan-head", 0, "plan-proj", "planner-head", None, None)

        assert db.project_get("plan-proj")["head_task_id"] == "generated-b"

    def test_invalid_project_plan_deletes_owned_snapshot_and_cancels_subtasks(self, isolated_orc):
        db.project_upsert({"name": "plan-proj", "status": "active"})
        db.task_upsert({
            "id": "planner-1",
            "project": "plan-proj",
            "type": "project_plan",
            "description": "planner",
            "priority": 100,
            "status": "in_progress",
            "attempts": 0,
            "max_attempts": 2,
            "dependencies": ["plan-proj-genesis"],
            "metadata": {},
        })
        db.task_upsert({
            "id": "bad-generated",
            "project": "plan-proj",
            "type": "feature",
            "description": "generated task",
            "priority": 50,
            "status": "pending",
            "dependencies": ["scripts/player.gd"],
            "metadata": {"parent_task_id": "planner-1"},
        })
        db.agent_upsert({
            "id": "agent-plan",
            "project": "plan-proj",
            "task_type": "project_plan",
            "status": "active",
            "spawned_at": "2026-04-03T10:00:00",
            "completed_at": None,
            "pid": None,
            "exit_code": None,
            "task_id": "planner-1",
            "log_path": "",
            "script_path": "",
            "output": "",
            "metadata": {},
            "input_tokens": 0,
            "output_tokens": 0,
        })

        with patch("swarm.agent_lifecycle.prune_history"), \
             patch("swarm.agent_lifecycle._fire_task_webhook"), \
             patch("swarm.learnings.extract_learnings_async", return_value=None):
            lifecycle._finish_agent("agent-plan", 0, "plan-proj", "planner-1", None, None)

        planner = db.task_get("planner-1")
        generated = db.task_get("bad-generated")
        plans = db.plan_get_by_project("plan-proj")
        assert planner["status"] == "pending"
        assert planner["attempts"] == 1
        assert generated["status"] == "cancelled"
        assert plans == []

    def test_project_plan_rejects_sequential_hint_without_matching_dependencies(self, isolated_orc):
        db.project_upsert({"name": "plan-proj", "status": "active"})
        db.task_upsert({
            "id": "planner-2",
            "project": "plan-proj",
            "type": "project_plan",
            "description": "planner",
            "priority": 100,
            "status": "in_progress",
            "attempts": 0,
            "max_attempts": 2,
            "dependencies": ["plan-proj-genesis"],
            "metadata": {},
        })
        db.task_upsert({
            "id": "sound-task",
            "project": "plan-proj",
            "type": "feature",
            "description": "[PARALLEL] Add level_up() sound method to SoundManager.",
            "priority": 50,
            "status": "pending",
            "dependencies": ["plan-proj-genesis"],
            "metadata": {"parent_task_id": "planner-2"},
        })
        db.task_upsert({
            "id": "notif-task",
            "project": "plan-proj",
            "type": "feature",
            "description": "[PARALLEL] Create LevelUpNotification scene.",
            "priority": 50,
            "status": "pending",
            "dependencies": ["plan-proj-genesis"],
            "metadata": {"parent_task_id": "planner-2"},
        })
        db.task_upsert({
            "id": "wire-task",
            "project": "plan-proj",
            "type": "feature",
            "description": "[SEQUENTIAL: after SoundManager + LevelUpNotification] Wire level-up feedback into main.gd.",
            "priority": 50,
            "status": "pending",
            "dependencies": ["plan-proj-genesis"],
            "metadata": {"parent_task_id": "planner-2"},
        })

        errors = lifecycle._validate_project_plan_subtasks("plan-proj", "planner-2")

        assert "wire-task: sequential hint 'SoundManager' missing dependency on sound-task" in errors
        assert "wire-task: sequential hint 'LevelUpNotification' missing dependency on notif-task" in errors

    def test_project_plan_rejects_parallel_hint_with_sibling_dependency(self, isolated_orc):
        db.project_upsert({"name": "plan-proj", "status": "active"})
        db.task_upsert({
            "id": "planner-3",
            "project": "plan-proj",
            "type": "project_plan",
            "description": "planner",
            "priority": 100,
            "status": "in_progress",
            "attempts": 0,
            "max_attempts": 2,
            "dependencies": ["plan-proj-genesis"],
            "metadata": {},
        })
        db.task_upsert({
            "id": "dep-task",
            "project": "plan-proj",
            "type": "feature",
            "description": "Base dependency task",
            "priority": 50,
            "status": "pending",
            "dependencies": ["plan-proj-genesis"],
            "metadata": {"parent_task_id": "planner-3"},
        })
        db.task_upsert({
            "id": "parallel-task",
            "project": "plan-proj",
            "type": "feature",
            "description": "[PARALLEL] Claimed parallel task",
            "priority": 50,
            "status": "pending",
            "dependencies": ["dep-task"],
            "metadata": {"parent_task_id": "planner-3"},
        })

        errors = lifecycle._validate_project_plan_subtasks("plan-proj", "planner-3")

        assert "parallel-task: marked parallel but depends on sibling task(s): dep-task" in errors

    def test_project_plan_allows_parallel_hint_with_file_aware_auto_dependency(self, isolated_orc):
        db.project_upsert({"name": "plan-proj", "status": "active"})
        db.task_upsert({
            "id": "planner-4",
            "project": "plan-proj",
            "type": "project_plan",
            "description": "planner",
            "priority": 100,
            "status": "in_progress",
            "attempts": 0,
            "max_attempts": 2,
            "dependencies": ["plan-proj-genesis"],
            "metadata": {},
        })
        db.task_upsert({
            "id": "dep-task",
            "project": "plan-proj",
            "type": "feature",
            "description": "Base dependency task",
            "priority": 50,
            "status": "pending",
            "dependencies": ["plan-proj-genesis"],
            "metadata": {"parent_task_id": "planner-4"},
        })
        db.task_upsert({
            "id": "parallel-task",
            "project": "plan-proj",
            "type": "feature",
            "description": "[PARALLEL] Claimed parallel task",
            "priority": 50,
            "status": "pending",
            "dependencies": ["dep-task"],
            "metadata": {
                "parent_task_id": "planner-4",
                "file_aware_auto_dep_indices": [0],
            },
        })

        errors = lifecycle._validate_project_plan_subtasks("plan-proj", "planner-4")

        assert "parallel-task: marked parallel but depends on sibling task(s): dep-task" not in errors


# ---------------------------------------------------------------------------
# Exit file override
# ---------------------------------------------------------------------------

class TestExitFileOverride:
    def test_exit_file_overrides_process_exit_code(self, isolated_orc):
        """Agent writes .exit file with 0 even though process exits 1."""
        script = f"""\
import sys
from pathlib import Path
# Write exit 0 despite returning 1 from sys.exit
Path(__file__).with_suffix(".exit").write_text("0")
sys.exit(1)
"""
        task = _seed_task()
        agent_id = orc.spawn_agent(task, lambda t: script)
        assert _wait_for_subprocess(agent_id)

        with patch("swarm.agent_lifecycle.prune_history"):
            _check_agent_status_sync()

        # Exit file said 0 → task should be completed, not retried
        updated = db.task_get(task["id"])
        assert updated["status"] == "completed"
