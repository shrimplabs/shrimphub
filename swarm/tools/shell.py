"""
swarm.tools.shell -- shell execution, godot detection, and git commit/push.

Imports from swarm.tools.core at call time to avoid circular imports.
Config vars (WORKSPACE, PROJECT, TASK_TYPE, READONLY, etc.) are set via
_sync_core_globals() in agent_runtime before each agent run.
"""

import subprocess
from pathlib import Path

from swarm.platform import kill_process_tree, popen_session_kwargs, shell_quote_command_arg
from swarm.tools._shared import _project_root, _safe_cwd
from swarm.tools.path_guard import (
    _check_catastrophic_command,
    _command_attempts_protected_write,
    _protected_path_error,
)




def run(cmd: str, cwd=None, timeout: int = 60):
    """Run a shell command; returns (returncode, stdout, stderr)."""
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_safe_cwd(cwd),
        **popen_session_kwargs(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        proc.communicate()
        return -1, "", f"Command timed out after {timeout}s"


def _looks_like_godot_command(cmd: str) -> bool:
    lowered = (cmd or "").lower()
    return "godot" in lowered


def _godot_runtime_error(stdout: str, stderr: str) -> str | None:
    combined = "\n".join(part for part in ((stdout or "").strip(), (stderr or "").strip()) if part).strip()
    if not combined:
        return None
    failure_patterns = (
        "SCRIPT ERROR:",
        "SCENE ERROR:",
        "ERROR: Parse Error:",
        "Parse Error:",
        "Could not find type ",
        "Failed to load script",
        "Failed to compile",
    )
    for line in combined.splitlines():
        stripped = line.strip()
        if any(pattern in stripped for pattern in failure_patterns):
            return stripped
    return None


# ---------------------------------------------------------------------------
# Shell / git tools
# ---------------------------------------------------------------------------

def run_command(cmd: str, timeout: int = 120) -> dict:
    catastrophic = _check_catastrophic_command(cmd)
    if catastrophic:
        return {
            "ok": False,
            "error": (
                f"Command blocked: {catastrophic}. "
                "This operation is not permitted in agent tasks because it is "
                "irreversible or has unbounded destructive scope. "
                "Use targeted file operations (patch_file, write_file, run_command with a specific path) instead."
            ),
        }
    protected_write = _command_attempts_protected_write(cmd)
    if protected_write:
        reason, path_hint = protected_write
        return _protected_path_error(path_hint, reason)
    timeout = min(int(timeout), 300)
    code, out, err = run(cmd, timeout=timeout)
    if code != 0 and "timed out" in err.lower():
        return {"ok": False, "stdout": out, "stderr": f"Command timed out after {timeout}s. For long-running commands like Godot headless tests, pass a higher timeout e.g. run_command(cmd, timeout=300)"}
    godot_error = _godot_runtime_error(out, err) if _looks_like_godot_command(cmd) else None
    if godot_error:
        err = "\n".join(part for part in ((err or "").strip(), f"Godot runtime error detected: {godot_error}") if part)
        return {"ok": False, "stdout": out, "stderr": err}
    return {"ok": code == 0, "stdout": out, "stderr": err}


def git_commit(message: str, files: list = None) -> dict:
    import swarm.tools.core as _core
    READONLY = _core.READONLY
    TASK_TYPE = _core.TASK_TYPE
    if READONLY:
        return {"ok": False, "error": "Read-only task: git_commit is disabled"}
    if TASK_TYPE in ("python_plan", "plan"):
        return {"ok": False, "error": "plan tasks are read-only planners -- use create_task() instead of writing code"}
    _protected = ["addons/", ".godot/", ".import/"]
    if TASK_TYPE not in ("project_plan", "python_plan", "plan", "project_create", "audit", "triage"):
        code, staged, _ = run("git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard")
        _violations = [
            f for f in staged.splitlines()
            if any(f.startswith(p) for p in _protected) or f.endswith(".uid")
        ]
        if _violations:
            return {
                "ok": False,
                "error": (
                    f"Commit blocked: modifying {_protected + ['*.uid']} is not allowed for {TASK_TYPE} tasks. "
                    f"Offending files: {_violations}. "
                    "Do not modify vendor or generated engine artifacts."
                ),
            }
    if files:
        for f in files:
            code, out, err = run(f"git add -- {shell_quote_command_arg(f)}")
            if code != 0:
                return {"ok": False, "error": f"git add {f} failed: {err}"}
    else:
        code, out, err = run("git add -A")
    if code != 0:
        return {"ok": False, "error": f"git add failed: {err}"}
    code, out, err = run(f'git commit -m "{message}"')
    if code != 0:
        return {"ok": False, "error": f"git commit failed: {err}"}
    return {"ok": True, "message": "Committed", "output": out}


def git_push() -> dict:
    import swarm.tools.core as _core
    if _core.READONLY:
        return {"ok": False, "error": "Read-only task: git_push is disabled"}
    code, out, err = run("git push 2>&1")
    if code != 0:
        return {"ok": False, "error": f"git push failed: {err}"}
    return {"ok": True, "message": "Pushed", "output": out}
