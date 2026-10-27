import json
import sys
from unittest.mock import patch

import pytest

from swarm.tools import playthrough_kit as kit


class FakeClient:
    def __init__(self, states):
        self.states = iter(states)
        self.sent = []

    def wait_ready(self, timeout=10):
        return True

    def get_state(self):
        return next(self.states)

    def a11y_tree(self):
        return {"a11y_tree": []}

    def send(self, payload):
        self.sent.append(payload)
        return {"ok": True}


def _receipt(stdout: str) -> dict:
    line = next(line for line in stdout.splitlines()
                if line.startswith("PLAYTHROUGH_RESULT: "))
    return json.loads(line.split(": ", 1)[1])


def test_victory_receipt_and_keyboard_action(tmp_path, capsys):
    client = FakeClient([{"state": "playing"}, {"state": "victory", "wave": 6}])
    argv = ["bot", "--no-launch", "--out-dir", str(tmp_path), "--stuck-window", "5"]

    with patch.object(sys, "argv", argv), patch.object(kit, "StateServerClient", return_value=client):
        with pytest.raises(SystemExit) as exc:
            kit.run_bot_cli(
                lambda state, a11y, history: kit.Action(
                    kind="key_combo", keys=["a", "space"]
                ),
                lambda state: state.get("state") == "victory",
                classify_failure=lambda state: "game_over" if state.get("state") == "game_over" else None,
                progress=lambda state: {
                    "completed": state.get("state") == "victory",
                    "wave": state.get("wave", 1),
                },
            )

    assert exc.value.code == 0
    assert client.sent == [{"command": "input", "type": "key_combo", "keys": ["a", "space"]}]
    receipt = _receipt(capsys.readouterr().out)
    assert receipt["outcome"] == "complete"
    assert receipt["progress"] == {"completed": True, "wave": 6}


def test_game_over_is_structured_failure(tmp_path, capsys):
    client = FakeClient([{"state": "game_over", "wave": 1}])
    argv = ["bot", "--no-launch", "--out-dir", str(tmp_path)]

    with patch.object(sys, "argv", argv), patch.object(kit, "StateServerClient", return_value=client):
        with pytest.raises(SystemExit) as exc:
            kit.run_bot_cli(
                lambda state, a11y, history: kit.Action(kind="noop"),
                lambda state: state.get("state") == "victory",
                classify_failure=lambda state: "game_over" if state.get("state") == "game_over" else None,
                progress=lambda state: {"completed": False, "wave": state.get("wave", 0)},
            )

    assert exc.value.code == 1
    receipt = _receipt(capsys.readouterr().out)
    assert receipt["status"] == "failure"
    assert receipt["outcome"] == "game_over"


# ---------------------------------------------------------------------------
# click_label -- roadmap #10 convergence onto observe_common.match_a11y_element
# ---------------------------------------------------------------------------

class _RecordingClient:
    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)
        return {"ok": True}


def test_click_label_clicks_bounds_center_on_match():
    client = _RecordingClient()
    a11y = {"a11y_tree": [
        {"label": "Start Game", "bounds": [100, 200, 40, 20], "visible": True},
    ]}

    result = kit.click_label(client, a11y, "start")

    assert result is True
    assert client.sent == [{"command": "input", "type": "click", "x": 120.0, "y": 210.0}]


def test_click_label_returns_false_when_no_match():
    client = _RecordingClient()
    a11y = {"a11y_tree": [{"label": "Settings", "bounds": [0, 0, 10, 10], "visible": True}]}

    assert kit.click_label(client, a11y, "start") is False
    assert client.sent == []


def test_click_label_skips_invisible_entries():
    client = _RecordingClient()
    a11y = {"a11y_tree": [{"label": "Start", "bounds": [0, 0, 10, 10], "visible": False}]}

    assert kit.click_label(client, a11y, "start") is False
    assert client.sent == []


def test_click_label_returns_false_when_bounds_missing():
    client = _RecordingClient()
    a11y = {"a11y_tree": [{"label": "Start", "visible": True}]}

    assert kit.click_label(client, a11y, "start") is False
    assert client.sent == []


def test_click_label_handles_missing_a11y_tree_key():
    client = _RecordingClient()
    assert kit.click_label(client, {}, "start") is False
