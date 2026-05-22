"""
swarm.runtime_config -- typed agent runtime configuration and helpers.

Extracted from agent_runtime.py. These utilities are self-contained and do not
depend on the full agent_runtime config blob, making them safe to import from
other modules without creating circular dependencies.

All functions read config from agent_runtime at call-time via lazy import to
avoid capturing stale values.

RuntimeConfig
-------------
A typed snapshot of the configuration values that govern a single agent run.
Constructed once via ``build_runtime_config()`` (or directly for tests) and
passed into subsystems instead of having each subsystem re-read module globals.

This is the foundation layer -- subsystems still read module globals today, but
new code should accept a RuntimeConfig where possible so the dependency is
explicit and testable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# RuntimeConfig dataclass
# ---------------------------------------------------------------------------

@dataclass
class RuntimeConfig:
    """Typed snapshot of all configuration values for one agent run.

    Fields mirror the module-level globals in ``swarm.agent_runtime``.
    Defaults match the agent_runtime module defaults so tests that construct
    RuntimeConfig directly work without supplying every field.
    """

    # --- Workspace / project identity ---
    workspace: Path = field(default_factory=lambda: Path("."))
    data_dir: str = "data"
    project: str = ""
    project_path_override: str = ""   # non-empty when running inside a git worktree
    worktree_branch: str = ""         # branch name inside the worktree

    # --- Task identity ---
    task_id: str = "unknown"
    task_type: str = "feature"
    task_desc: str = ""
    task_priority: int = 50
    task_metadata: dict = field(default_factory=dict)

    # --- Limits ---
    max_tool_loops: int = 200
    max_lines: int = 5000
    ignore_dirs: frozenset = field(default_factory=lambda: frozenset({"addons", ".git", ".godot"}))
    ignore_extensions: frozenset = field(default_factory=frozenset)

    # --- LLM provider ---
    llm_provider: str = "minimax"
    llm_providers: dict = field(default_factory=dict)

    # --- Meta-investigation ---
    meta_investigation_enabled: bool = True
    meta_investigation_provider: str = ""

    # --- Managed projects / access control ---
    managed_projects: list = field(default_factory=list)
    readonly: bool = False

    # --- QA / vision ---
    qa_config: dict = field(default_factory=dict)
    qa_cycle: int = 0
    qa_max_cycles: int = 3

    # --- API ---
    api_port: int = 5001

    # --- MCP ---
    mcp_servers: dict = field(default_factory=dict)

    def project_root(self) -> Path:
        """Return the effective project root, honouring project_path_override."""
        if self.project_path_override:
            return Path(self.project_path_override)
        return self.workspace / self.project

    def is_worktree(self) -> bool:
        """Return True when this agent is running inside a git worktree."""
        return bool(self.project_path_override)

    def is_unmanaged(self) -> bool:
        """Return True when this project is outside the managed_projects list.

        An empty managed_projects list means all projects are allowed.
        """
        return bool(self.managed_projects) and self.project not in self.managed_projects


def build_runtime_config() -> RuntimeConfig:
    """Construct a RuntimeConfig snapshot from the current agent_runtime module globals.

    Call this once at the start of ``main()`` to capture the full config. The
    returned object is a value snapshot -- subsequent changes to the module
    globals do NOT propagate into it.
    """
    import swarm.agent_runtime as _rt
    from swarm import constants
    from swarm.provider_utils import LLM_PROVIDERS

    return RuntimeConfig(
        workspace=Path(_rt.WORKSPACE),
        data_dir=_rt.DATA_DIR,
        project=_rt.PROJECT,
        project_path_override=_rt.PROJECT_PATH_OVERRIDE,
        worktree_branch=_rt.WORKTREE_BRANCH,
        task_id=_rt.TASK_ID,
        task_type=_rt.TASK_TYPE,
        task_desc=_rt.TASK_DESC,
        task_priority=_rt.TASK_PRIORITY,
        task_metadata=dict(_rt.TASK_METADATA),
        max_tool_loops=_rt.MAX_TOOL_LOOPS,
        max_lines=_rt.MAX_LINES,
        ignore_dirs=frozenset(_rt.IGNORE_DIRS),
        ignore_extensions=frozenset(_rt.IGNORE_EXTENSIONS),
        llm_provider=_rt.LLM_PROVIDER,
        llm_providers=dict(LLM_PROVIDERS),
        meta_investigation_enabled=_rt.META_INVESTIGATION_ENABLED,
        meta_investigation_provider=_rt.META_INVESTIGATION_PROVIDER,
        managed_projects=list(_rt.MANAGED_PROJECTS),
        readonly=_rt.READONLY,
        qa_config=dict(_rt.QA_CONFIG),
        qa_cycle=_rt.QA_CYCLE,
        qa_max_cycles=_rt.QA_MAX_CYCLES,
        api_port=_rt.API_PORT,
        mcp_servers=dict(_rt.MCP_SERVERS),
    )


def _get_provider_runtime_limits() -> tuple[int, int]:
    """Return (context_window, max_output_tokens) for the active LLM provider."""
    import swarm.agent_runtime as _rt
    from swarm.provider_utils import LLM_PROVIDERS
    cfg = dict(LLM_PROVIDERS.get(_rt.LLM_PROVIDER, LLM_PROVIDERS.get("minimax", {})))
    context_window = int(cfg.get("context_window", 120_000))
    max_output_tokens = int(cfg.get("max_tokens", 8_096))
    return context_window, max_output_tokens


def _get_compaction_threshold() -> int:
    """Return the token count at which conversation compaction should trigger."""
    import swarm.agent_runtime as _rt
    context_window, max_output_tokens = _get_provider_runtime_limits()
    reserve = max(12_000, min(max_output_tokens + 8_000, context_window // 4))
    threshold = context_window - reserve
    if _rt.TASK_TYPE == "project_plan":
        threshold = min(context_window - 20_000, threshold + 12_000)
    return max(60_000, threshold)


def _project_supports_harness() -> bool:
    """Return True if the current project has a test_harness.gd autoload."""
    import swarm.agent_runtime as _rt
    project_root = (
        Path(_rt.PROJECT_PATH_OVERRIDE)
        if _rt.PROJECT_PATH_OVERRIDE
        else (_rt.WORKSPACE / _rt.PROJECT)
    )
    return (project_root / "autoload" / "test_harness.gd").exists()


def _parse_extra_args(raw) -> list:
    """Parse extra_args from tool call — accepts list, JSON string, or plain string."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        txt = raw.strip()
        if not txt:
            return []
        try:
            parsed = json.loads(txt)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [txt]
    return []


def _resolve_harness_action(args: dict) -> dict:
    """Coerce harness action arg to a dict (accepts dict or JSON string)."""
    raw = args.get("action")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        txt = raw.strip()
        if txt.startswith("{"):
            try:
                parsed = json.loads(txt)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        if txt:
            payload = {"type": txt}
            for k, v in args.items():
                if k not in ("action", "timeout"):
                    payload[k] = v
            return payload
    if "type" in args:
        return {k: v for k, v in args.items() if k != "timeout"}
    return {"type": "noop"}
