"""Containment breaker (swarm-controller-2z0n, L3).

A spike of guard-blocked calls across agents suspends auto-mode and puts the
offending tasks on hold for human review.  Only an operator can release a
task or re-enable auto-mode; agent tokens cannot clear the hold.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from swarm import containment_breaker as cb
from swarm import db
from tests.test_api_authz import H, OWN, _make_app, _seed, _teardown


@pytest.fixture()
def app(tmp_path):
    flask_app, saved = _make_app(tmp_path, {"containment_breaker_enabled": True, "containment_spike_threshold": 5,
                                            "containment_spike_window_minutes": 60})
    _seed()
    yield flask_app
    _teardown(saved)


@pytest.fixture()
def client(app):
    return app.test_client()


def _signal(agent_id: str, task_id: str, blocked: dict, *, minutes_ago: float = 1.0, project: str = OWN):
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    db.agent_signals_upsert({
        "agent_id": agent_id, "task_id": task_id, "project": project, "task_type": "feature",
        "extracted_at": at, "terminal_status": "complete", "tool_sequence": "[]",
        "blocked_calls": json.dumps(blocked), "outbound_hosts": "{}",
    })


def _meta(task_id: str) -> dict:
    return db.task_get(task_id).get("metadata") or {}


# ---------------------------------------------------------------------------
# Pure decision + config
# ---------------------------------------------------------------------------

class TestEvaluate:
    def test_below_threshold_does_not_trip(self):
        rows = [{"agent_id": "a", "task_id": "t", "blocked_calls": '{"shell": 4}'}]
        assert cb.evaluate(rows, threshold=5) is None

    def test_sums_across_agents_and_orders_offenders(self):
        rows = [
            {"agent_id": "a", "task_id": "t1", "blocked_calls": '{"shell": 2}'},
            {"agent_id": "b", "task_id": "t2", "blocked_calls": '{"path": 3, "fetch": 1}'},
            {"agent_id": "c", "task_id": "t3", "blocked_calls": "{}"},
            {"agent_id": "d", "task_id": "t4", "blocked_calls": None},
        ]
        trip = cb.evaluate(rows, threshold=5)
        assert trip["total"] == 6
        assert [o["task_id"] for o in trip["offenders"]] == ["t2", "t1"]

    def test_malformed_rows_are_ignored(self):
        rows = [{"agent_id": "a", "task_id": "t", "blocked_calls": "not json"},
                {"agent_id": "b", "task_id": "t", "blocked_calls": '{"shell": "x", "path": -3}'}]
        assert cb.evaluate(rows, threshold=1) is None

    def test_settings_defaults_and_bad_values(self):
        # Off unless opted in: the breaker is a debugging aid, the guards always run.
        assert cb.settings({}) == (False, cb.DEFAULT_THRESHOLD, cb.DEFAULT_WINDOW_MINUTES)
        assert cb.settings({"containment_breaker_enabled": True})[0] is True
        assert cb.settings({"containment_breaker_enabled": True, "containment_spike_threshold": "junk",
                            "containment_spike_window_minutes": 0}) == (True, cb.DEFAULT_THRESHOLD, 1)


# ---------------------------------------------------------------------------
# check(): windowing, flagging, acknowledgement
# ---------------------------------------------------------------------------

class TestCheck:
    CFG = {"containment_breaker_enabled": True, "containment_spike_threshold": 5, "containment_spike_window_minutes": 60}

    def test_synthetic_spike_flags_offending_tasks(self, app):
        _signal("ag-1", "task-own-1", {"shell": 3, "path": 1})
        _signal("ag-2", "task-own-2", {"fetch": 2})
        trip = cb.check(db, self.CFG)
        assert trip and trip["total"] == 6
        assert sorted(trip["flagged"]) == ["task-own-1", "task-own-2"]
        meta = _meta("task-own-1")
        assert meta[cb.HOLD_KEY] is True
        assert meta["needs_human_review"] is True
        assert "path=1, shell=3" in meta["human_review_reason"]
        assert meta["containment_agent_id"] == "ag-1"

    def test_rows_outside_window_do_not_count(self, app):
        _signal("ag-1", "task-own-1", {"shell": 10}, minutes_ago=120)
        assert cb.check(db, self.CFG) is None
        assert not cb.is_held(db.task_get("task-own-1"))

    def test_disabled_never_trips(self, app):
        _signal("ag-1", "task-own-1", {"shell": 50})
        assert cb.check(db, {**self.CFG, "containment_breaker_enabled": False}) is None

    def test_ack_excludes_rows_seen_before_it(self, app):
        _signal("ag-1", "task-own-1", {"shell": 10}, minutes_ago=5)
        ack = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        assert cb.check(db, {**self.CFG, "containment_ack_at": ack}) is None

    def test_released_task_is_not_reflagged_for_same_agent(self, app):
        _signal("ag-1", "task-own-1", {"shell": 10})
        assert cb.check(db, self.CFG)["flagged"] == ["task-own-1"]
        cb.release(db, "task-own-1")
        # Reviewed blocks no longer count at all: no re-trip, no re-hold.
        assert cb.check(db, self.CFG) is None
        assert not cb.is_held(db.task_get("task-own-1"))

    def _live_log(self, tmp_path, agent_id, task_id, kinds):
        log = tmp_path / f"agent_{agent_id}.log"
        log.write_text("".join(
            f"[Agent] Executing tool: run_command\n[Agent] [GUARD] blocked kind={k} tool=run_command detail='x'\n"
            for k in kinds))
        db.agent_upsert({"id": agent_id, "project": OWN, "task_type": "feature", "status": "active",
                         "task_id": task_id, "pid": 0, "log_path": str(log)})

    def test_running_agent_blocks_count_before_it_finishes(self, app, tmp_path):
        # No agent_signals row yet -- the agent is still running.
        self._live_log(tmp_path, "ag-live", "task-own-2", ["shell"] * 4 + ["path"])
        trip = cb.check(db, self.CFG)
        assert trip and trip["total"] == 5
        assert trip["flagged"] == ["task-own-2"]
        assert trip["running"] == ["ag-live"]
        assert cb.is_held(db.task_get("task-own-2"))

    def test_live_and_finished_blocks_sum(self, app, tmp_path):
        _signal("ag-done", "task-own-1", {"shell": 3})
        self._live_log(tmp_path, "ag-live", "task-own-2", ["shell", "python"])
        trip = cb.check(db, self.CFG)
        assert trip and trip["total"] == 5
        assert trip["running"] == ["ag-live"]

    def test_finished_agent_is_not_double_counted(self, app, tmp_path):
        # Signals extracted but the agent row still says active (finish race).
        self._live_log(tmp_path, "ag-1", "task-own-1", ["shell"] * 3)
        _signal("ag-1", "task-own-1", {"shell": 3})
        assert cb.check(db, self.CFG) is None

    def test_released_live_agent_is_not_retripped(self, app, tmp_path):
        self._live_log(tmp_path, "ag-live", "task-own-2", ["shell"] * 6)
        assert cb.check(db, self.CFG)["flagged"] == ["task-own-2"]
        cb.release(db, "task-own-2")
        assert cb.check(db, self.CFG) is None

    def test_missing_live_log_is_ignored(self, app, tmp_path):
        db.agent_upsert({"id": "ag-x", "project": OWN, "task_type": "feature", "status": "active",
                         "task_id": "task-own-2", "pid": 0, "log_path": str(tmp_path / "gone.log")})
        assert cb.check(db, self.CFG) is None

    def test_check_never_raises(self):
        class Broken:
            def agent_signals_since(self, _):
                raise RuntimeError("db gone")
        assert cb.check(Broken(), self.CFG) is None


# ---------------------------------------------------------------------------
# Monitor tick: auto-mode suspension + operator acknowledgement
# ---------------------------------------------------------------------------

class TestAutoModeSuspension:
    def test_spike_suspends_auto_mode_and_quota_ok_does_not_resume(self, app, client):
        assert client.post("/api/auto-mode", json={"enabled": True}).status_code == 200
        _signal("ag-1", "task-own-1", {"shell": 6})
        app.extensions["containment_tick"]()

        state = client.get("/api/auto-mode").get_json()
        assert state["enabled"] is False
        assert state["suspended_for_containment"] is True
        assert state["containment_trip"]["flagged"] == ["task-own-1"]
        assert json.loads(open(app.config["CONFIG_FILE"]).read())[
            "auto_mode_enabled"] is False

    def test_operator_reenable_acknowledges_and_stops_retripping(self, app, client):
        _signal("ag-1", "task-own-1", {"shell": 6})
        app.extensions["containment_tick"]()
        r = client.post("/api/auto-mode", json={"enabled": True})
        assert r.status_code == 200
        state = client.get("/api/auto-mode").get_json()
        assert state["enabled"] is True and state["suspended_for_containment"] is False
        saved = json.loads(open(app.config["CONFIG_FILE"]).read())
        assert saved["containment_ack_at"]

        app.extensions["containment_tick"]()  # same rows: now older than the ack
        assert client.get("/api/auto-mode").get_json()["enabled"] is True

    def test_below_threshold_leaves_auto_mode_alone(self, app, client):
        client.post("/api/auto-mode", json={"enabled": True})
        _signal("ag-1", "task-own-1", {"shell": 4})
        app.extensions["containment_tick"]()
        assert client.get("/api/auto-mode").get_json()["enabled"] is True


# ---------------------------------------------------------------------------
# Held tasks: never scheduled or spawned; only operators release them
# ---------------------------------------------------------------------------

class TestHold:
    def _hold(self):
        _signal("ag-1", "task-own-1", {"shell": 6})
        cb.check(db, {"containment_breaker_enabled": True, "containment_spike_threshold": 5})
        assert cb.is_held(db.task_get("task-own-1"))

    def test_scheduler_skips_held_task(self, app):
        import swarm.orchestrator as orc
        self._hold()
        db.task_update("task-own-2", {"status": "completed"})
        db.task_update("task-other-1", {"status": "completed"})
        assert orc._get_next_task() is None
        cb.release(db, "task-own-1")
        assert orc._get_next_task()["id"] == "task-own-1"

    def test_manual_spawn_of_held_task_is_refused(self, app, client):
        self._hold()
        r = client.post("/api/spawn", json={"task_id": "task-own-1", "force": True})
        assert r.status_code == 409
        assert "containment hold" in r.get_json()["error"]

    def test_agent_cannot_clear_hold_via_patch(self, app, client):
        self._hold()
        r = client.patch("/api/tasks/task-own-1", json={"metadata": {}}, headers=H)
        assert r.status_code == 403
        meta = dict(_meta("task-own-1"))
        meta.pop(cb.HOLD_KEY)
        r = client.patch("/api/tasks/task-own-1", json={"metadata": meta}, headers=H)
        assert r.status_code == 403
        assert cb.is_held(db.task_get("task-own-1"))

    def test_agent_metadata_patch_preserving_hold_is_allowed(self, app, client):
        self._hold()
        meta = {**_meta("task-own-1"), "helper_delegations": []}
        r = client.patch("/api/tasks/task-own-1", json={"metadata": meta}, headers=H)
        assert r.status_code == 200, r.get_json()

    def test_agent_cannot_set_hold_fields_on_other_task(self, app, client):
        r = client.patch("/api/tasks/task-own-2", json={"metadata": {"needs_human_review": False,
                                                                      cb.HOLD_KEY: True}}, headers=H)
        assert r.status_code == 403

    def test_agent_cannot_call_release(self, app, client):
        self._hold()
        r = client.post("/api/tasks/task-own-1/containment-release", headers=H)
        assert r.status_code == 403
        assert cb.is_held(db.task_get("task-own-1"))

    def test_operator_release(self, app, client):
        self._hold()
        r = client.post("/api/tasks/task-own-1/containment-release")
        assert r.status_code == 200
        meta = _meta("task-own-1")
        assert cb.HOLD_KEY not in meta and meta["needs_human_review"] is False
        assert client.post("/api/tasks/missing/containment-release").status_code == 404


def test_breaker_is_a_protected_guardrail_file(monkeypatch):
    from swarm.tools import path_guard
    target = path_guard._SWARM_CONTROLLER_ROOT / "swarm" / "containment_breaker.py"
    monkeypatch.setattr(path_guard, "_guardrail_active", lambda: True)
    assert path_guard._guardrail_reason(target) == "guardrail"
    assert path_guard._command_targets_protected_path("echo x > swarm/containment_breaker.py") is not None


# ---------------------------------------------------------------------------
# Freeze / thaw: a running offender is paused, not killed
# ---------------------------------------------------------------------------

import subprocess
import time as _time

from swarm import agent_lifecycle
from swarm.platform import popen_session_kwargs


def _proc_state(pid: int) -> str:
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()


def _wait_state(pid: int, stopped: bool) -> str:
    # Signal delivery is asynchronous; poll briefly for the state to settle.
    state = ""
    for _ in range(50):
        state = _proc_state(pid)
        if state.startswith("T") == stopped:
            break
        _time.sleep(0.02)
    return state


@pytest.fixture()
def live_agent(app, tmp_path):
    """A real session-leader process registered as an active agent handle."""
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 60 & wait"], **popen_session_kwargs())
    # Let the shell finish forking its child first: a group SIGSTOP that lands
    # mid-fork can be dropped for the parent on macOS (test-only race; real
    # agents are never frozen milliseconds after spawn).
    for _ in range(100):
        if subprocess.run(["pgrep", "-P", str(proc.pid)], capture_output=True).returncode == 0 \
                and _proc_state(proc.pid).startswith("S"):
            break
        _time.sleep(0.02)
    log = tmp_path / "agent_ag-run.log"
    log.write_text("[Agent] [GUARD] blocked kind=shell tool=run_command detail='x'\n" * 6)
    db.agent_upsert({"id": "ag-run", "project": OWN, "task_type": "feature", "status": "active",
                     "task_id": "task-own-2", "pid": proc.pid, "log_path": str(log)})
    db.task_update_status("task-own-2", "in_progress", agent_id="ag-run")
    started = _time.time() - 100
    with agent_lifecycle._handle_lock:
        agent_lifecycle._active_handles["ag-run"] = {"process": proc, "started": started}
    yield proc
    with agent_lifecycle._handle_lock:
        agent_lifecycle._active_handles.pop("ag-run", None)
    try:
        import os, signal
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    proc.wait(timeout=5)


class TestFreeze:
    CFG = {"containment_breaker_enabled": True, "containment_spike_threshold": 5, "containment_spike_window_minutes": 60}

    def test_trip_freezes_running_offender_and_release_thaws_it(self, app, client, live_agent):
        app.extensions["containment_tick"]()
        assert _wait_state(live_agent.pid, True).startswith("T")  # stopped, not dead
        assert live_agent.poll() is None
        handle = agent_lifecycle._active_handles["ag-run"]
        assert handle[cb.FROZEN_KEY] is True and handle.get("freeze_started")
        assert cb.is_held(db.task_get("task-own-2"))

        resp = client.post("/api/tasks/task-own-2/containment-release")
        assert resp.status_code == 200
        assert not _wait_state(live_agent.pid, False).startswith("T")
        assert live_agent.poll() is None
        assert cb.FROZEN_KEY not in handle and "freeze_started" not in handle

    def test_thaw_does_not_count_frozen_time_toward_timeout(self, app, live_agent):
        handle = agent_lifecycle._active_handles["ag-run"]
        before = handle["started"]
        assert cb.freeze_agent(db, "ag-run")
        handle["freeze_started"] -= 50  # pretend it was frozen for 50s
        assert cb.thaw_agent(db, "ag-run")
        assert handle["started"] - before >= 50

    def test_watchdog_never_reaps_a_contained_agent(self, app, live_agent, monkeypatch):
        assert cb.freeze_agent(db, "ag-run")
        handle = agent_lifecycle._active_handles["ag-run"]
        handle["started"] -= 10 ** 6  # far past the zombie deadline
        agent_lifecycle.check_agent_status()
        assert live_agent.poll() is None
        assert "ag-run" in agent_lifecycle._active_handles

    def test_freeze_of_exited_agent_is_a_noop(self, app, live_agent):
        import os, signal
        os.killpg(live_agent.pid, signal.SIGKILL)
        live_agent.wait(timeout=5)
        assert cb.freeze_agent(db, "ag-run") is False


# ---------------------------------------------------------------------------
# Recovery after a server restart
# ---------------------------------------------------------------------------

@pytest.fixture()
def orphan_agent(app, tmp_path):
    """A real process registered only in the DB (no in-memory handle), as an
    agent looks to a freshly restarted server.  Its argv names the agent's
    wrapper script so the pid identity check can match it."""
    stub = tmp_path / "data" / "agent_ag-orph.py"  # harmless stub: conftest lets it run
    stub.parent.mkdir(exist_ok=True)
    stub.write_text("# test stub, never executed\n")
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 60 & wait", str(stub)],
                            **popen_session_kwargs())
    for _ in range(100):
        if subprocess.run(["pgrep", "-P", str(proc.pid)], capture_output=True).returncode == 0 \
                and _proc_state(proc.pid).startswith("S"):
            break
        _time.sleep(0.02)
    db.agent_upsert({"id": "ag-orph", "project": OWN, "task_type": "feature", "status": "active",
                     "task_id": "task-own-2", "pid": proc.pid,
                     "spawned_at": (datetime.now() - timedelta(days=1)).isoformat()})
    db.task_update_status("task-own-2", "in_progress", agent_id="ag-orph")
    yield proc
    try:
        import os, signal
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    proc.wait(timeout=5)


def _hold_for(task_id, agent_id, *, minutes_ago=5.0, blocked=None):
    meta = dict(_meta(task_id))
    meta.update({
        cb.HOLD_KEY: True, "needs_human_review": True,
        "containment_agent_id": agent_id,
        "containment_blocked_calls": blocked or {"shell": 3, "path": 1},
        "containment_tripped_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(),
    })
    db.task_update("task-own-2" if task_id is None else task_id, {"metadata": meta})


class TestRecoverAfterRestart:
    CFG = {"containment_breaker_enabled": True, "containment_spike_threshold": 5, "containment_spike_window_minutes": 60}

    def test_nothing_held_nothing_recovered(self, app):
        assert cb.recover(db, self.CFG) is None

    def test_refreezes_surviving_offender_and_rebuilds_trip(self, app, orphan_agent):
        _hold_for("task-own-2", "ag-orph")
        assert not _proc_state(orphan_agent.pid).startswith("T")  # e.g. thawed by accident
        trip = cb.recover(db, self.CFG)
        assert _wait_state(orphan_agent.pid, True).startswith("T")
        assert orphan_agent.poll() is None
        assert trip["running"] == ["ag-orph"] and trip["flagged"] == ["task-own-2"]
        off = trip["offenders"][0]
        assert off["blocked"] == {"shell": 3, "path": 1} and off["count"] == 4 and off["live"]
        assert trip["recovered_after_restart"] is True

    def test_release_after_restart_thaws_via_db_pid(self, app, client, orphan_agent):
        _hold_for("task-own-2", "ag-orph")
        cb.recover(db, self.CFG)
        assert _wait_state(orphan_agent.pid, True).startswith("T")
        resp = client.post("/api/tasks/task-own-2/containment-release")
        assert resp.status_code == 200
        assert not _wait_state(orphan_agent.pid, False).startswith("T")
        assert orphan_agent.poll() is None

    def test_reused_pid_is_never_signalled(self, app, orphan_agent):
        # The DB pid now belongs to some other process (argv lacks agent_<id>).
        _hold_for("task-own-2", "ag-other")
        db.agent_upsert({"id": "ag-other", "project": OWN, "task_type": "feature",
                         "status": "active", "task_id": "task-own-2", "pid": orphan_agent.pid})
        trip = cb.recover(db, self.CFG)
        _time.sleep(0.2)
        assert not _proc_state(orphan_agent.pid).startswith("T")
        assert trip["running"] == [] and trip["offenders"][0]["live"] is False
        assert cb.thaw_agent(db, "ag-other") is False and cb.freeze_agent(db, "ag-other") is False

    def test_acknowledged_hold_without_live_agent_is_not_a_trip(self, app):
        _hold_for("task-own-2", "ag-gone", minutes_ago=120)
        ack = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        assert cb.recover(db, {**self.CFG, "containment_ack_at": ack}) is None
        # ...but an unacknowledged one is, even when its agent is gone
        trip = cb.recover(db, self.CFG)
        assert trip["flagged"] == ["task-own-2"] and trip["running"] == []

    def test_live_offender_is_recovered_even_if_acknowledged(self, app, orphan_agent):
        _hold_for("task-own-2", "ag-orph", minutes_ago=120)
        ack = datetime.now(timezone.utc).isoformat()
        trip = cb.recover(db, {**self.CFG, "containment_ack_at": ack})
        assert trip["running"] == ["ag-orph"]

    def test_recover_never_raises(self):
        class Boom:
            def task_get_all(self, *a, **k):
                raise RuntimeError("db down")
        assert cb.recover(Boom(), {}) is None

    def test_restart_restores_suspension_and_keeps_orphan(self, app, orphan_agent, tmp_path):
        _hold_for("task-own-2", "ag-orph")
        app2, saved = _make_app(tmp_path, self.CFG)  # same DB file: a server restart
        try:
            state = app2.test_client().get("/api/auto-mode").get_json()
            assert state["suspended_for_containment"] is True and state["enabled"] is False
            assert state["containment_trip"]["running"] == ["ag-orph"]
            # The day-old orphan was not failed by the startup orphan sweep.
            assert db.agent_get("ag-orph")["status"] == "active"
            assert db.task_get("task-own-2")["status"] == "in_progress"
            assert _wait_state(orphan_agent.pid, True).startswith("T")
            # Operator re-enable acknowledges it as usual.
            r = app2.test_client().post("/api/auto-mode", json={"enabled": True}).get_json()
            state = app2.test_client().get("/api/auto-mode").get_json()
            assert state["suspended_for_containment"] is False and state["containment_trip"] is None
        finally:
            _teardown(saved)


def test_pid_identity_needs_exact_script_name(app, orphan_agent):
    assert cb._pid_is_agent(orphan_agent.pid, "ag-orph")
    assert not cb._pid_is_agent(orphan_agent.pid, "ag-or")      # prefix of the real id
    assert not cb._pid_is_agent(orphan_agent.pid, "ag-orph2")
    assert not cb._pid_is_agent(999999, "ag-orph")               # no such process



class TestReviewedBlocksNeverRecount:
    """2026-10-01 trip: 5 of 10 counted blocks were already acknowledged --
    the agents were running at ack time and their signals rows were written
    after containment_ack_at when they finished."""
    CFG = {"containment_breaker_enabled": True, "containment_spike_threshold": 5, "containment_spike_window_minutes": 60}

    def _live(self, tmp_path, agent_id, task_id, n):
        log = tmp_path / f"agent_{agent_id}.log"
        log.write_text("[Agent] [GUARD] blocked kind=shell tool=run_command detail='x'\n" * n)
        db.agent_upsert({"id": agent_id, "project": OWN, "task_type": "feature", "status": "active",
                         "task_id": task_id, "log_path": str(log)})
        return log

    def test_acked_live_blocks_do_not_recount_when_agent_finishes(self, app, tmp_path):
        self._live(tmp_path, "ag-a", "task-own-1", 4)
        ack = datetime.now(timezone.utc).isoformat()
        cfg = {**self.CFG, "containment_ack_at": ack, cb.ACK_COUNTS_KEY: cb.ack_snapshot(db)}
        assert cfg[cb.ACK_COUNTS_KEY] == {"ag-a": {"shell": 4}}
        # The agent finishes after the ack; its signals row carries all 4 blocks.
        db.agent_update_status("ag-a", "completed")
        _signal("ag-a", "task-own-1", {"shell": 4}, minutes_ago=-1)
        _signal("ag-b", "task-own-2", {"shell": 2}, minutes_ago=-1)
        assert cb.check(db, cfg) is None              # 2 new, not 6
        _signal("ag-c", "task-own-2", {"path": 3}, minutes_ago=-1)
        trip = cb.check(db, cfg)
        assert trip["total"] == 5
        assert {o["agent_id"] for o in trip["offenders"]} == {"ag-b", "ag-c"}

    def test_new_blocks_after_ack_still_count(self, app, tmp_path):
        log = self._live(tmp_path, "ag-a", "task-own-1", 4)
        cfg = {**self.CFG, cb.ACK_COUNTS_KEY: cb.ack_snapshot(db),
               "containment_ack_at": datetime.now(timezone.utc).isoformat()}
        assert cb.check(db, cfg) is None
        with log.open("a") as f:
            f.write("[Agent] [GUARD] blocked kind=shell tool=run_command detail='y'\n" * 5)
        trip = cb.check(db, cfg)
        assert trip["total"] == 5 and trip["offenders"][0]["blocked"] == {"shell": 5}

    def test_released_run_that_keeps_blocking_is_held_again(self, app):
        _signal("ag-1", "task-own-1", {"shell": 5})
        assert cb.check(db, self.CFG)["flagged"] == ["task-own-1"]
        cb.release(db, "task-own-1")
        _signal("ag-1", "task-own-1", {"shell": 11})   # same run, 6 more blocks
        trip = cb.check(db, self.CFG)
        assert trip["total"] == 6 and trip["flagged"] == ["task-own-1"]
        assert cb.is_held(db.task_get("task-own-1"))
        assert _meta("task-own-1")["containment_blocked_calls"] == {"shell": 11}

    def test_operator_ack_persists_snapshot(self, app, client, tmp_path):
        self._live(tmp_path, "ag-a", "task-own-1", 3)
        _signal("ag-1", "task-own-2", {"shell": 6})
        app.extensions["containment_tick"]()
        assert client.get("/api/auto-mode").get_json()["suspended_for_containment"] is True
        client.post("/api/auto-mode", json={"enabled": True})
        cfg = json.loads(open(app.config["CONFIG_FILE"]).read())
        assert cfg[cb.ACK_COUNTS_KEY] == {"ag-a": {"shell": 3}}
        assert cfg["containment_ack_at"]


def test_breaker_is_off_by_default(app):
    # No opt-in: even a large spike never trips, holds or freezes anything.
    _signal("ag-1", "task-own-1", {"shell": 50})
    assert cb.check(db, {}) is None
    assert cb.check(db, {"containment_spike_threshold": 1}) is None
    assert not cb.is_held(db.task_get("task-own-1"))


def test_example_config_ships_breaker_off():
    import json
    from pathlib import Path
    example = json.loads((Path(__file__).parent.parent / "config.example.json").read_text())
    assert example["containment_breaker_enabled"] is False
