"""macOS sandbox-exec (seatbelt) containment for agent subprocesses.

Layer 2 of the containment sprint (beads swarm-controller-uwu0).

Layer 1 -- path_guard, shell_guard, the env allowlist, the fetch_url egress
allowlist and api_authz -- lives *inside* the agent's Python process.  It can
only police tools the agent invokes through the dispatcher.  Anything that
reaches a shell by another route, or any child process the agent starts, is
outside its reach.

This module wraps the agent's argv in ``sandbox-exec``, which the kernel
enforces on the agent and every descendant.  The profile lives at
``sandbox/agent.sb`` and is checked into the repo so it reviews like code.

What the profile buys, measured on macOS 15 (Darwin 25.5):

    write ~/.ssh                blocked      write own project      allowed
    write sibling project       blocked      write own worktree     allowed
    read  ~/.ssh                blocked      write data dir         allowed
    read  ~/.config/gh          blocked      write /tmp             allowed
    curl https://example.com    blocked      localhost:5001         allowed
    curl https://api.minimax.io blocked      inference-host:8083    allowed
    write controller checkout   blocked      gitea-host:3000        allowed

Godot runs headless under it (validation passes, StateServer binds on 11009),
git works against the LAN Gitea, and ``import swarm.agent_runtime`` succeeds.

Known limitation, documented at length in the profile: seatbelt's ``remote ip``
accepts only ``localhost:<port>`` or ``*:<port>``, so non-loopback rules are
port-scoped rather than host-scoped.  Ports 80 and 443 stay denied, which is
what removes the broad exfiltration path.  The host-level check remains
in-process in ``swarm/tools/core.py``.

Enable with ``"sandbox_agents": true`` in config.json.  Default is off until
validated against live QA / art_pass / playthrough_bot runs -- flipping it on
before that would turn a containment win into an outage.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

#: Profile path, relative to the controller checkout root.
PROFILE_RELATIVE_PATH = Path("sandbox") / "agent.sb"


def _controller_root() -> Path:
    """Directory of the swarm-controller checkout (parent of this package)."""
    return Path(__file__).resolve().parent.parent


def profile_path() -> Path:
    """Absolute path to the seatbelt profile."""
    return _controller_root() / PROFILE_RELATIVE_PATH


def is_supported() -> bool:
    """True when this host can actually apply the profile.

    macOS only, and only when both sandbox-exec and the profile are present.
    Every other platform silently runs unsandboxed -- callers must treat a
    False here as "Layer 2 unavailable", not as an error.
    """
    if sys.platform != "darwin":
        return False
    if not (os.path.exists(SANDBOX_EXEC) or shutil.which("sandbox-exec")):
        return False
    return profile_path().is_file()


def _real(path) -> str:
    """Resolve symlinks. Seatbelt matches on the real path, so /tmp must
    become /private/tmp or every write rule silently misses."""
    return os.path.realpath(str(path))


_SENTINEL = "/dev/null/no-such-root"


def project_dir_param(
    workspace: os.PathLike | str, project: Optional[str]
) -> str:
    """Resolved write-root for *project*, or a sentinel when there is none.

    Always the project's REAL path (most managed projects are symlinks from
    the workspace into a separate source tree, and seatbelt matches
    resolved paths).  Never the workspace itself or a shared parent: the
    workspace holds every other project, and the parent of a symlinked
    project also holds the controller checkout.  A project that does not
    exist yet (project_create) still resolves to workspace/<name>.
    """
    if not project or os.sep in str(project) or str(project) in (".", ".."):
        return _SENTINEL
    resolved = _real(Path(workspace) / project)
    if resolved in (_real(workspace), _real(Path(workspace).parent), "/"):
        return _SENTINEL
    return resolved


def worktree_dir_param(worktree: Optional[os.PathLike | str]) -> str:
    """Resolved path of this agent's own git worktree, or a sentinel."""
    return _real(worktree) if worktree else _SENTINEL


def remote_dir_param(workspace: os.PathLike | str, project: Optional[str]) -> str:
    """The project's local bare remote (workspace/_remotes/<project>.git)."""
    if not project or os.sep in str(project) or str(project) in (".", ".."):
        return _SENTINEL
    return _real(Path(workspace) / "_remotes" / f"{project}.git")


def build_command(
    argv: Sequence[str],
    *,
    workspace: os.PathLike | str,
    data_dir: os.PathLike | str,
    home_dir: Optional[os.PathLike | str] = None,
    project: Optional[str] = None,
    worktree: Optional[os.PathLike | str] = None,
) -> List[str]:
    """Wrap ``argv`` in sandbox-exec with the agent profile and its parameters.

    Returns a new argv list.  Raises RuntimeError if the sandbox is not
    supported here -- callers check ``is_supported()`` (or use
    ``maybe_wrap``) rather than relying on the exception.
    """
    if not is_supported():
        raise RuntimeError(
            "sandbox-exec containment is not available on this host "
            f"(platform={sys.platform}, profile={profile_path()})"
        )

    home = _real(home_dir or Path.home())
    params = {
        "HOME_DIR": home,
        "WORKSPACE_DIR": _real(workspace),
        "CONTROLLER_DIR": _real(_controller_root()),
        "DATA_DIR": _real(data_dir),
        "TMP_DIR": _real("/tmp"),
        "VAR_FOLDERS": _real(tempfile.gettempdir()),
        "PROJECT_DIR": project_dir_param(workspace, project),
        "WORKTREE_DIR": worktree_dir_param(worktree),
        "REMOTE_DIR": remote_dir_param(workspace, project),
    }

    cmd: List[str] = [SANDBOX_EXEC, "-f", str(profile_path())]
    for key, value in params.items():
        cmd += ["-D", f"{key}={value}"]
    cmd += list(argv)
    return cmd


def enabled(config: Optional[dict]) -> bool:
    """Whether config asks for sandboxing.  Off unless explicitly turned on."""
    if not config:
        return False
    return bool(config.get("sandbox_agents", False))


def maybe_wrap(
    argv: Sequence[str],
    *,
    config: Optional[dict],
    workspace: os.PathLike | str,
    data_dir: os.PathLike | str,
    home_dir: Optional[os.PathLike | str] = None,
    project: Optional[str] = None,
    worktree: Optional[os.PathLike | str] = None,
) -> tuple[List[str], Optional[str]]:
    """Wrap argv if sandboxing is both requested and possible.

    Returns ``(argv, note)``.  ``note`` is None when nothing needed saying,
    otherwise a human-readable line the caller should log: either confirming
    containment is on, or explaining why it is not.  Sandboxing never fails
    the spawn -- an unsandboxed agent with Layer 1 guards is the documented
    fallback, and a silent one would be worse than a loud one.
    """
    if not enabled(config):
        return list(argv), None

    if not is_supported():
        return list(argv), (
            "sandbox_agents is on but sandbox-exec containment is unavailable "
            f"(platform={sys.platform}, profile={profile_path()}); "
            "agent runs with Layer 1 in-process guards only"
        )

    try:
        wrapped = build_command(
            argv,
            workspace=workspace,
            data_dir=data_dir,
            home_dir=home_dir,
            project=project,
            worktree=worktree,
        )
    except Exception as err:  # pragma: no cover - defensive
        return list(argv), (
            f"sandbox-exec wrap failed ({err}); agent runs with Layer 1 guards only"
        )

    return wrapped, "sandbox-exec containment active"
