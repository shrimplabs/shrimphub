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

log() and _sanitize_text() also live here so files.py can call them
without triggering the core.py <-> files.py circular import.
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
    import os
    return os.getcwd()


def _sync_core_globals(TASK_TYPE_VAL, WORKSPACE_VAL, PROJECT_VAL="", PROJECT_PATH_OVERRIDE_VAL=""):
    global TASK_TYPE, WORKSPACE, PROJECT, PROJECT_PATH_OVERRIDE
    TASK_TYPE = TASK_TYPE_VAL
    WORKSPACE = Path(WORKSPACE_VAL)
    PROJECT = PROJECT_VAL
    PROJECT_PATH_OVERRIDE = PROJECT_PATH_OVERRIDE_VAL


# Map Windows-1252 "fancy" punctuation → plain ASCII equivalents.
_FANCY_PUNCT_TABLE = str.maketrans({
    "\u2014": "--",  # em-dash → two hyphens
    "\u2013": "-",   # en-dash → hyphen
    "\u201c": '"',   # left double quote → ASCII quote
    "\u201d": '"',   # right double quote → ASCII quote
    "\u2018": "'",   # left single quote → ASCII apostrophe
    "\u2019": "'",   # right single quote → ASCII apostrophe
    "\u2026": "...", # ellipsis → three dots
    "\u00b7": "*",   # middle dot → asterisk
})


def _sanitize_text(content: str) -> str:
    """Replace fancy Unicode punctuation with ASCII equivalents and normalise line endings."""
    return content.translate(_FANCY_PUNCT_TABLE).replace('\r\n', '\n').replace('\r', '\n')


def log(msg: str):
    print(f"[Agent] {msg}", flush=True)
