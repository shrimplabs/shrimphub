"""
swarm.tool_dispatch -- tool call validation, authority gating, and dispatch.

Extracted from agent_runtime.py. Reads runtime config from agent_runtime at
call-time via lazy import to avoid capturing stale values.

## Tool registry

Tools are declared as ``ToolSpec`` instances in ``_TOOL_REGISTRY``.  Each spec
bundles the tool's name, handler, required args, and availability policy
(QA-only, harness-only, Godot-only, aliases).  ``execute_tool`` and
``validate_tool_call`` both read from the registry so the three concerns —
validation, availability, and dispatch — stay in one place.

Adding a new tool: append a ``ToolSpec`` to ``_TOOL_REGISTRY``.  No changes
to ``_TOOL_REQUIRED_ARGS`` or the conditional dispatch blocks are needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Set


# ---------------------------------------------------------------------------
# ToolSpec -- declarative tool descriptor
# ---------------------------------------------------------------------------

@dataclass
class ToolSpec:
    """Metadata and dispatch binding for a single agent tool.

    Attributes:
        name:         Canonical tool name used in [TOOL_CALL] blocks.
        handler:      Callable(args: dict) -> dict.  Receives the full args
                      dict from the tool call and returns the result dict.
        required_args: Arg names that must be present and non-empty.
        task_types:   If non-empty, the tool is only available when
                      TASK_TYPE is one of these values.
        godot_only:   Available only when project.godot exists in the
                      project root (and READONLY is False).
        aliases:      Alternative names that map to this spec (e.g. model
                      hallucinations from other prompt variants).
    """
    name: str
    handler: Callable
    required_args: list = field(default_factory=list)
    task_types: Set[str] = field(default_factory=set)   # empty = all
    godot_only: bool = False
    aliases: list = field(default_factory=list)

# Tool function imports — these are safe at module level (no circular imports)
from swarm.tools.core import (
    log, _project_root, _safe_cwd, run,
    run_command, run_python, git_commit, git_push,
    mcp_call_tool, mcp_list_tools,
    rag_query, web_search, fetch_url,
    broadcast_read, broadcast_write, delegate_helper,
)
from swarm.tools.files import (
    read_file, list_files, search_code, get_file_stats, get_file_outline,
    read_file_range, patch_file, write_file, append_file,
)
from swarm.tools.tasks import (
    create_subtask, create_task, create_tasks_file_aware, create_tasks, delegate_task_batch,
    list_tasks, list_subtasks,
    annotate_downstream_tasks, split_task, prune_task, cancel_task, insert_dependency, set_task_complexity,
)
from swarm.tools.knowledge import (
    scratchpad_write, scratchpad_read,
    read_agent_knowledge, update_knowledge,
    update_validation_state, read_validation_state,
    get_task_context, read_shared_knowledge, update_shared_knowledge,
)
from swarm.qa_tools import (
    qa_focus_game, qa_position_window, qa_get_window_bounds,
    launch_game as qa_launch_game,
    launch_game_headless,
    take_screenshot as qa_take_screenshot,
    screenshot_burst as qa_screenshot_burst,
    click_at as qa_click_at,
    click_element as qa_click_element,
    qa_key_press, qa_press_button, qa_wait,
    qa_wait_for_idle, qa_poll_until, qa_wait_until, qa_run_sequence,
    key_hold as qa_key_hold,
    play_macro as qa_play_macro,
    mouse_drag as qa_mouse_drag,
    right_click as qa_right_click,
    double_click as qa_double_click,
    scroll as qa_scroll,
    key_combo as qa_key_combo,
    kill_game as qa_kill_game,
    qa_create_bug_task, qa_requeue_self,
    get_game_state as qa_get_game_state,
    vision_query as qa_vision_query,
    harness_launch_game, harness_step, harness_take_screenshot,
    harness_kill_game,
    harness_poll_state, harness_inject,
)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
# Handlers are defined as module-level lambdas/functions that take an ``args``
# dict.  They are collected into _TOOL_REGISTRY below the imports.
# All three concerns — required-arg validation, availability policy, and
# dispatch — are read from a single ToolSpec, not from separate tables.
#
# _TOOL_REQUIRED_ARGS is kept as a backward-compatible derived view so
# external code that reads it directly (e.g. tests) keeps working.

_TOOL_REGISTRY: list[ToolSpec] = []   # populated at bottom of module

# Backward-compat view: derived from _TOOL_REGISTRY after it is populated
_TOOL_REQUIRED_ARGS: dict = {}        # updated by _build_required_args_index()

def _build_required_args_index():
    """Rebuild _TOOL_REQUIRED_ARGS from the current _TOOL_REGISTRY."""
    global _TOOL_REQUIRED_ARGS
    _TOOL_REQUIRED_ARGS = {spec.name: spec.required_args for spec in _TOOL_REGISTRY}

def _registry_by_name() -> dict[str, ToolSpec]:
    """Return a name→ToolSpec lookup (canonical names + aliases)."""
    out: dict[str, ToolSpec] = {}
    for spec in _TOOL_REGISTRY:
        out[spec.name] = spec
        for alias in spec.aliases:
            out[alias] = spec
    return out


# ---------------------------------------------------------------------------
# Tool call validation
# ---------------------------------------------------------------------------

def _normalize_tool_call(tool_call: dict) -> dict:
    """Normalize common M3 arg key variants in-place and return the dict."""
    args = tool_call.get("args")
    if isinstance(args, dict):
        # M3 uses "cmd" instead of "command" for run_command
        if "cmd" in args and "command" not in args:
            args["command"] = args.pop("cmd")
    return tool_call


def validate_tool_call(tool_call: dict) -> str:
    """Return an error string if the tool call is malformed, else empty string."""
    tool_call = _normalize_tool_call(tool_call)
    tool = tool_call.get("tool", "")
    args = tool_call.get("args") or {}

    if not tool:
        return "Tool call is missing the 'tool' field."

    if not isinstance(args, dict):
        return f"Tool '{tool}': 'args' must be a JSON object, got {type(args).__name__}."

    # Resolve alias then look up required args from the registry
    reg = _registry_by_name()
    canonical = tool
    if tool in reg:
        canonical = reg[tool].name
    required = reg[canonical].required_args if canonical in reg else _TOOL_REQUIRED_ARGS.get(tool, [])
    missing = [k for k in required if not args.get(k) and args.get(k) != 0]
    if missing:
        example_args = {k: f"<{k}>" for k in required}
        return (
            f"Tool '{tool}' is missing required arg(s): {missing}. "
            f"Correct format: "
            f'[TOOL_CALL]{{"tool": "{tool}", "args": {json.dumps(example_args)}}}[/TOOL_CALL]'
        )

    return ""


# ---------------------------------------------------------------------------
# Authority gating
# ---------------------------------------------------------------------------

def _task_tool_authority_error(tool: str, reason: str) -> dict:
    import swarm.agent_runtime as _rt
    return {"ok": False, "error": f"{_rt.TASK_TYPE} task cannot use {tool}: {reason}"}


def _tool_authority_denial(tool: str, args: dict) -> dict | None:
    import swarm.agent_runtime as _rt
    from swarm.runtime_helpers import _normalized_report_path, _has_active_sibling_tasks

    task_type = _rt.TASK_TYPE
    task_metadata = _rt.TASK_METADATA
    run_broadcast_write_count = _rt.RUN_BROADCAST_WRITE_COUNT

    mutating_tools = {"write_file", "patch_file", "append_file", "git_commit", "git_push"}
    # plan/python_plan also block run_command — agents can bypass write restrictions via shell
    plan_blocked = mutating_tools | {"run_command"}
    if task_type in ("plan", "python_plan") and tool in plan_blocked:
        return _task_tool_authority_error(tool, "planning tasks are read-only")

    if tool == "delegate_helper" and task_type not in {"feature", "bug", "refactor", "polish", "audit", "research", "triage"}:
        return _task_tool_authority_error(
            tool,
            "this task type does not support transient helper delegation",
        )

    if tool == "delegate_task_batch" and task_type not in {"feature", "bug", "refactor", "polish"}:
        return _task_tool_authority_error(
            tool,
            "this task type does not support structured child-task delegation",
        )
    if tool == "delegate_task_batch" and task_metadata.get("delegation_batch_id"):
        return _task_tool_authority_error(
            tool,
            "nested structured child-task delegation is disabled in the initial rollout",
        )

    if task_type == "project_plan" and tool in (mutating_tools | {"run_command", "create_task", "create_tasks"}):
        return _task_tool_authority_error(
            tool,
            "project planners must inspect the repo and delegate through create_tasks_file_aware() only",
        )

    if task_type == "scenario_qa":
        if tool in {"patch_file", "append_file", "git_commit", "git_push", "run_command", "create_task", "create_tasks_file_aware"}:
            return _task_tool_authority_error(
                tool,
                "scenario_qa tasks are read-only; use write_scenario to save scenario JSON files",
            )
        if tool == "write_file":
            report_path = _normalized_report_path(args.get("path", ""))
            if not report_path.startswith("test/scenarios/"):
                return _task_tool_authority_error(tool, "scenario_qa may only write files under test/scenarios/")

    if task_type in ("qa", "hybrid_qa", "harness_qa"):
        if tool in {"patch_file", "append_file", "git_commit", "git_push", "run_command", "create_task", "create_tasks_file_aware"}:
            return _task_tool_authority_error(
                tool,
                "QA tasks are read-only testers; file follow-ups should go through create_bug_task/requeue_self",
            )
        if tool == "write_file":
            report_path = _normalized_report_path(args.get("path", ""))
            if report_path != "QA_REPORT.md":
                return _task_tool_authority_error(tool, "QA tasks may only write QA_REPORT.md")

    if task_type == "triage":
        if tool in {"patch_file", "append_file", "git_commit", "git_push", "create_task", "create_tasks_file_aware"}:
            return _task_tool_authority_error(
                tool,
                "triage tasks are read-only except for bug filing and the triage report",
            )
        if tool == "write_file":
            report_path = _normalized_report_path(args.get("path", ""))
            if report_path != "TRIAGE_REPORT.md":
                return _task_tool_authority_error(tool, "triage may only write TRIAGE_REPORT.md")

    if task_type == "research":
        if tool in {"patch_file", "append_file", "git_push"}:
            return _task_tool_authority_error(tool, "research tasks may record findings but must not implement code changes")
        if tool == "write_file":
            report_path = _normalized_report_path(args.get("path", ""))
            if not report_path.startswith("research/") or not report_path.endswith(".md"):
                return _task_tool_authority_error(tool, "research findings must be written under research/*.md")

    if task_type == "audit" and tool in mutating_tools:
        return _task_tool_authority_error(tool, "audit tasks may inspect and use API calls, but must not edit repo files or commit")

    if task_metadata.get("is_recovery_task") and tool in {"create_task", "create_tasks_file_aware"}:
        return _task_tool_authority_error(
            tool,
            "recovery tasks must repair the branch directly or fail into canonical continuation; they cannot spawn arbitrary child work",
        )

    # Plugin permission enforcement (profile + tools_blocked + allowlist)
    try:
        from swarm.plugins import get_plugin, is_tool_blocked_by_plugin
        _plugin = get_plugin(task_type)
        if _plugin and is_tool_blocked_by_plugin(_plugin, tool):
            return _task_tool_authority_error(
                tool,
                f"blocked by plugin '{_plugin.plugin_id}' "
                f"(profile={_plugin.permission_profile})",
            )
    except Exception:
        pass

    if (
        tool in {"write_file", "patch_file", "append_file"}
        and task_type in {"feature", "bug", "refactor", "polish"}
        and run_broadcast_write_count <= 0
        and _has_active_sibling_tasks()
    ):
        path = _normalized_report_path(args.get("path", ""))
        return _task_tool_authority_error(
            tool,
            "active sibling tasks are running on this project; before your first edit, call "
            f"broadcast_write() with a one-line shared-file claim for '{path or 'the files you will touch'}'",
        )

    return None


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def execute_tool(tool_call: dict) -> dict:
    import swarm.agent_runtime as _rt
    from swarm.runtime_helpers import _normalized_project_file_path, _lock_project_file, _spawn_lock_conflict_handoff
    from swarm.runtime_config import _parse_extra_args, _resolve_harness_action, _project_supports_harness

    tool_call = _normalize_tool_call(tool_call)
    tool = tool_call.get("tool", "")
    args = tool_call.get("args", {})
    log(f"Executing tool: {tool}")

    denied = _tool_authority_denial(tool, args)
    if denied:
        return denied

    validation_error = validate_tool_call(tool_call)
    if validation_error:
        return {"ok": False, "error": f"Tool call validation failed: {validation_error}"}

    if _rt.TASK_TYPE == "project_plan" and tool in {"create_task", "create_tasks"}:
        return {
            "ok": False,
            "error": (
                "project_plan must use create_tasks_file_aware() once with the full task list. "
                "Do not use create_task() or create_tasks() for project planning."
            ),
        }

    if tool in {"write_file", "patch_file", "append_file"}:
        rel_path = _normalized_project_file_path(args.get("path", ""))
        if rel_path and rel_path not in _rt.CLAIMED_FILE_PATHS:
            lock_result = _lock_project_file(args.get("path", ""))
            if not lock_result.get("ok"):
                owner = lock_result.get("task_id") or lock_result.get("locked_by")
                owner_text = f" by {owner}" if owner else ""
                handoff = {}
                if owner:
                    try:
                        handoff = _spawn_lock_conflict_handoff(rel_path, owner)
                    except Exception as exc:
                        log(f"WARNING: failed to create lock conflict handoff for {_rt.TASK_ID}: {exc}")
                return {
                    "ok": False,
                    "error": (
                        f"Cannot edit '{rel_path}': it is currently locked{owner_text}. "
                        "Do not touch this file. Re-evaluate the task, validate whether the sibling already satisfied the requirement, "
                        "or finish with a handoff/broadcast instead of overlapping edits."
                    ),
                    "lock_conflict_handoff_created": bool(handoff.get("ok") and handoff.get("followup_task_id")),
                    "followup_task_id": handoff.get("followup_task_id"),
                    "blocked_by_task_id": owner,
                    "reparented_dependents": handoff.get("reparented_dependents", []),
                }

    workspace = _rt.WORKSPACE
    project = _rt.PROJECT

    # Build the active dispatch table from the registry.
    # The registry is static; availability filtering (Godot, QA, harness) is
    # done here so execute_tool always reflects the current runtime state.
    dispatch: dict[str, Callable] = {}
    dispatch_canonical: dict[str, str] = {}
    alias_names: set[str] = set()

    _godot_project_file = (
        Path(_rt.PROJECT_PATH_OVERRIDE) if _rt.PROJECT_PATH_OVERRIDE else (workspace / project)
    ) / "project.godot"
    _godot_available = _godot_project_file.exists() and not _rt.READONLY
    _qa_types = {"qa", "art_pass", "hybrid_qa"}
    _harness_types = {"harness_qa"}
    _harness_available = (
        _rt.TASK_TYPE in _harness_types
        or (_rt.TASK_TYPE == "hybrid_qa" and _project_supports_harness())
    )

    available_specs: list[ToolSpec] = []
    for spec in _TOOL_REGISTRY:
        # Skip tools restricted to specific task types
        if spec.task_types and _rt.TASK_TYPE not in spec.task_types:
            continue
        # Skip Godot-only tools when project.godot is absent
        if spec.godot_only and not _godot_available:
            continue
        available_specs.append(spec)
        # Handler is a factory: call with (args, workspace, project) context
        dispatch[spec.name] = lambda s=spec: s.handler(args, workspace, project)
        dispatch_canonical[spec.name] = spec.name

    # Register aliases only for specs available to this task type. This keeps
    # harness-only aliases from shadowing same-named QA tools in normal QA runs.
    for spec in available_specs:
        for alias in spec.aliases:
            if alias not in dispatch:
                dispatch[alias] = lambda s=spec: s.handler(args, workspace, project)
                dispatch_canonical[alias] = spec.name
                alias_names.add(alias)

    canonical_tool = dispatch_canonical.get(tool, tool)
    if tool in alias_names and canonical_tool != tool:
        log(f"Tool alias: {tool} → {canonical_tool}")

    fn = dispatch.get(tool)
    if fn:
        result = fn()
        if canonical_tool == "broadcast_write" and isinstance(result, dict) and result.get("ok", True) is not False:
            _rt.RUN_BROADCAST_WRITE_COUNT += 1
        return result
    return {
        "ok": False,
        "error": f"Unknown tool: {tool}. Valid tools: {', '.join(k for k in dispatch if k not in alias_names)}",
    }


# ---------------------------------------------------------------------------
# Tool registry population
# ---------------------------------------------------------------------------
# Handlers are module-level functions taking (args, workspace, project).
# Availability (task_types, godot_only) is declared on the ToolSpec.

_QA_TYPES = {"qa", "art_pass", "hybrid_qa", "polish", "scenario_qa"}
_HARNESS_TYPES = {"harness_qa", "hybrid_qa"}
_SCENARIO_QA_TYPES = {"scenario_qa"}


def _write_scenario_file(a: dict, workspace: Path, project: str) -> dict:
    """Write a scenario JSON dict to test/scenarios/<path> in the project directory."""
    import json as _json
    import swarm.agent_runtime as _rt
    from pathlib import Path as _Path
    rel_path = a.get("path", "")
    scenario = a.get("scenario")
    if not rel_path:
        return {"ok": False, "error": "write_scenario requires 'path'"}
    if not isinstance(scenario, dict):
        return {"ok": False, "error": "write_scenario requires 'scenario' to be a dict"}
    proj_path = _Path(_rt.PROJECT_PATH_OVERRIDE) if _rt.PROJECT_PATH_OVERRIDE else (workspace / project)
    abs_path = proj_path / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(_json.dumps(scenario, indent=2))
    return {"ok": True, "path": str(abs_path)}


def _populate_registry():
    """Build _TOOL_REGISTRY from handler functions.  Called once at module load."""
    _TOOL_REGISTRY.clear()

    def _reg(name, handler, required_args=None, task_types=(), godot_only=False, aliases=()):
        _TOOL_REGISTRY.append(ToolSpec(
            name=name,
            handler=handler,
            required_args=list(required_args or []),
            task_types=set(task_types),
            godot_only=godot_only,
            aliases=list(aliases),
        ))

    # --- File tools ---
    _reg("read_file",       lambda a, ws, p: read_file(a.get("path", ""), a.get("offset", 0), a.get("limit", 0)),         ["path"])
    _reg("list_files",      lambda a, ws, p: list_files(a.get("path", ".")))
    _reg("search_code",     lambda a, ws, p: search_code(a.get("query", "")),                                              ["query"])
    _reg("get_file_stats",  lambda a, ws, p: get_file_stats(a.get("path", ".")),                                           ["path"])
    _reg("get_file_outline",lambda a, ws, p: get_file_outline(a.get("path", "")),                                          ["path"])
    _reg("read_file_range", lambda a, ws, p: read_file_range(a.get("path", ""), a.get("start_line", 1), a.get("end_line", 100)), ["path", "start_line", "end_line"])
    _reg("patch_file",      lambda a, ws, p: patch_file(a.get("path", ""), a.get("old", ""), a.get("new", "")),            ["path", "old", "new"])
    _reg("write_file",      lambda a, ws, p: write_file(a.get("path", ""), a.get("content", "")),                          ["path", "content"])
    _reg("append_file",     lambda a, ws, p: append_file(a.get("path", ""), a.get("content", "")),                         ["path", "content"])

    # --- Shell / git ---
    _reg("run_command",     lambda a, ws, p: run_command(a.get("command", ""), a.get("timeout", 60)),                       ["command"])
    _reg("run_python",      lambda a, ws, p: run_python(a.get("code", ""), a.get("timeout", 30)),                           ["code"])
    _reg("git_commit",      lambda a, ws, p: git_commit(a.get("message", "Agent commit"), a.get("files")))
    _reg("git_push",        lambda a, ws, p: git_push())

    # --- Web / network ---
    _reg("web_search",      lambda a, ws, p: web_search(a.get("query", ""), a.get("max_results", 3)),                       ["query"])
    _reg("fetch_url",       lambda a, ws, p: fetch_url(a.get("url", ""), a.get("extract_text", True)),                      ["url"])
    _reg("rag_query",       lambda a, ws, p: rag_query(a.get("question", ""), a.get("top_k", 5)),                           ["question"])

    # --- MCP ---
    _reg("mcp_call_tool",   lambda a, ws, p: mcp_call_tool(a.get("server", ""), a.get("tool", ""), a.get("args", {})),     ["server", "tool"])
    _reg("mcp_list_tools",  lambda a, ws, p: mcp_list_tools(a.get("server", "")))

    # --- Task tools ---
    _reg("create_subtask",  lambda a, ws, p: create_subtask(a.get("description", ""), a.get("type", "feature"), a.get("priority", 50), a.get("files_touched"), a.get("depends_on_current", True), a.get("max_depth", 2), a.get("project"), a.get("metadata")), ["description"])
    _reg("create_task",     lambda a, ws, p: create_task(a.get("description", ""), a.get("type", "feature"), a.get("priority", 50), a.get("dependencies", []), a.get("project"), a.get("parent_task_id"), a.get("metadata")), ["description"])
    _reg("create_tasks",   lambda a, ws, p: create_tasks(a.get("tasks", []), a.get("project")), ["tasks"])
    _reg("create_tasks_file_aware", lambda a, ws, p: create_tasks_file_aware(a.get("tasks", []), a.get("project")),         ["tasks"])
    _reg("list_tasks",      lambda a, ws, p: list_tasks(a.get("project")))
    _reg("list_subtasks",   lambda a, ws, p: list_subtasks(a.get("parent_task_id")))
    _reg("annotate_downstream_tasks", lambda a, ws, p: annotate_downstream_tasks(a.get("findings", ""), a.get("task_ids")), ["findings"])
    _reg("split_task",      lambda a, ws, p: split_task(a.get("task_id", ""), a.get("replacement_tasks", [])),             ["task_id", "replacement_tasks"])
    _reg("prune_task",      lambda a, ws, p: prune_task(a.get("task_id", ""), a.get("reason", "")),                        ["task_id", "reason"])
    _reg("cancel_task",     lambda a, ws, p: cancel_task(a.get("task_id", ""), a.get("reason", "")) if _rt.TASK_TYPE == "pruner" else {"ok": False, "error": "cancel_task is only available to the pruner agent"}, ["task_id", "reason"])
    _reg("insert_dependency", lambda a, ws, p: insert_dependency(a.get("from_task_id", ""), a.get("to_task_id", "")),      ["from_task_id", "to_task_id"])
    _reg("set_task_complexity", lambda a, ws, p: set_task_complexity(a.get("task_id", ""), a.get("complexity", ""), a.get("reason", "")), ["task_id", "complexity"])
    _reg("delegate_task_batch", lambda a, ws, p: delegate_task_batch(a.get("children", []), a.get("mode", "integrate"), a.get("project")), ["children"])

    # --- Knowledge / scratchpad ---
    _reg("scratchpad_write",    lambda a, ws, p: scratchpad_write(a.get("type", "note"), a.get("content", ""), a.get("files"), a.get("key")))
    _reg("scratchpad_read",     lambda a, ws, p: scratchpad_read(a.get("files"), a.get("type"), a.get("key")))
    _reg("update_knowledge",         lambda a, ws, p: update_knowledge(a.get("content", "")),         ["content"])
    _reg("update_validation_state",  lambda a, ws, p: update_validation_state(a.get("content", "")), ["content"])
    _reg("read_agent_knowledge",     lambda a, ws, p: read_agent_knowledge())
    _reg("get_task_context",    lambda a, ws, p: get_task_context())
    _reg("read_shared_knowledge",   lambda a, ws, p: read_shared_knowledge(a.get("topic", "")))
    _reg("update_shared_knowledge", lambda a, ws, p: update_shared_knowledge(a.get("content", ""), a.get("topic", "")),   ["content"])

    # --- Broadcast ---
    _reg("broadcast_read",  lambda a, ws, p: broadcast_read(a.get("tail", 50)))
    _reg("broadcast_write", lambda a, ws, p: broadcast_write(a.get("message", "")),                                        ["message"])
    _reg("delegate_helper", lambda a, ws, p: delegate_helper(a.get("question", ""), a.get("files", []), a.get("scope", ""), a.get("max_chars", 12000)), ["question"])

    # --- Godot game verification (all non-readonly Godot projects) ---
    _reg("launch_game",   lambda a, ws, p: launch_game_headless(a.get("project_path", str(ws / p))),   godot_only=True)
    _reg("get_game_state",lambda a, ws, p: qa_get_game_state(command=a.get("command", "state")),         godot_only=True)
    _reg("wait",          lambda a, ws, p: qa_wait(a.get("seconds", 1)),                                 godot_only=True)
    _reg("kill_game",     lambda a, ws, p: qa_kill_game(),                                               godot_only=True)

    # --- QA-only tools ---
    # launch_game override for QA: hybrid_qa may use harness_launch_game when
    # extra_args are present and the project supports a harness.
    def _qa_launch_game(a, ws, p):
        from swarm.runtime_config import _parse_extra_args, _project_supports_harness
        import swarm.agent_runtime as _rt
        extra = _parse_extra_args(a.get("extra_args"))
        if _rt.TASK_TYPE == "hybrid_qa" and _project_supports_harness() and extra:
            return harness_launch_game(a.get("project_path", str(ws / p)), extra)
        return qa_launch_game(a.get("project_path", str(ws / p)))
    _reg("launch_game",     _qa_launch_game,                                                             task_types=_QA_TYPES)
    _reg("focus_game",      lambda a, ws, p: qa_focus_game(),                                            task_types=_QA_TYPES)
    _reg("position_window", lambda a, ws, p: qa_position_window(),                                       task_types=_QA_TYPES)
    _reg("get_window_bounds",lambda a, ws, p: qa_get_window_bounds(a.get("process_name", "Godot_4")),    task_types=_QA_TYPES)
    _reg("take_screenshot",   lambda a, ws, p: qa_take_screenshot(a.get("filename", "/tmp/qa_screenshot.png")), task_types=_QA_TYPES)
    _reg("screenshot_burst",  lambda a, ws, p: qa_screenshot_burst(a.get("filename_prefix", "burst"), a.get("count", 4), a.get("interval", 0.25)), task_types=_QA_TYPES)
    _reg("click_at",          lambda a, ws, p: qa_click_at(a.get("x", 0), a.get("y", 0)),                  task_types=_QA_TYPES)
    _reg("click_element",     lambda a, ws, p: qa_click_element(a.get("image_path", ""), a.get("element_description", "")), task_types=_QA_TYPES)
    _reg("key_press",         lambda a, ws, p: qa_key_press(a.get("key", "")),                             task_types=_QA_TYPES)
    _reg("key_hold",          lambda a, ws, p: qa_key_hold(a.get("key", ""), a.get("duration", 1.0)),      task_types=_QA_TYPES)
    _reg("key_combo",         lambda a, ws, p: qa_key_combo(a.get("keys", [])),                            task_types=_QA_TYPES)
    _reg("play_macro",        lambda a, ws, p: qa_play_macro(a.get("actions", [])),                        task_types=_QA_TYPES)
    _reg("mouse_drag",        lambda a, ws, p: qa_mouse_drag(a.get("x1", 0), a.get("y1", 0), a.get("x2", 0), a.get("y2", 0), a.get("duration", 0.3)), task_types=_QA_TYPES)
    _reg("right_click",       lambda a, ws, p: qa_right_click(a.get("x", 0), a.get("y", 0)),              task_types=_QA_TYPES)
    _reg("double_click",      lambda a, ws, p: qa_double_click(a.get("x", 0), a.get("y", 0)),             task_types=_QA_TYPES)
    _reg("scroll",            lambda a, ws, p: qa_scroll(a.get("x", 0), a.get("y", 0), a.get("delta", 3.0)), task_types=_QA_TYPES)
    _reg("press_button",      lambda a, ws, p: qa_press_button(a.get("text", "")),                         task_types=_QA_TYPES)
    _reg("vision_query",      lambda a, ws, p: qa_vision_query(a.get("image_path", ""), a.get("question", ""), a.get("model", "fast")), task_types=_QA_TYPES)
    _reg("wait_for_idle",     lambda a, ws, p: qa_wait_for_idle(a.get("timeout", 10.0), a.get("interval", 0.5)), task_types=_QA_TYPES)
    _reg("poll_until",        lambda a, ws, p: qa_poll_until(a.get("condition_key", ""), a.get("condition_value"), a.get("timeout", 10.0), a.get("interval", 0.1), a.get("negate", False)), task_types=_QA_TYPES)
    _reg("wait_until",        lambda a, ws, p: qa_wait_until(a.get("state_key", ""), a.get("target_value"), a.get("timeout", 10.0), a.get("interval", 0.1)), task_types=_QA_TYPES)
    _reg("run_sequence",      lambda a, ws, p: qa_run_sequence(a.get("actions", [])),                      task_types=_QA_TYPES)
    _reg("create_bug_task", lambda a, ws, p: qa_create_bug_task(a.get("description", ""), a.get("evidence_path", ""), a.get("priority", 80), a.get("dependencies", None)), task_types=_QA_TYPES)
    _reg("requeue_self",    lambda a, ws, p: qa_requeue_self(a.get("bug_task_ids", [])),                  task_types=_QA_TYPES)

    # --- Harness QA tools ---
    def _harness_launch_game(a, ws, p):
        from swarm.runtime_config import _parse_extra_args
        return harness_launch_game(str(ws / p), _parse_extra_args(a.get("extra_args")))

    def _harness_step(a, ws, p):
        from swarm.runtime_config import _resolve_harness_action
        return harness_step(_resolve_harness_action(a), int(a.get("timeout", 30)))

    _reg("harness_launch_game",    _harness_launch_game,                                                                    task_types=_HARNESS_TYPES)
    _reg("harness_step",           _harness_step,                                                                           task_types=_HARNESS_TYPES)
    _reg("harness_kill_game",      lambda a, ws, p: harness_kill_game(),                                                      task_types=_HARNESS_TYPES)
    _reg("harness_poll_state",     lambda a, ws, p: harness_poll_state(int(a.get("timeout", 5))),                             task_types=_HARNESS_TYPES)
    _reg("harness_take_screenshot",lambda a, ws, p: harness_take_screenshot(a.get("filename", f"data/harness_screenshot_{int(__import__('time').time())}.png")), task_types=_HARNESS_TYPES)
    _reg("harness_inject",         lambda a, ws, p: harness_inject(
        a.get("command") if isinstance(a.get("command"), dict)
        else (__import__('json').loads(a["command"]) if isinstance(a.get("command"), str) and a["command"].strip().startswith("{")
              else {k: v for k, v in a.items() if k not in ("timeout",)} if "command" in a
              else a),
        int(a.get("timeout", 5)),
    ), task_types=_HARNESS_TYPES)
    # create_bug_task + requeue_self also available in harness_qa
    _reg("create_bug_task_harness", lambda a, ws, p: qa_create_bug_task(a.get("description", ""), a.get("evidence_path", ""), a.get("priority", 80), a.get("dependencies", None)), task_types={"harness_qa"}, aliases=["create_bug_task"])
    _reg("requeue_self_harness",    lambda a, ws, p: qa_requeue_self(a.get("bug_task_ids", [])),                              task_types={"harness_qa"}, aliases=["requeue_self"])

    # --- Scenario QA tools ---
    def _run_scenario(a, ws, p):
        """Execute a scenario JSON file via ScenarioExecutor. Returns pass/fail + trace path."""
        import os
        import subprocess
        import sys
        scenario_path = a.get("scenario_path", "")
        project_path_arg = a.get("project_path", "")
        swarm_url = a.get("swarm_url", "http://localhost:5001")
        out_dir = a.get("out_dir", "/tmp/scenario_qa")
        if not scenario_path:
            return {"ok": False, "error": "run_scenario requires 'scenario_path'"}
        # Resolve relative to project path
        import swarm.agent_runtime as _rt
        _proj_path = project_path_arg or str(ws / p)
        scenario_abs = os.path.join(_proj_path, scenario_path) if not os.path.isabs(scenario_path) else scenario_path
        if not os.path.exists(scenario_abs):
            return {"ok": False, "error": f"scenario file not found: {scenario_abs}"}
        try:
            from swarm.tools.scenario_qa import ScenarioExecutor, StateServerClient
            import json, tempfile
            from pathlib import Path as _Path
            scenario = json.loads(open(scenario_abs).read())
            client = StateServerClient(port=11009)
            out = _Path(out_dir) / _Path(scenario_abs).stem
            out.mkdir(parents=True, exist_ok=True)
            # Try to reuse the running game process from qa_tools if available
            try:
                from swarm import qa_tools as _qt
                game_process = _qt._qa_game_process
                client = StateServerClient(port=_qt._state_port)
            except Exception:
                game_process = None
            # Vision function
            try:
                from swarm.qa_tools import vision_query as _vq
                def vision_fn(image_path, question):
                    result = _vq(image_path, question, model_tier="fast")
                    if isinstance(result, dict):
                        if "error" in result:
                            return result
                        return result.get("answer", result.get("text", str(result)))
                    return str(result)
            except Exception:
                vision_fn = None
            executor = ScenarioExecutor(
                scenario=scenario,
                client=client,
                out_dir=out,
                game_process=game_process,
                vision_fn=vision_fn,
                project=_rt.PROJECT,
                swarm_url=swarm_url,
                project_path=_proj_path,
            )
            return executor.run()
        except Exception as e:
            return {"ok": False, "error": f"run_scenario exception: {e}"}

    _reg("run_scenario", _run_scenario, required_args=["scenario_path"], task_types=_SCENARIO_QA_TYPES)
    _reg("write_scenario", lambda a, ws, p: _write_scenario_file(a, ws, p), required_args=["path", "scenario"], task_types=_SCENARIO_QA_TYPES)

    # Aliases — model sometimes hallucinates tool names from other prompt variants
    # (applied via the aliases= field on existing specs rather than separate entries)
    for spec in list(_TOOL_REGISTRY):
        if spec.name == "launch_game":
            spec.aliases.extend(["start_game"])
        if spec.name == "harness_launch_game":
            spec.aliases.extend(["harness_launch_game"])  # no-op self-alias; real aliases below
        if spec.name == "get_game_state":
            spec.aliases.extend(["harness_get_state"])
        if spec.name == "take_screenshot":
            spec.aliases.extend(["harness_screenshot"])

    _build_required_args_index()


_populate_registry()
