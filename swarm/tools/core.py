"""
swarm.tools.core — file I/O, git, shell, web, and task tools.

Config vars in this module are set by agent_runtime._sync_core_globals()
at the start of each agent run, mirroring the values set by the wrapper
script on swarm.agent_runtime before calling main().
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
import urllib.request as _ur
import time
import uuid
from pathlib import Path
import random

from swarm import db as swarm_db
from swarm.platform import kill_process_tree, popen_session_kwargs, shell_quote_command_arg
from swarm.task_chains import chain_to_project_head

# ---------------------------------------------------------------------------
# Config vars — set via _sync_core_globals() in agent_runtime before use
# ---------------------------------------------------------------------------

WORKSPACE: Path = Path(".")
DATA_DIR: str = "data"
PROJECT: str = ""
PROJECT_PATH_OVERRIDE: str = ""
WORKTREE_BRANCH: str = ""
TASK_TYPE: str = "feature"
TASK_ID: str = "unknown"
TASK_PRIORITY: int = 50
MAX_LINES: int = 5000
IGNORE_DIRS: set = {"addons", ".git", ".godot"}
IGNORE_EXTENSIONS: set = frozenset()
MAX_TOOL_LOOPS: int = 120
API_PORT: int = 5001
QA_MAX_CYCLES: int = 3
MCP_SERVERS: dict = {}
MANAGED_PROJECTS: list = []
READONLY: bool = False
mcp_client = None


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def log(msg: str):
    print(f"[Agent] {msg}", flush=True)


# Map Windows-1252 "fancy" punctuation → plain ASCII equivalents.
# Applied to all text written via write_file / patch_file so LLM-generated
# em-dashes and smart quotes never produce non-UTF-8 bytes in GDScript files.
_FANCY_PUNCT_TABLE = str.maketrans({
    "—": "--",   # em-dash (also cp1252 0x97)
    "–": "-",    # en-dash (also cp1252 0x96)
    "“": '"',    # left double quote (also cp1252 0x93)
    "”": '"',    # right double quote (also cp1252 0x94)
    "‘": "'",    # left single quote (also cp1252 0x91)
    "’": "'",    # right single quote (also cp1252 0x92)
    "…": "...",  # ellipsis (also cp1252 0x85)
    "·": "*",    # middle dot (also cp1252 0xb7)
})


def _sanitize_text(content: str) -> str:
    """Replace fancy Unicode punctuation with ASCII equivalents and normalise line endings."""
    return content.translate(_FANCY_PUNCT_TABLE).replace('\r\n', '\n').replace('\r', '\n')


def _api_request(method: str, path: str, payload: dict | None = None, timeout: int = 15) -> dict:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = _ur.Request(
        f"http://localhost:{API_PORT}{path}",
        data=body,
        method=method,
        headers=headers,
    )
    with _ur.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _get_task_from_api(task_id: str) -> dict:
    return _api_request("GET", f"/api/tasks/{task_id}", timeout=10).get("task") or {}


def _patch_task_via_api(task_id: str, updates: dict) -> dict:
    return _api_request("PATCH", f"/api/tasks/{task_id}", payload=updates, timeout=10).get("task") or {}


def _project_root() -> str:
    """Return the effective project root, honouring PROJECT_PATH_OVERRIDE (worktrees)."""
    if PROJECT_PATH_OVERRIDE:
        return PROJECT_PATH_OVERRIDE
    return str(Path(WORKSPACE) / PROJECT)


def _safe_cwd(cwd=None):
    """Return a valid working directory, falling back to WORKSPACE for virtual projects."""
    if cwd:
        return cwd
    root = _project_root()
    if os.path.isdir(root):
        return root
    ws = str(WORKSPACE) if WORKSPACE else os.path.expanduser("~")
    return ws if os.path.isdir(ws) else os.getcwd()


def _read_text_with_fallback(path: str | Path) -> tuple[str, str]:
    """Read text files that may have been authored with Windows ANSI tools.

    Always returns LF-only text regardless of what line endings are on disk,
    so patch_file matches reliably on Windows without agents needing to
    account for CRLF vs LF differences.
    """
    raw = Path(path).read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            return text.replace('\r\n', '\n').replace('\r', '\n'), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").replace('\r\n', '\n').replace('\r', '\n'), "utf-8-replace"


def _read_lines_with_fallback(path: str | Path) -> tuple[list[str], str]:
    text, encoding = _read_text_with_fallback(path)
    return text.splitlines(keepends=True), encoding


def _resolve_project_path(path_arg: str) -> Path:
    p = Path(path_arg)
    project_root = Path(_project_root())
    try:
        rel = p.relative_to(project_root)
        return project_root / rel
    except ValueError:
        return project_root / path_arg


def _is_vendor_path(path: Path) -> bool:
    return _protected_path_reason(path) == "vendor"


def _protected_path_reason(path: Path) -> str | None:
    try:
        rel = path.relative_to(Path(_project_root()))
    except ValueError:
        rel = path
    parts = rel.parts
    if not parts:
        return None
    if parts[0] == "addons":
        return "vendor"
    if parts[0] in {".godot", ".import"}:
        return "generated"
    if rel.suffix == ".uid":
        return "generated"
    return None


def _protected_path_error(path_arg: str, reason: str) -> dict:
    if reason == "vendor":
        detail = "Files under addons/ are third-party and read-only."
        label = "vendor code"
    else:
        detail = (
            "Files under .godot/, .import/, and *.uid are generated artifacts and "
            "must be regenerated by the engine instead of hand-edited."
        )
        label = "generated artifacts"
    return {
        "ok": False,
        "error": (
            f"Refusing to modify {label}: {path_arg}. "
            f"{detail}"
        ),
    }


def _command_targets_protected_path(cmd: str) -> tuple[str, str] | None:
    patterns = [
        ("vendor", r"(?<![\w./-])addons/"),
        ("generated", r"(?<![\w./-])\.godot/"),
        ("generated", r"(?<![\w./-])\.import/"),
        ("generated", r"(?<![\w./-])[^\\s'\"`|;&]+\.uid\b"),
    ]
    for reason, pattern in patterns:
        match = re.search(pattern, cmd)
        if match:
            return reason, match.group(0).strip()
    return None


def _embedded_protected_path_matches(cmd: str) -> list[tuple[str, str]]:
    patterns = [
        ("vendor", r"['\"](?P<path>addons/[^'\"]+)['\"]"),
        ("generated", r"['\"](?P<path>\.godot/[^'\"]+)['\"]"),
        ("generated", r"['\"](?P<path>\.import/[^'\"]+)['\"]"),
        ("generated", r"['\"](?P<path>[^'\"]+\.uid)['\"]"),
    ]
    matches: list[tuple[str, str]] = []
    for reason, pattern in patterns:
        for match in re.finditer(pattern, cmd):
            path_hint = (match.groupdict().get("path") or match.group(0)).strip("'\"")
            matches.append((reason, path_hint))
    return matches


def _command_attempts_embedded_protected_write(cmd: str) -> tuple[str, str] | None:
    lowered = cmd.lower()
    embedded_matches = _embedded_protected_path_matches(cmd)
    if not embedded_matches:
        return None

    python_write_patterns = [
        r"\bopen\s*\(\s*['\"][^'\"]+['\"]\s*,\s*['\"][waxt+]",
        r"\bpath\s*\(\s*['\"][^'\"]+['\"]\s*\)\.(?:write_text|write_bytes|touch|mkdir|rename|replace|unlink)\b",
        r"\bos\.(?:mkdir|makedirs|remove|unlink|rename|replace)\s*\(",
        r"\bshutil\.(?:copy|copy2|copyfile|move|rmtree)\s*\(",
    ]
    if any(re.search(pattern, lowered) for pattern in python_write_patterns):
        return embedded_matches[0]

    tee_match = re.search(r"\btee\b(?P<args>[^;&|]*)", cmd)
    if tee_match:
        tee_args = tee_match.group("args") or ""
        for reason, path_hint in embedded_matches:
            if path_hint in tee_args:
                return reason, path_hint

    return None


_CATASTROPHIC_PATTERNS: list[tuple[str, str]] = [
    # Recursive / force deletes — rm -r, rm -rf, rm -fr, find … -delete
    (r"\brm\s+(?:[^;|&\n]*\s)?-[a-z]*r[a-z]*f?[a-z]*\b", "recursive rm (-r/-rf/-fr)"),
    (r"\brm\s+(?:[^;|&\n]*\s)?-[a-z]*f[a-z]*r[a-z]*\b", "recursive rm (-fr/-rf)"),
    (r"\bfind\b[^;|&\n]*\s--delete\b", "find --delete (recursive file removal)"),
    (r"\bfind\b[^;|&\n]*\s-delete\b", "find -delete (recursive file removal)"),
    # Destructive git operations
    (r"\bgit\s+push\s+(?:[^;|&\n]*\s)?(?:--force|-f)\b", "git push --force"),
    (r"\bgit\s+push\s+(?:[^;|&\n]*\s)?--force-with-lease\b", "git push --force-with-lease"),
    (r"\bgit\s+reset\s+(?:[^;|&\n]*\s)?--hard\b", "git reset --hard"),
    (r"\bgit\s+branch\s+(?:[^;|&\n]*\s)?-D\b", "git branch -D (force branch delete)"),
    (r"\bgit\s+clean\s+(?:[^;|&\n]*\s)?-[a-z]*f[a-z]*\b", "git clean -f (destroys untracked files)"),
    (r"\bgit\s+filter-branch\b", "git filter-branch (rewrites history)"),
    (r"\bgit\s+filter-repo\b", "git filter-repo (rewrites history)"),
    # Process killing by name — too broad / irreversible
    (r"\bpkill\b", "pkill (mass process kill by name)"),
    (r"\bkillall\b", "killall (mass process kill by name)"),
    # Disk-level writes
    (r"\bdd\s+if=", "dd if= (raw disk write)"),
    # Recursive permission/ownership bombs
    (r"\bchmod\s+(?:[^;|&\n]*\s)?-[a-z]*R[a-z]*\b", "chmod -R (recursive permission change)"),
    (r"\bchown\s+(?:[^;|&\n]*\s)?-[a-z]*R[a-z]*\b", "chown -R (recursive ownership change)"),
    # Truncating or overwriting the DB directly
    (r"swarm\.db", "direct access to swarm.db"),
    # Wiping the swarm data directory
    (r"\bdata/agent", "direct manipulation of swarm data/agent files via shell"),
]


def _check_catastrophic_command(cmd: str) -> str | None:
    """Return a human-readable reason if the command matches a catastrophic pattern, else None."""
    for pattern, reason in _CATASTROPHIC_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return reason
    return None


def _command_attempts_protected_write(cmd: str) -> tuple[str, str] | None:
    if TASK_TYPE == "project_create":
        return None

    target = _command_targets_protected_path(cmd)
    if not target:
        return None
    reason, path_hint = target
    lowered = cmd.lower()

    mutating_patterns = [
        r"\bcp\b",
        r"\bmv\b",
        r"\brm\b",
        r"\binstall\b",
        r"\bmkdir\b",
        r"\btouch\b",
        r"\bln\b",
        r"\brsync\b",
        r"\btee\b",
        r"\bsed\s+-i\b",
        r"\bperl\s+-i\b",
        r"\bgit\s+checkout\b",
        r"\bgit\s+restore\b",
        r"\bgit\s+clean\b",
    ]
    if any(re.search(pattern, lowered) for pattern in mutating_patterns):
        return reason, path_hint

    redirection_match = re.search(
        r"(?:^|[;&|])[^\\n]*?(?:>|>>)\s*(['\"]?)([^'\";&|]+)\1",
        cmd,
    )
    if redirection_match:
        redir_target = redirection_match.group(2).strip()
        if _command_targets_protected_path(redir_target):
            return reason, redir_target

    embedded_write = _command_attempts_embedded_protected_write(cmd)
    if embedded_write:
        return embedded_write

    return None


def _looks_like_dependency_path(value: str) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if "/" in text or "\\" in text:
        return True
    return text.lower().endswith((".gd", ".tscn", ".tres", ".py", ".json", ".md", ".png", ".wav"))


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
# File tools
# ---------------------------------------------------------------------------

def read_file(relative_path: str, offset: int = 0, limit: int = 0) -> dict:
    """Read a file. offset/limit are in lines (0 = no limit)."""
    path = os.path.join(_project_root(), relative_path)
    try:
        lines, encoding = _read_lines_with_fallback(path)
        total = len(lines)
        if offset or limit:
            chunk = lines[offset: offset + limit] if limit else lines[offset:]
            content = "".join(chunk)
            return {"ok": True, "content": content, "total_lines": total,
                    "returned_lines": len(chunk), "offset": offset, "encoding": encoding}
        return {"ok": True, "content": "".join(lines), "total_lines": total, "encoding": encoding}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_files(relative_path: str = ".") -> dict:
    path = os.path.join(_project_root(), relative_path)
    try:
        if os.path.isfile(path):
            return {"ok": True, "files": [os.path.basename(path)], "type": "file"}
        files = []
        for item in os.listdir(path):
            full = os.path.join(path, item)
            files.append(item + "/" if os.path.isdir(full) else item)
        return {"ok": True, "files": files}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def search_code(query: str) -> dict:
    path = _project_root()
    try:
        matches = []
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                if not fname.endswith(".gd"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        if query in f.read():
                            matches.append(fpath)
                except OSError:
                    pass
        return {"ok": True, "files": matches}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_file_stats(relative_path: str) -> dict:
    path = os.path.join(_project_root(), relative_path)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = sum(1 for _ in f)
        size = os.path.getsize(path)
        return {"ok": True, "lines": lines, "size": size}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_file_outline(relative_path: str) -> dict:
    """Get outline of functions and classes in a file with line numbers.

    For Python: uses AST.
    For GDScript: uses regex patterns.
    """
    import ast
    import re
    path = os.path.join(_project_root(), relative_path)

    if not os.path.exists(path):
        return {"ok": False, "error": "file not found"}

    ext = os.path.splitext(path)[1].lower()

    if ext == '.py':
        try:
            source, _encoding = _read_text_with_fallback(path)
            tree = ast.parse(source)
            outline = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    outline.append({
                        "type": "function",
                        "name": node.name,
                        "line": node.lineno,
                        "end_line": node.end_lineno,
                    })
                elif isinstance(node, ast.ClassDef):
                    outline.append({
                        "type": "class",
                        "name": node.name,
                        "line": node.lineno,
                        "end_line": node.end_lineno,
                    })
            outline.sort(key=lambda x: x["line"])
            return {"ok": True, "outline": outline}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    elif ext == '.gd':
        try:
            lines, _encoding = _read_lines_with_fallback(path)

            outline = []
            func_pattern = re.compile(r'^\s*func\s+(\w+)')
            class_pattern = re.compile(r'^\s*class\s+(\w+)')

            for i, line in enumerate(lines, start=1):
                func_match = func_pattern.match(line)
                class_match = class_pattern.match(line)
                if func_match:
                    outline.append({"type": "function", "name": func_match.group(1), "line": i, "end_line": None})
                elif class_match:
                    outline.append({"type": "class", "name": class_match.group(1), "line": i, "end_line": None})

            def get_indent(line):
                return len(line) - len(line.lstrip()) if line.strip() else 999

            for i, item in enumerate(outline):
                start_line = item["line"]
                start_indent = get_indent(lines[start_line - 1]) if start_line <= len(lines) else 0
                end_line = len(lines)
                for j in range(i + 1, len(outline)):
                    next_line = lines[outline[j]["line"] - 1] if outline[j]["line"] <= len(lines) else ""
                    if get_indent(next_line) <= start_indent:
                        end_line = outline[j]["line"] - 1
                        break
                item["end_line"] = end_line

            return {"ok": True, "outline": outline}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    else:
        return {"ok": False, "error": "unsupported file type"}


def read_file_range(relative_path: str, start_line: int, end_line: int) -> dict:
    """Read a specific line range from a file. Line numbers are 1-indexed and inclusive.
    Hard cap: max 300 lines per call.
    """
    path = os.path.join(_project_root(), relative_path)

    if not os.path.exists(path):
        return {"ok": False, "error": "file not found"}

    try:
        all_lines, encoding = _read_lines_with_fallback(path)

        total_lines = len(all_lines)
        start_line = max(1, start_line)
        end_line = min(total_lines, end_line)

        truncated = False
        if end_line - start_line > 300:
            end_line = start_line + 300
            truncated = True

        content = "".join(all_lines[start_line - 1:end_line])

        return {
            "ok": True,
            "content": content,
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": total_lines,
            "truncated": truncated,
            "encoding": encoding,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def patch_file(relative_path: str, old: str, new: str) -> dict:
    """Replace an exact string in a file.

    Fails if file not found, old string not found, or old string appears
    more than once (ambiguous). Provides a fuzzy hint when old not found.
    """
    if READONLY:
        return {"ok": False, "error": "Read-only task: patch_file is disabled"}
    if TASK_TYPE in ("python_plan", "plan"):
        return {"ok": False, "error": "plan tasks are read-only planners — use create_task() to delegate implementation"}
    if str(relative_path).startswith("res://"):
        return {"ok": False, "error": f"'res://' is a Godot virtual path, not a filesystem path. Use the real path instead: {str(relative_path).replace('res://', '<project_dir>/')}"}

    path = _resolve_project_path(relative_path)

    protected_reason = _protected_path_reason(path)
    if protected_reason:
        return _protected_path_error(relative_path, protected_reason)

    if not os.path.exists(path):
        return {"ok": False, "error": "file not found"}

    try:
        content, _encoding = _read_text_with_fallback(path)

        count = content.count(old)

        if count == 0:
            import difflib
            old_first = old.splitlines()[0].strip() if old else ""
            lines = content.splitlines()
            best_ratio, best_line_no, best_lines = 0.0, -1, []
            for i, line in enumerate(lines):
                r = difflib.SequenceMatcher(None, old_first, line.strip()).ratio()
                if r > best_ratio:
                    best_ratio, best_line_no = r, i + 1
                    best_lines = lines[max(0, i - 1):i + 3]
            hint = ""
            if best_ratio > 0.5 and best_line_no >= 0:
                preview = "\n".join(f"  {l}" for l in best_lines)
                hint = f" Closest match at line {best_line_no} (similarity {best_ratio:.0%}):\n{preview}"
            return {"ok": False, "error": f"string not found in {relative_path}.{hint}", "searched_chars": len(content)}

        if count > 1:
            return {"ok": False, "error": f"ambiguous: found {count} occurrences"}

        new_content = _sanitize_text(content.replace(old, new, 1))

        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(new_content)

        return {"ok": True, "replaced": 1}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def write_file(path_arg: str, content: str) -> dict:
    if READONLY:
        return {"ok": False, "error": "Read-only task: write_file is disabled"}
    if TASK_TYPE in ("python_plan", "plan"):
        return {"ok": False, "error": "plan tasks are read-only planners — use create_task() to delegate implementation"}
    if "res://" in str(path_arg):
        return {"ok": False, "error": f"'res://' is a Godot virtual path, not a filesystem path. Use the real path instead, e.g. the project directory + the relative path after 'res://': {str(path_arg).replace('res://', '<project_dir>/')}"}
    path = _resolve_project_path(path_arg)
    protected_reason = _protected_path_reason(path)
    if protected_reason:
        return _protected_path_error(path_arg, protected_reason)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(_sanitize_text(content), encoding='utf-8', newline="\n")
        return {"ok": True, "path": str(path)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def append_file(path_arg: str, content: str) -> dict:
    """Append content to a file without reading/rewriting the whole thing.

    Creates the file if it does not exist (including parent directories).
    Appends a newline before content if the file is non-empty and doesn't end in newline.
    """
    if READONLY:
        return {"ok": False, "error": "Read-only task: append_file is disabled"}
    if "res://" in str(path_arg):
        return {"ok": False, "error": f"'res://' is a Godot virtual path, not a filesystem path. Use the real path instead: {str(path_arg).replace('res://', '<project_dir>/')}"}

    path = _resolve_project_path(path_arg)
    protected_reason = _protected_path_reason(path)
    if protected_reason:
        return _protected_path_error(path_arg, protected_reason)

    path.parent.mkdir(parents=True, exist_ok=True)

    add_newline = False
    if path.exists() and path.stat().st_size > 0:
        with open(path, 'rb') as f:
            f.seek(-1, 2)
            last_byte = f.read(1)
            if last_byte != b"\n":
                add_newline = True

    with open(path, 'a', encoding='utf-8', newline='\n') as f:
        if add_newline:
            f.write("\n")
        f.write(content)

    bytes_written = len(content) + (1 if add_newline else 0)
    return {"ok": True, "bytes_written": bytes_written}


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
    if READONLY:
        return {"ok": False, "error": "Read-only task: git_commit is disabled"}
    if TASK_TYPE in ("python_plan", "plan"):
        return {"ok": False, "error": "plan tasks are read-only planners — use create_task() instead of writing code"}
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
        import shlex
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
    if READONLY:
        return {"ok": False, "error": "Read-only task: git_push is disabled"}
    code, out, err = run("git push 2>&1")
    if code != 0:
        return {"ok": False, "error": f"git push failed: {err}"}
    return {"ok": True, "message": "Pushed", "output": out}


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

def mcp_call_tool(server: str, tool: str, args=None) -> dict:
    if mcp_client is None:
        return {"ok": False, "error": "MCP not configured"}
    return mcp_client.call_tool(server, tool, args)


def mcp_list_tools(server: str) -> list:
    if mcp_client is None:
        return []
    return mcp_client.list_tools(server)


# ---------------------------------------------------------------------------
# Search / fetch tools
# ---------------------------------------------------------------------------

def rag_query(question: str, top_k: int = 5) -> dict:
    """Query the RAG system for relevant code context.

    Requires 'rag' section in config.json:
        {"rag": {"enabled": true, "index_path": "/path/to/godot-doc-index"}}

    Returns a clear "RAG not configured" message when index_path is absent
    rather than a stack trace.
    """
    # Load index path from swarm config.json
    try:
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "config.json")
        with open(config_path) as f:
            config = json.load(f)
        rag_cfg = config.get("rag", {})
        if not rag_cfg.get("enabled", False):
            return {"ok": False, "error": "RAG is disabled in config.json (set rag.enabled to true)"}
        index_path = rag_cfg.get("index_path", "").strip()
        if not index_path:
            return {
                "ok": False,
                "error": (
                    "RAG index_path not configured in config.json. "
                    "Add {\"rag\": {\"index_path\": \"/path/to/godot-doc-index\"}} to your config.json."
                ),
            }
        index_path = str(Path(index_path).expanduser().resolve())
        if not os.path.isdir(index_path):
            return {"ok": False, "error": f"RAG index_path does not exist: {index_path}"}
    except Exception as err:
        return {"ok": False, "error": f"Failed to read config.json: {err}"}

    try:
        import sys as _sys
        _sys.path.insert(0, index_path)
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("HF_DATASETS_OFFLINE", "1")

        import yaml
        config_yaml = os.path.join(index_path, "config.yaml")
        with open(config_yaml) as f:
            cfg = yaml.safe_load(f)
        persist_dir = cfg["vector_db"]["persist_directory"]
        if not os.path.isabs(persist_dir):
            persist_dir = os.path.join(index_path, persist_dir)

        from src.embeddings import Embeddings
        from src.vector_store import VectorStore
        from src.retriever import Retriever
        embeddings = Embeddings(
            model_name=cfg["embeddings"]["model"],
            device=cfg["embeddings"]["device"]
        )
        vector_store = VectorStore(
            persist_directory=persist_dir,
            collection_name=cfg["vector_db"].get("collection_name", "godot_docs")
        )
        retriever = Retriever(
            vector_store=vector_store,
            embeddings=embeddings,
            top_k=top_k,
            similarity_threshold=cfg["retrieval"].get("similarity_threshold", 0.0),
            use_query_expansion=cfg["retrieval"].get("use_query_expansion", True),
            use_keyword_boost=cfg["retrieval"].get("use_keyword_boost", True)
        )
        context = retriever.get_context(question)
        raw_docs = retriever.retrieve(question)
        return {
            "ok": True,
            "context": context,
            "sources": [d.get("source", "") for d in raw_docs],
            "num_results": len(raw_docs),
        }
    except Exception as err:
        return {"ok": False, "error": str(err)[:200]}


def web_search(query: str, max_results: int = 3) -> dict:
    """Search the web. Falls back through Tavily -> Brave -> Serper -> DuckDuckGo."""
    import urllib.request

    max_results = min(max_results, 5)

    # Try Tavily first
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily_key:
        try:
            req = urllib.request.Request(
                "https://api.tavily.com/search",
                data=json.dumps({"query": query, "max_results": max_results}).encode(),
                headers={"Content-Type": "application/json", "Api-Key": tavily_key},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            results = []
            for r in data.get("results", [])[:max_results]:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("content", r.get("snippet", "")),
                })
            if results:
                return {"ok": True, "results": results}
        except Exception:
            pass

    # Try Brave Search
    brave_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if brave_key:
        try:
            q = urllib.parse.quote(query)
            req = urllib.request.Request(
                f"https://api.search.brave.com/res/v1/web/search?q={q}&count={max_results}",
                headers={"Accept": "application/json", "X-Subscription-Token": brave_key},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            results = []
            for r in data.get("web", {}).get("results", [])[:max_results]:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("description", r.get("snippet", "")),
                })
            if results:
                return {"ok": True, "results": results}
        except Exception:
            pass

    # Try Serper (Google)
    serper_key = os.environ.get("SERPER_API_KEY", "").strip()
    if serper_key:
        try:
            req = urllib.request.Request(
                "https://google.serper.dev/search",
                data=json.dumps({"q": query, "num": max_results}).encode(),
                headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            results = []
            for r in data.get("organic", [])[:max_results]:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("link", ""),
                    "snippet": r.get("snippet", ""),
                })
            if results:
                return {"ok": True, "results": results}
        except Exception:
            pass

    # Fall back to DuckDuckGo via ddgs package (no key required)
    try:
        from ddgs import DDGS
        hits = list(DDGS().text(query, max_results=max_results))
        results = [
            {"title": h.get("title", ""), "url": h.get("href", ""), "snippet": h.get("body", "")}
            for h in hits
        ]
        if results:
            return {"ok": True, "results": results}
    except Exception:
        pass

    return {"ok": False, "error": "All search providers failed. Set BRAVE_API_KEY, TAVILY_API_KEY, or SERPER_API_KEY in .env for reliable results."}


def fetch_url(url: str, extract_text: bool = True) -> dict:
    """Fetch a URL and return its content as clean markdown."""
    import re
    try:
        import urllib.request
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        if extract_text:
            try:
                import html2text
                h = html2text.HTML2Text()
                h.ignore_links = False
                h.ignore_images = True
                h.ignore_tables = False
                h.body_width = 0
                h.single_line_break = True
                content = h.handle(raw)
                content = re.sub(r"\n{3,}", "\n\n", content).strip()
            except ImportError:
                content = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL | re.IGNORECASE)
                content = re.sub(r"<style[^>]*>.*?</style>", "", content, flags=re.DOTALL | re.IGNORECASE)
                content = re.sub(r"<[^>]+>", " ", content)
                content = re.sub(r"\s+", " ", content).strip()
        else:
            content = raw

        truncated = False
        if len(content) > 8000:
            content = content[:8000] + "\n\n[... truncated at 8000 characters ...]"
            truncated = True

        return {"ok": True, "content": content, "length": len(content), "truncated": truncated}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Broadcast log tools
# ---------------------------------------------------------------------------

def broadcast_read(tail: int = 50) -> dict:
    """Read recent entries from the project broadcast log."""
    try:
        import urllib.request as _ur
        url = f"http://localhost:{API_PORT}/api/broadcast/{PROJECT}?tail={tail}"
        with _ur.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def broadcast_write(message: str) -> dict:
    """Append an observation or intent to the project broadcast log."""
    try:
        import urllib.request as _ur
        body = json.dumps({"agent_id": TASK_ID, "task_id": TASK_ID, "message": message}).encode()
        req = _ur.Request(
            f"http://localhost:{API_PORT}/api/broadcast/{PROJECT}",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def delegate_helper(question: str, files: list | None = None, scope: str = "", max_chars: int = 12000) -> dict:
    """Run a transient read-only helper analysis and return findings to the parent task.

    This is intentionally non-durable: it does not create a task row and must not mutate
    project state. The helper only sees the provided question, optional scope text, and
    bounded excerpts from the requested files.
    """
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "question is required"}

    files = [f for f in (files or []) if isinstance(f, str) and f.strip()]
    max_chars = max(2000, min(int(max_chars or 12000), 24000))

    context_parts = [
        f"Parent task id: {TASK_ID}",
        f"Parent task type: {TASK_TYPE}",
        f"Project: {PROJECT}",
    ]
    if scope.strip():
        context_parts.append(f"Scope:\n{scope.strip()}")

    remaining = max_chars
    consulted = []
    for rel_path in files[:8]:
        resolved = _resolve_project_path(rel_path)
        if not resolved.exists() or not resolved.is_file():
            continue
        try:
            text = resolved.read_text(errors="replace")
        except Exception:
            continue
        budget = min(max(500, remaining // max(1, (len(files[:8]) - len(consulted)))), 4000)
        excerpt = text[:budget]
        consulted.append(rel_path)
        context_parts.append(f"File: {rel_path}\n```text\n{excerpt}\n```")
        remaining -= len(excerpt)
        if remaining <= 0:
            break

    helper_system = (
        "You are a transient read-only helper agent working for a parent swarm task. "
        "Answer the question using only the provided context. "
        "Do not propose file edits unless explicitly asked; prefer analysis, risks, and concrete next-step guidance. "
        "Keep the response concise and actionable."
    )
    helper_user = "\n\n".join(context_parts) + f"\n\nQuestion:\n{question}"

    try:
        from swarm.llm_utils import call_llm
        text, tokens = call_llm(helper_system, [{"role": "user", "content": helper_user}])
    except Exception as e:
        return {"ok": False, "error": str(e)}

    try:
        parent_task = _get_task_from_api(TASK_ID)
        parent_meta = dict(parent_task.get("metadata") or {})
        helper_history = list(parent_meta.get("helper_delegations") or [])
        helper_history.append({
            "at": datetime.now().isoformat(),
            "question": question[:280],
            "files": consulted,
            "scope": scope.strip()[:280],
            "answer_summary": text.strip()[:400],
            "input_tokens": tokens.get("input", 0) if isinstance(tokens, dict) else 0,
            "output_tokens": tokens.get("output", 0) if isinstance(tokens, dict) else 0,
        })
        parent_meta["helper_delegations"] = helper_history[-8:]
        _patch_task_via_api(TASK_ID, {"metadata": parent_meta})
    except Exception as e:
        log(f"[delegate_helper] WARNING: failed to persist helper metadata: {e}")

    return {
        "ok": True,
        "answer": text.strip(),
        "files_consulted": consulted,
        "input_tokens": tokens.get("input", 0) if isinstance(tokens, dict) else 0,
        "output_tokens": tokens.get("output", 0) if isinstance(tokens, dict) else 0,
    }


# ---------------------------------------------------------------------------
# Task management tools
# ---------------------------------------------------------------------------

def create_task(description: str, task_type: str = "feature", priority: int = 50,
                dependencies: list = None, project: str = None,
                parent_task_id: str = None, metadata: dict = None) -> dict:
    """Create a task in the swarm controller. Returns the task ID.

    task_type: feature | bug | refactor | polish | qa
    priority: capped at 90 (agents cannot create higher-priority tasks)
    dependencies: list of task ID strings that must exist
    project: project name (defaults to current project)
    parent_task_id: pass TASK_ID to create a sub-task; max depth is 2
    metadata: extra key/value metadata to store on the task
    """
    VALID_TYPES = {"feature", "bug", "polish", "refactor", "qa", "python_plan", "plan"}
    if task_type not in VALID_TYPES:
        return {"ok": False, "error": f"Invalid task type: '{task_type}'. Must be one of: {', '.join(VALID_TYPES)}"}

    priority = min(int(priority), 90)
    proj = project or PROJECT

    task_metadata = dict(metadata) if metadata else {}
    if parent_task_id:
        import urllib.request as _parent_ur
        try:
            with _parent_ur.urlopen(f"http://localhost:{API_PORT}/api/tasks", timeout=10) as resp:
                all_tasks = json.loads(resp.read()).get("tasks", [])
            parent = next((t for t in all_tasks if t.get("id") == parent_task_id), None)
            if not parent:
                return {"ok": False, "error": f"Parent task not found: {parent_task_id}"}
            parent_depth = parent.get("metadata", {}).get("task_depth", 0)
            if parent_depth >= 2:
                return {"ok": False, "error": "max sub-task depth (2) reached"}
            task_metadata["parent_task_id"] = parent_task_id
            task_metadata["task_depth"] = parent_depth + 1
        except Exception as e:
            return {"ok": False, "error": f"Failed to resolve parent_task_id: {str(e)}"}

    if isinstance(dependencies, str):
        deps_str = dependencies.strip()
        if deps_str.startswith("["):
            try:
                dependencies = json.loads(deps_str)
            except Exception:
                dependencies = [d.strip().strip('"\'') for d in deps_str.strip("[]").split(",") if d.strip().strip('"\'')]
        elif deps_str:
            dependencies = [d.strip().strip('"\'') for d in deps_str.split(",") if d.strip()]
        else:
            dependencies = []

    if dependencies:
        try:
            with _ur.urlopen(f"http://localhost:{API_PORT}/api/tasks", timeout=10) as resp:
                data = json.loads(resp.read())
            existing_ids = {t["id"] for t in data.get("tasks", []) if t.get("project") == proj}
            missing = [d for d in dependencies if d not in existing_ids]
            if missing:
                return {"ok": False, "error": f"Dependency task IDs not found: {missing}"}
        except Exception as e:
            return {"ok": False, "error": f"Failed to validate dependencies: {str(e)}"}

    generated_id = f"{task_type}-{int(time.time() * 1000) % 10**9}-agent"

    task = {
        "id": generated_id,
        "project": proj,
        "type": task_type,
        "priority": priority,
        "description": description,
        "dependencies": dependencies or [],
        "max_attempts": 3,
    }
    if task_metadata:
        task["metadata"] = task_metadata

    try:
        body = json.dumps(task).encode()
        req = _ur.Request(
            f"http://localhost:{API_PORT}/api/tasks",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        task_id = result.get("task", {}).get("id", generated_id)
        log(f"[create_task] Created {task_type} task {task_id}: {description[:60]}")
        return {"ok": True, "task_id": task_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def delegate_task_batch(children: list, mode: str = "integrate", project: str = None) -> dict:
    """Create a structured batch of child tasks linked back to the current task.

    Children are durable tasks in the project graph. Each child may declare:
      - description: required
      - type: feature|bug|refactor|polish
      - priority: optional
      - files: list[str] write scope (used for file-aware auto ordering)
      - dependencies: explicit external task ids
      - depends_on_children: list[int] indices into this batch
      - metadata: extra metadata
    """
    if not isinstance(children, list) or not children:
        return {"ok": False, "error": "children list is empty"}
    if len(children) > 6:
        return {"ok": False, "error": "delegation batch exceeds rollout limit of 6 children"}
    if mode not in {"wait", "integrate", "replace"}:
        return {"ok": False, "error": "mode must be one of: wait, integrate, replace"}

    proj = project or PROJECT
    valid_types = {"feature", "bug", "refactor", "polish"}
    batch_id = f"delegation-{uuid.uuid4().hex[:10]}"

    file_owners: dict[str, list[int]] = {}
    normalized_children: list[dict] = []

    for i, child in enumerate(children):
        if not isinstance(child, dict):
            return {"ok": False, "error": f"child {i} must be an object"}
        desc = (child.get("description") or "").strip()
        if not desc:
            return {"ok": False, "error": f"child {i} is missing description"}
        child_type = child.get("type", "feature")
        if child_type not in valid_types:
            return {"ok": False, "error": f"child {i} has invalid type '{child_type}'"}

        files = [f.strip() for f in (child.get("files") or []) if isinstance(f, str) and f.strip()]
        if not files:
            return {"ok": False, "error": f"child {i} must declare files for delegated write ownership"}
        explicit_ids = [d for d in (child.get("dependencies") or []) if isinstance(d, str) and d.strip()]
        bad_dep = next((dep for dep in explicit_ids if _looks_like_dependency_path(dep)), None)
        if bad_dep:
            return {
                "ok": False,
                "error": (
                    f"child {i} has invalid dependency '{bad_dep}'. "
                    "Use dependencies for task IDs and files for ownership/write scope."
                ),
            }
        depends_on_children = []
        for raw in (child.get("depends_on_children") or []):
            if isinstance(raw, int) and 0 <= raw < len(children) and raw != i:
                depends_on_children.append(raw)
            else:
                return {"ok": False, "error": f"child {i} has invalid depends_on_children entry: {raw}"}

        for f in files:
            file_owners.setdefault(f, []).append(i)

        normalized_children.append({
            "description": desc,
            "type": child_type,
            "priority": min(int(child.get("priority", 50)), 90),
            "files": files,
            "dependencies": explicit_ids,
            "depends_on_children": sorted(set(depends_on_children)),
            "metadata": dict(child.get("metadata") or {}),
        })

    for path, owners in sorted(file_owners.items()):
        if len(owners) < 2:
            continue
        owner_set = set(owners)
        for idx in owners:
            ordered_peers = set(normalized_children[idx]["depends_on_children"]) & owner_set
            ordered_by_peers = {
                other_idx for other_idx in owners
                if idx in normalized_children[other_idx]["depends_on_children"]
            }
            if not (ordered_peers or ordered_by_peers):
                return {
                    "ok": False,
                    "error": (
                        f"overlapping delegated write scope '{path}' requires explicit sequential ordering "
                        f"between children {owners}"
                    ),
                }

    root_child_indices: list[int] = []
    for i, child in enumerate(normalized_children):
        if not child["depends_on_children"] and TASK_ID not in child["dependencies"]:
            child["dependencies"] = [*child["dependencies"], TASK_ID]
            root_child_indices.append(i)

    batch_tasks = []
    for i, child in enumerate(normalized_children):
        metadata = dict(child["metadata"])
        metadata.update({
            "parent_task_id": TASK_ID,
            "delegation_batch_id": batch_id,
            "delegation_mode": mode,
            "delegated_files": child["files"],
            "delegated_child_index": i,
        })
        batch_tasks.append({
            "id": f"{child['type']}-{uuid.uuid4().hex[:12]}-agent",
            "project": proj,
            "type": child["type"],
            "description": child["description"],
            "priority": child["priority"],
            "dependencies": child["dependencies"],
            "depends_on": sorted(set(child["depends_on_children"])),
            "max_attempts": 3,
            "metadata": metadata,
        })

    body = json.dumps({
        "project": proj,
        "tasks": batch_tasks,
        "chain_to_head": False,
    }).encode()
    try:
        req = _ur.Request(
            f"http://localhost:{API_PORT}/api/tasks/batch",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if "error" in result:
        return {"ok": False, "error": result["error"]}

    ids = result.get("ids", [])
    successor_task_id = None
    successor_kind = None

    def _create_successor_task(kind: str, description: str) -> str:
        successor_id = f"{TASK_TYPE}-{uuid.uuid4().hex[:12]}-agent"
        successor_metadata = {
            "parent_task_id": TASK_ID,
            "delegation_batch_id": batch_id,
            "delegation_mode": mode,
            "delegation_successor_kind": kind,
            "delegation_child_task_ids": ids,
            "delegated_files": sorted({path for child in normalized_children for path in child["files"]}),
        }
        successor_payload = {
            "id": successor_id,
            "project": proj,
            "type": TASK_TYPE if TASK_TYPE in valid_types else "feature",
            "description": description,
            "priority": TASK_PRIORITY,
            "max_attempts": 3,
            "dependencies": ids,
            "metadata": successor_metadata,
        }
        created = _api_request("POST", "/api/tasks", payload=successor_payload, timeout=15)
        return (created.get("task") or {}).get("id", successor_id)

    if mode == "integrate":
        successor_kind = "integration"
        successor_task_id = _create_successor_task(
            "integration",
            (
                f"Integrate delegated child work for parent task {TASK_ID}.\n\n"
                "Read the completed child tasks in this delegation batch, reconcile their changes, "
                "resolve any seams between subsystems, run the narrowest validation that proves the "
                "combined work behaves correctly, then finish the original parent objective."
            ),
        )
    elif mode == "wait":
        successor_kind = "resume"
        successor_task_id = _create_successor_task(
            "resume",
            (
                f"Resume delegated parent task {TASK_ID} after its child tasks complete.\n\n"
                "Review the child-task outputs in this delegation batch, continue the remaining work "
                "that still belongs to the original parent task, and then validate and wrap it up."
            ),
        )

    try:
        parent_task = _get_task_from_api(TASK_ID)
        parent_meta = dict(parent_task.get("metadata") or {})
        parent_meta.update({
            "delegation_batch_id": batch_id,
            "delegation_mode": mode,
            "delegation_state": "delegated",
            "delegated_child_task_ids": ids,
            "delegated_child_count": len(ids),
            "delegated_root_child_indices": root_child_indices,
        })
        if successor_task_id:
            parent_meta["delegation_successor_task_id"] = successor_task_id
            parent_meta["delegation_successor_kind"] = successor_kind
        _patch_task_via_api(TASK_ID, {"metadata": parent_meta})
    except Exception as e:
        log(f"[delegate_task_batch] WARNING: failed to persist parent delegation metadata: {e}")

    log(
        f"[delegate_task_batch] Created {len(ids)} child tasks under {TASK_ID} "
        f"(mode={mode}, successor={successor_task_id or 'none'})"
    )
    response = {
        "ok": True,
        "task_ids": ids,
        "count": len(ids),
        "delegation_batch_id": batch_id,
        "mode": mode,
        "root_child_indices": root_child_indices,
        "parent_action": "complete_parent",
    }
    if successor_task_id:
        response["successor_task_id"] = successor_task_id
        response["successor_kind"] = successor_kind
    return response


def create_tasks_file_aware(tasks: list, project: str = None) -> dict:
    """Create multiple tasks with automatic file-aware dependency chaining.

    Analyses which files each task will touch and automatically adds dependencies
    between any two tasks that share a file — preventing merge conflicts from
    parallel agents working on the same file.

    Each task dict must have:
      - "type": "feature" | "bug" | "refactor" | "polish"
      - "description": str
      - "files": list[str] — files this task will CREATE or MODIFY
      - "priority": int (optional, default 50)
      - "dependencies": list[str] (optional) — explicit extra task IDs

    Returns {"ok": True, "tasks": [...], "count": N} on success.
    """
    if not tasks:
        return {"ok": False, "error": "tasks list is empty"}

    proj = project or PROJECT

    # Build file → first-owner index map and auto-chain deps
    file_owner: dict = {}   # filename → index of first task that owns it
    auto_deps: list = [set() for _ in tasks]

    for i, task in enumerate(tasks):
        explicit_ids = [d for d in task.get("dependencies", []) if isinstance(d, str)]
        bad_dep = next((dep for dep in explicit_ids if _looks_like_dependency_path(dep)), None)
        if bad_dep:
            return {
                "ok": False,
                "error": (
                    f"Task {i} has invalid dependency '{bad_dep}'. "
                    "Planner dependencies must be task IDs only; keep file ownership in files=[...]."
                ),
            }
        for f in task.get("files", []):
            f = f.strip()
            if not f:
                continue
            if f in file_owner:
                auto_deps[i].add(file_owner[f])
            else:
                file_owner[f] = i

    # Topological sort: roots first
    order = []
    remaining = list(range(len(tasks)))
    created_indices: list = []

    while remaining:
        ready = [i for i in remaining if all(d in created_indices for d in auto_deps[i])]
        if not ready:
            ready = [remaining[0]]
        for i in ready:
            order.append(i)
            created_indices.append(i)
            remaining.remove(i)

    ordered_pos = {task_idx: pos for pos, task_idx in enumerate(order)}
    batch_tasks = []
    generated_ids: dict[int, str] = {}

    for i in order:
        task = tasks[i]
        explicit_ids = [d for d in task.get("dependencies", []) if isinstance(d, str)]
        if TASK_TYPE in ("project_plan", "plan", "python_plan") and TASK_ID and not explicit_ids and not auto_deps[i]:
            explicit_ids = [TASK_ID]
        task_type = task.get("type", "feature")
        priority = min(int(task.get("priority", 50)), 90)
        generated_id = f"{task_type}-{uuid.uuid4().hex[:12]}-agent"
        generated_ids[i] = generated_id
        metadata = dict(task.get("metadata") or {})
        if TASK_TYPE in ("project_plan", "plan", "python_plan"):
            metadata.setdefault("parent_task_id", TASK_ID)
            metadata.setdefault("planner_task_type", TASK_TYPE)
            metadata.setdefault("planned_files", list(task.get("files", [])))
            metadata.setdefault("file_aware_auto_dep_indices", sorted(auto_deps[i]))

        batch_tasks.append({
            "id": generated_id,
            "project": proj,
            "type": task_type,
            "description": task.get("description", ""),
            "priority": priority,
            "dependencies": explicit_ids,
            "depends_on": [ordered_pos[d] for d in sorted(auto_deps[i])],
            "max_attempts": 3,
            "metadata": metadata,
        })

    body = json.dumps({
        "project": proj,
        "tasks": batch_tasks,
        "chain_to_head": True,
    }).encode()
    try:
        req = _ur.Request(
            f"http://localhost:{API_PORT}/api/tasks/batch",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if "error" in result:
        return {"ok": False, "error": result["error"]}

    response_id_map = {
        int(index): task_id
        for index, task_id in (result.get("id_map") or {}).items()
    }
    results = []
    for batch_pos, i in enumerate(order):
        task_id = response_id_map.get(batch_pos, generated_ids[i])
        task_type = batch_tasks[batch_pos]["type"]
        auto_dep_ids = [
            response_id_map.get(ordered_pos[d], generated_ids[d])
            for d in sorted(auto_deps[i])
        ]
        results.append({
            "task_id": task_id,
            "type": task_type,
            "auto_deps": auto_dep_ids,
            "parallel": len(auto_deps[i]) == 0,
        })
        log(f"[create_tasks_file_aware] Created {task_type} {task_id}: {batch_tasks[batch_pos]['description'][:60]}")

    return {
        "ok": True,
        "tasks": results,
        "count": len(results),
        "errors": None,
    }


def create_tasks(tasks: list, project: str = None, chain_to_head: bool = False) -> dict:
    """Create multiple tasks in one call with reliable DAG dependency wiring.

    Use this instead of calling create_task() repeatedly when you need to create
    3+ tasks with dependencies between them. All IDs are generated upfront so
    depends_on index references always resolve — no chicken-and-egg problem.

    Each task dict supports:
      - "type": "feature" | "bug" | "refactor" | "polish" | "qa" | "plan"
      - "description": str
      - "priority": int (optional, default 50)
      - "depends_on": list[int] — indices into THIS tasks list (resolved to IDs before creation)
      - "dependencies": list[str] — explicit task IDs outside this batch (merged with depends_on)

    chain_to_head: if True, the first rootless task in the batch automatically depends on
                   the project HEAD (last completed task), keeping history connected.

    Example — bug fixes in parallel, feature waits for both:
      create_tasks([
        {"type": "bug",     "description": "Fix crash in login",   "priority": 80},
        {"type": "bug",     "description": "Fix session timeout",  "priority": 80},
        {"type": "feature", "description": "Add OAuth",            "priority": 50, "depends_on": [0, 1]},
      ])

    Returns {"ok": bool, "ids": [...], "id_map": {"0": "<id>", ...}, "count": N}
    """
    proj = project or PROJECT
    body = json.dumps({
        "project": proj,
        "tasks": tasks,
        "chain_to_head": chain_to_head,
    }).encode()
    try:
        req = _ur.Request(
            f"http://localhost:{API_PORT}/api/tasks/batch",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        if "error" in result:
            return {"ok": False, "error": result["error"]}
        ids = result.get("ids", [])
        log(f"[create_tasks] Created {len(ids)} tasks for '{proj}'")
        return {"ok": True, "ids": ids, "id_map": result.get("id_map", {}), "count": len(ids)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_tasks(project: str = None) -> dict:
    """List all tasks for the current (or specified) project."""
    target_project = project if project else PROJECT
    try:
        with _ur.urlopen(f"http://localhost:{API_PORT}/api/tasks", timeout=10) as resp:
            data = json.loads(resp.read())
        tasks = [
            {
                "id": t["id"],
                "type": t.get("type", ""),
                "status": t.get("status", ""),
                "description": t.get("description", "")[:80],
            }
            for t in data.get("tasks", [])
            if t.get("project") == target_project
        ]
        return {"ok": True, "tasks": tasks, "project": target_project}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_subtasks(parent_task_id: str = None) -> dict:
    """List sub-tasks. Defaults to the current TASK_ID if parent not specified."""
    target = parent_task_id if parent_task_id else TASK_ID
    try:
        with _ur.urlopen(f"http://localhost:{API_PORT}/api/tasks", timeout=10) as resp:
            data = json.loads(resp.read())
        subtasks = [
            {
                "id": t["id"],
                "type": t.get("type", ""),
                "status": t.get("status", ""),
                "description": t.get("description", "")[:80],
            }
            for t in data.get("tasks", [])
            if t.get("metadata", {}).get("parent_task_id") == target
        ]
        return {"ok": True, "subtasks": subtasks, "parent_task_id": target}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Adaptive Graph tools ──────────────────────────────────────────────────────
# These tools allow an agent to mutate downstream tasks in the dependency graph
# as it discovers information during its own work. Agents may only touch tasks
# that are downstream of their own task (pending, not in-progress).

def _get_downstream_task_ids(task_id: str, all_tasks: list) -> set:
    """Return all task IDs that transitively depend on task_id."""
    dependents = set()
    changed = True
    frontier = {task_id}
    while changed:
        changed = False
        for t in all_tasks:
            if t["id"] in dependents:
                continue
            if any(d in frontier for d in (t.get("dependencies") or [])):
                dependents.add(t["id"])
                frontier = {t["id"]}
                changed = True
    return dependents


def _fetch_all_project_tasks(proj: str) -> tuple[list, str]:
    """Fetch all tasks for the project. Returns (tasks, error)."""
    try:
        with _ur.urlopen(f"http://localhost:{API_PORT}/api/tasks", timeout=10) as resp:
            data = json.loads(resp.read())
        tasks = [t for t in data.get("tasks", []) if t.get("project") == proj]
        return tasks, ""
    except Exception as e:
        return [], str(e)


def annotate_downstream_tasks(findings: str, task_ids: list = None) -> dict:
    """Prepend a 'CONTEXT FROM UPSTREAM AGENT' block to downstream pending tasks.

    Call this before TASK_COMPLETE to share what you learned with agents that
    will work on tasks that depend on yours. Useful for:
    - Sharing API shapes, file paths, or architectural decisions you made
    - Warning about constraints or gotchas you discovered
    - Clarifying assumptions the planner made that turned out to be wrong

    findings: free-text summary of what you learned (2-10 sentences)
    task_ids: optional list of specific downstream task IDs to annotate;
              if omitted, all pending downstream tasks are annotated
    """
    if not findings or not findings.strip():
        return {"ok": False, "error": "findings must not be empty"}

    proj = PROJECT
    all_tasks, err = _fetch_all_project_tasks(proj)
    if err:
        return {"ok": False, "error": err}

    downstream_ids = _get_downstream_task_ids(TASK_ID, all_tasks)
    targets = [
        t for t in all_tasks
        if t["id"] in downstream_ids
        and t["status"] == "pending"
        and (task_ids is None or t["id"] in task_ids)
    ]

    if not targets:
        return {"ok": True, "annotated": 0, "note": "no eligible downstream tasks found"}

    annotation_block = (
        f"\n\n---\n"
        f"CONTEXT FROM UPSTREAM AGENT (task {TASK_ID}):\n"
        f"{findings.strip()}\n"
        f"---\n"
    )

    annotated = []
    errors = []
    for t in targets:
        new_desc = t.get("description", "") + annotation_block
        try:
            result = _api_request("PATCH", f"/api/tasks/{t['id']}", {"description": new_desc})
            if result.get("error"):
                errors.append(f"{t['id']}: {result['error']}")
            else:
                annotated.append(t["id"])
                log(f"[AdaptiveGraph] Annotated downstream task {t['id']}")
        except Exception as e:
            errors.append(f"{t['id']}: {e}")

    return {"ok": True, "annotated": len(annotated), "task_ids": annotated, "errors": errors}


def split_task(task_id: str, replacement_tasks: list) -> dict:
    """Replace a pending downstream task with multiple smaller tasks.

    The original task is deleted. Tasks that depended on it are rewired to
    depend on the last replacement task. The replacements are chained in order
    (each depends on the previous).

    task_id: ID of the downstream pending task to replace
    replacement_tasks: list of dicts, each with:
        - description (required)
        - type: feature | bug | refactor | polish (default: same as original)
        - priority: int (default: same as original)

    Safety: only works on pending tasks downstream of the current task.
    Cannot split tasks that are in-progress or already completed.
    """
    if not replacement_tasks or len(replacement_tasks) < 2:
        return {"ok": False, "error": "replacement_tasks must contain at least 2 tasks"}

    proj = PROJECT
    all_tasks, err = _fetch_all_project_tasks(proj)
    if err:
        return {"ok": False, "error": err}

    # Safety: must be downstream and pending
    downstream_ids = _get_downstream_task_ids(TASK_ID, all_tasks)
    original = next((t for t in all_tasks if t["id"] == task_id), None)
    if not original:
        return {"ok": False, "error": f"Task {task_id} not found in project {proj}"}
    if task_id not in downstream_ids:
        return {"ok": False, "error": f"Task {task_id} is not downstream of the current task {TASK_ID} — cannot split"}
    if original["status"] != "pending":
        return {"ok": False, "error": f"Task {task_id} is {original['status']} — can only split pending tasks"}

    original_deps = original.get("dependencies") or []
    original_type = original.get("type", "feature")
    original_priority = original.get("priority", 50)

    # Build replacement task records, chained in sequence
    new_ids = []
    new_tasks = []
    for i, rt in enumerate(replacement_tasks):
        new_id = f"{proj}-split-{int(time.time() * 1000) % 10**9}-{i}"
        deps = original_deps if i == 0 else [new_ids[i - 1]]
        new_tasks.append({
            "id": new_id,
            "project": proj,
            "type": rt.get("type", original_type),
            "priority": int(rt.get("priority", original_priority)),
            "description": rt["description"].strip(),
            "status": "pending",
            "dependencies": deps,
            "metadata": {"split_from": task_id, "split_index": i, "created_by_agent": TASK_ID},
        })
        new_ids.append(new_id)

    # Cycle check: verify no new task introduces a cycle
    # (simple check: none of the new IDs appear in their own transitive deps)
    existing_ids = {t["id"] for t in all_tasks}
    for nt in new_tasks:
        for d in nt["dependencies"]:
            if d not in existing_ids and d not in new_ids:
                return {"ok": False, "error": f"Dependency {d} does not exist"}

    # Create replacement tasks
    created = []
    for nt in new_tasks:
        try:
            result = _api_request("POST", "/api/tasks", nt)
            if result.get("error"):
                return {"ok": False, "error": f"Failed to create replacement task: {result['error']}", "created_so_far": created}
            created.append(nt["id"])
        except Exception as e:
            return {"ok": False, "error": str(e), "created_so_far": created}

    # Rewire: tasks that depended on the original now depend on the last replacement
    last_id = new_ids[-1]
    rewired = []
    for t in all_tasks:
        deps = t.get("dependencies") or []
        if task_id in deps and t["id"] != task_id:
            new_deps = [last_id if d == task_id else d for d in deps]
            try:
                _api_request("PATCH", f"/api/tasks/{t['id']}", {"dependencies": new_deps})
                rewired.append(t["id"])
            except Exception as e:
                log(f"[AdaptiveGraph] Warning: failed to rewire {t['id']}: {e}")

    # Delete the original task
    try:
        _api_request("DELETE", f"/api/tasks/{task_id}")
        log(f"[AdaptiveGraph] Deleted original task {task_id}, replaced with {new_ids}")
    except Exception as e:
        return {"ok": False, "error": f"Replacements created but failed to delete original: {e}",
                "created": created, "rewired": rewired}

    return {
        "ok": True,
        "original_deleted": task_id,
        "replacements_created": created,
        "tasks_rewired": rewired,
    }


def prune_task(task_id: str, reason: str) -> dict:
    """Mark a pending downstream task as completed (redundant/already done).

    Use this when your work has made a downstream task unnecessary — for example
    you implemented something that was going to be a separate task, or you
    discovered the task described work that doesn't need to happen.

    task_id: ID of the downstream pending task to prune
    reason: explanation of why the task is no longer needed (recorded in metadata)

    Safety: only works on pending tasks downstream of the current task.
    """
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason must not be empty"}

    proj = PROJECT
    all_tasks, err = _fetch_all_project_tasks(proj)
    if err:
        return {"ok": False, "error": err}

    downstream_ids = _get_downstream_task_ids(TASK_ID, all_tasks)
    target = next((t for t in all_tasks if t["id"] == task_id), None)
    if not target:
        return {"ok": False, "error": f"Task {task_id} not found in project {proj}"}
    if task_id not in downstream_ids:
        return {"ok": False, "error": f"Task {task_id} is not downstream of {TASK_ID} — cannot prune"}
    if target["status"] != "pending":
        return {"ok": False, "error": f"Task {task_id} is {target['status']} — can only prune pending tasks"}

    existing_meta = target.get("metadata") or {}
    try:
        _api_request("PATCH", f"/api/tasks/{task_id}", {
            "status": "completed",
            "metadata": {
                **existing_meta,
                "pruned_by_agent": TASK_ID,
                "prune_reason": reason.strip(),
            },
        })
        log(f"[AdaptiveGraph] Pruned task {task_id}: {reason[:80]}")
        return {"ok": True, "pruned": task_id, "reason": reason.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def insert_dependency(from_task_id: str, to_task_id: str) -> dict:
    """Add a dependency edge: from_task_id will now depend on to_task_id.

    Use this when you discover that two existing downstream tasks have an
    ordering constraint the planner didn't know about — i.e. from_task_id
    cannot safely run until to_task_id completes.

    from_task_id: the task that needs to wait
    to_task_id: the task that must complete first

    Safety:
    - Both tasks must be pending and downstream of the current task
    - The edge must not create a cycle
    - Cannot modify tasks that are in-progress or completed
    """
    proj = PROJECT
    all_tasks, err = _fetch_all_project_tasks(proj)
    if err:
        return {"ok": False, "error": err}

    downstream_ids = _get_downstream_task_ids(TASK_ID, all_tasks)
    from_task = next((t for t in all_tasks if t["id"] == from_task_id), None)
    to_task = next((t for t in all_tasks if t["id"] == to_task_id), None)

    if not from_task:
        return {"ok": False, "error": f"Task {from_task_id} not found"}
    if not to_task:
        return {"ok": False, "error": f"Task {to_task_id} not found"}
    if from_task_id not in downstream_ids:
        return {"ok": False, "error": f"{from_task_id} is not downstream of {TASK_ID}"}
    if to_task_id not in downstream_ids:
        return {"ok": False, "error": f"{to_task_id} is not downstream of {TASK_ID}"}
    if from_task["status"] != "pending":
        return {"ok": False, "error": f"{from_task_id} is {from_task['status']} — can only insert deps on pending tasks"}
    if to_task["status"] == "in_progress":
        return {"ok": False, "error": f"{to_task_id} is in_progress — inserting this dep would block work already underway"}

    # Cycle check: would adding from→to create a cycle?
    # A cycle exists if to_task_id is already reachable from from_task_id
    # (i.e. to already transitively depends on from)
    reachable_from_from = _get_downstream_task_ids(from_task_id, all_tasks)
    if to_task_id in reachable_from_from:
        return {"ok": False, "error": f"Adding {from_task_id} → {to_task_id} would create a cycle"}
    if from_task_id == to_task_id:
        return {"ok": False, "error": "A task cannot depend on itself"}

    existing_deps = list(from_task.get("dependencies") or [])
    if to_task_id in existing_deps:
        return {"ok": True, "note": "dependency already exists", "from": from_task_id, "to": to_task_id}

    new_deps = existing_deps + [to_task_id]
    try:
        _api_request("PATCH", f"/api/tasks/{from_task_id}", {"dependencies": new_deps})
        log(f"[AdaptiveGraph] Inserted dep: {from_task_id} now depends on {to_task_id}")
        return {"ok": True, "inserted": {"from": from_task_id, "to": to_task_id}}
    except Exception as e:
        return {"ok": False, "error": str(e)}
