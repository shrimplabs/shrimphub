"""Tests for the _finish_agent pipeline phase functions in swarm.agent_finish.

Covers:
- _phase_release_file_locks: safety-net unlock, errors are swallowed
- _phase_reparent_continuation: dependency reparenting when continuation detected
- _phase_complete_task: task marked completed, stale recovery cancelled, webhook fired
- _phase_handle_worktree_validation_failure: stale vs. real regression, config blocker
- _phase_post_completion_pipeline: plan validation failure suppresses auto-tasks
- _finish_agent ordering: agent finished before task completion; project lock released
"""

import json
import threading

from unittest.mock import MagicMock, patch

import pytest

from swarm import db
import swarm.agent_lifecycle as lifecycle
import swarm.agent_finish as af


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db_path = tmp_path / "data" / "swarm.db"
    db_path.parent.mkdir(parents=True)
    db.init(db_path)
    lifecycle.DATA_DIR = tmp_path / "data"
    lifecycle._configured = True
    lifecycle.WORKSPACE = tmp_path / "ws"
    lifecycle.LOCK_PROJECT = False
    lifecycle._project_registry = None
    yield tmp_path
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None
    lifecycle._configured = False
    # Restore lazy-import slots so later tests get the real modules,
    # not any MagicMock injected by these tests.
    lifecycle._validation = None
    lifecycle._learnings = None


def _seed_task(task_id="t-1", project="proj", task_type="feature", status="in_progress", metadata=None):
    db.project_upsert({"name": project, "status": "active"})
    db.task_upsert({
        "id": task_id, "project": project, "type": task_type,
        "description": "d", "priority": 50, "status": status,
        "dependencies": [], "metadata": metadata or {}, "attempts": 0, "max_attempts": 3,
    })


def _seed_agent(agent_id, task_id, project="proj"):
    db.agent_upsert({
        "id": agent_id, "project": project, "task_type": "feature",
        "status": "active", "spawned_at": "2026-01-01T00:00:00",
        "pid": 99999, "log_path": "", "script_path": "",
        "task_id": task_id,
    })


# ---------------------------------------------------------------------------
# _phase_release_file_locks
# ---------------------------------------------------------------------------

class TestPhaseReleaseFileLocks:
    def test_no_op_when_no_project_registry(self, isolated_db):
        # Should not raise when _project_registry is None.
        lifecycle._project_registry = None
        af._phase_release_file_locks("agent-1", "proj")  # no error

    def test_calls_unlock_all_for_agent(self, isolated_db):
        mock_registry = MagicMock()
        mock_registry.unlock_all_for_agent.return_value = ["file.gd"]
        lifecycle._project_registry = mock_registry
        af._phase_release_file_locks("agent-1", "proj")
        mock_registry.unlock_all_for_agent.assert_called_once_with("proj", "agent-1")

    def test_swallows_exception_from_registry(self, isolated_db):
        mock_registry = MagicMock()
        mock_registry.unlock_all_for_agent.side_effect = RuntimeError("boom")
        lifecycle._project_registry = mock_registry
        af._phase_release_file_locks("agent-1", "proj")  # should not raise

    def test_no_op_when_project_is_none(self, isolated_db):
        mock_registry = MagicMock()
        lifecycle._project_registry = mock_registry
        af._phase_release_file_locks("agent-1", None)
        mock_registry.unlock_all_for_agent.assert_not_called()


# ---------------------------------------------------------------------------
# _phase_reparent_continuation
# ---------------------------------------------------------------------------

class TestPhaseReparentContinuation:
    def test_no_continuation_in_log_returns_false(self, isolated_db):
        _seed_task("t-orig")
        result = af._phase_reparent_continuation("t-orig", "nothing relevant here")
        assert result is False

    def test_continuation_found_returns_true(self, isolated_db):
        _seed_task("t-orig")
        _seed_task("t-cont")
        _seed_task("t-dep")
        db.task_update("t-dep", {"dependencies": ["t-orig"]})

        result = af._phase_reparent_continuation("t-orig", "Continuation task created: t-cont")
        assert result is True

    def test_dependents_reparented_to_continuation(self, isolated_db):
        _seed_task("t-orig")
        _seed_task("t-cont")
        _seed_task("t-dep")
        db.task_update("t-dep", {"dependencies": ["t-orig"]})

        af._phase_reparent_continuation("t-orig", "Continuation task created: t-cont")

        dep_task = db.task_get("t-dep")
        assert dep_task["dependencies"] == ["t-cont"]

    def test_continuation_keeps_dependency_on_original(self, isolated_db):
        _seed_task("t-orig")
        _seed_task("t-cont")
        db.task_update("t-cont", {"dependencies": ["t-orig"]})

        af._phase_reparent_continuation("t-orig", "Continuation task created: t-cont")

        cont_task = db.task_get("t-cont")
        assert cont_task["dependencies"] == ["t-orig"]

    def test_non_dependent_tasks_unchanged(self, isolated_db):
        _seed_task("t-orig")
        _seed_task("t-cont")
        _seed_task("t-unrelated")
        db.task_update("t-unrelated", {"dependencies": ["t-other"]})

        af._phase_reparent_continuation("t-orig", "Continuation task created: t-cont")

        t = db.task_get("t-unrelated")
        assert t["dependencies"] == ["t-other"]

    def test_continuation_id_parsed_with_parenthesis_suffix(self, isolated_db):
        _seed_task("t-orig")
        _seed_task("t-cont")
        _seed_task("t-dep")
        db.task_update("t-dep", {"dependencies": ["t-orig"]})

        af._phase_reparent_continuation(
            "t-orig",
            "Continuation task created: t-cont (will resume after merge)",
        )
        dep_task = db.task_get("t-dep")
        assert dep_task["dependencies"] == ["t-cont"]


# ---------------------------------------------------------------------------
# _phase_complete_task
# ---------------------------------------------------------------------------

class TestPhaseCompleteTask:
    def test_task_marked_completed(self, isolated_db):
        _seed_task("t-1")
        af._phase_complete_task("t-1", "proj", "")
        task = db.task_get("t-1")
        assert task["status"] == "completed"

    def test_returns_pre_complete_snapshot(self, isolated_db):
        _seed_task("t-1")
        snapshot = af._phase_complete_task("t-1", "proj", "")
        # Snapshot was taken before status was changed to 'completed'.
        assert snapshot is not None
        assert snapshot["id"] == "t-1"
        assert snapshot["status"] == "in_progress"

    def test_stale_recovery_task_cancelled(self, isolated_db):
        _seed_task("t-orig")
        _seed_task("t-rec", task_type="feature", status="pending")
        db.task_update("t-rec", {"metadata": {"is_recovery_task": True, "failed_task_id": "t-orig"}})

        af._phase_complete_task("t-orig", "proj", "")

        rec = db.task_get("t-rec")
        assert rec["status"] == "cancelled"

    def test_unrelated_recovery_task_not_cancelled(self, isolated_db):
        _seed_task("t-orig")
        _seed_task("t-other-orig")
        _seed_task("t-rec", status="pending")
        db.task_update("t-rec", {"metadata": {"is_recovery_task": True, "failed_task_id": "t-other-orig"}})

        af._phase_complete_task("t-orig", "proj", "")

        rec = db.task_get("t-rec")
        assert rec["status"] == "pending"

    def test_webhook_fired_on_completion(self, isolated_db):
        _seed_task("t-1")
        with patch.object(lifecycle, "_fire_task_webhook") as mock_wh:
            af._phase_complete_task("t-1", "proj", "2 files changed")
            mock_wh.assert_called_once()
            call_kwargs = mock_wh.call_args
            assert call_kwargs[0][0] == "task_completed"


# ---------------------------------------------------------------------------
# _phase_handle_worktree_validation_failure
# ---------------------------------------------------------------------------

class TestPhaseHandleWorktreeValidationFailure:
    def _make_validation_mock(self, main_failed=False, is_blocker=False):
        mock_v = MagicMock()
        mock_v._post_task_validation_in_worktree.return_value = (main_failed, "err" if main_failed else "")
        mock_v.is_controller_config_blocker.return_value = is_blocker
        mock_v._llm_summarise_fix_attempt.return_value = "summary"
        return mock_v

    def test_stale_worktree_error_task_marked_completed(self, isolated_db, tmp_path):
        _seed_task("t-1")
        lifecycle._validation = self._make_validation_mock(main_failed=False)

        af._phase_handle_worktree_validation_failure(
            "agent-1", "t-1", "proj",
            str(tmp_path / "wt"), "wt-branch",
            "validation error", None,
        )

        task = db.task_get("t-1")
        assert task["status"] == "completed"

    def test_real_regression_spawns_bug_task(self, isolated_db, tmp_path):
        _seed_task("t-1")
        mock_v = self._make_validation_mock(main_failed=True)
        lifecycle._validation = mock_v

        af._phase_handle_worktree_validation_failure(
            "agent-1", "t-1", "proj",
            str(tmp_path / "wt"), "wt-branch",
            "validation error output", None,
        )

        mock_v._spawn_validation_bug_task.assert_called_once()
        task = db.task_get("t-1")
        assert task["status"] == "completed"

    def test_config_blocker_calls_handle_task_failure(self, isolated_db, tmp_path):
        _seed_task("t-1")
        mock_v = self._make_validation_mock(main_failed=True, is_blocker=True)
        lifecycle._validation = mock_v

        import swarm.agent_recovery as recovery
        with patch.object(recovery, "_handle_task_failure") as mock_htf:
            af._phase_handle_worktree_validation_failure(
                "agent-1", "t-1", "proj",
                str(tmp_path / "wt"), "wt-branch",
                "blocker error", None,
            )
            mock_htf.assert_called_once()


# ---------------------------------------------------------------------------
# _finish_agent ordering: agent marked finished before task completion
# ---------------------------------------------------------------------------

class TestFinishAgentOrdering:
    """Verify that the agent is marked finished in the DB before the task is
    marked completed (ordering requirement for monitor thread safety)."""

    def test_agent_finished_before_task_completed(self, isolated_db, tmp_path):
        _seed_task("t-1")
        _seed_agent("agent-1", "t-1")

        log_path = tmp_path / "agent.log"
        log_path.write_text("TASK_COMPLETE\n")

        completion_order = []

        original_agent_update = db.agent_update_status
        original_task_update = db.task_update_status

        def tracking_agent_update(agent_id, status, **kw):
            completion_order.append(("agent", status))
            return original_agent_update(agent_id, status, **kw)

        def tracking_task_update(task_id, status, **kw):
            completion_order.append(("task", status))
            return original_task_update(task_id, status, **kw)

        with patch.object(db, "agent_update_status", side_effect=tracking_agent_update), \
             patch.object(db, "task_update_status", side_effect=tracking_task_update), \
             patch("swarm.agent_finish._finish_worktree_phase",
                   return_value=af._WorktreeFinishResult(success=True)), \
             patch("swarm.agent_finish._capture_project_diff_stat", return_value=""), \
             patch("swarm.agent_finish._read_agent_token_usage", return_value=(0, 0, 0, 0, 0, "", "")), \
             patch("swarm.agent_finish._phase_post_completion_pipeline"):
            af._finish_agent(
                agent_id="agent-1",
                exit_code=0,
                project="proj",
                task_id="t-1",
                script_path=None,
                log_path=str(log_path),
            )

        # Agent must be marked finished before task is marked completed.
        agent_idx = next(i for i, e in enumerate(completion_order) if e[0] == "agent")
        task_idx = next(i for i, e in enumerate(completion_order) if e[0] == "task" and e[1] == "completed")
        assert agent_idx < task_idx, f"Expected agent before task; got: {completion_order}"

    def test_continuation_handoff_completes_original_instead_of_retrying(self, isolated_db, tmp_path):
        _seed_task("t-orig")
        _seed_task("t-cont", status="pending")
        db.task_update("t-cont", {"dependencies": ["t-orig"]})
        _seed_task("t-downstream", status="pending")
        db.task_update("t-downstream", {"dependencies": ["t-orig"]})
        _seed_agent("agent-1", "t-orig")

        log_path = tmp_path / "agent.log"
        log_path.write_text(
            "[Agent] Progress saved\n"
            "[Agent] Continuation task created: t-cont\n"
            "[Agent] Task ended without TASK_COMPLETE -- marking as failed\n"
        )

        import swarm.agent_recovery as recovery
        with patch.object(recovery, "_handle_task_failure") as mock_failure, \
             patch("swarm.agent_finish._finish_worktree_phase",
                   return_value=af._WorktreeFinishResult(success=True)), \
             patch("swarm.agent_finish._capture_project_diff_stat", return_value=""), \
             patch("swarm.agent_finish._read_agent_token_usage", return_value=(0, 0, 0, 0, 0, "", "")), \
             patch("swarm.agent_finish._phase_post_completion_pipeline"):
            af._finish_agent(
                agent_id="agent-1",
                exit_code=1,
                project="proj",
                task_id="t-orig",
                script_path=None,
                log_path=str(log_path),
            )

        mock_failure.assert_not_called()
        assert db.task_get("t-orig")["status"] == "completed"
        assert db.task_get("t-cont")["dependencies"] == ["t-orig"]
        assert db.task_get("t-downstream")["dependencies"] == ["t-cont"]
        assert db.agent_get("agent-1")["status"] == "completed"

    def test_experiment_metrics_records_terminal_task_status(self, isolated_db, tmp_path):
        _seed_task("t-1", metadata={
            "experiment_id": "exp-1",
            "experiment_variant": "variant-a",
            "pipeline_variant": ["work", "validate"],
            "source_project": "source-proj",
            "source_task_id": "source-task-1",
        })
        _seed_agent("agent-1", "t-1")

        log_path = tmp_path / "agent.log"
        log_path.write_text("TASK_COMPLETE\n")

        with patch("swarm.agent_finish._finish_worktree_phase",
                   return_value=af._WorktreeFinishResult(success=True)), \
             patch("swarm.agent_finish._capture_project_diff_stat", return_value=""), \
             patch("swarm.agent_finish._read_agent_token_usage", return_value=(0, 0, 0, 0, 0, "", "")), \
             patch("swarm.agent_finish._phase_post_completion_pipeline"):
            af._finish_agent(
                agent_id="agent-1",
                exit_code=0,
                project="proj",
                task_id="t-1",
                script_path=None,
                log_path=str(log_path),
            )

        metrics_path = lifecycle.DATA_DIR / "experiment_metrics.jsonl"
        records = [json.loads(line) for line in metrics_path.read_text().splitlines()]
        assert records[-1]["status"] == "completed"

    def test_project_lock_released_after_task_completion(self, isolated_db, tmp_path):
        _seed_task("t-1")
        _seed_agent("agent-1", "t-1")
        lifecycle.LOCK_PROJECT = True
        db.project_upsert({"name": "proj", "status": "active"})
        db.project_set_locked("proj", True)

        log_path = tmp_path / "agent.log"
        log_path.write_text("TASK_COMPLETE\n")

        with patch("swarm.agent_finish._finish_worktree_phase",
                   return_value=af._WorktreeFinishResult(success=True)), \
             patch("swarm.agent_finish._capture_project_diff_stat", return_value=""), \
             patch("swarm.agent_finish._read_agent_token_usage", return_value=(0, 0, 0, 0, 0, "", "")), \
             patch("swarm.agent_finish._phase_post_completion_pipeline"):
            af._finish_agent(
                agent_id="agent-1",
                exit_code=0,
                project="proj",
                task_id="t-1",
                script_path=None,
                log_path=str(log_path),
            )

        proj = db.project_get("proj")
        assert not proj.get("locked"), "Project lock should be released"

    def test_script_file_deleted_on_completion(self, isolated_db, tmp_path):
        _seed_task("t-1")
        _seed_agent("agent-1", "t-1")

        script_path = tmp_path / "agent.py"
        script_path.write_text("# script")
        log_path = tmp_path / "agent.log"
        log_path.write_text("TASK_COMPLETE\n")

        with patch("swarm.agent_finish._finish_worktree_phase",
                   return_value=af._WorktreeFinishResult(success=True)), \
             patch("swarm.agent_finish._capture_project_diff_stat", return_value=""), \
             patch("swarm.agent_finish._read_agent_token_usage", return_value=(0, 0, 0, 0, 0, "", "")), \
             patch("swarm.agent_finish._phase_post_completion_pipeline"):
            af._finish_agent(
                agent_id="agent-1",
                exit_code=0,
                project="proj",
                task_id="t-1",
                script_path=str(script_path),
                log_path=str(log_path),
            )

        assert not script_path.exists(), "Script file should be deleted after completion"

    def test_post_completion_not_called_on_failure(self, isolated_db, tmp_path):
        _seed_task("t-1")
        _seed_agent("agent-1", "t-1")

        log_path = tmp_path / "agent.log"
        log_path.write_text("Agent failed\n")

        with patch("swarm.agent_finish._finish_worktree_phase",
                   return_value=af._WorktreeFinishResult(success=False)), \
             patch("swarm.agent_finish._capture_project_diff_stat", return_value=""), \
             patch("swarm.agent_finish._read_agent_token_usage", return_value=(0, 0, 0, 0, 0, "", "")), \
             patch("swarm.agent_finish._phase_post_completion_pipeline") as mock_post, \
             patch("swarm.agent_recovery._handle_task_failure"):
            af._finish_agent(
                agent_id="agent-1",
                exit_code=1,
                project="proj",
                task_id="t-1",
                script_path=None,
                log_path=str(log_path),
            )

        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# _phase_post_completion_pipeline: plan validation failure suppresses auto-tasks
# ---------------------------------------------------------------------------

class TestPhasePostCompletionPipeline:
    def test_plan_validation_failure_suppresses_auto_tasks(self, isolated_db, tmp_path):
        """When project_plan validation fails, auto-tasks must not be spawned."""
        _seed_task("t-plan", task_type="project_plan")

        with patch("swarm.agent_recovery._validate_project_plan_subtasks",
                   return_value=["error: missing dep"]), \
             patch("swarm.agent_recovery._project_plan_subtasks", return_value=[]), \
             patch("swarm.agent_finish._phase_post_completion_pipeline") as _pp:
            # Call the real function but intercept auto_spawn_qa_task to verify it's never reached.
            pass

        # Direct test: call _phase_post_completion_pipeline with a project_plan task that fails validation.
        _seed_task("t-plan2", task_type="project_plan")
        lifecycle._learnings = MagicMock()
        lifecycle._validation = MagicMock()

        auto_calls = []

        with patch("swarm.agent_recovery._validate_project_plan_subtasks",
                   return_value=["dep error"]), \
             patch("swarm.agent_recovery._project_plan_subtasks", return_value=[]), \
             patch("swarm.agent_recovery._handle_task_failure"), \
             patch("swarm.agent_auto_tasks.auto_spawn_qa_task",
                   side_effect=lambda **kw: auto_calls.append("qa")), \
             patch("swarm.agent_auto_tasks.auto_spawn_integration_task",
                   side_effect=lambda **kw: auto_calls.append("integration")), \
             patch("swarm.agent_auto_tasks.auto_handle_sprint_qa",
                   side_effect=lambda **kw: auto_calls.append("sprint_qa")):
            af._phase_post_completion_pipeline(
                agent_id="agent-1",
                task_id="t-plan2",
                project="proj",
                full_output="",
                diff_stat="",
                task_snapshot_pre_complete=None,
                wt_path_str=None,
                spawned_continuation=False,
                task_snapshot_early=None,
                exit_code=0,
                log_path=None,
            )

        assert auto_calls == [], f"Auto-tasks should not be spawned after plan validation failure, got: {auto_calls}"


# ---------------------------------------------------------------------------
# _build_completion_evidence — attributed diff/commit evidence
# ---------------------------------------------------------------------------

import subprocess as _sp


def _git(cwd, *args):
    _sp.run(["git", *args], cwd=str(cwd), check=True,
            capture_output=True, text=True)


class TestBuildCompletionEvidence:
    def _init_repo(self, path):
        path.mkdir(parents=True, exist_ok=True)
        _git(path, "init", "-q")
        _git(path, "config", "user.email", "t@t.t")
        _git(path, "config", "user.name", "t")
        (path / "seed.txt").write_text("seed")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "seed")

    def test_no_head_at_spawn_returns_unattributed(self, isolated_db):
        ev = af._build_completion_evidence("proj", None, True, 0, True)
        assert ev["attributed"] is False
        assert ev["new_commits"] == 0

    def test_zero_new_commits_when_head_unchanged(self, isolated_db):
        repo = lifecycle.WORKSPACE / "proj"
        self._init_repo(repo)
        head = _sp.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                       capture_output=True, text=True).stdout.strip()
        ev = af._build_completion_evidence("proj", head, True, 0, True)
        assert ev["attributed"] is True
        assert ev["new_commits"] == 0
        assert ev["commit_hash"] == head

    def test_counts_new_commits_since_spawn(self, isolated_db):
        repo = lifecycle.WORKSPACE / "proj"
        self._init_repo(repo)
        spawn_head = _sp.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                             capture_output=True, text=True).stdout.strip()
        # Agent makes a real commit after spawn.
        (repo / "feature.txt").write_text("work")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "feat: real work")
        ev = af._build_completion_evidence("proj", spawn_head, True, 0, True)
        assert ev["attributed"] is True
        assert ev["new_commits"] == 1
        assert "feature.txt" in ev["diff_stat"]
