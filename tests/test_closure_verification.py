from unittest.mock import MagicMock, patch

from swarm.closure.verification import (
    _resolve_godot_command,
    execute_check,
    execute_ready_check,
    run_godot_scene_check,
    run_command_check,
    run_http_check,
)


def test_run_command_check_passes_through_success():
    completed = MagicMock(returncode=0, stdout="ok\n", stderr="")
    with patch("subprocess.run", return_value=completed) as run:
        result = run_command_check("pytest -q", cwd="/tmp/proj", timeout=30)

    run.assert_called_once()
    assert result["ok"] is True
    assert result["status"] == "passed"
    assert result["command"] == "pytest -q"
    assert result["stdout"] == "ok\n"


def test_run_command_check_normalizes_failure():
    completed = MagicMock(returncode=2, stdout="", stderr="boom")
    with patch("subprocess.run", return_value=completed):
        result = run_command_check("pytest -q")

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["exit_code"] == 2
    assert "status 2" in result["error"]


def test_run_command_check_treats_empty_gut_run_as_failure():
    completed = MagicMock(returncode=0, stdout="[ERROR]:  Nothing was run.\n", stderr="")
    with patch("subprocess.run", return_value=completed):
        result = run_command_check("godot --headless --path . -s res://addons/gut/gut_cmdln.gd -- -gdir=res://test -gexit")

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "Nothing was run" in result["error"]


def test_resolve_godot_command_rewrites_legacy_gut(monkeypatch):
    monkeypatch.setattr("swarm.closure.verification.resolve_godot_binary", lambda: r"C:\Godot\Godot_console.exe")
    resolved = _resolve_godot_command("gut --run --exit")

    assert r"C:\Godot\Godot_console.exe" in resolved
    assert "--headless --path . --script" in resolved
    assert "gut_cmdln.gd" in resolved


def test_run_command_check_normalizes_timeout():
    with patch("subprocess.run", side_effect=TimeoutError()):
        result = run_command_check("pytest -q")

    assert result["ok"] is False
    assert result["status"] == "error"


def test_run_command_check_handles_subprocess_timeout():
    import subprocess

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pytest -q", timeout=12)):
        result = run_command_check("pytest -q", timeout=12)

    assert result["ok"] is False
    assert result["status"] == "timeout"
    assert result["timed_out"] is True


def test_run_http_check_passes_expected_status():
    response = MagicMock(status_code=200, text='{"ok":true}')
    with patch("requests.get", return_value=response) as get:
        result = run_http_check("http://127.0.0.1:5001/api/health", timeout=5)

    get.assert_called_once()
    assert result["ok"] is True
    assert result["status"] == "passed"
    assert result["response_status"] == 200


def test_run_http_check_normalizes_unexpected_status():
    response = MagicMock(status_code=503, text="unavailable")
    with patch("requests.get", return_value=response):
        result = run_http_check("http://127.0.0.1:5001/api/health")

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["response_status"] == 503
    assert "unexpected status 503" == result["error"]


def test_run_http_check_normalizes_timeout_and_exception():
    with patch("requests.get", side_effect=Exception("connection refused")):
        error_result = run_http_check("http://127.0.0.1:5001/api/health")
    assert error_result["ok"] is False
    assert error_result["status"] == "error"
    assert "connection refused" in error_result["error"]

    import requests

    with patch("requests.get", side_effect=requests.Timeout()):
        timeout_result = run_http_check("http://127.0.0.1:5001/api/health", timeout=3)
    assert timeout_result["status"] == "timeout"
    assert timeout_result["timed_out"] is True


def test_execute_check_dispatches_and_rejects_unknown_types():
    with patch("swarm.closure.verification.run_command_check", return_value={"ok": True, "status": "passed"}) as command:
        result = execute_check({"type": "command", "command": "pytest -q"})
    command.assert_called_once()
    assert result["status"] == "passed"

    invalid = execute_check({"type": "godot_scene"})
    assert invalid["ok"] is False
    assert invalid["status"] == "failed"


def test_execute_check_dispatches_godot_scene(tmp_path):
    (tmp_path / "project.godot").write_text('[application]\nrun/main_scene="res://scenes/main.tscn"\n')
    completed = MagicMock(returncode=0, stdout="MAIN SCENE OK\n", stderr="")
    with patch("subprocess.run", return_value=completed) as run:
        result = execute_check({"type": "godot_scene"}, cwd=tmp_path)
    run.assert_called_once()
    assert result["ok"] is True
    assert result["status"] == "passed"


def test_execute_ready_check_requires_payload():
    result = execute_ready_check(None)
    assert result["ok"] is False
    assert result["status"] == "invalid"
