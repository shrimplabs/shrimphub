from pathlib import Path
from unittest.mock import patch

import swarm.platform as platform_mod
import swarm.qa_tools as qa_tools


def test_project_venv_executable_prefers_windows_scripts(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_mod, "is_windows", lambda: True)
    exe = tmp_path / ".venv" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")

    result = platform_mod.project_venv_executable(tmp_path, "python")

    assert result == exe


def test_project_venv_executable_prefers_unix_bin(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_mod, "is_windows", lambda: False)
    exe = tmp_path / ".venv" / "bin" / "pytest"
    exe.parent.mkdir(parents=True)
    exe.write_text("")

    result = platform_mod.project_venv_executable(tmp_path, "pytest")

    assert result == exe


def test_popen_session_kwargs_isolates_windows_process_group(monkeypatch):
    monkeypatch.setattr(platform_mod, "is_windows", lambda: True)
    monkeypatch.setattr(platform_mod.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)

    assert platform_mod.popen_session_kwargs() == {"creationflags": 512}


def test_popen_session_kwargs_isolates_unix_session(monkeypatch):
    monkeypatch.setattr(platform_mod, "is_windows", lambda: False)
    monkeypatch.setattr(platform_mod.os, "setsid", lambda: None, raising=False)

    result = platform_mod.popen_session_kwargs()

    assert result["preexec_fn"] is platform_mod.os.setsid


def test_godot_binary_available_accepts_path_lookup(monkeypatch):
    monkeypatch.setattr(platform_mod.shutil, "which", lambda name: "/usr/bin/godot" if name == "godot" else None)

    assert platform_mod.godot_binary_available("godot") is True


def test_godot_binary_available_rejects_directory(tmp_path):
    godot_dir = tmp_path / "Godot_v4.6.2-stable_win64"
    godot_dir.mkdir()

    assert platform_mod.godot_binary_available(str(godot_dir)) is False


def test_resolve_godot_binary_expands_windows_godot_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_mod, "is_windows", lambda: True)
    monkeypatch.setattr(platform_mod, "is_macos", lambda: False)
    monkeypatch.setattr(platform_mod.shutil, "which", lambda name: None)
    godot_dir = tmp_path / "Godot_v4.6.2-stable_win64"
    godot_dir.mkdir()
    godot_exe = godot_dir / "Godot_v4.6.2-stable_win64.exe"
    godot_exe.write_text("")
    monkeypatch.setenv("GODOT_PATH", str(godot_dir))

    assert platform_mod.resolve_godot_binary() == str(godot_exe)


def test_resolve_godot_binary_prefers_windows_console_exe(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_mod, "is_windows", lambda: True)
    monkeypatch.setattr(platform_mod, "is_macos", lambda: False)
    monkeypatch.setattr(platform_mod.shutil, "which", lambda name: None)
    godot_dir = tmp_path / "Godot_v4.6.2-stable_win64"
    godot_dir.mkdir()
    gui_exe = godot_dir / "Godot_v4.6.2-stable_win64.exe"
    console_exe = godot_dir / "Godot_v4.6.2-stable_win64_console.exe"
    gui_exe.write_text("")
    console_exe.write_text("")
    monkeypatch.setenv("GODOT_PATH", str(godot_dir))

    assert platform_mod.resolve_godot_binary() == str(console_exe)


def test_shell_quote_command_arg_quotes_windows_spaces(monkeypatch):
    monkeypatch.setattr(platform_mod, "is_windows", lambda: True)

    assert platform_mod.shell_quote_command_arg(r"C:\Program Files\Godot\Godot.exe") == r'"C:\Program Files\Godot\Godot.exe"'


def test_qa_focus_game_returns_clear_error_without_macos_automation(monkeypatch):
    monkeypatch.setattr(qa_tools, "macos_gui_automation_available", lambda: False)

    result = qa_tools.qa_focus_game()

    assert result["ok"] is False
    assert "macOS GUI automation support" in result["error"]


def test_take_screenshot_fallback_reports_platform_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(qa_tools, "is_macos", lambda: False)
    monkeypatch.setattr(qa_tools, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(qa_tools, "_state_server_send", lambda *args, **kwargs: {"error": "not available"})

    result = qa_tools.take_screenshot("shot")

    assert result["ok"] is False
    assert "macOS screencapture" in result["error"]


def _pgrep_result(stdout):
    return type("R", (), {"stdout": stdout, "returncode": 0})()


def test_godot_processes_uses_pgrep_not_setuid_ps(monkeypatch):
    # /bin/ps is setuid; the agent seatbelt profile refuses to exec it.
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _pgrep_result(
            "101 /opt/homebrew/bin/godot --path /w/proj --resolution 1280x720\n"
            "102 /usr/bin/python3 tool.py --path /w/godot-proj\n"
            "103 /Applications/Godot.app/Contents/MacOS/Godot --headless\n"
        )

    monkeypatch.setattr(platform_mod, "is_windows", lambda: False)
    monkeypatch.setattr(platform_mod.subprocess, "run", fake_run)
    procs = platform_mod.godot_processes()
    assert [c[0] for c in calls] == ["pgrep"]
    # Only the executable name counts -- a "godot" argument is not Godot.
    assert sorted(procs) == [101, 103]
    assert procs[101].endswith("--resolution 1280x720")


def test_reap_stale_godot_only_kills_matching_project(monkeypatch, tmp_path):
    proj = tmp_path / "proj"
    other = tmp_path / "proj-other"
    link = tmp_path / "link"
    proj.mkdir()
    link.symlink_to(proj)
    monkeypatch.setattr(qa_tools, "godot_processes", lambda: {
        201: f"/opt/homebrew/bin/godot --path {proj} --resolution 1280x720",
        202: f"/opt/homebrew/bin/godot --headless {proj}",
        203: f"/opt/homebrew/bin/godot --path {other}",
        204: f"/opt/homebrew/bin/godot --path {link}/ --headless",
    })
    killed = []
    monkeypatch.setattr(qa_tools.os, "kill", lambda pid, sig: killed.append(pid))
    assert qa_tools._reap_stale_godot_for_path(str(proj)) == [201, 204]
    assert killed == [201, 204]
