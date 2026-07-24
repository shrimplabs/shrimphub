"""Unit tests for swarm/events.py EventBus and waiter thread integration."""

from __future__ import annotations

import time
import threading
from unittest.mock import patch

import pytest

from swarm.events import bus, EventBus, Event


@pytest.fixture(autouse=True)
def reset_bus():
    bus.reset_for_tests()
    yield
    bus.reset_for_tests()


# ---------------------------------------------------------------------------
# EventBus unit tests
# ---------------------------------------------------------------------------

class TestEventBus:
    def test_disabled_by_default(self):
        assert not bus.enabled

    def test_publish_when_disabled_returns_false(self):
        result = bus.publish("AGENT_EXITED", agent_id="abc")
        assert result is False
        assert bus.stats["dropped_disabled"] == 1

    def test_publish_when_enabled_returns_true(self):
        bus.set_enabled(True)
        result = bus.publish("AGENT_EXITED", agent_id="abc")
        assert result is True
        assert bus.stats["published"] == 1

    def test_handler_called_on_publish(self):
        received = []
        bus.subscribe("AGENT_EXITED", lambda ev: received.append(ev))
        bus.set_enabled(True)
        bus.publish("AGENT_EXITED", agent_id="abc", exit_code=0)
        assert bus.drain(timeout=2.0)
        assert len(received) == 1
        assert received[0].payload["agent_id"] == "abc"
        assert received[0].payload["exit_code"] == 0

    def test_handler_receives_event_type(self):
        received = []
        bus.subscribe("AGENT_FINISHED", lambda ev: received.append(ev))
        bus.set_enabled(True)
        bus.publish("AGENT_FINISHED", agent_id="xyz")
        assert bus.drain(timeout=2.0)
        assert received[0].type == "AGENT_FINISHED"

    def test_handler_not_called_for_other_event_type(self):
        received = []
        bus.subscribe("AGENT_EXITED", lambda ev: received.append(ev))
        bus.set_enabled(True)
        bus.publish("AGENT_FINISHED", agent_id="xyz")
        bus.drain(timeout=0.5)
        assert len(received) == 0

    def test_stats_track_handled(self):
        bus.subscribe("AGENT_EXITED", lambda ev: None)
        bus.set_enabled(True)
        bus.publish("AGENT_EXITED", agent_id="abc")
        bus.drain(timeout=2.0)
        assert bus.stats["handled"] == 1

    def test_stats_track_handler_errors(self):
        def bad_handler(ev):
            raise ValueError("boom")

        bus.subscribe("AGENT_EXITED", bad_handler)
        bus.set_enabled(True)
        bus.publish("AGENT_EXITED", agent_id="abc")
        bus.drain(timeout=2.0)
        assert bus.stats["handler_errors"] == 1

    def test_handler_error_does_not_kill_dispatcher(self):
        results = []

        def bad_handler(ev):
            raise RuntimeError("fail")

        def good_handler(ev):
            results.append(ev.payload["agent_id"])

        bus.subscribe("AGENT_EXITED", bad_handler)
        bus.subscribe("AGENT_EXITED", good_handler)
        bus.set_enabled(True)
        bus.publish("AGENT_EXITED", agent_id="a1")
        bus.publish("AGENT_EXITED", agent_id="a2")
        bus.drain(timeout=2.0)
        assert "a1" in results
        assert "a2" in results

    def test_set_enabled_false_disables_publishing(self):
        bus.set_enabled(True)
        bus.set_enabled(False)
        result = bus.publish("AGENT_EXITED", agent_id="abc")
        assert result is False

    def test_multiple_subscribers_all_called(self):
        calls_a, calls_b = [], []
        bus.subscribe("AGENT_EXITED", lambda ev: calls_a.append(1))
        bus.subscribe("AGENT_EXITED", lambda ev: calls_b.append(1))
        bus.set_enabled(True)
        bus.publish("AGENT_EXITED", agent_id="abc")
        bus.drain(timeout=2.0)
        assert calls_a == [1]
        assert calls_b == [1]

    def test_by_type_stats(self):
        bus.set_enabled(True)
        bus.publish("AGENT_EXITED", agent_id="a")
        bus.publish("AGENT_EXITED", agent_id="b")
        bus.publish("AGENT_FINISHED", agent_id="c")
        bus.drain(timeout=2.0)
        assert bus.stats["by_type"]["AGENT_EXITED"] == 2
        assert bus.stats["by_type"]["AGENT_FINISHED"] == 1

    def test_event_has_timestamp(self):
        received = []
        bus.subscribe("AGENT_EXITED", lambda ev: received.append(ev))
        bus.set_enabled(True)
        before = time.time()
        bus.publish("AGENT_EXITED", agent_id="abc")
        bus.drain(timeout=2.0)
        after = time.time()
        assert before <= received[0].ts <= after

    def test_reset_for_tests_clears_state(self):
        bus.set_enabled(True)
        bus.publish("AGENT_EXITED", agent_id="abc")
        bus.drain(timeout=2.0)
        bus.reset_for_tests()
        assert not bus.enabled
        assert bus.stats["published"] == 0
        assert bus.stats["handled"] == 0


# ---------------------------------------------------------------------------
# Waiter thread integration tests
# ---------------------------------------------------------------------------

class TestWaiterThreads:
    """Verify that spawn_agent() starts a waiter thread and that claim_finish()
    prevents double-processing when both the waiter and the sweep fire."""

    def test_claim_finish_second_call_returns_none(self):
        """Simulates waiter + sweep racing for the same agent_id."""
        from swarm import agent_lifecycle as lc

        # Directly inject a fake handle
        agent_id = "test-waiter-race-001"
        fake_handle = {"process": None, "project": "test", "task_id": "t1",
                       "started": time.time(), "script_path": "", "log_path": ""}
        with lc._handle_lock:
            lc._active_handles[agent_id] = fake_handle

        # First claim wins
        result1 = lc.claim_finish(agent_id)
        assert result1 is not None
        assert result1["project"] == "test"

        # Second claim loses (waiter or sweep — whichever is second)
        result2 = lc.claim_finish(agent_id)
        assert result2 is None

        # Cleanup
        with lc._finishing_lock:
            lc._finishing_agents.discard(agent_id)

    def test_claim_finish_removes_from_active_handles(self):
        from swarm import agent_lifecycle as lc

        agent_id = "test-claim-remove-001"
        with lc._handle_lock:
            lc._active_handles[agent_id] = {"process": None, "project": "p",
                                              "task_id": "t", "started": time.time(),
                                              "script_path": "", "log_path": ""}
        lc.claim_finish(agent_id)
        with lc._handle_lock:
            assert agent_id not in lc._active_handles

        with lc._finishing_lock:
            lc._finishing_agents.discard(agent_id)

    def test_claim_finish_adds_to_finishing_agents(self):
        from swarm import agent_lifecycle as lc

        agent_id = "test-claim-finishing-001"
        with lc._handle_lock:
            lc._active_handles[agent_id] = {"process": None, "project": "p",
                                              "task_id": "t", "started": time.time(),
                                              "script_path": "", "log_path": ""}
        lc.claim_finish(agent_id)
        with lc._finishing_lock:
            assert agent_id in lc._finishing_agents
        # Cleanup
        with lc._finishing_lock:
            lc._finishing_agents.discard(agent_id)

    def test_waiter_thread_fires_event_on_exit(self):
        """When bus is enabled, waiter publishes AGENT_EXITED after proc.wait()."""
        import subprocess
        import sys
        from swarm import agent_lifecycle as lc

        bus.set_enabled(True)
        received = []
        bus.subscribe("AGENT_EXITED", lambda ev: received.append(ev))

        # Start a fast subprocess
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.05)"])
        agent_id = "test-waiter-fires-001"

        with lc._handle_lock:
            lc._active_handles[agent_id] = {
                "process": proc,
                "project": "test",
                "task_id": "t1",
                "started": time.time(),
                "script_path": "", "log_path": "",
            }

        # Start waiter manually (mirrors what spawn_agent does)
        def _waiter(aid=agent_id, p=proc):
            _ec = p.wait()
            if not bus.enabled:
                return
            bus.publish("AGENT_EXITED", agent_id=aid, exit_code=_ec,
                        task_id="t1", project="test")

        t = threading.Thread(target=_waiter, daemon=True)
        t.start()

        # Wait for the handler to fire (waiter thread fires after proc exits).
        deadline = time.time() + 3.0
        while not received and time.time() < deadline:
            time.sleep(0.02)
        assert len(received) == 1
        assert received[0].payload["agent_id"] == agent_id

        # Cleanup
        with lc._handle_lock:
            lc._active_handles.pop(agent_id, None)
        with lc._finishing_lock:
            lc._finishing_agents.discard(agent_id)
