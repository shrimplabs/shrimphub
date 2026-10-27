"""
swarm.tools.shell -- shell execution, godot detection, and git commit/push.

Imports from swarm.tools.core at call time to avoid circular imports.
Config vars (WORKSPACE, PROJECT, TASK_TYPE, READONLY, etc.) are set via
_sync_core_globals() in agent_runtime before each agent run.
"""

import os
import subprocess
import tempfile
import threading
import time
import re
import shlex
from pathlib import Path

from swarm.guard_log import log_blocked, log_outbound, outbound_hosts
from swarm.platform import kill_process_tree, popen_session_kwargs, shell_quote_command_arg
from swarm.tools._shared import _project_root, _safe_cwd
from swarm.tools.path_guard import (
    _command_attempts_protected_write,
    _protected_path_error,
)
_SHELL_ENV_STRIP = frozenset({"SWARM_AGENT_TOKEN", "SWARM_OPERATOR_TOKEN"})

from swarm.tools.shell_guard import (
    check_command,
    check_python_source,
    minimal_env,
    pinned_cwd,
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




def run(cmd: str, cwd=None, timeout: int = 60, env: dict | None = None):
    """Run a shell command; returns (returncode, stdout, stderr).

    ``cwd`` is pinned to the project root: a caller-supplied cwd outside the
    root is ignored (see shell_guard.pinned_cwd). ``env`` optionally replaces
    the inherited environment (used by run_python for a minimal env).
    """
    # Use temp files instead of PIPE. Agents sometimes launch background Godot
    # processes from shell snippets; inherited pipe FDs can keep communicate()
    # waiting forever after the shell exits.
    if env is None:
        # Shell children never see the per-spawn API token (or an operator
        # token): `env`/`printenv` in run_command must not be able to leak it
        # into a hand-rolled request.
        env = {k: v for k, v in os.environ.items() if k not in _SHELL_ENV_STRIP}
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
            cwd=pinned_cwd(cwd),
            env=env,
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

_PYTHON_BIN = re.compile(r"^(?:[\w./-]*/)?python(?:3(?:\.\d+)?)?$")
_ALLOWED_STDERR = ("2>&1", "2>/dev/null")
_LINE_COUNT = re.compile(r"^-(?:n)?(\d+)$")
_SED_RANGE = re.compile(r"^(\d+)(?:,(\d+))?p$")
_INLINE_STDIN_MAX = 5 * 1024 * 1024


def _unquoted_text(cmd: str) -> str | None:
    """``cmd`` with every quoted span removed (None if quotes are unbalanced)."""
    out, quote, i = [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if quote == "'":
            if ch == "'":
                quote = None
        elif quote == '"':
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "\\":
            i += 1
        else:
            out.append(ch)
        i += 1
    return None if quote else "".join(out)


def _line_filter(tokens: list[str]):
    """``head``/``tail`` with ``-N`` / ``-n N`` as (kind, n), else None."""
    if not tokens or tokens[0] not in ("head", "tail"):
        return None
    args = tokens[1:]
    if args[:1] == ["-n"] and len(args) == 2 and args[1].isdigit():
        return tokens[0], int(args[1])
    if len(args) == 1 and _LINE_COUNT.match(args[0]):
        return tokens[0], int(_LINE_COUNT.match(args[0]).group(1))
    if not args:
        return tokens[0], 10
    return None


def _inline_python_snippet(cmd: str) -> dict | None:
    """Parse the ``python -c`` one-liners agents keep writing, else None.

    Accepted shape (nothing else -- no other commands, substitutions or
    redirects)::

        [cd DIR &&] [PRODUCER FILE [2>...] |] python3 -c CODE [2>...] [| head/tail N]...

    PRODUCER is ``cat``, ``head``/``tail`` (-N / -n N) or ``sed -n 'A[,B]p'``
    over one file.  run_command reroutes these to run_python: the producer and
    output filters are applied in Python, so no shell ever runs, and the code
    still gets run_python's source and protected-path checks.  The code is
    never shell-expanded.
    """
    cmd = (cmd or "").strip()
    unquoted = _unquoted_text(cmd)
    if unquoted is None:
        return None
    rest = unquoted
    for allowed in _ALLOWED_STDERR:
        rest = rest.replace(allowed, " ")
    rest = rest.replace("&&", " ").replace("|", " ")
    if re.search(r"[\n;&<>`$()\\]", rest):
        return None
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars="|&")
        lexer.whitespace_split = True
        raw = list(lexer)
    except ValueError:
        return None
    # `&` is punctuation to the lexer, so `2>&1` arrives as `2>`, `&`, `1`.
    tokens, i = [], 0
    while i < len(raw):
        if raw[i:i + 3] == ["2>", "&", "1"]:
            tokens.append("2>&1")
            i += 3
        else:
            tokens.append(raw[i])
            i += 1
    # Group into simple commands; remember the operator before each one.
    segments, cur, ops = [], [], []
    for tok in tokens:
        if tok in ("|", "&&"):
            if not cur:
                return None
            segments.append(cur)
            ops.append(tok)
            cur = []
        elif tok in ("2>&1", "2>/dev/null"):
            continue
        elif tok in ("&", "||") or tok.startswith(("|", "&")):
            return None
        else:
            cur.append(tok)
    if not cur:
        return None
    segments.append(cur)
    ops.append(None)

    cd_dir = ""
    if segments[0][0] == "cd":
        if len(segments[0]) != 2 or ops[0] != "&&":
            return None
        cd_dir = segments[0][1]
        segments, ops = segments[1:], ops[1:]
    if not segments:
        return None

    producer = None
    py_index = next((i for i, seg in enumerate(segments) if _PYTHON_BIN.match(seg[0])), None)
    if py_index is None or py_index > 1:
        return None
    if py_index == 1:
        if ops[0] != "|":
            return None
        seg = segments[0]
        if seg[0] == "cat" and len(seg) == 2:
            producer = ("cat", None, seg[1])
        elif seg[0] == "sed" and len(seg) == 4 and seg[1] == "-n" and _SED_RANGE.match(seg[2]):
            m = _SED_RANGE.match(seg[2])
            producer = ("sed", (int(m.group(1)), int(m.group(2) or m.group(1))), seg[3])
        elif seg[0] in ("head", "tail") and len(seg) >= 2:
            filt = _line_filter(seg[:-1])
            if filt is None:
                return None
            producer = (filt[0], filt[1], seg[-1])
        else:
            return None
    py = segments[py_index]
    if len(py) != 3 or py[1] != "-c":
        return None
    filters = []
    for i in range(py_index + 1, len(segments)):
        if ops[i - 1] != "|":
            return None
        filt = _line_filter(segments[i])
        if filt is None:
            return None
        filters.append(filt)
    return {"code": py[2], "cd_dir": cd_dir, "producer": producer, "filters": filters}


def _apply_lines(kind: str, arg, text: str) -> str:
    lines = text.splitlines(keepends=True)
    if kind == "head":
        lines = lines[:arg]
    elif kind == "tail":
        lines = lines[-arg:] if arg else []
    elif kind == "sed":
        lines = lines[arg[0] - 1:arg[1]]
    return "".join(lines)


def _run_inline_python(parsed: dict, timeout: int) -> dict | None:
    """Run a parsed one-liner via run_python, or None to fall back to the guard."""
    from swarm.tools.path_guard import _confine_project_path
    base = None
    if parsed["cd_dir"]:
        base, err = _confine_project_path(parsed["cd_dir"], quiet=True)
        if err is not None or base is None or not base.is_dir():
            return None
    stdin_text = None
    if parsed["producer"]:
        kind, arg, path_arg = parsed["producer"]
        if base is not None and not os.path.isabs(os.path.expanduser(path_arg)):
            path_arg = str(base / path_arg)
        path, err = _confine_project_path(path_arg, quiet=True, read_only=True)
        if err is not None or path is None or not path.is_file():
            return None
        if path.stat().st_size > _INLINE_STDIN_MAX:
            return None
        stdin_text = path.read_text(errors="replace")
        if kind != "cat":
            stdin_text = _apply_lines(kind, arg, stdin_text)
    code = parsed["code"]
    if base is not None:
        code = f"import os as _os\n_os.chdir({str(base)!r})\n" + code
    result = run_python(code, timeout=timeout, stdin_text=stdin_text)
    if parsed["filters"] and "output" in result:
        out = result["output"]
        for kind, n in parsed["filters"]:
            out = _apply_lines(kind, n, out)
        result["output"] = out.rstrip("\n")
    result["note"] = ("python -c is not allowed in run_command; ran the snippet with "
                      "run_python instead (call run_python directly next time)")
    return result


# One target only: a quoted pattern, or a single unquoted word, mentioning godot.
_GODOT_KILL = re.compile(
    r"^(?:pkill|killall)\s+(?:-(?:9|KILL|TERM|15|f|i)\s+)*"
    r"(?:\"[^\"]*godot[^\"]*\"|'[^']*godot[^']*'|[^\s'\"]*godot[^\s'\"]*)$",
    re.IGNORECASE,
)
_GODOT_KILL_NOISE = re.compile(r"^(?:kill_game|sleep\s+[\d.]+|true|echo(?:\s+.*)?|:)$")


def _is_godot_kill_only(cmd: str) -> bool:
    """True when ``cmd`` only tries to stop Godot (pkill/killall godot, plus
    kill_game / sleep / echo / true glue) -- e.g.
    ``kill_game 2>/dev/null; pkill -f "godot.*--path.*raccoon"; sleep 1; echo done``.

    Mass kills are blocked because ``pkill -f godot`` also kills every other
    agent's game and validation runs; agents only ever mean their own game."""
    found = False
    for segment in re.split(r"\|\||&&|;|\n", (cmd or "").strip()):
        seg = re.sub(r"\s*(?:2>&1|2>/dev/null|>/dev/null)\s*", " ", segment).strip()
        if not seg:
            continue
        if _GODOT_KILL.match(seg):
            found = True
        elif not _GODOT_KILL_NOISE.match(seg):
            return False
    return found


def _kill_own_godot() -> dict:
    """Stop only this agent's Godot: the game launched via launch_game plus any
    Godot whose --path is this agent's project root (its private worktree)."""
    from swarm.qa_tools import _reap_stale_godot_for_path, kill_game
    killed = kill_game()
    reaped = _reap_stale_godot_for_path(str(_project_root() or ""))
    return {
        "ok": True,
        "output": f"stopped this agent's Godot (launched game: {killed.get('note', 'killed')}; "
                  f"other processes for this project: {len(reaped)})",
        "note": ("pkill/killall godot is not allowed -- it kills every agent's Godot. Ran kill_game "
                 "(this agent's processes only) instead; call the kill_game tool directly next time."),
    }


def _top_level_segments(cmd: str) -> list[tuple[str, str]] | None:
    """Split ``cmd`` at top-level ``;`` / ``&&`` / ``||`` / newlines into
    (segment, separator-after) pairs.  None if quotes are unbalanced."""
    out, buf, quote, i = [], [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(cmd):
                buf.append(cmd[i + 1]); i += 2; continue
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch; buf.append(ch)
        elif cmd.startswith("&&", i) or cmd.startswith("||", i):
            out.append(("".join(buf), cmd[i:i + 2])); buf = []; i += 2; continue
        elif ch in ";\n":
            out.append(("".join(buf), ch)); buf = []
        else:
            buf.append(ch)
        i += 1
    if quote:
        return None
    out.append(("".join(buf), ""))
    return out


def _split_leading_godot_kill(cmd: str) -> str | None:
    """If ``cmd`` STARTS with pkill/killall godot (plus kill_game/sleep/echo/true
    glue) and then does other work, return the rest; else None.

    Only a leading kill is rerouted, so the remaining commands keep their
    order and still go through every guard."""
    segs = _top_level_segments((cmd or "").strip())
    if not segs:
        return None
    lead, found = 0, False
    for seg, _sep in segs:
        clean = re.sub(r"\s*(?:2>&1|2>/dev/null|>/dev/null)\s*", " ", seg).strip()
        if _GODOT_KILL.match(clean):
            found = True
        elif clean and not _GODOT_KILL_NOISE.match(clean):
            break
        lead += 1
    if not found or lead == len(segs):
        return None
    rest = "".join(seg + sep for seg, sep in segs[lead:]).strip()
    return rest.rstrip(";&|").strip() or None


def run_command(cmd: str, timeout: int = 120) -> dict:
    if _sandboxed_agent():
        rerouted = _reroute_git_network(cmd, timeout)
        if rerouted is not None:
            return rerouted
    if _is_godot_kill_only(cmd):
        return _kill_own_godot()
    rest = _split_leading_godot_kill(cmd)
    if rest is not None:
        killed = _kill_own_godot()
        result = run_command(rest, timeout)
        result["note"] = (killed["note"] + " " + result.get("note", "")).strip()
        return result
    parsed = _inline_python_snippet(cmd)
    if parsed is not None:
        result = _run_inline_python(parsed, min(int(timeout), 300))
        if result is not None:
            return result
    experiment_sync_error = _experiment_git_sync_error(cmd)
    if experiment_sync_error:
        return {"ok": False, "error": experiment_sync_error, "stdout": "", "stderr": experiment_sync_error}
    catastrophic = check_command(cmd)
    if catastrophic:
        log_blocked("shell", "run_command", f"{catastrophic}: {cmd}")
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
        return _protected_path_error(path_hint, reason, tool="run_command", command=cmd)
    for host in outbound_hosts(cmd):
        log_outbound(host, "run_command", allowed=True)
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


def _git_toplevel_error(root: str | None = None) -> str | None:
    """Return None if *root* is the top level of a git repository (or a linked
    worktree), else a refusal string.

    swarm-controller-5dnz: git_commit/git_push used to run ``git`` through
    run() with cwd=pinned_cwd(None), which silently falls back to WORKSPACE or
    the process cwd when the project root is not a directory -- so an agent
    for a non-repo project could commit/push whatever repo the *controller*
    happened to be running from. Never fall back: the pinned root must itself
    be the repo top level.
    """
    root = root or _project_root()
    if not root:
        return "git refused: no project root is configured"
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:  # git missing, root unreadable, timeout
        return f"git refused: {root} is not the top level of a git repository ({exc})"
    if proc.returncode != 0:
        return f"git refused: {root} is not the top level of a git repository"
    top = proc.stdout.strip()
    try:
        same = top and os.path.realpath(top) == os.path.realpath(str(root))
    except Exception:
        same = False
    if not same:
        return (
            f"git refused: {root} is not the top level of a git repository "
            f"(git reports top level {top or '<none>'})"
        )
    return None


def git_root() -> tuple[str | None, str | None]:
    """Return ``(root, None)`` when the pinned project root (or worktree
    override) is a git top level, else ``(None, error)``. Used by every git
    write path so nothing ever runs against the process cwd."""
    root = _project_root()
    err = _git_toplevel_error(root)
    if err:
        return None, err
    return str(root), None


def git_commit(message: str, files: list = None) -> dict:
    import swarm.tools.core as _core
    READONLY = _core.READONLY
    TASK_TYPE = _core.TASK_TYPE
    if READONLY:
        return {"ok": False, "error": "Read-only task: git_commit is disabled"}
    if TASK_TYPE in ("python_plan", "plan"):
        return {"ok": False, "error": "plan tasks are read-only planners -- use create_task() instead of writing code"}
    root, root_err = git_root()
    if root_err:
        return {"ok": False, "error": root_err}
    _protected = ["addons/", ".godot/", ".import/"]
    if TASK_TYPE not in ("project_plan", "python_plan", "plan", "project_create", "audit", "triage"):
        code, staged, _ = run("git diff --name-only HEAD 2>/dev/null; git ls-files --others --exclude-standard", cwd=root)
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
            code, out, err = run(f"git add -- {shell_quote_command_arg(f)}", cwd=root)
            if code != 0:
                return {"ok": False, "error": f"git add {f} failed: {err}"}
    else:
        code, out, err = run("git add -A", cwd=root)
        if code == 0:
            # Unstage broadcast claim files — they are ephemeral coordination state,
            # not project artifacts. Committing them causes stale locks to accumulate
            # across agent runs and block future commits.
            run("git reset HEAD -- agent/broadcast/", cwd=root)
    if code != 0:
        return {"ok": False, "error": f"git add failed: {err}"}
    code, out, err = run(f'git commit -m "{message}"', cwd=root)
    if code != 0:
        return {"ok": False, "error": f"git commit failed: {err}"}
    # Invalidate cache — filesystem state changed after a commit
    try:
        import swarm.tools.core as _core2
        invalidate_shell_cache(_core2.PROJECT)
    except Exception:
        pass
    # Auto-push if a remote is configured
    push_out = ""
    _rc, _remotes, _ = run("git remote", cwd=root)
    if _remotes.strip():
        pushed = git_push()
        push_out = f"\nPush: {'ok' if pushed.get('ok') else 'failed: ' + str(pushed.get('error', '')).strip()}"
        if pushed.get("message") and pushed.get("ok"):
            push_out = f"\nPush: {pushed['message']}"
    return {"ok": True, "message": "Committed", "output": out + push_out}


def git_push() -> dict:
    import swarm.tools.core as _core
    if _core.READONLY:
        return {"ok": False, "error": "Read-only task: git_push is disabled"}
    root, root_err = git_root()
    if root_err:
        return {"ok": False, "error": root_err}
    from swarm.agent_token import AGENT_TOKEN_ENV
    if os.environ.get(AGENT_TOKEN_ENV):
        # Swarm agents are sandboxed: no route to GitHub and no git
        # credentials.  The controller pushes for them (swarm.git_push).
        if getattr(_core, "WORKTREE_BRANCH", ""):
            return {"ok": True, "message": (
                "Not pushed from the worktree: the controller merges this branch into "
                "the project and pushes it to origin when the task completes."
            )}
        result = _core._api_request("POST", f"/api/projects/{_core.PROJECT}/push", payload={}, timeout=150)
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error", "unknown error") if isinstance(result, dict) else str(result)
            return {"ok": False, "error": f"git push (via controller) failed: {err}"}
        return {"ok": True, "message": "Pushed (via controller)", "output": result.get("output", "")}
    code, out, err = run("git push 2>&1", cwd=root)
    if code != 0:
        return {"ok": False, "error": f"git push failed: {err}"}
    return {"ok": True, "message": "Pushed", "output": out}


def _sandboxed_agent() -> bool:
    from swarm.agent_token import AGENT_TOKEN_ENV
    return bool(os.environ.get(AGENT_TOKEN_ENV))


def git_fetch() -> dict:
    """Fetch origin.  Swarm agents have no network or git credentials, so the
    controller fetches into the project repo; worktrees share its refs, so
    origin/<branch> is then visible here."""
    import swarm.tools.core as _core
    from swarm.git_push import FETCH_TIMEOUT
    root, root_err = git_root()
    if root_err:
        return {"ok": False, "error": root_err}
    if _sandboxed_agent():
        result = _core._api_request("POST", f"/api/projects/{_core.PROJECT}/fetch", payload={},
                                     timeout=FETCH_TIMEOUT + 60)
        if not isinstance(result, dict):
            return {"ok": False, "error": f"git fetch (via controller) failed: {result}"}
        if result.get("ok"):
            result["message"] = "Fetched origin (via controller)"
        return result
    code, out, err = run("git fetch --prune origin 2>&1", cwd=root, timeout=FETCH_TIMEOUT)
    if code != 0:
        return {"ok": False, "error": f"git fetch failed: {out or err}"}
    _c, head, _ = run("git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null", cwd=root)
    default = head.strip().split("/", 1)[1] if head.strip().startswith("origin/") else "main"
    return {"ok": True, "default_branch": default, "message": "Fetched origin"}


def git_sync(strategy: str = "merge") -> dict:
    """Bring the current branch up to date with origin: fetch, then merge (or
    rebase) origin/<default branch> locally.  On conflicts the merge is LEFT
    IN PROGRESS and the conflicted files are returned: resolve them with
    patch_file, then git_commit (merge) or `git rebase --continue` (rebase)."""
    root, root_err = git_root()
    if root_err:
        return {"ok": False, "error": root_err}
    fetched = git_fetch()
    if not fetched.get("ok"):
        return fetched
    upstream = f"origin/{fetched.get('default_branch') or 'main'}"
    if strategy == "ff-only":
        cmd = f"git merge --ff-only {upstream}"
    elif strategy == "rebase":
        cmd = f"git rebase {upstream}"
    else:
        cmd = f"git merge --no-edit {upstream}"
    code, out, err = run(cmd + " 2>&1", cwd=root, timeout=120)
    if code == 0:
        return {"ok": True, "message": f"Up to date with {upstream}", "output": (out or "").strip()[-1500:]}
    _c, conflicted, _ = run("git diff --name-only --diff-filter=U", cwd=root)
    files = [f for f in conflicted.splitlines() if f.strip()]
    if files:
        cont = "git_commit" if strategy != "rebase" else "run_command('git rebase --continue')"
        return {"ok": False, "conflicts": files, "error": (
            f"{strategy} with {upstream} has conflicts in {len(files)} file(s): {files}. "
            f"Resolve each (remove the <<<<<<< ======= >>>>>>> markers keeping both sides' intent), "
            f"run validation, then {cont}. To give up instead: run_command('git {'rebase' if strategy == 'rebase' else 'merge'} --abort')."
        )}
    return {"ok": False, "error": f"{cmd} failed: {(out or err).strip()[-1500:]}"}


_GIT_NET = re.compile(r"^\s*git\s+(pull|fetch|push)\b(.*)$")


def _git_network_segment(seg: str) -> dict | None:
    """Run a typed `git pull/fetch/push` segment the sandbox-safe way, or
    None if it is not one.  Force pushes are never translated (the guard
    blocks them)."""
    m = _GIT_NET.match(seg)
    if not m:
        return None
    op, rest = m.group(1), m.group(2)
    try:
        args = [a for a in shlex.split(rest) if a not in ("2>&1",)]
    except ValueError:
        return None
    if op == "fetch":
        return git_fetch()
    if op == "push":
        if any(a in ("-f", "--force", "--force-with-lease", "--mirror", "--delete", "-d") or a.startswith("+")
               or a.startswith("--force") for a in args):
            return None
        return git_push()
    strategy = "rebase" if "--rebase" in args or "-r" in args else ("ff-only" if "--ff-only" in args else "merge")
    return git_sync(strategy)


def _reroute_git_network(cmd: str, timeout: int):
    """Typed git network commands in run_command (`git pull origin main
    --ff-only && ...`).  Network segments go through git_fetch/git_sync/
    git_push; the others run through run_command and its guards, in order,
    stopping at the first failure.  None when there is nothing to reroute."""
    segs = _top_level_segments((cmd or "").strip())
    if not segs or not any(_GIT_NET.match(seg) for seg, _ in segs):
        return None
    if any(sep == "||" for _seg, sep in segs):
        return None
    outputs = []
    for seg, _sep in segs:
        if not seg.strip():
            continue
        net = _git_network_segment(seg.strip())
        if net is None and _GIT_NET.match(seg.strip()):
            return None
        res = net if net is not None else run_command(seg.strip(), timeout)
        text = res.get("output") or res.get("stdout") or res.get("message") or ""
        if text:
            outputs.append(str(text).strip())
        if not res.get("ok"):
            res = dict(res)
            res["output"] = "\n".join(outputs)
            return res
    return {"ok": True, "stdout": "\n".join(outputs), "note": (
        "git pull/fetch/push ran through the controller (agents have no network for git); "
        "you can also call git_sync / git_fetch / git_push directly.")}


def run_python(code: str, timeout: int = 30, stdin_text: str | None = None) -> dict:
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

    blocked = check_python_source(code)
    if blocked:
        log_blocked("python", "run_python", blocked)
        return {
            "ok": False,
            "error": f"Code blocked: {blocked}. "
                     "This operation is not permitted for agent safety.",
        }
    # Same protected-path rule as run_command: a snippet that writes to
    # vendor/generated/guardrail paths (addons/, .godot/, swarm/tools/shell.py,
    # prompts/, config.json, ...) is refused before the interpreter starts.
    protected_write = _command_attempts_protected_write(code)
    if protected_write:
        reason, path_hint = protected_write
        return _protected_path_error(path_hint, reason, tool="run_python", command=code)

    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp_path = f.name
    stdin_path = None
    if stdin_text is not None:
        with tempfile.NamedTemporaryFile(suffix=".in", mode="w", delete=False, encoding="utf-8") as f:
            f.write(stdin_text)
            stdin_path = f.name

    try:
        cwd = pinned_cwd(None)
        redirect = f" < {shell_quote_command_arg(stdin_path)}" if stdin_path else ""
        code_rc, out, err = run(
            f"{shell_quote_command_arg(str(python_bin))} {shell_quote_command_arg(tmp_path)}{redirect}",
            cwd=cwd, timeout=timeout, env=minimal_env(),
        )
        combined = (out + ("\n" + err if err.strip() else "")).strip()
        if code_rc != 0:
            return {"ok": False, "returncode": code_rc, "output": combined}
        return {"ok": True, "output": combined}
    finally:
        for _p in (tmp_path, stdin_path):
            if _p:
                try:
                    Path(_p).unlink()
                except Exception:
                    pass
