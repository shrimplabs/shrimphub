"""swarm.tools._shared -- shared config vars imported by core.py and path_guard.py.

This module exists to break the circular import between core.py (which defines
TASK_TYPE and the full _project_root() with worktree-override logic) and
path_guard.py (which needs both to evaluate protected-path rules).
Both modules import from here; neither imports from the other directly.
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


def _sync_core_globals(TASK_TYPE_VAL, WORKSPACE_VAL, PROJECT_VAL="", PROJECT_PATH_OVERRIDE_VAL=""):
    global TASK_TYPE, WORKSPACE, PROJECT, PROJECT_PATH_OVERRIDE
    TASK_TYPE = TASK_TYPE_VAL
    WORKSPACE = Path(WORKSPACE_VAL)
    PROJECT = PROJECT_VAL
    PROJECT_PATH_OVERRIDE = PROJECT_PATH_OVERRIDE_VAL
