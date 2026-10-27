"""Controller-side git network operations (fetch / push) for agent projects.

Sandboxed agents cannot push to GitHub themselves: seatbelt can only open
outbound ports, not hosts, so allowing 443 for github.com would open 443 to
the whole internet; and the operator's git credentials (osxkeychain, ~/.ssh)
are deliberately unreadable inside the sandbox.  The controller runs
unsandboxed with the operator's credentials, so pushes happen here instead:

* after an agent's worktree branch is merged into the project
  (agent_finish), and
* when an agent working directly in the project calls git_push / git_commit
  (POST /api/projects/<name>/push, own-project agent token only).

Fetch is the other half: POST /api/projects/<name>/fetch runs ``git fetch
origin`` in the project repo.  Worktrees share the project's refs, so the
agent then sees ``origin/<branch>`` and merges or rebases LOCALLY in its
worktree, resolving any conflicts itself -- reconciliation stays with the
agent, exactly as before the sandbox; only the network hop moved here.

Only the project's current branch is pushed to ``origin``, never with
--force, and never prompting for credentials.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

PUSH_TIMEOUT = 120
# Fetches can pull large packs (art assets); a 4.5k-object pack took >2 min.
FETCH_TIMEOUT = 900


def push_project(project_path: os.PathLike | str, timeout: int = PUSH_TIMEOUT) -> dict:
    """Push the current branch of ``project_path`` to origin.  Never raises."""
    path = Path(project_path)
    if not (path / ".git").exists():
        return {"ok": False, "skipped": True, "error": f"{path} is not a git repository"}
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/usr/bin/false"}

    def _git(*args, t=30):
        return subprocess.run(["git", *args], cwd=str(path), capture_output=True,
                              text=True, timeout=t, env=env)

    try:
        if "origin" not in _git("remote").stdout.split():
            return {"ok": False, "skipped": True, "error": "no origin remote"}
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if not branch or branch == "HEAD":
            return {"ok": False, "error": "detached HEAD; nothing to push"}
        r = _git("push", "origin", f"HEAD:refs/heads/{branch}", t=timeout)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            return {"ok": False, "branch": branch, "error": out[-2000:] or f"git push exited {r.returncode}"}
        return {"ok": True, "branch": branch, "output": out[-2000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"git push timed out after {timeout}s"}
    except Exception as exc:
        return {"ok": False, "error": f"git push failed: {exc}"}


def _git_env() -> dict:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/usr/bin/false"}


def fetch_project(project_path: os.PathLike | str, timeout: int = FETCH_TIMEOUT) -> dict:
    """``git fetch origin --prune`` in the project repo.  Never raises.

    Returns the default remote branch and, for the project's current branch,
    how far it is ahead of / behind that branch, so callers can decide
    whether a merge is needed."""
    path = Path(project_path)
    if not (path / ".git").exists():
        return {"ok": False, "skipped": True, "error": f"{path} is not a git repository"}
    env = _git_env()

    def _git(*args, t=30):
        return subprocess.run(["git", *args], cwd=str(path), capture_output=True,
                              text=True, timeout=t, env=env)

    try:
        if "origin" not in _git("remote").stdout.split():
            return {"ok": False, "skipped": True, "error": "no origin remote"}
        r = _git("fetch", "--prune", "origin", t=timeout)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stdout + r.stderr).strip()[-2000:] or "git fetch failed"}
        head = _git("symbolic-ref", "--short", "refs/remotes/origin/HEAD").stdout.strip()
        default = head.split("/", 1)[1] if head.startswith("origin/") else ""
        if not default:
            for cand in ("main", "master"):
                if _git("rev-parse", "--verify", "-q", f"refs/remotes/origin/{cand}").returncode == 0:
                    default = cand
                    break
        out = {"ok": True, "default_branch": default or "main"}
        if default:
            counts = _git("rev-list", "--left-right", "--count", f"HEAD...origin/{default}").stdout.split()
            if len(counts) == 2:
                out["ahead"], out["behind"] = int(counts[0]), int(counts[1])
        return out
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"git fetch timed out after {timeout}s"}
    except Exception as exc:
        return {"ok": False, "error": f"git fetch failed: {exc}"}


def is_rejected_push(result: dict) -> bool:
    """True when a push failed because origin has commits we don't."""
    err = (result or {}).get("error", "") or ""
    return any(k in err for k in ("[rejected]", "non-fast-forward", "fetch first", "Updates were rejected"))
