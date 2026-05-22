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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# ToolContext -- single config object passed into tool sync
# ---------------------------------------------------------------------------

@dataclass
class ToolContext:
    """Typed config snapshot for tool modules.

    Built from a RuntimeConfig (or equivalent) and pushed into each tool
    module via sync_tool_context(). Tool modules keep their module-level
    globals for backward compat; sync_tool_context sets them all at once
    through this single entry point instead of scattered _sync_*_globals().
    """
    workspace: Path = field(default_factory=lambda: Path("."))
    data_dir: str = "data"
    project: str = ""
    project_path_override: str = ""
    worktree_branch: str = ""
    task_id: str = "unknown"
    task_type: str = "feature"
    task_priority: int = 50
    max_lines: int = 5000
    ignore_dirs: set = field(default_factory=lambda: {"addons", ".git", ".godot"})
    ignore_extensions: set = field(default_factory=set)
    max_tool_loops: int = 200
    api_port: int = 5001
    mcp_servers: dict = field(default_factory=dict)
    managed_projects: list = field(default_factory=list)
    readonly: bool = False
    qa_config: dict = field(default_factory=dict)
    qa_cycle: int = 0
    qa_max_cycles: int = 3
    mcp_client: Any = None


def sync_tool_context(ctx: ToolContext) -> None:
    """Push ctx into all tool module globals.

    This is the single entry point that replaces the four _sync_*_globals()
    calls in agent_runtime.  Tool modules keep their module-level vars for
    backward compat; only this function mutates them.
    """
    # Update _shared vars (used by path_guard, shell via _safe_cwd, etc.)
    global TASK_TYPE, WORKSPACE, PROJECT, PROJECT_PATH_OVERRIDE
    TASK_TYPE = ctx.task_type
    WORKSPACE = ctx.workspace
    PROJECT = ctx.project
    PROJECT_PATH_OVERRIDE = ctx.project_path_override

    # Push into each tool module at call-time via lazy imports.
    # (Imports are deferred to avoid circular import issues at module load.)
    try:
        import swarm.tools.core as _core
        _core.WORKSPACE = ctx.workspace
        _core.DATA_DIR = ctx.data_dir
        _core.PROJECT = ctx.project
        _core.PROJECT_PATH_OVERRIDE = ctx.project_path_override
        _core.WORKTREE_BRANCH = ctx.worktree_branch
        _core.TASK_TYPE = ctx.task_type
        _core.TASK_ID = ctx.task_id
        _core.TASK_PRIORITY = ctx.task_priority
        _core.MAX_LINES = ctx.max_lines
        _core.IGNORE_DIRS = ctx.ignore_dirs
        _core.IGNORE_EXTENSIONS = ctx.ignore_extensions
        _core.MAX_TOOL_LOOPS = ctx.max_tool_loops
        _core.API_PORT = ctx.api_port
        _core.MCP_SERVERS = ctx.mcp_servers
        _core.MANAGED_PROJECTS = ctx.managed_projects
        _core.READONLY = ctx.readonly
        _core.mcp_client = ctx.mcp_client
    except ImportError:
        pass

    try:
        import swarm.tools.tasks as _tasks
        _tasks.PROJECT = ctx.project
        _tasks.TASK_TYPE = ctx.task_type
        _tasks.TASK_ID = ctx.task_id
        _tasks.TASK_PRIORITY = ctx.task_priority
        _tasks.API_PORT = ctx.api_port
    except ImportError:
        pass

    try:
        import swarm.tools.knowledge as _knowledge
        _knowledge.WORKSPACE = ctx.workspace
        _knowledge.DATA_DIR = ctx.data_dir
        _knowledge.PROJECT = ctx.project
        _knowledge.PROJECT_PATH_OVERRIDE = ctx.project_path_override
        _knowledge.TASK_ID = ctx.task_id
        _knowledge.API_PORT = ctx.api_port
        _knowledge.READONLY = ctx.readonly
        _knowledge.TASK_TYPE = ctx.task_type
    except ImportError:
        pass

    try:
        from swarm import qa_tools as _qa
        _qa.WORKSPACE = ctx.workspace
        _qa.DATA_DIR = ctx.data_dir
        _qa.PROJECT = ctx.project
        _qa.PROJECT_PATH_OVERRIDE = ctx.project_path_override
        _qa.TASK_TYPE = ctx.task_type
        _qa.API_PORT = ctx.api_port
        _qa.QA_CONFIG = ctx.qa_config
        _qa.QA_CYCLE = ctx.qa_cycle
        _qa.QA_MAX_CYCLES = ctx.qa_max_cycles
        _qa.mcp_client = ctx.mcp_client
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Config vars -- set via sync_tool_context() in agent_runtime before use
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
