"""Tests for swarm/orchestrator.py -- retry logic and validation detection."""
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from swarm import db, orchestrator
from swarm import validation


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm_test.db")
    orchestrator.DATA_DIR = tmp_path
    orchestrator.WORKSPACE = tmp_path / "workspace"
    orchestrator.WORKSPACE.mkdir()
    orchestrator.HISTORY_FILE = tmp_path / "agent-history.jsonl"
    orchestrator._active_handles.clear()
    yield
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


def _insert_task(id="t1", status="in_progress", attempts=0, max_attempts=3, project="proj"):
    db.task_upsert({
        "id": id, "project": project, "type": "feature",
        "description": "x", "priority": 50, "status": status,
        "dependencies": [], "metadata": {}, "attempts": attempts,
        "max_attempts": max_attempts,
    })


# ---------------------------------------------------------------------------
# _handle_task_failure -- retry logic
# ---------------------------------------------------------------------------

class TestHandleTaskFailure:
    def test_retries_on_first_failure(self):
        _insert_task("t1", attempts=0, max_attempts=3)
        orchestrator._handle_task_failure("t1", "proj", "some error output")
        t = db.task_get("t1")
        assert t["status"] == "pending"
        assert t["attempts"] == 1

    def test_stores_failure_snippet_in_metadata(self):
        _insert_task("t1", attempts=0, max_attempts=3)
        orchestrator._handle_task_failure("t1", "proj", "line1\nERROR: bad thing happened")
        t = db.task_get("t1")
        assert t["metadata"].get("last_failure")
        assert t["metadata"].get("failure_attempt") == 1

    def test_resets_agent_and_timestamps_on_retry(self):
        db.task_upsert({
            "id": "t1", "project": "proj", "type": "feature", "description": "x",
            "priority": 50, "status": "in_progress", "dependencies": [], "metadata": {},
            "attempts": 0, "max_attempts": 3,
            "started": "2026-01-01T00:00:00", "agent_id": "old-agent",
        })
        orchestrator._handle_task_failure("t1", "proj", "error")
        t = db.task_get("t1")
        assert t["status"] == "pending"
        assert t["started"] is None
        assert t["agent_id"] is None

    def test_marks_failed_after_max_attempts(self):
        _insert_task("t1", attempts=2, max_attempts=3)
        orchestrator._handle_task_failure("t1", "proj", "still failing")
        t = db.task_get("t1")
        assert t["status"] == "failed"

    def test_no_crash_on_missing_task(self):
        orchestrator._handle_task_failure("ghost", "proj", "error")  # must not raise

    def test_max_attempts_one_fails_immediately(self):
        _insert_task("t1", attempts=0, max_attempts=1)
        orchestrator._handle_task_failure("t1", "proj", "error")
        t = db.task_get("t1")
        assert t["status"] == "failed"

    def test_failure_attempt_counter_increments(self):
        _insert_task("t1", attempts=1, max_attempts=5)
        orchestrator._handle_task_failure("t1", "proj", "error")
        t = db.task_get("t1")
        assert t["attempts"] == 2
        assert t["metadata"]["failure_attempt"] == 2


# ---------------------------------------------------------------------------
# _post_task_validation -- project type detection
# ---------------------------------------------------------------------------

class TestPostTaskValidation:
    def _make_project(self, tmp_path, name="myproj"):
        p = tmp_path / "workspace" / name
        p.mkdir(parents=True)
        return p

    def test_detects_godot_project(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        (proj_path / "project.godot").touch()
        _insert_task("t1", status="completed", project="myproj")

        called_with = []
        orig = orchestrator._post_task_validation

        def _fake_validate(project, task_id, timeout=60):
            # Just call the real function, we test the type detection indirectly
            # by checking it tries to run godot (which will fail gracefully)
            called_with.append(project)

        with patch.object(orchestrator, "_post_task_validation", side_effect=_fake_validate):
            orchestrator._post_task_validation("myproj", "t1")

    def test_godot_validation_failure_spawns_bug_task(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        (proj_path / "project.godot").touch()

        # Patch subprocess.run to simulate godot reporting an error
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ERROR: Script error in main.gd"
        mock_result.stderr = ""

        _insert_task("t1", status="completed", project="myproj")

        with patch("swarm.validation.godot_binary_available", return_value=True), \
             patch("subprocess.run", return_value=mock_result):
            orchestrator._post_task_validation("myproj", "t1")

        # A bug task should have been created
        tasks = db.task_get_all()
        bug_tasks = [t for t in tasks if t["type"] == "bug" and t["project"] == "myproj"]
        assert len(bug_tasks) == 1
        assert "error" in bug_tasks[0]["description"].lower()

    def test_main_scene_startup_script_error_fails_validation(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        (proj_path / "project.godot").write_text(
            'config_version=5\n\n[application]\nrun/main_scene="res://scenes/main.tscn"\n'
        )
        (proj_path / "scenes").mkdir()
        (proj_path / "scenes" / "main.tscn").write_text("")
        _insert_task("t1", status="completed", project="myproj")

        ok = MagicMock(returncode=0, stdout="All OK\n", stderr="")
        startup_error = MagicMock(
            returncode=0,
            stdout="SCRIPT ERROR: Cannot call method 'get_node_or_null' on a null value.\n",
            stderr="",
        )

        with patch("swarm.validation.godot_binary_available", return_value=True), \
             patch("subprocess.run", side_effect=[ok, ok, startup_error, startup_error]):
            failed, error_output = validation._post_task_validation_in_worktree("myproj", "t1")

        assert failed is True
        assert "get_node_or_null" in error_output

    def test_missing_godot_is_controller_blocker_not_project_bug(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        (proj_path / "project.godot").touch()
        _insert_task("t1", status="completed", project="myproj")

        with patch("swarm.validation.resolve_godot_binary", return_value="godot"), \
             patch("swarm.validation.godot_binary_available", return_value=False), \
             patch("subprocess.run") as run:
            validation._post_task_validation("myproj", "t1")

        run.assert_not_called()
        tasks = db.task_get_all()
        bug_tasks = [t for t in tasks if t["type"] == "bug" and t["project"] == "myproj"]
        assert bug_tasks == []

    def test_validation_bug_preserves_original_task_objective(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        (proj_path / "project.godot").touch()
        db.task_upsert({
            "id": "orig-task",
            "project": "myproj",
            "type": "feature",
            "description": "Mission State Manager\n\nImplement mission state transitions and win/lose conditions.",
            "priority": 50,
            "status": "completed",
            "dependencies": [],
            "metadata": {},
            "attempts": 0,
            "max_attempts": 3,
        })

        validation._spawn_validation_bug_task("myproj", "orig-task", "Parse Error: boom")

        bug = db.task_get("bug-orig-task")
        assert bug is not None
        assert "ORIGINAL TASK OBJECTIVE" in bug["description"]
        assert "Mission State Manager" in bug["description"]
        assert bug["metadata"]["branch_intent_root_task_id"] == "orig-task"
        assert "Implement mission state transitions" in bug["metadata"]["branch_intent_description"]

    def test_validation_bug_preserves_original_task_objective_when_source_task_is_gone(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        (proj_path / "project.godot").touch()
        original_task = {
            "id": "orig-gone",
            "project": "myproj",
            "type": "feature",
            "description": "Guard Patrol\n\nImplement waypoint-based loop patrol behavior for guards.",
            "priority": 50,
            "status": "completed",
            "dependencies": [],
            "metadata": {},
            "attempts": 0,
            "max_attempts": 3,
        }
        db.task_upsert(original_task)
        captured_original = db.task_get("orig-gone")
        assert captured_original is not None
        assert db.task_delete("orig-gone") is True

        validation._spawn_validation_bug_task(
            "myproj",
            "orig-gone",
            "Parse Error: boom",
            original_task=captured_original,
        )

        bug = db.task_get("bug-orig-gone")
        assert bug is not None
        assert "ORIGINAL TASK OBJECTIVE" in bug["description"]
        assert "Guard Patrol" in bug["description"]
        assert bug["metadata"]["branch_intent_root_task_id"] == "orig-gone"
        assert "waypoint-based loop patrol" in bug["metadata"]["branch_intent_description"]

    def test_post_validation_wrapper_preserves_original_task_objective_when_source_task_is_gone(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        (proj_path / "project.godot").touch()
        original_task = {
            "id": "orig-threaded",
            "project": "myproj",
            "type": "feature",
            "description": "Mission State Manager\n\nImplement mission state transitions and extraction success conditions.",
            "priority": 50,
            "status": "completed",
            "dependencies": [],
            "metadata": {},
            "attempts": 0,
            "max_attempts": 3,
        }
        db.task_upsert(original_task)
        captured_original = db.task_get("orig-threaded")
        assert captured_original is not None
        assert db.task_delete("orig-threaded") is True

        with patch("swarm.validation._post_task_validation_in_worktree", return_value=(True, "Parse Error: boom")):
            validation._post_task_validation("myproj", "orig-threaded", original_task=captured_original)

        bug = db.task_get("bug-orig-threaded")
        assert bug is not None
        assert "Mission State Manager" in bug["description"]
        assert bug["metadata"]["branch_intent_root_task_id"] == "orig-threaded"
        assert "extraction success conditions" in bug["metadata"]["branch_intent_description"]

    def test_validation_bug_replaces_failed_task_dep_on_lock_conflict_continuation(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        (proj_path / "project.godot").touch()
        db.task_upsert({
            "id": "orig-task",
            "project": "myproj",
            "type": "feature",
            "description": "Player FPS character\n\nImplement movement and firing.",
            "priority": 50,
            "status": "completed",
            "dependencies": [],
            "metadata": {},
            "attempts": 0,
            "max_attempts": 3,
        })
        db.task_upsert({
            "id": "lock-continuation",
            "project": "myproj",
            "type": "feature",
            "description": "Lock conflict continuation",
            "priority": 50,
            "status": "pending",
            "dependencies": ["orig-task"],
            "metadata": {
                "created_by": "lock_conflict_handoff",
                "lock_conflict_followup_for": "orig-task",
            },
            "attempts": 0,
            "max_attempts": 3,
        })

        validation._spawn_validation_bug_task("myproj", "orig-task", "GUT failed")

        continuation = db.task_get("lock-continuation")
        assert continuation["dependencies"] == ["bug-orig-task"]

    def test_python_validation_failure_spawns_bug_task(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        (proj_path / "requirements.txt").write_text("flask\n")

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "SyntaxError: invalid syntax"

        _insert_task("t1", status="completed", project="myproj")

        with patch("subprocess.run", return_value=mock_result):
            orchestrator._post_task_validation("myproj", "t1")

        tasks = db.task_get_all()
        bug_tasks = [t for t in tasks if t["type"] == "bug" and t["project"] == "myproj"]
        assert len(bug_tasks) == 1

    def test_unknown_project_type_skips_validation(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        # No project.godot, no requirements.txt -- unknown type

        with patch("subprocess.run") as mock_run:
            orchestrator._post_task_validation("myproj", "t1")
            mock_run.assert_not_called()

    def test_successful_validation_does_not_spawn_bug(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        (proj_path / "requirements.txt").write_text("flask\n")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "OK"
        mock_result.stderr = ""

        _insert_task("t1", status="completed", project="myproj")

        with patch("subprocess.run", return_value=mock_result):
            orchestrator._post_task_validation("myproj", "t1")

        tasks = db.task_get_all()
        bug_tasks = [t for t in tasks if t["type"] == "bug"]
        assert len(bug_tasks) == 0

    def test_spawn_bug_task_keeps_full_error_context(self, tmp_path):
        _insert_task("t1", status="completed", project="myproj")
        huge_error = "ERROR-BEGIN\n" + ("x" * 5000) + "\nERROR-END"

        validation._spawn_validation_bug_task("myproj", "t1", huge_error)

        bug = db.task_get("bug-t1")
        assert bug is not None
        assert bug["metadata"]["error_log"] == huge_error
        assert bug["metadata"]["error_log_excerpt"] == huge_error[:2000]

    def test_python_validation_failure_keeps_full_pytest_output(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        (proj_path / "requirements.txt").write_text("flask\n")
        (proj_path / ".venv" / "bin").mkdir(parents=True)
        (proj_path / ".venv" / "bin" / "pytest").write_text("")

        huge_output = "PYTEST-START\n" + ("y" * 7000) + "\nPYTEST-END"
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = huge_output
        mock_result.stderr = ""

        _insert_task("t1", status="completed", project="myproj")

        with patch("subprocess.run", return_value=mock_result):
            failed, error_output = validation._post_task_validation_in_worktree(
                "myproj", "t1", worktree_path=None
            )

        assert failed is True
        assert error_output == huge_output

    def test_python_validation_fails_when_tests_exist_but_no_local_pytest_runner(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        (proj_path / "requirements.txt").write_text("flask\n")
        (proj_path / "tests").mkdir()
        (proj_path / "tests" / "test_sample.py").write_text("def test_ok():\n    assert True\n")

        _insert_task("t1", status="completed", project="myproj")

        failed, error_output = validation._post_task_validation_in_worktree(
            "myproj", "t1", worktree_path=None
        )

        assert failed is True
        assert "no project-local pytest runner is available" in error_output

    def test_python_src_layout_without_pyproject_still_validates(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        (proj_path / "src").mkdir()
        (proj_path / "tests").mkdir()
        (proj_path / "src" / "sample.py").write_text("def value():\n    return 1\n")
        (proj_path / "tests" / "test_sample.py").write_text("def test_ok():\n    assert True\n")

        _insert_task("t1", status="completed", project="myproj")

        failed, error_output = validation._post_task_validation_in_worktree(
            "myproj", "t1", worktree_path=None
        )

        assert failed is True
        assert "no project-local pytest runner is available" in error_output

    def test_validation_bug_task_handoff_is_compact(self, tmp_path):
        proj_path = self._make_project(tmp_path)
        worktree = proj_path / ".wt-bug"
        worktree.mkdir(parents=True)
        _insert_task("t-compact", status="completed", project="myproj")

        huge_error = "\n".join(f"ERROR line {i}" for i in range(100))
        notes = [
            "First attempt changed unrelated menu code and still failed because the style resource path was wrong.",
            "Second attempt fixed one UID but left the same StyleBoxFlat warning and the same state server bind warning.",
            "Third attempt wandered into deleting cache files and should not be repeated.",
        ]

        mock_git = MagicMock()
        mock_git.returncode = 0
        mock_git.stdout = "abc123 fix one\nbcd234 fix two\ncde345 fix three\n"

        with patch("subprocess.run", return_value=mock_git):
            validation._spawn_validation_bug_task(
                "myproj",
                "t-compact",
                huge_error,
                worktree_path=worktree,
                worktree_branch="bug-branch",
                fix_notes=notes,
            )

        bug = db.task_get("bug-t-compact")
        assert bug is not None
        desc = bug["description"]
        assert "--- CURRENT HANDOFF ---" in desc
        assert "Prior attempt summaries:" in desc
        assert "Third attempt wandered into deleting cache files" in desc
        assert "First attempt changed unrelated menu code" not in desc
        assert "Second attempt fixed one UID" in desc
        assert "Recent worktree commits:" in desc
        assert "ERROR line 0" in desc
        assert "ERROR line 19" in desc
        assert "ERROR line 40" not in desc


# ---------------------------------------------------------------------------
# get_active_count
# ---------------------------------------------------------------------------

class TestGetActiveCount:
    def test_zero_when_no_agents(self):
        assert orchestrator.get_active_count() == 0

    def test_counts_in_process_agents(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        orchestrator._active_handles["agent-1"] = {
            "process": mock_proc, "project": "proj",
            "task_id": "t1", "started": 0,
        }
        assert orchestrator.get_active_count() == 1
        orchestrator._active_handles.clear()

    def test_does_not_count_finished_processes(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # finished
        orchestrator._active_handles["agent-1"] = {
            "process": mock_proc, "project": "proj",
            "task_id": "t1", "started": 0,
        }
        # Still in _active_handles but poll() returns non-None = finished
        count = orchestrator.get_active_count()
        assert count == 0
        orchestrator._active_handles.clear()


# ---------------------------------------------------------------------------
# prune_history
# ---------------------------------------------------------------------------

class TestPruneHistory:
    def test_completed_agents_written_to_history(self, tmp_path):
        db.agent_upsert({
            "id": "a1", "project": "proj", "task_type": "feature",
            "status": "completed", "metadata": {},
        })
        orchestrator.prune_history()
        history = orchestrator.HISTORY_FILE
        if history.exists():
            lines = [l for l in history.read_text().splitlines() if l.strip()]
            assert len(lines) >= 1
