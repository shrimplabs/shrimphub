"""Execution adapters for closure verification checks."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

import requests

from swarm.platform import resolve_godot_binary, shell_quote_command_arg
from swarm.closure.specs import normalize_godot_command


def _bounded_text(value: str | None, limit: int = 2000) -> str:
    return (value or "")[:limit]


def _result(
    *,
    ok: bool,
    status: str,
    check_type: str,
    command: str | None = None,
    url: str | None = None,
    exit_code: int | None = None,
    timed_out: bool = False,
    error: str | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    response_status: int | None = None,
    body_preview: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "status": status,
        "check_type": check_type,
        "command": command,
        "url": url,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "error": _bounded_text(error),
        "stdout": _bounded_text(stdout),
        "stderr": _bounded_text(stderr),
        "response_status": response_status,
        "body_preview": _bounded_text(body_preview),
    }


def _looks_like_gut_command(command: str | None) -> bool:
    text = (command or "").lower()
    return "gut_cmdln.gd" in text or re.search(r"\bgut\s+--run\b", text) is not None


def _gut_runtime_error(stdout: str | None, stderr: str | None) -> str | None:
    combined = "\n".join(part for part in [stdout or "", stderr or ""] if part).lower()
    if "nothing was run" in combined:
        return "gut reported 'Nothing was run'"
    if "you do not have any directories configured" in combined:
        return "gut has no configured test directory"
    return None


def _resolve_godot_command(command: str) -> str:
    normalized = normalize_godot_command(command) or command
    stripped = normalized.strip()
    if not stripped.lower().startswith("godot "):
        return normalized
    godot = shell_quote_command_arg(resolve_godot_binary())
    return godot + stripped[5:]


def run_command_check(command: str, *, cwd: str | Path | None = None, timeout: int = 60) -> dict[str, Any]:
    if not command or not isinstance(command, str):
        return _result(ok=False, status="invalid", check_type="command", error="command check requires a non-empty command")
    command = _resolve_godot_command(command)
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return _result(
            ok=False,
            status="timeout",
            check_type="command",
            command=command,
            timed_out=True,
            stdout=getattr(exc, "stdout", None),
            stderr=getattr(exc, "stderr", None),
            error=f"command timed out after {timeout}s",
        )
    except Exception as exc:
        return _result(
            ok=False,
            status="error",
            check_type="command",
            command=command,
            error=str(exc),
        )

    gut_error = _gut_runtime_error(completed.stdout, completed.stderr) if _looks_like_gut_command(command) else None
    ok = completed.returncode == 0 and not gut_error
    return _result(
        ok=ok,
        status="passed" if ok else "failed",
        check_type="command",
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        error=None if ok else (gut_error or f"command exited with status {completed.returncode}"),
    )


def _get_main_scene_from_project(project_root: Path) -> str | None:
    project_file = project_root / "project.godot"
    if not project_file.exists():
        return None
    try:
        content = project_file.read_text()
    except Exception:
        return None
    match = re.search(r'^run/main_scene="([^"]+)"', content, flags=re.MULTILINE)
    return match.group(1) if match else None


def run_godot_scene_check(
    *,
    cwd: str | Path | None = None,
    scene: str | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    project_root = Path(cwd) if cwd is not None else Path.cwd()
    main_scene = scene or _get_main_scene_from_project(project_root)
    if not main_scene:
        return _result(
            ok=False,
            status="failed",
            check_type="godot_scene",
            error="no main scene configured in project.godot",
        )

    validator_name = "_swarm_closure_scene_check.gd"
    validator_path = project_root / validator_name
    validator_source = f"""extends SceneTree
func _init():
\tvar packed = load("{main_scene}")
\tif packed == null:
\t\tprint("SCENE ERROR: Could not load main scene: {main_scene}")
\t\tquit(1)
\t\treturn
\tvar instance = packed.instantiate()
\tif instance == null:
\t\tprint("SCENE ERROR: Could not instantiate main scene: {main_scene}")
\t\tquit(1)
\t\treturn
\troot.add_child(instance)
\tprint("MAIN SCENE OK: {main_scene}")
\tquit(0)
"""
    validator_path.write_text(validator_source)
    godot_bin = resolve_godot_binary()
    command = f"{godot_bin} --headless --path . --script res://{validator_name} --quit"
    try:
        return run_command_check(command, cwd=project_root, timeout=timeout)
    finally:
        try:
            validator_path.unlink()
        except FileNotFoundError:
            pass


def run_http_check(
    url: str,
    *,
    timeout: int = 10,
    expected_status: int | None = 200,
) -> dict[str, Any]:
    if not url or not isinstance(url, str):
        return _result(ok=False, status="invalid", check_type="http", error="http check requires a non-empty url")
    try:
        response = requests.get(url, timeout=timeout)
    except requests.Timeout:
        return _result(
            ok=False,
            status="timeout",
            check_type="http",
            url=url,
            timed_out=True,
            error=f"http check timed out after {timeout}s",
        )
    except Exception as exc:
        return _result(
            ok=False,
            status="error",
            check_type="http",
            url=url,
            error=str(exc),
        )

    if expected_status is None:
        ok = 200 <= response.status_code < 300
    else:
        ok = response.status_code == expected_status
    return _result(
        ok=ok,
        status="passed" if ok else "failed",
        check_type="http",
        url=url,
        response_status=response.status_code,
        body_preview=response.text,
        error=None if ok else f"unexpected status {response.status_code}",
    )


def execute_check(check: Mapping[str, Any], *, cwd: str | Path | None = None, timeout: int | None = None) -> dict[str, Any]:
    check_type = (check.get("type") or "").strip().lower()
    if check_type == "command":
        return run_command_check(
            check.get("command") or "",
            cwd=check.get("cwd") or cwd,
            timeout=int(check.get("timeout") or timeout or 60),
        )
    if check_type == "godot_scene":
        return run_godot_scene_check(
            cwd=check.get("cwd") or cwd,
            scene=check.get("scene"),
            timeout=int(check.get("timeout") or timeout or 60),
        )
    if check_type == "http":
        return run_http_check(
            check.get("url") or "",
            timeout=int(check.get("timeout") or timeout or 10),
            expected_status=check.get("expected_status", 200),
        )
    return _result(
        ok=False,
        status="invalid",
        check_type=check_type or "unknown",
        error=f"unsupported verification check type: {check_type or 'unknown'}",
    )


def execute_ready_check(ready_check: Mapping[str, Any] | None, *, cwd: str | Path | None = None) -> dict[str, Any]:
    if not ready_check:
        return _result(ok=False, status="invalid", check_type="unknown", error="ready check is required")
    return execute_check(ready_check, cwd=cwd)
