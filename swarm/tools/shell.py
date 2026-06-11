"""
swarm.tools.shell -- shell execution, godot detection, and git commit/push.

Imports from swarm.tools.core at call time to avoid circular imports.
Config vars (WORKSPACE, PROJECT, TASK_TYPE, READONLY, etc.) are set via
_sync_core_globals() in agent_runtime before each agent run.
"""

import subprocess
import tempfile
import threading
import time
import re
from pathlib import Path

from swarm.platform import kill_process_tree, popen_session_kwargs, shell_quote_command_arg
from swarm.tools._shared import _project_root, _safe_cwd
from swarm.tools.path_guard import (
    _check_catastrophic_command,
    _command_attempts_protected_write,
    _protected_path_error,
)

# ---------------------------------------------------------------------------
# Concurrency gate — cap simultaneous shell subprocesses so 12+ agents don't
# all spawn `find` / `grep` at the same time and thrash the filesystem.
# Godot validation runs in the monitor thread via a separate code path and is
# not affected by this gate.
# ---------------------------------------------------------------------------
_SHELL_SLOTS = int(__import__("os").environ.get("SWARM_SHELL_SLOTS", "6"))
_shell_semaphore = threading.Semaphore(_SHELL_SLOTS)

# ---------------------------------------------------------------------------
# Result cache — keyed on (command, cwd), TTL 30s.
# Invalidated when write_file / git_commit fires (see invalidate_shell_cache).
# Only read-only-ish commands are cached (find, grep, git log, ls, wc, cat).
# ---------------------------------------------------------------------------
_CACHE_TTL = 30  # seconds
_cache: dict[tuple, tuple] = {}  # key -> (result, expires_at)
_cache_lock = threading.Lock()

_CACHEABLE_PREFIXES = ("find ", "grep ", "git log", "git show", "git diff",
                       "ls ", "ls\n", "wc ", "cat ", "head ", "tail ",
                       "git status", "git branch")

_EXPERIMENT_PROJECT_RE = re.compile(r"-run\d+$")
_GIT_SYNC_RE = re.compile(r"(^|[;&|]\s*)git\s+(pull|fetch)\b")


def _is_cacheable(cmd: str) -> bool:
    stripped = cmd.strip()
    return any(stripped.startswith(p) for p in _CACHEABLE_PREFIXES)


def _cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[1] > time.monotonic():
            return entry[0]
        if entry:
            del _cache[key]
    return None


def _cache_set(key, value):
    with _cache_lock:
        _cache[key] = (value, time.monotonic() + _CACHE_TTL)


def invalidate_shell_cache(project: str = None):
    """Call after write_file / git_commit to flush stale entries.

    If project is given, only evict entries whose command contains the
    project name. Otherwise flush everything.
    """
    with _cache_lock:
        if project:
            evict = [k for k in _cache if project in k[0]]
        else:
            evict = list(_cache.keys())
        for k in evict:
            del _cache[k]


def _local_origin_repo_name(origin: str) -> str:
    origin = (origin or "").strip()
    if not origin or "://" in origin or "@" in origin:
        return ""
    name = Path(origin).name
    if name.endswith(".git"):
        name = name[:-4]
    return name


def _experiment_git_sync_error(cmd: str) -> str | None:
    """Block experiment arms from syncing against their source repo.

    Experiment clones still need normal pull/fetch for their own worktrees, but
    the origin must be the arm's isolated remote. A clone whose origin is still
    the source project can silently fast-forward to another run's history.
    """
    if not _GIT_SYNC_RE.search(cmd or ""):
        return None
    try:
        from swarm.tools import _shared
        project = getattr(_shared, "PROJECT", "") or ""
        workspace = Path(getattr(_shared, "WORKSPACE", Path(".")))
    except Exception:
        return None
    if not project or not _EXPERIMENT_PROJECT_RE.search(project):
        return None

    cwd = _safe_cwd(None)
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    origin = proc.stdout.strip()
    origin_name = _local_origin_repo_name(origin)
    if not origin_name:
        return None
    if origin_name != project:
        return (
            f"Command blocked: experiment project '{project}' has origin '{origin}', "
            f"which resolves to local repo '{origin_name}' instead of this arm. "
            "Experiment arms may pull/fetch only from their own isolated remote."
        )
    return None




def run(cmd: str, cwd=None, timeout: int = 60):
    """Run a shell command; returns (returncode, stdout, stderr)."""
    # Use temp files instead of PIPE. Agents sometimes launch background Godot
    # processes from shell snippets; inherited pipe FDs can keep communicate()
    # waiting forever after the shell exits.
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as stdout_file, \
            tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as stderr_file:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=_safe_cwd(cwd),
            **popen_session_kwargs(),
        )
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            stdout_file.seek(0)
            stderr_file.seek(0)
            return -1, stdout_file.read(), f"{stderr_file.read()}Command timed out after {timeout}s"

        stdout_file.seek(0)
        stderr_file.seek(0)
        return proc.returncode, stdout_file.read(), stderr_file.read()


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
    experiment_sync_error = _experiment_git_sync_error(cmd)
    if experiment_sync_error:
        return {"ok": False, "error": experiment_sync_error, "stdout": "", "stderr": experiment_sync_error}
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

    # Cache hit — skip the semaphore entirely for repeated read-only commands
    cache_key = (cmd.strip(), str(_safe_cwd(None)))
    if _is_cacheable(cmd):
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    # Semaphore gate — wait for a shell slot before spawning a subprocess
    with _shell_semaphore:
        code, out, err = run(cmd, timeout=timeout)

    if code != 0 and "timed out" in err.lower():
        return {"ok": False, "stdout": out, "stderr": f"Command timed out after {timeout}s. For long-running commands like Godot headless tests, pass a higher timeout e.g. run_command(cmd, timeout=300)"}
    godot_error = _godot_runtime_error(out, err) if _looks_like_godot_command(cmd) else None
    if godot_error:
        err = "\n".join(part for part in ((err or "").strip(), f"Godot runtime error detected: {godot_error}") if part)
        return {"ok": False, "stdout": out, "stderr": err}

    # Cap output to prevent large outputs from bloating context
    _MAX_OUTPUT_CHARS = 20_000
    truncated_stdout = out
    truncated_stderr = err
    output_note = None
    if len(out) + len(err) > _MAX_OUTPUT_CHARS:
        keep = _MAX_OUTPUT_CHARS // 2
        if len(out) > keep:
            truncated_stdout = out[:keep] + f"\n[stdout truncated — {len(out) - keep} chars omitted]"
        if len(err) > keep:
            truncated_stderr = err[:keep] + f"\n[stderr truncated — {len(err) - keep} chars omitted]"
        output_note = "Output truncated. Pipe through grep/head to reduce output if you need a specific section."

    result = {"ok": code == 0, "stdout": truncated_stdout, "stderr": truncated_stderr}
    if output_note:
        result["note"] = output_note

    # Cache successful read-only results
    if _is_cacheable(cmd) and code == 0:
        _cache_set(cache_key, result)

    return result


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
    # Invalidate cache — filesystem state changed after a commit
    try:
        import swarm.tools.core as _core2
        invalidate_shell_cache(_core2.PROJECT)
    except Exception:
        pass
    return {"ok": True, "message": "Committed", "output": out}


def git_push() -> dict:
    import swarm.tools.core as _core
    if _core.READONLY:
        return {"ok": False, "error": "Read-only task: git_push is disabled"}
    code, out, err = run("git push 2>&1")
    if code != 0:
        return {"ok": False, "error": f"git push failed: {err}"}
    return {"ok": True, "message": "Pushed", "output": out}


def run_python(code: str, timeout: int = 30) -> dict:
    """Write a Python snippet to a temp file and execute it with the venv Python.

    Finds the best available interpreter: project .venv, swarm .venv, then
    sys.executable as fallback. Returns stdout/stderr combined.
    """
    import sys as _sys
    import swarm.tools.core as _core

    # Find best Python interpreter
    candidates = []
    proj_root = _project_root()
    if proj_root:
        candidates.append(Path(proj_root) / ".venv" / "bin" / "python")
    # Swarm controller's own venv
    swarm_root = Path(__file__).parent.parent.parent
    candidates.append(swarm_root / ".venv" / "bin" / "python")
    # Current interpreter as last resort
    candidates.append(Path(_sys.executable))

    python_bin = None
    for c in candidates:
        if c.exists():
            python_bin = c
            break

    if python_bin is None:
        return {"ok": False, "error": "No Python interpreter found"}

    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp_path = f.name

    try:
        cwd = _project_root() or Path(".")
        code_rc, out, err = run(f"{python_bin} {tmp_path}", cwd=cwd, timeout=timeout)
        combined = (out + ("\n" + err if err.strip() else "")).strip()
        if code_rc != 0:
            return {"ok": False, "returncode": code_rc, "output": combined}
        return {"ok": True, "output": combined}
    finally:
        try:
            Path(tmp_path).unlink()
        except Exception:
            pass
