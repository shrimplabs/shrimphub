"""swarm.tools.shell_guard -- extended command / Python-source denylist for agent shells.

Agents run as the host user, so ``run_command`` (shell=True) and ``run_python``
are the main host-risk surface. This module layers an extended denylist on top
of the small catastrophic-pattern check in ``swarm.tools.path_guard`` and also
inspects Python source handed to ``run_python``.

Public API:

    check_command(cmd)        -> str | None   reason to block a shell command
    check_python_source(src)  -> str | None   reason to block a run_python snippet
    pinned_cwd(cwd)           -> str          caller cwd clamped to the project root
    minimal_env()             -> dict         PATH/HOME/LANG/PYTHONPATH/GODOT_PATH only

All matching is done on *command boundaries* (start of string, or after
``;``, ``&&``, ``||``, ``|``, ``(``/``$(``, backtick, ``sudo``, ``xargs``,
``env``, ``nohup``, ``-exec``) so that ``grep osascript foo.gd`` is not
blocked while ``osascript -e ...`` is.

path_guard.py is deliberately not modified here -- we call its
``_check_catastrophic_command`` first and add on top.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
from pathlib import Path

from swarm.tools._shared import _project_root, _safe_cwd, log
from swarm.tools.path_guard import _check_catastrophic_command, _is_asset_library_path, _is_scratch_path

# ---------------------------------------------------------------------------
# Command-boundary prefix
# ---------------------------------------------------------------------------
# A command name is considered "at a boundary" when it is at the start of the
# string, follows a shell separator (; & | ( ` newline), or follows a wrapper
# that executes its arguments (sudo/xargs/env/nohup/time/exec/find -exec) and
# any number of leading VAR=value assignments.
_B = (
    r"(?:^|[;&|(`\n]\s*|"
    r"\b(?:xargs|nohup|time|exec|env|command|builtin|then|do|else)\s+|"
    r"-exec(?:dir)?\s+)"
    r"(?:[A-Za-z_]\w*=\S*\s+)*"
    r"(?:[\w.~/-]*/)?"  # optional path prefix (/usr/bin/osascript, ./x)
)
_END = r"(?=\s|$)"
_ARGS = r"(?P<args>[^;&|\n]*)"


def _name(*names: str) -> str:
    return _B + r"(?:" + "|".join(re.escape(n) for n in names) + r")" + _END


# (pattern, reason) -- plain "command name at boundary" denylist
_BLOCKED_COMMANDS: list[tuple[str, str]] = [
    (_name("osascript"), "osascript (AppleScript / system automation)"),
    (_name("launchctl"), "launchctl (launchd service control)"),
    (_B + r"defaults\s+(?:write|delete|import)" + _END, "defaults write/delete (macOS preference mutation)"),
    (_name("security"), "security (macOS keychain CLI)"),
    (_name("crontab"), "crontab (persistent scheduled execution)"),
    (_name("ssh", "scp", "sftp"), "ssh/scp/sftp (remote shell / file transfer)"),
    (_name("sudo", "doas"), "sudo/doas (privilege escalation)"),
    (_name("open"), "open (macOS application launcher)"),
    (_name("nc", "ncat", "socat", "netcat"), "nc/ncat/socat (raw network sockets)"),
    (_name("shutdown", "reboot", "halt", "poweroff"), "shutdown/reboot (host power control)"),
    (_name("diskutil"), "diskutil (disk / volume manipulation)"),
    (_name("tmutil"), "tmutil (Time Machine manipulation)"),
    (_name("csrutil"), "csrutil (System Integrity Protection control)"),
    (_name("spctl"), "spctl (Gatekeeper control)"),
    (_name("networksetup"), "networksetup (network configuration)"),
    (_name("pfctl"), "pfctl (packet filter / firewall control)"),
    (_name("killall"), "killall (mass process kill by name)"),
    (_B + r"kill\s+(?:[^;&|\n]*\s)?-1" + _END, "kill -1 (signal every process)"),
    # Inline interpreter code: agents have run_python for this (which is guarded).
    (_B + r"python(?:\d(?:\.\d+)?)?\s+(?:[^;&|\n]*\s)?-c" + _END, "python -c (inline code execution; use run_python)"),
    (_B + r"perl\s+(?:[^;&|\n]*\s)?-[a-zA-Z]*e" + _END, "perl -e (inline code execution)"),
    (_B + r"ruby\s+(?:[^;&|\n]*\s)?-e" + _END, "ruby -e (inline code execution)"),
    (_B + r"node\s+(?:[^;&|\n]*\s)?(?:-e|--eval|-p|--print)" + _END, "node -e (inline code execution)"),
    # Download-and-execute
    (r"\b(?:curl|wget)\b[^;&|\n]*\|\s*(?:sudo\s+)?(?:[\w./-]*/)?(?:sh|bash|zsh|ksh|dash|python(?:\d(?:\.\d+)?)?|perl|ruby|node)" + _END,
     "curl/wget piped into an interpreter (download-and-execute)"),
    (r"\b(?:sh|bash|zsh|ksh|dash)\s+<\(\s*(?:curl|wget)\b", "shell process substitution from curl/wget (download-and-execute)"),
]

# curl / wget flags that send data or change the HTTP method.
_CURL_SEND_FLAGS = re.compile(
    r"(?:^|\s)(?:"
    r"-[a-zA-Z]*[dFT][a-zA-Z]*"  # -d, -F, -T and letter clusters containing them
    r"|--data(?:-\w+)?|--form(?:-string)?|--upload-file|--json"
    r"|--post-data|--post-file|--body-data|--body-file"
    r"|--method(?:\s*=?\s*(?:POST|PUT|DELETE|PATCH))?"
    r")(?=\s|=|$)",
)  # case-sensitive: curl short flags differ by case (-f != -F, -t != -T)
_CURL_METHOD = re.compile(r"(?:^|\s)(?:-X|--request)\s*=?\s*['\"]?(?:POST|PUT|DELETE|PATCH)\b", re.IGNORECASE)
_CURL_OUTPUT = re.compile(r"(?:^|\s)(?:-o|--output)\s*=?\s*(?P<t>\"[^\"]*\"|'[^']*'|[^\s;&|]+)")
_CURL_CMD = re.compile(_B + r"(?:curl|wget)" + _END + _ARGS, re.IGNORECASE)
# The swarm controller API (see swarm.api_authz).  run_command may only GET it:
# every mutation must go through the tool layer, which carries the agent token.
_SWARM_API_TARGET = re.compile(
    r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|::1):5001(?=[/\s'\"?#]|$)"
    r"|:5001/api(?=[/\s'\"?#]|$)",
    re.IGNORECASE,
)
_CURL_ANY_METHOD = re.compile(
    r"(?:^|\s)(?:-X|--request|--method)\s*=?\s*['\"]?(?P<m>[A-Za-z]+)", re.IGNORECASE
)

_FILE_OP_CMD = re.compile(_B + r"(?:rm|mv|cp)" + _END + _ARGS, re.IGNORECASE)
_TEE_CMD = re.compile(_B + r"tee" + _END + _ARGS, re.IGNORECASE)
_REDIRECT = re.compile(r"(?<![<>])\d?>{1,2}\s*(?P<t>\"[^\"]*\"|'[^']*'|[^\s;&|<>]+)")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _resolved_root() -> Path:
    root = _project_root() or "."
    try:
        return Path(root).expanduser().resolve()
    except Exception:
        return Path(root).absolute()


def _is_inside_root(p: Path) -> bool:
    root = _resolved_root()
    try:
        resolved = p.expanduser().resolve()
    except Exception:
        resolved = p.absolute()
    return resolved == root or root in resolved.parents


def _unsafe_path_reason(token: str) -> str | None:
    """Return a reason if *token* points outside the project root (or is a
    home-relative / parent-traversal path), else None."""
    t = (token or "").strip().strip("'\"")
    if not t or t.startswith("&"):
        return None
    if t.startswith("~") or t.startswith("$HOME") or t.startswith("${HOME"):
        return f"home-relative path '{t}'"
    if t == ".." or t.startswith("../") or "/../" in t or t.endswith("/.."):
        return f"parent-directory traversal '{t}'"
    if t.startswith("/"):
        if t.startswith("/dev/"):
            return None
        if not _is_inside_root(Path(t)) and not _is_scratch_path(Path(os.path.realpath(t))):
            return f"absolute path outside the project root '{t}'"
    return None


def _split_args(args: str) -> list[str]:
    try:
        return shlex.split(args, posix=True)
    except ValueError:
        return args.split()


def _check_file_op_paths(cmd: str) -> str | None:
    for m in _FILE_OP_CMD.finditer(cmd):
        # The verb is the word before the args (group(0) also holds the args).
        verb = m.string[m.start():m.start("args")].strip().split()[-1].split("/")[-1]
        paths = [t for t in _split_args(m.group("args")) if not t.startswith("-")]
        for i, tok in enumerate(paths):
            # cp may read FROM a configured asset library; its destination
            # (last argument) and every rm/mv argument still must be safe.
            src = tok.strip("'\"")
            if verb.lower() == "cp" and i < len(paths) - 1 and src.startswith("/") \
                    and _is_asset_library_path(Path(os.path.realpath(src))):
                continue
            reason = _unsafe_path_reason(tok)
            if reason:
                return f"{verb} targeting {reason}"
    return None


def _check_redirect_targets(cmd: str) -> str | None:
    for m in _REDIRECT.finditer(cmd):
        reason = _unsafe_path_reason(m.group("t"))
        if reason:
            return f"shell redirection writing to {reason}"
    for m in _TEE_CMD.finditer(cmd):
        for tok in _split_args(m.group("args")):
            if tok.startswith("-"):
                continue
            reason = _unsafe_path_reason(tok)
            if reason:
                return f"tee writing to {reason}"
    return None


def _check_curl(cmd: str) -> str | None:
    for m in _CURL_CMD.finditer(cmd):
        args = m.group("args") or ""
        if _SWARM_API_TARGET.search(args):
            mm = _CURL_ANY_METHOD.search(args)
            if (mm and mm.group("m").upper() not in ("GET", "HEAD")) or _CURL_SEND_FLAGS.search(args):
                return ("curl/wget to the swarm API (:5001) with a non-GET method -- "
                        "use the task tools (create_task, list_tasks, ...) instead")
        if _CURL_METHOD.search(args):
            return "curl/wget with a mutating HTTP method (POST/PUT/DELETE/PATCH)"
        if _CURL_SEND_FLAGS.search(args):
            return "curl/wget sending data (-d/--data/-F/-T/--upload-file)"
        out = _CURL_OUTPUT.search(args)
        if out:
            reason = _unsafe_path_reason(out.group("t"))
            if reason:
                return f"curl/wget output to {reason}"
    return None


# ---------------------------------------------------------------------------
# Public: shell command check
# ---------------------------------------------------------------------------

def check_command(cmd: str) -> str | None:
    """Return a human-readable reason when *cmd* must be blocked, else None.

    Runs path_guard's catastrophic check first, then the extended denylist.
    """
    cmd = cmd or ""
    if not isinstance(cmd, str):
        cmd = str(cmd)
    catastrophic = _check_catastrophic_command(cmd)
    if catastrophic:
        return catastrophic
    for pattern, reason in _BLOCKED_COMMANDS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return reason
    for checker in (_check_curl, _check_file_op_paths, _check_redirect_targets):
        reason = checker(cmd)
        if reason:
            return reason
    return None


# ---------------------------------------------------------------------------
# Public: Python source check (run_python)
# ---------------------------------------------------------------------------

# Attribute / function names that execute a command string or argv list.
_EXEC_FUNCS = {
    "system", "popen", "run", "call", "check_call", "check_output", "Popen",
    "getoutput", "getstatusoutput", "execv", "execve", "execvp", "execvpe",
    "execl", "execle", "execlp", "execlpe", "spawnl", "spawnle", "spawnlp",
    "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn",
    "posix_spawnp",
}
# Calls that delete / move / create filesystem entries: every string argument
# is treated as a path and must live inside the project root.
_MUTATE_FUNCS = {
    "rmtree", "remove", "unlink", "rmdir", "removedirs", "rename", "renames",
    "replace", "move", "copy", "copy2", "copyfile", "copytree", "copymode",
    "copystat", "mkdir", "makedirs", "write_text", "write_bytes", "touch",
    "symlink", "link", "chmod", "chown", "truncate",
}
_WRITE_MODE = re.compile(r"[wax+]")
_NET_MODULES = {
    "socket", "urllib", "urllib3", "requests", "http", "httpx", "aiohttp",
    "ftplib", "smtplib", "telnetlib", "websocket", "websockets", "paramiko",
    "xmlrpc", "asyncio.streams",
}
_STRING_LITERAL = re.compile(r"(?P<q>['\"])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)", re.DOTALL)


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _strings_in(node: ast.AST) -> list[str]:
    out: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
    return out


def _uses_expanduser(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and _call_name(sub) in {"expanduser", "home"}:
            return True
    return False


def _check_python_ast(tree: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in _EXEC_FUNCS:
            joined = " ".join(s for arg in node.args for s in _strings_in(arg))
            for kw in node.keywords:
                if kw.arg in ("args", "cmd", "command"):
                    joined += " " + " ".join(_strings_in(kw.value))
            reason = check_command(joined) if joined.strip() else None
            if reason:
                return f"subprocess/os call invoking a blocked command: {reason}"
        elif name in _MUTATE_FUNCS:
            # Scan args, keywords, and the receiver (Path('/x').write_text(...)).
            scan = list(node.args) + [kw.value for kw in node.keywords]
            if isinstance(node.func, ast.Attribute):
                scan.append(node.func.value)
            for arg in scan:
                if _uses_expanduser(arg):
                    return f"{name}() on a home-relative path"
                for s in _strings_in(arg):
                    reason = _unsafe_path_reason(s)
                    if reason:
                        return f"{name}() on {reason}"
        elif name == "open":
            mode = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value)
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = str(kw.value.value)
            if mode and _WRITE_MODE.search(mode) and node.args:
                if _uses_expanduser(node.args[0]):
                    return "open() for write on a home-relative path"
                for s in _strings_in(node.args[0]):
                    reason = _unsafe_path_reason(s)
                    if reason:
                        return f"open() for write on {reason}"
    return None


def _log_network_usage(tree: ast.AST) -> None:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _NET_MODULES or alias.name in _NET_MODULES:
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            if top in _NET_MODULES or node.module in _NET_MODULES:
                found.add(node.module)
    if found:
        log(f"run_python: snippet uses network module(s) {sorted(found)} (allowed, logged for audit)")


def check_python_source(source: str) -> str | None:
    """Return a reason when a run_python snippet must be blocked, else None.

    Network usage (socket/urllib/requests/http.client/...) is allowed but
    logged via the shared log() helper.
    """
    source = source or ""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Unparseable: fall back to scanning every string literal as a shell
        # command and every literal as a path if it looks like one.
        for m in _STRING_LITERAL.finditer(source):
            s = m.group("s")
            reason = check_command(s)
            if reason:
                return f"string literal contains a blocked command: {reason}"
        if re.search(r"\b(?:rmtree|remove|unlink|rmdir)\s*\(", source):
            for m in _STRING_LITERAL.finditer(source):
                reason = _unsafe_path_reason(m.group("s"))
                if reason:
                    return f"filesystem mutation on {reason}"
        return None
    reason = _check_python_ast(tree)
    if reason:
        return reason
    _log_network_usage(tree)
    return None


# ---------------------------------------------------------------------------
# Public: cwd pinning + minimal env
# ---------------------------------------------------------------------------

def pinned_cwd(cwd=None) -> str:
    """Return *cwd* if it lies inside the project root, else the project root.

    A caller-supplied cwd outside the root (or that does not exist) is ignored
    so a shell command can never be anchored somewhere on the host by an agent.
    """
    base = _safe_cwd(None)
    if not cwd:
        return base
    try:
        candidate = Path(cwd).expanduser().resolve()
        base_resolved = Path(base).resolve()
    except Exception:
        return base
    if candidate.is_dir() and (candidate == base_resolved or base_resolved in candidate.parents):
        return str(candidate)
    log(f"shell: ignoring cwd '{cwd}' outside project root; using '{base}'")
    return base


_ENV_KEEP = ("PATH", "HOME", "LANG", "PYTHONPATH", "GODOT_PATH")


def minimal_env() -> dict:
    """Minimal environment for run_python subprocesses (PATH/HOME/LANG/PYTHONPATH/GODOT_PATH)."""
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env.setdefault("PATH", os.defpath)
    return env
