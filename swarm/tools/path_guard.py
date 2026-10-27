"""swarm.tools.path_guard -- path-protection and command-safety helpers."""

import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

from swarm.guard_log import log_blocked
from swarm.tools import _shared
from swarm.tools._shared import _project_root

# Absolute, symlink-resolved path of the swarm-controller checkout that this
# module lives in (swarm/tools/path_guard.py -> repo root is three levels up).
_SWARM_CONTROLLER_ROOT = Path(os.path.realpath(__file__)).parents[2]

# Files an agent must never modify through file tools or shell commands when
# it is working on swarm-controller itself: the guard code, the tool dispatcher,
# the agent lifecycle/runtime, the prompt library, and the live config.
_GUARDRAIL_FILES = frozenset({
    "swarm/tools/path_guard.py",
    "swarm/tools/shell.py",
    "swarm/tool_dispatch.py",
    "swarm/agent_lifecycle.py",
    "swarm/agent_runtime.py",
    "swarm/guard_log.py",
    "swarm/containment_breaker.py",
    "config.json",
})
_GUARDRAIL_DIRS = ("prompts",)


def _temp_roots() -> tuple[Path, ...]:
    """Scratch roots agents may write to outside the project.

    Mirrors the sandbox profile (swarm/sandbox.py: TMP_DIR, VAR_FOLDERS,
    /private/var/tmp) so Layer 1 is not stricter than Layer 2 about scratch
    files.  The per-user temp dir is only trusted when it really is a system
    temp location -- a TMPDIR pointed at $HOME must not widen the allowlist.
    """
    if sys.platform == "win32":
        return ()
    roots = {os.path.realpath("/tmp"), os.path.realpath("/var/tmp")}
    per_user = os.path.realpath(tempfile.gettempdir())
    if per_user.startswith(("/private/var/folders/", "/var/folders/")):
        roots.add(per_user)
    return tuple(Path(r) for r in sorted(roots))


def _is_temp_path(resolved: Path) -> bool:
    """True if an already-realpath'd ``resolved`` lies strictly inside a temp root."""
    return any(root in resolved.parents for root in _temp_roots())


_GODOT_NAME_RE = re.compile(r'^\s*config/name\s*=\s*"([^"]*)"', re.MULTILINE)


def _godot_userdata_roots() -> tuple[Path, ...]:
    """This project's own Godot ``user://`` dir(s), where the game keeps saves.

    Agents reset saves and read game output there while testing.  Only the
    dir named after *this* project (``config/name`` in project.godot, plus the
    swarm project name) is trusted -- other games' user data, the shared
    ``Godot/remote`` dir and the rest of $HOME stay outside.
    """
    root = _real_project_root()
    project_file = root / "project.godot"
    if not project_file.is_file():
        return ()
    names = {_shared.PROJECT or ""}
    try:
        m = _GODOT_NAME_RE.search(project_file.read_text(errors="replace"))
        if m:
            names.add(m.group(1))
    except OSError:
        pass
    names = {n for n in names if n.strip() and n not in (".", "..") and "/" not in n and os.sep not in n}
    home = Path(os.path.expanduser("~"))
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "Godot" / "app_userdata"
    elif sys.platform.startswith("linux"):
        base = home / ".local" / "share" / "godot" / "app_userdata"
    else:
        return ()
    return tuple(Path(os.path.realpath(base / n)) for n in sorted(names))


def _is_scratch_path(resolved: Path) -> bool:
    """True if an already-realpath'd ``resolved`` lies strictly inside a temp
    root or this project's Godot user-data dir."""
    return _is_temp_path(resolved) or any(root in resolved.parents for root in _godot_userdata_roots())


# Files outside the project that one task type is told to read (never write).
# playthrough_bot.yaml has the agent build on swarm/tools/playthrough_kit.py.
_TASK_READ_ALLOWANCES: dict[str, tuple[str, ...]] = {
    "playthrough_bot": ("swarm/tools/playthrough_kit.py",),
}


ASSET_LIBRARY_ENV = "SWARM_ASSET_LIBRARY_PATHS"


def _asset_library_roots() -> tuple[Path, ...]:
    """Read-only asset libraries agents may copy FROM (config asset_library_paths).

    Never a home dir or filesystem root, so a misconfigured entry cannot
    open up $HOME."""
    roots = []
    home = Path(os.path.realpath(os.path.expanduser("~")))
    for raw in os.environ.get(ASSET_LIBRARY_ENV, "").split(os.pathsep):
        raw = raw.strip()
        if not raw:
            continue
        root = Path(os.path.realpath(os.path.expanduser(raw)))
        if root in (Path("/"), home) or root in home.parents or len(root.parts) < 3:
            continue
        roots.append(root)
    return tuple(roots)


def _is_asset_library_path(resolved: Path) -> bool:
    return any(root == resolved or root in resolved.parents for root in _asset_library_roots())


def _is_task_read_allowance(resolved: Path) -> bool:
    """True if ``resolved`` is a file this task type may read outside its root."""
    for rel in _TASK_READ_ALLOWANCES.get(_get_task_type(), ()):
        if resolved == Path(os.path.realpath(_SWARM_CONTROLLER_ROOT / rel)):
            return True
    return False


class PathConfinementError(ValueError):
    """Raised when a tool path resolves outside the active project root."""


def _get_task_type() -> str:
    return _shared.TASK_TYPE


def _real_project_root() -> Path:
    """Project root with ~ expanded and symlinks resolved (realpath)."""
    return Path(os.path.realpath(os.path.expanduser(str(_project_root()))))


def _confine_project_path(path_arg, quiet: bool = False, read_only: bool = False) -> tuple[Path | None, dict | None]:
    """Single boundary check for every file tool.

    Expands ``~``, anchors relative paths at the project root, resolves the
    result with realpath (so ``..`` segments and symlinks cannot escape) and
    verifies it is inside the project root returned by ``_project_root()``
    (which honours the worktree override).

    Returns ``(path, None)`` on success, where ``path`` is the confined path
    anchored at the (unresolved) project root, or ``(None, error_dict)`` on a
    violation. The error dict is already in tool-result shape. Violations are
    logged as ``[GUARD] blocked kind=path`` unless ``quiet`` (used when the
    caller is filtering a directory walk, not honouring an agent request).
    """
    raw = os.path.expanduser(str(path_arg or "."))
    real_root = _real_project_root()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = real_root / candidate
    try:
        resolved = Path(os.path.realpath(str(candidate)))
    except ValueError as exc:  # embedded NUL byte: refuse, never raise into the tool
        if not quiet:
            log_blocked("path", "file", f"{path_arg!r}: invalid path")
        return None, {
            "ok": False,
            "error": (
                f"Refusing to access {str(path_arg)!r}: invalid path ({exc}). "
                "File tools are confined to the project directory; use a plain "
                "path relative to the project root."
            ),
        }
    try:
        rel = resolved.relative_to(real_root)
    except ValueError:
        if _is_scratch_path(resolved):
            return resolved, None
        if read_only and (_is_task_read_allowance(resolved) or _is_asset_library_path(resolved)):
            return resolved, None
        if not quiet:
            log_blocked("path", "file", f"{path_arg} -> {resolved}")
        return None, {
            "ok": False,
            "error": (
                f"Refusing to access '{path_arg}': it resolves to '{resolved}', "
                f"which is outside the project root '{real_root}'. "
                "File tools are confined to the project directory; use a path "
                "relative to the project root."
            ),
        }
    project_root = Path(_project_root())
    return (project_root / rel) if rel.parts else project_root, None


_DENIED_SENTINEL_DIR = ".swarm_path_denied"


def _resolve_project_path(path_arg: str, strict: bool = False) -> Path:
    """Resolve ``path_arg`` inside the project root.

    If the path escapes the project root: with ``strict=True`` raise
    ``PathConfinementError``; otherwise return a sentinel path under the
    project root that is guaranteed not to exist, so legacy callers that only
    check ``.exists()`` (e.g. ``core.consult_helper``) never touch the
    outside file. Tool functions should prefer ``_confine_project_path``,
    which returns an error dict instead.
    """
    path, err = _confine_project_path(path_arg)
    if err:
        if strict:
            raise PathConfinementError(err["error"])
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(path_arg))[:64] or "path"
        return Path(_project_root()) / _DENIED_SENTINEL_DIR / safe_name
    return path


def _is_vendor_path(path: Path) -> bool:
    return _protected_path_reason(path) == "vendor"


def _guardrail_active() -> bool:
    """True when the agent is working on the swarm-controller repo itself."""
    if _shared.PROJECT == "swarm-controller":
        return True
    try:
        _real_project_root().relative_to(_SWARM_CONTROLLER_ROOT)
        return True
    except ValueError:
        return False


def _is_guardrail_rel(rel: Path) -> bool:
    posix = rel.as_posix()
    if posix in _GUARDRAIL_FILES:
        return True
    return bool(rel.parts) and rel.parts[0] in _GUARDRAIL_DIRS


def _guardrail_reason(path: Path) -> str | None:
    """Return "guardrail" if ``path`` is one of the protected swarm-controller files."""
    real = Path(os.path.realpath(os.path.expanduser(str(path))))
    roots: list[Path] = []
    if _shared.PROJECT == "swarm-controller":
        roots.append(_real_project_root())
    roots.append(_SWARM_CONTROLLER_ROOT)
    for root in roots:
        try:
            rel = real.relative_to(root)
        except ValueError:
            continue
        if _is_guardrail_rel(rel):
            return "guardrail"
    return None


def _protected_path_reason(path: Path) -> str | None:
    rel = None
    for root in (Path(_project_root()), _real_project_root()):
        try:
            rel = path.relative_to(root)
            break
        except ValueError:
            continue
    if rel is None:
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
    if _guardrail_active() and _guardrail_reason(path):
        return "guardrail"
    return None


def _protected_path_error(path_arg: str, reason: str, tool: str = "file", command: str = "") -> dict:
    # Log the command too: the matched path alone does not say what was attempted.
    log_blocked("protected", tool, f"{reason}: {path_arg}" + (f": {command}" if command else ""))
    if reason == "vendor":
        detail = "Files under addons/ are third-party and read-only."
        label = "vendor code"
    elif reason == "guardrail":
        detail = (
            "This file is part of the swarm-controller safety guardrail "
            "(path guard, shell guard, tool dispatch, agent lifecycle/runtime, "
            "prompts/, config.json) and may not be modified by agents."
        )
        label = "guardrail code"
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


_GUARDRAIL_CMD_PATTERNS = [
    ("guardrail", r"(?<![\w-])swarm/tools/path_guard\.py\b"),
    ("guardrail", r"(?<![\w-])swarm/tools/shell\.py\b"),
    ("guardrail", r"(?<![\w-])swarm/tool_dispatch\.py\b"),
    ("guardrail", r"(?<![\w-])swarm/agent_lifecycle\.py\b"),
    ("guardrail", r"(?<![\w-])swarm/agent_runtime\.py\b"),
    ("guardrail", r"(?<![\w-])swarm/guard_log\.py\b"),
    ("guardrail", r"(?<![\w-])swarm/containment_breaker\.py\b"),
    ("guardrail", r"(?<![\w./-])prompts/"),
    ("guardrail", r"(?<![\w.-])config\.json\b"),
]


_PROTECTED_CMD_PATTERNS: list[tuple[str, str]] = [
    # addons/x, ./addons/x and a bare `addons` dir argument (find addons ...).
    ("vendor", r"(?<![\w.-])(?:\./)?addons(?:/|(?![\w./-]))"),
    ("generated", r"(?<![\w.-])(?:\./)?\.godot/"),
    ("generated", r"(?<![\w.-])(?:\./)?\.import/"),
    ("generated", r"(?<![\w./-])[^\s'\"`|;&]+\.uid\b"),
]


def _command_targets_protected_path(cmd: str) -> tuple[str, str] | None:
    patterns = list(_PROTECTED_CMD_PATTERNS)
    if _guardrail_active():
        patterns.extend(_GUARDRAIL_CMD_PATTERNS)
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
    if _guardrail_active():
        patterns.extend(
            ("guardrail", r"['\"](?P<path>" + core + r"[^'\"]*)['\"]")
            for _, core in _GUARDRAIL_CMD_PATTERNS
        )
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
    # Recursive / force deletes -- rm -r, rm -rf, rm -fr, find ... -delete
    (r"\brm\s+(?:[^;|&\n]*\s)?-[a-z]*r[a-z]*f?[a-z]*\b", "recursive rm (-r/-rf/-fr)"),
    (r"\brm\s+(?:[^;|&\n]*\s)?-[a-z]*f[a-z]*r[a-z]*\b", "recursive rm (-fr/-rf)"),
    (r"\bfind\b[^;|&\n]*\s--delete\b", "find --delete (recursive file removal)"),
    (r"\bfind\b[^;|&\n]*\s-delete\b", "find -delete (recursive file removal)"),
    # Destructive git operations
    (r"\bgit(?:\s+-{1,2}[^\s;|&]+(?:\s+[^\s;|&-][^\s;|&]*)?)*\s+push\s+(?:[^;|&\n]*\s)?(?:--force|-f)\b", "git push --force"),
    (r"\bgit(?:\s+-{1,2}[^\s;|&]+(?:\s+[^\s;|&-][^\s;|&]*)?)*\s+push\s+(?:[^;|&\n]*\s)?--force-with-lease\b", "git push --force-with-lease"),
    (r"\bgit(?:\s+-{1,2}[^\s;|&]+(?:\s+[^\s;|&-][^\s;|&]*)?)*\s+reset\s+(?:[^;|&\n]*\s)?--hard\b", "git reset --hard"),
    (r"\bgit(?:\s+-{1,2}[^\s;|&]+(?:\s+[^\s;|&-][^\s;|&]*)?)*\s+branch\s+(?:[^;|&\n]*\s)?-D\b", "git branch -D (force branch delete)"),
    (r"\bgit(?:\s+-{1,2}[^\s;|&]+(?:\s+[^\s;|&-][^\s;|&]*)?)*\s+clean\s+(?:[^;|&\n]*\s)?-[a-z]*f[a-z]*\b", "git clean -f (destroys untracked files)"),
    (r"\bgit\s+filter-branch\b", "git filter-branch (rewrites history)"),
    (r"\bgit\s+filter-repo\b", "git filter-repo (rewrites history)"),
    # Process killing by name -- too broad / irreversible
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
            if reason.startswith(("recursive rm", "find")) and _recursive_delete_is_contained(cmd):
                continue
            if reason == "git reset --hard" and _worktree_reset_is_contained(cmd):
                continue
            return reason
    return None


# Split a command into simple commands at ; && || | & and newlines, but not
# at the & of a 2>&1 / >&2 redirection.
_CMD_SEPARATOR = re.compile(r"\|\||&&|;|\n|\|(?!\|)|(?<![<>])&(?!\d)")
_GLOB_CHARS = frozenset("*?[]{}$`~")


def _is_generated_file_target(token: str) -> bool:
    """True if ``token`` names one existing-or-not *file* (never a directory)
    strictly inside .godot/ or .import/ of the project root."""
    if not token or any(ch in _GLOB_CHARS for ch in token):
        return False
    if ".." in Path(token).parts:
        return False
    root = _real_project_root()
    candidate = Path(token) if os.path.isabs(token) else root / token
    resolved = Path(os.path.realpath(candidate))
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return False
    gen = [i for i, part in enumerate(rel.parts) if part in (".godot", ".import")]
    if not gen or gen[0] == len(rel.parts) - 1:
        return False
    return not resolved.is_dir()


def _is_scratch_delete_target(token: str) -> bool:
    """True if ``token`` is an absolute path strictly inside a temp root.

    A glob is allowed only after a fixed directory that is itself strictly
    inside the temp root (``/tmp/bot/*`` yes, ``/tmp/*`` no).  Symlinks are
    resolved, so ``/tmp/link`` pointing at $HOME does not count."""
    if not token.startswith("/") or any(ch in token for ch in "$`~"):
        return False
    if ".." in Path(token).parts:
        return False
    globbed = [i for i, ch in enumerate(token) if ch in "*?[]{}"]
    fixed = token if not globbed else token[:token.rfind("/", 0, globbed[0]) + 1]
    if not fixed or fixed == "/":
        return False
    # With a glob, everything it can match lives under the fixed directory.
    return _is_temp_path(Path(os.path.realpath(fixed)))


def _recursive_delete_is_contained(cmd: str) -> bool:
    """True if every recursive ``rm`` and every ``find ... -delete`` in ``cmd``
    only touches single generated files in the project (``rm -rf
    .godot/uid_cache.bin``) or paths strictly inside a temp root (``rm -rf
    /tmp/playthrough_bot``).  Agents reach for -rf out of habit; on those
    targets it is harmless.  Directories and globs in the project, anything
    outside it, and symlink-following finds still count as catastrophic."""
    found = False
    for segment in _CMD_SEPARATOR.split(cmd):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            return False
        if not tokens:
            continue
        prog = tokens[0].rsplit("/", 1)[-1]
        if prog == "rm":
            recursive, targets, end_of_opts = False, [], False
            for tok in tokens[1:]:
                if not end_of_opts and tok == "--":
                    end_of_opts = True
                elif not end_of_opts and tok.startswith("-"):
                    if tok == "--recursive" or (not tok.startswith("--") and "r" in tok.lower()):
                        recursive = True
                elif "<" in tok or ">" in tok:
                    continue  # 2>/dev/null, 2>&1
                else:
                    targets.append(tok)
            if not recursive:
                continue
            found = True
            if not targets or not all(
                _is_generated_file_target(t) or _is_scratch_delete_target(t) for t in targets
            ):
                return False
        elif prog == "find":
            if not any(t in ("-delete", "--delete") for t in tokens):
                continue
            found = True
            # -L/-H/-follow make find traverse symlinks out of the temp root
            if any(t in ("-L", "-H", "-follow") for t in tokens):
                return False
            starts = []
            for tok in tokens[1:]:
                if tok.startswith(("-", "(", "!")):
                    break
                starts.append(tok)
            if not starts or not all(_is_scratch_delete_target(t) for t in starts):
                return False
    return found


_REDIRECT_OUT_OF_ROOT = re.compile(r"\bcd\b|\bpushd\b|\bgit\s+(?:[^;|&\n]*\s)?-C\b|--git-dir|--work-tree|GIT_DIR|GIT_WORK_TREE|\$\(|`")


def _worktree_reset_is_contained(cmd: str) -> bool:
    """True when ``git reset --hard`` can only move this agent's own worktree.

    Each agent normally runs in a dedicated worktree on ``agent/<id>``: a hard
    reset there only moves that branch (commits stay in the reflog) and drops
    that worktree's uncommitted changes -- the main checkout, ``main`` and
    other agents are untouched.  It is refused when the agent is not in a
    worktree (fallback to the main checkout) or the command could aim git at
    another checkout (cd, git -C, --git-dir/--work-tree, GIT_DIR, subshells,
    absolute paths)."""
    import swarm.tools.core as _core
    if not getattr(_core, "WORKTREE_BRANCH", ""):
        return False
    root = _real_project_root()
    if not (root / ".git").is_file():  # a linked worktree has a .git *file*
        return False
    if _REDIRECT_OUT_OF_ROOT.search(cmd):
        return False
    for segment in _CMD_SEPARATOR.split(cmd):
        if re.search(r"\bgit\s+reset\b", segment):
            try:
                tokens = shlex.split(segment, posix=True)
            except ValueError:
                return False
            if tokens[:2] != ["git", "reset"]:
                return False
            if any(t.startswith(("/", "~")) or ".." in t.split("/") for t in tokens[2:]):
                return False
    return True


_OTHER_MUTATING_VERBS = [
    r"\bcp\b", r"\bmv\b", r"\binstall\b", r"\bmkdir\b", r"\btouch\b",
    r"\bln\b", r"\brsync\b", r"\btee\b", r"\bsed\s+-i\b", r"\bperl\s+-i\b",
    r"\bgit\s+checkout\b", r"\bgit\s+restore\b", r"\bgit\s+clean\b",
]


def _is_generated_removal(cmd: str, lowered: str) -> bool:
    """True when the command's only file mutation is ``rm``/``git rm`` and every
    protected path it names is a generated artifact (.godot/, .import/, *.uid).

    Deleting a generated file is safe inside the project -- the engine
    regenerates it -- and agents have no other way to clean up orphaned .uid
    files.  Any vendor/guardrail target or any other mutating verb disqualifies
    the whole command.  Recursive deletes are still refused by the
    catastrophic check; redirection and embedded writes are still checked by
    the caller.
    """
    if not re.search(r"\brm\b", lowered):
        return False
    if any(re.search(p, lowered) for p in _OTHER_MUTATING_VERBS):
        return False
    patterns = list(_PROTECTED_CMD_PATTERNS)
    if _guardrail_active():
        patterns.extend(_GUARDRAIL_CMD_PATTERNS)
    reasons = {reason for reason, pattern in patterns if re.search(pattern, cmd)}
    return reasons == {"generated"}


# Constructs that can route a write to a path named in a *different* simple
# command (xargs, find -exec, substitutions, eval, shell -c, sourcing,
# variables).  With any of these present the protected-write check falls back
# to whole-command matching.
_WRITE_INDIRECTION = re.compile(
    r"\bxargs\b|-exec(?:dir)?\b|-ok\b|\$\(|`|<\(|>\(|\beval\b|\bsource\b|(?:^|[;&|(\n])\s*\.\s+\S"
    r"|\b(?:ba|z|da|k)?sh\b|\benv\b|\bparallel\b|\bpushd\b|\bpopd\b|\bwhile\b|\bfor\b"
    r"|(?:^|[\s;&|(])[A-Za-z_]\w*=|\$\{?[A-Za-z_]"
)
_CD_SEGMENT = re.compile(r"^\s*cd(?:\s+(?P<dir>\S+))?\s*$")


def _cd_enters_protected_tree(cmd: str) -> str | None:
    """The protected tree a ``cd``/``pushd`` in ``cmd`` enters, if any.

    After ``cd addons`` a write to ``gut/gut.gd`` names no protected path, so
    the path patterns alone cannot see it."""
    for m in re.finditer(r"(?:^|[;&|(\n])\s*(?:cd|pushd)\s+(['\"]?)([^\s;&|'\"]+)\1", cmd):
        for part in m.group(2).split("/"):
            if part == "addons":
                return "vendor"
            if part in (".godot", ".import"):
                return "generated"
    return None


def _write_scope_is_ambiguous(cmd: str) -> bool:
    """True unless ``cmd`` is a plain list of simple commands whose writes can
    only touch paths named in the same simple command.  A ``cd`` is fine
    unless it enters a protected tree (or, for swarm-controller, at all)."""
    if _WRITE_INDIRECTION.search(cmd):
        return True
    for segment in _CMD_SEPARATOR.split(cmd):
        m = _CD_SEGMENT.match(segment)
        if not m and not re.match(r"^\s*cd\b", segment):
            continue
        if not m or _guardrail_active():
            return True
        target = m.group("dir") or ""
        if re.search(r"addons|\.godot|\.import|\.uid", target):
            return True
    return False


def _command_attempts_protected_write(cmd: str) -> tuple[str, str] | None:
    if _get_task_type() == "project_create":
        return None

    target = _command_targets_protected_path(cmd)
    cd_reason = _cd_enters_protected_tree(cmd)
    if not target and not cd_reason:
        return None
    reason, path_hint = target or (cd_reason, "cd into protected tree")
    lowered = cmd.lower()
    removal_only = bool(target) and not cd_reason and _is_generated_removal(cmd, lowered)

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
    if not removal_only:
        if _write_scope_is_ambiguous(cmd):
            # Indirection could aim a write anywhere: any mutating verb anywhere
            # plus a protected path anywhere is treated as a protected write.
            if any(re.search(pattern, lowered) for pattern in mutating_patterns):
                return reason, path_hint
        else:
            # Plain command list: only a simple command that both mutates and
            # names a protected path counts.  `cp a tests/x.gd && godot -s
            # addons/gut/gut_cmdln.gd` reads addons/ and writes elsewhere.
            for segment in _CMD_SEPARATOR.split(cmd):
                seg_target = _command_targets_protected_path(segment)
                if seg_target and any(re.search(pattern, segment.lower()) for pattern in mutating_patterns):
                    return seg_target

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
