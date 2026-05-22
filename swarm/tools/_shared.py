"""swarm.tools._shared -- shared config vars imported by core.py and path_guard.py.

This module exists to break the circular import between core.py (which defines
TASK_TYPE and the full _project_root() with worktree-override logic) and
path_guard.py (which needs both to evaluate protected-path rules).
Both modules import from here; neither imports from the other directly.

_safe_cwd() lives here too to break the shell.py <-> core.py circular import:
shell.py was doing `import swarm.tools.core as _core` to read _core.WORKSPACE,
but core.py re-exports from shell.py -- creating a module-load circular dependency.
By putting _safe_cwd() in _shared.py (imported by neither), both modules can
read it without any import cycle.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Config vars -- set via _sync_core_globals() in agent_runtime before use
# ---------------------------------------------------------------------------

TASK_TYPE: str = "feature"
WORKSPACE: Path = Path(".")
PROJECT: str = ""
PROJECT_PATH_OVERRIDE: str = ""


def _project_root() -> str:
    """Return the effective project root, honouring PROJECT_PATH_OVERRIDE (worktrees)."""
    if PROJECT_PATH_OVERRIDE:
        return PROJECT_PATH_OVERRIDE
    return str(WORKSPACE / PROJECT)


def _safe_cwd(cwd=None) -> str:
    """Return a valid working directory, falling back to WORKSPACE for virtual projects."""
    if cwd:
        return cwd
    root = _project_root()
    if root and Path(root).is_dir():
        return str(root)
    ws = str(WORKSPACE) if WORKSPACE else None
    if ws and Path(ws).is_dir():
        return ws
    return "."


def _sync_core_globals(TASK_TYPE_VAL, WORKSPACE_VAL, PROJECT_VAL="", PROJECT_PATH_OVERRIDE_VAL=""):
    global TASK_TYPE, WORKSPACE, PROJECT, PROJECT_PATH_OVERRIDE
    TASK_TYPE = TASK_TYPE_VAL
    WORKSPACE = Path(WORKSPACE_VAL)
    PROJECT = PROJECT_VAL
    PROJECT_PATH_OVERRIDE = PROJECT_PATH_OVERRIDE_VAL
