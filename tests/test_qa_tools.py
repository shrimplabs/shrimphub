"""Unit tests for swarm/qa_tools.py.

All tests mock _state_server_send and OS-level calls so they run without
a live game, Godot binary, or macOS GUI automation.
"""
import json
import socket
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import swarm.qa_tools as qa_tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(**kw):
    return {"ok": True, **kw}


def _err(msg="StateServer not available"):
    return {"error": msg}


def _patch_ss(response):
    """Monkeypatch _state_server_send to return response."""
    return patch.object(qa_tools, "_state_server_send", return_value=response)


# ---------------------------------------------------------------------------
# _state_server_send — TCP protocol
# ---------------------------------------------------------------------------

class TestStateServerSend:
    def test_sends_json_newline_terminated(self, tmp_path):
        """Server receives a newline-terminated JSON command."""
        received = []

        def fake_server(port):
            with socket.socket() as srv:
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind(("127.0.0.1", port))
                srv.listen(1)
                conn, _ = srv.accept()
                data = b""
                while b"\n" not in data:
                    data += conn.recv(1024)
                received.append(data)
                conn.sendall(json.dumps({"ok": True}).encode() + b"\n")
                conn.close()

        port = 19877
        t = threading.Thread(target=fake_server, args=(port,), daemon=True)
        t.start()
        time.sleep(0.05)

        result = qa_tools._state_server_send({"command": "state"}, port=port)

        t.join(timeout=2)
        assert result == {"ok": True}
        payload = json.loads(received[0].decode().strip())
        assert payload == {"command": "state"}

    def test_returns_error_on_connection_refused(self):
        result = qa_tools._state_server_send({"command": "state"}, port=19999)
        assert result.get("error") is not None

    def test_screenshot_command_gets_extended_recv_timeout(self):
        """screenshot_b64 should use a longer recv timeout than default."""
        calls = []

        class FakeSock:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def sendall(self, data): pass
            def setsockopt(self, *a): pass
            def settimeout(self, t): calls.append(t)
            def recv(self, n): return json.dumps({"image_base64": "abc"}).encode() + b"\n"

        with patch("socket.create_connection", return_value=FakeSock()):
            qa_tools._state_server_send({"command": "screenshot_b64"}, timeout=5.0)

        # recv timeout for screenshot should be > the default 5s
        assert any(t > 5.0 for t in calls)

    def test_delegates_to_canonical_state_server_client(self):
        """Regression guard for roadmap #10: _state_server_send must genuinely
        delegate to scenario_qa.StateServerClient, not just coincidentally
        behave the same way. Catches future re-divergence."""
        from swarm.tools.scenario_qa import StateServerClient

        with patch.object(StateServerClient, "send", return_value={"ok": True}) as mock_send:
            result = qa_tools._state_server_send({"command": "state"}, port=12345, timeout=7.0)

        assert result == {"ok": True}
        mock_send.assert_called_once_with({"command": "state"}, timeout=7.0)


# ---------------------------------------------------------------------------
# key_press
# ---------------------------------------------------------------------------

class TestClickElement:
    """Roadmap #10: click_element's a11y-tree tier now delegates to
    observe_common.match_a11y_element(mode='scored') -- these tests guard
    that the tier-1 grounding path still picks the right element and presses
    it by path, without touching the color-detect/vision fallback tiers."""

    def test_matches_and_presses_best_a11y_element(self):
        tree = {"a11y_tree": [
            {"label": "Settings", "role": "button", "path": "/root/Settings", "visible": True},
            {"label": "Start Game", "role": "button", "path": "/root/Start", "visible": True},
        ]}

        def fake_send(cmd, **kw):
            if cmd.get("command") == "a11y_tree":
                return tree
            if cmd.get("command") == "press_button":
                assert cmd["id"] == "/root/Start"
                return {"ok": True}
            raise AssertionError(f"unexpected command {cmd}")

        with patch.object(qa_tools, "_state_server_send", side_effect=fake_send):
            result = qa_tools.click_element("shot.png", "start")

        assert result["ok"] is True
        assert result["source"] == "a11y_tree"
        assert result["element"]["path"] == "/root/Start"

    def test_no_a11y_match_does_not_press_button(self):
        tree = {"a11y_tree": [{"label": "Unrelated Label", "role": "label", "path": "/root/X", "visible": True}]}
        calls = []

        def fake_send(cmd, **kw):
            calls.append(cmd.get("command"))
            if cmd.get("command") == "a11y_tree":
                return tree
            return {"error": "should not be called"}

        with patch.object(qa_tools, "_state_server_send", side_effect=fake_send):
            qa_tools.click_element("shot.png", "zzz_totally_unmatched_xyz")

        assert "press_button" not in calls


class TestKeyPress:
    def test_uses_state_server_when_available(self, monkeypatch):
        monkeypatch.setattr(qa_tools, "_state_server_send", lambda cmd, **kw: {"ok": True})
        result = qa_tools.qa_key_press("space")
        assert result["ok"] is True
        assert result["source"] == "state_server"

    def test_falls_back_to_cliclick_when_state_server_unavailable(self, monkeypatch):
        monkeypatch.setattr(qa_tools, "_state_server_send", lambda cmd, **kw: _err())
        run_calls = []
        monkeypatch.setattr(
            qa_tools.subprocess, "run",
            lambda cmd, **kw: run_calls.append(cmd) or MagicMock(returncode=0),
        )
        result = qa_tools.qa_key_press("space")
        assert result["ok"] is True
        assert any("cliclick" in str(c) for c in run_calls)

    def test_sends_action_command_to_state_server(self, monkeypatch):
        sent = []
        monkeypatch.setattr(qa_tools, "_state_server_send",
                            lambda cmd, **kw: sent.append(cmd) or {"ok": True})
        qa_tools.qa_key_press("move_right")
        assert sent[0]["command"] == "input"
        assert sent[0]["type"] == "action"
        assert sent[0]["action"] == "move_right"


# ---------------------------------------------------------------------------
# key_hold
# ---------------------------------------------------------------------------

class TestKeyHold:
    def test_sends_hold_command_with_duration(self, monkeypatch):
        sent = []
        monkeypatch.setattr(qa_tools, "_state_server_send",
                            lambda cmd, **kw: sent.append(cmd) or {"ok": True})
        result = qa_tools.key_hold("move_right", 2.0)
        assert result["ok"] is True
        assert sent[0] == {"command": "input", "type": "hold", "action": "move_right", "duration": 2.0}

    def test_timeout_is_duration_plus_buffer(self, monkeypatch):
        """_state_server_send should be called with timeout > duration so the
        socket doesn't close before the server finishes the hold."""
        timeouts = []
        def fake_send(cmd, **kw):
            timeouts.append(kw.get("timeout", 5.0))
            return {"ok": True}
        monkeypatch.setattr(qa_tools, "_state_server_send", fake_send)
        qa_tools.key_hold("w", 3.0)
        assert timeouts[0] > 3.0

    def test_falls_back_to_repeated_key_press_when_state_server_fails(self, monkeypatch):
        monkeypatch.setattr(qa_tools, "_state_server_send", lambda cmd, **kw: _err())
        press_calls = []
        monkeypatch.setattr(qa_tools, "qa_key_press",
                            lambda key: press_calls.append(key) or {"ok": True})
        # Use a very short duration so the test runs fast
        result = qa_tools.key_hold("w", 0.1)
        assert result["ok"] is True
        assert result["source"] == "key_press_fallback"
        assert len(press_calls) > 0
        assert all(k == "w" for k in press_calls)

    def test_fallback_fires_multiple_presses_proportional_to_duration(self, monkeypatch):
        monkeypatch.setattr(qa_tools, "_state_server_send", lambda cmd, **kw: _err())
        press_calls = []
        monkeypatch.setattr(qa_tools, "qa_key_press",
                            lambda key: press_calls.append(key) or {"ok": True})
        qa_tools.key_hold("w", 0.2)
        # At 50ms interval over 0.2s we expect ~4 presses; at minimum more than 1
        assert len(press_calls) > 1


# ---------------------------------------------------------------------------
# play_macro
# ---------------------------------------------------------------------------

class TestPlayMacro:
    def test_sends_play_macro_command(self, monkeypatch):
        sent = []
        monkeypatch.setattr(qa_tools, "_state_server_send",
                            lambda cmd, **kw: sent.append(cmd) or {"ok": True})
        actions = [{"type": "hold", "action": "move_right", "duration": 1.0}]
        result = qa_tools.play_macro(actions)
        assert result["ok"] is True
        assert sent[0]["command"] == "play_macro"
        assert sent[0]["actions"] == actions

    def test_returns_action_count(self, monkeypatch):
        monkeypatch.setattr(qa_tools, "_state_server_send", lambda cmd, **kw: {"ok": True})
        actions = [{"type": "wait", "seconds": 0.1}, {"type": "hold", "action": "w", "duration": 0.5}]
        result = qa_tools.play_macro(actions)
        assert result["actions"] == 2

    def test_returns_error_when_state_server_unavailable(self, monkeypatch):
        monkeypatch.setattr(qa_tools, "_state_server_send", lambda cmd, **kw: _err())
        result = qa_tools.play_macro([{"type": "wait", "seconds": 0.1}])
        assert result["ok"] is False
        assert "error" in result


# ---------------------------------------------------------------------------
# mouse_drag
# ---------------------------------------------------------------------------

class TestMouseDrag:
    def test_sends_drag_command_with_coordinates(self, monkeypatch):
        sent = []
        monkeypatch.setattr(qa_tools, "_state_server_send",
                            lambda cmd, **kw: sent.append(cmd) or {"ok": True})
        result = qa_tools.mouse_drag(100, 200, 300, 400, 0.5)
        assert result["ok"] is True
        assert sent[0] == {
            "command": "input", "type": "drag",
            "x1": 100, "y1": 200, "x2": 300, "y2": 400,
            "duration": 0.5,
        }

    def test_returns_error_when_state_server_unavailable(self, monkeypatch):
        monkeypatch.setattr(qa_tools, "_state_server_send", lambda cmd, **kw: _err())
        result = qa_tools.mouse_drag(0, 0, 100, 100)
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# scroll, right_click, double_click
# ---------------------------------------------------------------------------

class TestScrollRightClickDoubleClick:
    def test_scroll_sends_correct_command(self, monkeypatch):
        sent = []
        monkeypatch.setattr(qa_tools, "_state_server_send",
                            lambda cmd, **kw: sent.append(cmd) or {"ok": True})
        qa_tools.scroll(400, 300, 5.0)
        assert sent[0] == {"command": "input", "type": "scroll", "x": 400, "y": 300, "delta": 5.0}

    def test_right_click_sends_correct_command(self, monkeypatch):
        sent = []
        monkeypatch.setattr(qa_tools, "_state_server_send",
                            lambda cmd, **kw: sent.append(cmd) or {"ok": True})
        qa_tools.right_click(200, 150)
        assert sent[0] == {"command": "input", "type": "right_click", "x": 200, "y": 150}

    def test_double_click_sends_correct_command(self, monkeypatch):
        sent = []
        monkeypatch.setattr(qa_tools, "_state_server_send",
                            lambda cmd, **kw: sent.append(cmd) or {"ok": True})
        qa_tools.double_click(100, 100)
        assert sent[0] == {"command": "input", "type": "double_click", "x": 100, "y": 100}

    def test_scroll_negative_delta_scrolls_down(self, monkeypatch):
        sent = []
        monkeypatch.setattr(qa_tools, "_state_server_send",
                            lambda cmd, **kw: sent.append(cmd) or {"ok": True})
        qa_tools.scroll(0, 0, -3.0)
        assert sent[0]["delta"] == -3.0


# ---------------------------------------------------------------------------
# key_combo
# ---------------------------------------------------------------------------

class TestKeyCombo:
    def test_sends_key_combo_command(self, monkeypatch):
        sent = []
        monkeypatch.setattr(qa_tools, "_state_server_send",
                            lambda cmd, **kw: sent.append(cmd) or {"ok": True})
        result = qa_tools.key_combo(["shift", "w"])
        assert result["ok"] is True
        assert sent[0] == {"command": "input", "type": "key_combo", "keys": ["shift", "w"]}

    def test_returns_error_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(qa_tools, "_state_server_send", lambda cmd, **kw: _err())
        result = qa_tools.key_combo(["ctrl", "z"])
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# screenshot_burst
# ---------------------------------------------------------------------------

class TestScreenshotBurst:
    def test_takes_n_screenshots(self, monkeypatch, tmp_path):
        monkeypatch.setattr(qa_tools, "_project_root", lambda: str(tmp_path))
        monkeypatch.setattr(qa_tools, "_state_server_send",
                            lambda cmd, **kw: _err())  # force screencapture path
        monkeypatch.setattr(qa_tools, "is_macos", lambda: False)

        # Mock take_screenshot to return success without touching the filesystem
        call_count = [0]
        def fake_take(filename):
            call_count[0] += 1
            return {"ok": True, "path": f"/tmp/{filename}.png"}
        monkeypatch.setattr(qa_tools, "take_screenshot", fake_take)

        result = qa_tools.screenshot_burst("test_burst", count=4, interval=0.0)
        assert result["ok"] is True
        assert result["count"] == 4
        assert len(result["paths"]) == 4
        assert call_count[0] == 4

    def test_returns_partial_results_on_failure(self, monkeypatch):
        call_count = [0]
        def fake_take(filename):
            call_count[0] += 1
            if call_count[0] <= 2:
                return {"ok": True, "path": f"/tmp/{filename}.png"}
            return {"ok": False, "error": "failed"}
        monkeypatch.setattr(qa_tools, "take_screenshot", fake_take)

        result = qa_tools.screenshot_burst("burst", count=4, interval=0.0)
        assert result["count"] == 2
        assert len(result["paths"]) == 2

    def test_uses_filename_prefix_per_frame(self, monkeypatch):
        filenames = []
        def fake_take(filename):
            filenames.append(filename)
            return {"ok": True, "path": f"/tmp/{filename}.png"}
        monkeypatch.setattr(qa_tools, "take_screenshot", fake_take)

        qa_tools.screenshot_burst("frame", count=3, interval=0.0)
        assert filenames == ["frame_0", "frame_1", "frame_2"]


# ---------------------------------------------------------------------------
# vision_query — single vs multi-image dispatch
# ---------------------------------------------------------------------------

class TestVisionQuery:
    def _make_cfg(self):
        return {
            "vision_provider": "test-provider",
            "vision_provider_fast": "test-provider",
            "vision_providers": {
                "test-provider": {
                    "format": "openai",
                    "model": "test-model",
                    "base_url": "http://localhost:9999/v1",
                    "api_key_env": "",
                }
            }
        }

    def test_single_image_calls_call_vision(self, monkeypatch):
        monkeypatch.setattr(qa_tools, "QA_CONFIG", self._make_cfg())
        monkeypatch.setattr(qa_tools, "mcp_client", None)

        from swarm import vision as vision_mod
        call_args = []
        monkeypatch.setattr(vision_mod, "call_vision",
                            lambda path, q, p, c: call_args.append(path) or "answer")
        monkeypatch.setattr(vision_mod, "call_vision_multi",
                            lambda paths, q, p, c: (_ for _ in ()).throw(AssertionError("should not call multi")))

        result = qa_tools.vision_query("/tmp/shot.png", "what is on screen?")
        assert result["ok"] is True
        assert call_args == ["/tmp/shot.png"]

    def test_list_of_images_calls_call_vision_multi(self, monkeypatch):
        monkeypatch.setattr(qa_tools, "QA_CONFIG", self._make_cfg())
        monkeypatch.setattr(qa_tools, "mcp_client", None)

        from swarm import vision as vision_mod
        multi_calls = []
        monkeypatch.setattr(vision_mod, "call_vision",
                            lambda path, q, p, c: (_ for _ in ()).throw(AssertionError("should not call single")))
        monkeypatch.setattr(vision_mod, "call_vision_multi",
                            lambda paths, q, p, c: multi_calls.append(paths) or "motion detected")

        paths = ["/tmp/frame_0.png", "/tmp/frame_1.png", "/tmp/frame_2.png"]
        result = qa_tools.vision_query(paths, "did the player move?")
        assert result["ok"] is True
        assert multi_calls == [paths]

    def test_single_element_list_calls_call_vision(self, monkeypatch):
        """A list with one item should use single-image path."""
        monkeypatch.setattr(qa_tools, "QA_CONFIG", self._make_cfg())
        monkeypatch.setattr(qa_tools, "mcp_client", None)

        from swarm import vision as vision_mod
        call_args = []
        monkeypatch.setattr(vision_mod, "call_vision",
                            lambda path, q, p, c: call_args.append(path) or "answer")
        monkeypatch.setattr(vision_mod, "call_vision_multi",
                            lambda paths, q, p, c: (_ for _ in ()).throw(AssertionError("should not call multi")))

        result = qa_tools.vision_query(["/tmp/only.png"], "what is shown?")
        assert result["ok"] is True
        assert call_args == ["/tmp/only.png"]

    def test_timeout_returns_error_dict(self, monkeypatch):
        monkeypatch.setattr(qa_tools, "QA_CONFIG", self._make_cfg())
        monkeypatch.setattr(qa_tools, "mcp_client", None)

        # Simulate a vision call whose wait exceeds the timeout budget
        # without leaving the worker thread alive after the test ends.
        # The cleanest representation is to block on a threading.Event
        # that the test never sets, which causes the caller's
        # `future.result(timeout=1)` to trip first with TimeoutError;
        # vision_query then converts that into
        # {"ok": False, "error": "vision_query timed out after 1s"}.
        # The worker is left running, so we cap its wait so the executor
        # shutdown at function exit doesn't hang the test past ~2s.
        import threading as _threading

        _released = _threading.Event()

        def slow_vision(*a, **kw):
            # Block the worker for a bounded interval (slightly longer than
            # the caller's 1s timeout) then return so the executor's
            # shutdown wait can complete.  The main thread times out at
            # 1s, observes `concurrent.futures.TimeoutError`, and vision_query
            # returns the timeout error dict.  The slow worker eventually
            # returns "never" but the test has already asserted and exited.
            _released.wait(timeout=2.5)
            return "never"

        # Patch `call_vision` on the source vision module -- this is the
        # binding `_run()` resolves via its local
        # `from swarm.vision import call_vision`.  Patching it here (and
        # restoring via monkeypatch teardown) is enough: Python re-reads
        # `swarm.vision.call_vision` each time the inner `_run()` runs the
        # `from` import, so the mock is picked up.
        from swarm import vision as _vision_mod
        monkeypatch.setattr(_vision_mod, "call_vision", slow_vision)
        # `call_vision_multi` is unused for a single-image call but patch
        # it defensively on the source module so a future refactor
        # flipping to multi doesn't silently bypass the timeout simulation.
        monkeypatch.setattr(
            _vision_mod, "call_vision_multi",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("expected single-image path")),
        )

        result = qa_tools.vision_query("/tmp/shot.png", "test", timeout=1)
        assert result["ok"] is False
        assert "timed out" in result["error"].lower()


# ---------------------------------------------------------------------------
# tool dispatch — new tools are registered for QA types
# ---------------------------------------------------------------------------

class TestToolDispatchRegistration:
    """Verify every new tool is reachable via the dispatch registry for the
    correct task types. Catches the class of bug where we add a function to
    qa_tools but forget to register it in tool_dispatch.py."""

    @pytest.fixture(autouse=True)
    def registry(self):
        from swarm.tool_dispatch import _registry_by_name
        self.reg = _registry_by_name()

    def _assert_registered(self, tool_name, task_type):
        spec = self.reg.get(tool_name)
        assert spec is not None, f"'{tool_name}' not registered in tool registry"
        assert task_type in spec.task_types, (
            f"'{tool_name}' not available for task_type='{task_type}', "
            f"only available for: {spec.task_types}"
        )

    @pytest.mark.parametrize("tool", [
        "key_hold", "play_macro", "mouse_drag", "scroll",
        "right_click", "double_click", "key_combo", "screenshot_burst",
    ])
    @pytest.mark.parametrize("task_type", ["qa", "art_pass", "polish"])
    def test_new_tools_registered_for_qa_art_polish(self, tool, task_type):
        self._assert_registered(tool, task_type)

    @pytest.mark.parametrize("tool", ["key_press", "take_screenshot", "vision_query", "press_button"])
    @pytest.mark.parametrize("task_type", ["qa", "art_pass", "polish"])
    def test_existing_tools_still_registered(self, tool, task_type):
        self._assert_registered(tool, task_type)

    def test_vision_query_handler_is_callable(self):
        spec = self.reg.get("vision_query")
        assert spec is not None
        assert callable(spec.handler)


class TestScreenshotNeverCapturesFullScreen:
    """take_screenshot must never grab the whole display.

    Its output is read verbatim by vision._load_image_b64() and sent to a VLM
    -- for the local-* providers that means leaving the machine via the
    router.  A full-screen capture would put whatever else is on screen into
    that request.  When the game window bounds are unknown the correct
    outcome is a clear failure, not a wider capture.
    """

    def _force_fallback(self, monkeypatch, tmp_path, bounds, lookup_ok=False):
        """Make the StateServer path fail so we land in the screencapture branch."""
        from swarm import qa_tools

        monkeypatch.setattr(qa_tools, "_state_server_send", lambda *a, **k: {})
        monkeypatch.setattr(qa_tools, "_project_root", lambda: str(tmp_path))
        monkeypatch.setattr(qa_tools, "is_macos", lambda: True)
        # Bounds reach _qa_window the way production does -- through the lookup.
        # Injecting them directly while stubbing the lookup as FAILED would
        # describe a state the code can no longer be in: after bev6, a failed
        # lookup invalidates the bounds rather than leaving them usable.
        if lookup_ok:
            monkeypatch.setattr(
                qa_tools, "qa_get_window_bounds",
                lambda *a, **k: {"ok": True, **bounds},
            )
        else:
            monkeypatch.setattr(
                qa_tools, "qa_get_window_bounds",
                lambda *a, **k: {"ok": False, "error": "bounds unavailable"},
            )
        monkeypatch.setattr(qa_tools, "_qa_window", dict(bounds))
        calls = []
        monkeypatch.setattr(
            qa_tools.subprocess, "run",
            lambda cmd, *a, **k: calls.append(cmd) or _DummyProc(),
        )
        return calls

    def test_refuses_when_window_bounds_unknown(self, monkeypatch, tmp_path):
        from swarm import qa_tools

        calls = self._force_fallback(
            monkeypatch, tmp_path, {"x": 0, "y": 0, "w": 0, "h": 0}
        )
        result = qa_tools.take_screenshot("probe.png")
        assert result["ok"] is False
        assert "refusing to capture" in result["error"]
        assert calls == [], "no capture command should have been run at all"

    def test_rect_is_scoped_to_the_window_when_bounds_are_known(
        self, monkeypatch, tmp_path
    ):
        from swarm import qa_tools

        calls = self._force_fallback(
            monkeypatch, tmp_path, {"x": 10, "y": 20, "w": 300, "h": 400},
            lookup_ok=True,
        )
        qa_tools.take_screenshot("probe.png")
        capture = [c for c in calls if c and c[0] == "screencapture"]
        assert capture, "expected a screencapture call"
        cmd = capture[0]
        assert "-R" in cmd, "capture must be rect-scoped, never full screen"
        assert cmd[cmd.index("-R") + 1] == "10,20,300,400"


class TestScreenshotRefusesUnmeasuredBounds:
    """Regression for beads bev6 -- the failure that reached production.

    The guard above used to be ``if not (w > 0 and h > 0)``.  When
    qa_get_window_bounds() failed, take_screenshot() left _qa_window at its
    module defaults (x=50 y=50 w=900 h=650), which are positive -- so the guard
    passed and screencapture grabbed a blind 900x650 slab of the display and
    handed it to a vision model.  Observed in agent 2e9126f9 on 2026-09-20,
    where every take_screenshot ran ``-R 50,50,900,650`` against a window whose
    bounds had never been resolved.

    These tests use the REAL module defaults rather than injected bounds.  That
    is the whole point: a test that injects w=0 cannot see this bug, because no
    code path ever produces w=0 here.
    """

    def _fallback_with_real_defaults(self, monkeypatch, tmp_path, bounds_result):
        from swarm import qa_tools
        import copy

        monkeypatch.setattr(qa_tools, "_state_server_send", lambda *a, **k: {})
        monkeypatch.setattr(qa_tools, "_project_root", lambda: str(tmp_path))
        monkeypatch.setattr(qa_tools, "is_macos", lambda: True)
        monkeypatch.setattr(
            qa_tools, "qa_get_window_bounds", lambda *a, **k: bounds_result
        )
        # Pristine copy of the module defaults -- NOT hand-written values.
        monkeypatch.setattr(
            qa_tools, "_qa_window", copy.deepcopy(_PRISTINE_QA_WINDOW)
        )
        calls = []
        monkeypatch.setattr(
            qa_tools.subprocess, "run",
            lambda cmd, *a, **k: calls.append(cmd) or _DummyProc(),
        )
        return calls

    def test_module_defaults_are_positive(self):
        """Canary: if this fails, the bug being guarded no longer applies.

        The whole hazard is that the placeholder rect looks valid to a
        positivity check.  Should someone change the defaults to zeros, this
        test should fail loudly so the guard's rationale gets re-read.
        """
        assert _PRISTINE_QA_WINDOW["w"] > 0 and _PRISTINE_QA_WINDOW["h"] > 0
        assert _PRISTINE_QA_WINDOW["bounds_measured"] is False

    def test_refuses_when_bounds_query_fails(self, monkeypatch, tmp_path):
        """The exact 2e9126f9 scenario: TCC denies the window lookup."""
        from swarm import qa_tools

        calls = self._fallback_with_real_defaults(
            monkeypatch, tmp_path,
            {"ok": False, "error": "Game window not found (grant Accessibility"
                                   " permission to Terminal in System Settings)"},
        )
        result = qa_tools.take_screenshot("probe.png")
        assert result["ok"] is False, (
            "take_screenshot captured using unmeasured placeholder bounds"
        )
        assert calls == [], f"no capture should have run, got: {calls}"
        assert "50,50,900,650" not in str(calls)

    def test_error_names_the_underlying_cause(self, monkeypatch, tmp_path):
        """A refusal the operator cannot diagnose just gets worked around."""
        from swarm import qa_tools

        self._fallback_with_real_defaults(
            monkeypatch, tmp_path,
            {"ok": False, "error": "Accessibility permission missing"},
        )
        result = qa_tools.take_screenshot("probe.png")
        assert "Accessibility permission missing" in result["error"]

    def test_stale_measured_bounds_are_invalidated(self, monkeypatch, tmp_path):
        """Bounds measured for an earlier window must not authorise a capture.

        Once a window has been measured the flag is True.  If the window then
        closes and the next lookup fails, those coordinates now point at
        whatever moved into that screen region -- so the flag must be cleared,
        not left set.
        """
        from swarm import qa_tools
        import copy

        window = copy.deepcopy(_PRISTINE_QA_WINDOW)
        window.update({"x": 100, "y": 120, "w": 640, "h": 480,
                       "bounds_measured": True})

        monkeypatch.setattr(qa_tools, "_state_server_send", lambda *a, **k: {})
        monkeypatch.setattr(qa_tools, "_project_root", lambda: str(tmp_path))
        monkeypatch.setattr(qa_tools, "is_macos", lambda: True)
        monkeypatch.setattr(qa_tools, "qa_get_window_bounds",
                            lambda *a, **k: {"ok": False, "error": "window gone"})
        monkeypatch.setattr(qa_tools, "_qa_window", window)
        calls = []
        monkeypatch.setattr(qa_tools.subprocess, "run",
                            lambda cmd, *a, **k: calls.append(cmd) or _DummyProc())

        result = qa_tools.take_screenshot("probe.png")
        assert result["ok"] is False
        assert calls == [], "captured using bounds from a window that is gone"

    def test_successful_lookup_still_captures(self, monkeypatch, tmp_path):
        """The guard must not block the working path."""
        from swarm import qa_tools

        calls = self._fallback_with_real_defaults(
            monkeypatch, tmp_path,
            {"ok": True, "x": 12, "y": 34, "w": 800, "h": 600},
        )
        result = qa_tools.take_screenshot("probe.png")
        assert result["ok"] is True, result.get("error")
        capture = [c for c in calls if c and c[0] == "screencapture"]
        assert capture, "expected a screencapture call"
        assert capture[0][capture[0].index("-R") + 1] == "12,34,800,600"


#: Snapshot of the shipped defaults, captured at import before any test
#: monkeypatches the module.  Tests assert against THIS rather than literals so
#: they keep tracking the real defaults if those ever change.
def _snapshot_defaults():
    from swarm import qa_tools
    import copy
    return copy.deepcopy(qa_tools._qa_window)


_PRISTINE_QA_WINDOW = _snapshot_defaults()


class _DummyProc:
    returncode = 0
    stdout = b""
    stderr = b""
