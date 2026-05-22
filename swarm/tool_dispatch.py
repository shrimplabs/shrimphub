"""
swarm.tool_dispatch -- tool call validation, authority gating, and dispatch.

Extracted from agent_runtime.py. Reads runtime config from agent_runtime at
call-time via lazy import to avoid capturing stale values.
"""

from __future__ import annotations

import json
from pathlib import Path

# Tool function imports — these are safe at module level (no circular imports)
from swarm.tools.core import (
    log, _project_root, _safe_cwd, run,
    run_command, git_commit, git_push,
    mcp_call_tool, mcp_list_tools,
    rag_query, web_search, fetch_url,
    broadcast_read, broadcast_write, delegate_helper,
)
from swarm.tools.files import (
    read_file, list_files, search_code, get_file_stats, get_file_outline,
    read_file_range, patch_file, write_file, append_file,
)
from swarm.tools.tasks import (
    create_task, create_tasks_file_aware, create_tasks, delegate_task_batch,
    list_tasks, list_subtasks,
    annotate_downstream_tasks, split_task, prune_task, insert_dependency, set_task_complexity,
)
from swarm.tools.knowledge import (
    scratchpad_write, scratchpad_read,
    read_agent_knowledge, update_knowledge,
    get_task_context, read_shared_knowledge, update_shared_knowledge,
)
from swarm.qa_tools import (
    qa_focus_game, qa_position_window, qa_get_window_bounds,
    launch_game as qa_launch_game,
    launch_game_headless,
    take_screenshot as qa_take_screenshot,
    click_at as qa_click_at,
    click_element as qa_click_element,
    qa_key_press, qa_press_button, qa_wait,
    qa_wait_for_idle, qa_poll_until, qa_wait_until, qa_run_sequence,
    kill_game as qa_kill_game,
    qa_create_bug_task, qa_requeue_self,
    get_game_state as qa_get_game_state,
    vision_query as qa_vision_query,
    harness_launch_game, harness_step, harness_take_screenshot,
    harness_kill_game,
    harness_poll_state, harness_inject,
)


# ---------------------------------------------------------------------------
# Required args validation table
# ---------------------------------------------------------------------------

_TOOL_REQUIRED_ARGS: dict = {
    "read_file":        ["path"],
    "read_file_range":  ["path", "start_line", "end_line"],
    "get_file_outline": ["path"],
    "get_file_stats":   ["path"],
    "write_file":       ["path", "content"],
    "patch_file":       ["path", "old", "new"],
    "append_file":      ["path", "content"],
    "run_command":      ["command"],
    "search_code":      ["query"],
    "web_search":       ["query"],
    "fetch_url":        ["url"],
    "rag_query":        ["question"],
    "create_task":      ["description"],
    "create_tasks_file_aware": ["tasks"],
    "update_knowledge": ["content"],
    "update_shared_knowledge": ["content"],
    "mcp_call_tool":    ["server", "tool"],
    "broadcast_read":  [],
    "broadcast_write": ["message"],
    "delegate_helper": ["question"],
    "delegate_task_batch": ["children"],
    "annotate_downstream_tasks": ["findings"],
    "split_task":       ["task_id", "replacement_tasks"],
    "prune_task":       ["task_id", "reason"],
    "insert_dependency": ["from_task_id", "to_task_id"],
    "set_task_complexity": ["task_id", "complexity"],
}


# ---------------------------------------------------------------------------
# Tool call validation
# ---------------------------------------------------------------------------

def validate_tool_call(tool_call: dict) -> str:
    """Return an error string if the tool call is malformed, else empty string."""
    tool = tool_call.get("tool", "")
    args = tool_call.get("args") or {}

    if not tool:
        return "Tool call is missing the 'tool' field."

    if not isinstance(args, dict):
        return f"Tool '{tool}': 'args' must be a JSON object, got {type(args).__name__}."

    required = _TOOL_REQUIRED_ARGS.get(tool, [])
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

    if task_type in ("plan", "python_plan") and tool in mutating_tools:
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

    tool = tool_call.get("tool", "")
    args = tool_call.get("args", {})
    log(f"Executing tool: {tool}")

    denied = _tool_authority_denial(tool, args)
    if denied:
        return denied

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

    dispatch = {
        "read_file":      lambda: read_file(args.get("path", ""), args.get("offset", 0), args.get("limit", 0)),
        "list_files":     lambda: list_files(args.get("path", ".")),
        "search_code":    lambda: search_code(args.get("query", "")),
        "get_file_stats": lambda: get_file_stats(args.get("path", ".")),
        "get_file_outline": lambda: get_file_outline(args.get("path", "")),
        "read_file_range": lambda: read_file_range(args.get("path", ""), args.get("start_line", 1), args.get("end_line", 100)),
        "patch_file": lambda: patch_file(args.get("path", ""), args.get("old", ""), args.get("new", "")),
        "write_file":     lambda: write_file(args.get("path", ""), args.get("content", "")),
        "run_command":    lambda: run_command(args.get("command", ""), args.get("timeout", 60)),
        "git_commit":     lambda: git_commit(args.get("message", "Agent commit"), args.get("files")),
        "git_push":       lambda: git_push(),
        "mcp_call_tool":  lambda: mcp_call_tool(args.get("server", ""), args.get("tool", ""), args.get("args", {})),
        "mcp_list_tools": lambda: mcp_list_tools(args.get("server", "")),
        "rag_query":      lambda: rag_query(args.get("question", ""), args.get("top_k", 5)),
        "web_search":     lambda: web_search(args.get("query", ""), args.get("max_results", 3)),
        "fetch_url":      lambda: fetch_url(args.get("url", ""), args.get("extract_text", True)),
        "create_task":    lambda: create_task(args.get("description", ""), args.get("type", "feature"), args.get("priority", 50), args.get("dependencies", []), args.get("project"), args.get("parent_task_id"), args.get("metadata")),
        "create_tasks_file_aware": lambda: create_tasks_file_aware(args.get("tasks", []), args.get("project")),
        "list_tasks":     lambda: list_tasks(args.get("project")),
        "list_subtasks":  lambda: list_subtasks(args.get("parent_task_id")),
        "append_file":     lambda: append_file(args.get("path", ""), args.get("content", "")),
        "scratchpad_write": lambda: scratchpad_write(args.get("type", "note"), args.get("content", ""), args.get("files"), args.get("key")),
        "scratchpad_read":  lambda: scratchpad_read(args.get("files"), args.get("type"), args.get("key")),
        "update_knowledge":  lambda: update_knowledge(args.get("content", "")),
        "read_shared_knowledge":   lambda: read_shared_knowledge(args.get("topic", "")),
        "update_shared_knowledge": lambda: update_shared_knowledge(args.get("content", ""), args.get("topic", "")),
        "get_task_context":        lambda: get_task_context(),
        "broadcast_read":   lambda: broadcast_read(args.get("tail", 50)),
        "broadcast_write":  lambda: broadcast_write(args.get("message", "")),
        "delegate_helper": lambda: delegate_helper(
            args.get("question", ""),
            args.get("files", []),
            args.get("scope", ""),
            args.get("max_chars", 12000),
        ),
        "delegate_task_batch": lambda: delegate_task_batch(
            args.get("children", []),
            args.get("mode", "integrate"),
            args.get("project"),
        ),
        "annotate_downstream_tasks": lambda: annotate_downstream_tasks(
            args.get("findings", ""),
            args.get("task_ids"),
        ),
        "split_task": lambda: split_task(
            args.get("task_id", ""),
            args.get("replacement_tasks", []),
        ),
        "prune_task": lambda: prune_task(
            args.get("task_id", ""),
            args.get("reason", ""),
        ),
        "insert_dependency": lambda: insert_dependency(
            args.get("from_task_id", ""),
            args.get("to_task_id", ""),
        ),
        "set_task_complexity": lambda: set_task_complexity(
            args.get("task_id", ""),
            args.get("complexity", ""),
            args.get("reason", ""),
        ),
    }

    # Godot game-verification tools — available to all non-readonly Godot tasks.
    _godot_project_file = (
        Path(_rt.PROJECT_PATH_OVERRIDE) if _rt.PROJECT_PATH_OVERRIDE else (workspace / project)
    ) / "project.godot"
    if _godot_project_file.exists() and not _rt.READONLY:
        dispatch.update({
            "launch_game":    lambda: launch_game_headless(args.get("project_path", str(workspace / project))),
            "get_game_state": lambda: qa_get_game_state(),
            "wait":           lambda: qa_wait(args.get("seconds", 1)),
            "kill_game":      lambda: qa_kill_game(),
        })

    # QA-only tools (vision-led game interaction)
    if _rt.TASK_TYPE in ("qa", "art_pass", "hybrid_qa"):
        dispatch.update({
            "focus_game":      lambda: qa_focus_game(),
            "position_window": lambda: qa_position_window(),
            "launch_game":     lambda: (
                harness_launch_game(
                    args.get("project_path", str(workspace / project)),
                    _parse_extra_args(args.get("extra_args")),
                )
                if _rt.TASK_TYPE == "hybrid_qa" and _project_supports_harness() and _parse_extra_args(args.get("extra_args"))
                else qa_launch_game(args.get("project_path", str(workspace / project)))
            ),
            "get_window_bounds": lambda: qa_get_window_bounds(args.get("process_name", "Godot_4")),
            "take_screenshot": lambda: qa_take_screenshot(
                args.get("filename", "/tmp/qa_screenshot.png"),
            ),
            "click_at":        lambda: qa_click_at(args.get("x", 0), args.get("y", 0)),
            "click_element":   lambda: qa_click_element(
                args.get("image_path", ""),
                args.get("element_description", ""),
            ),
            "key_press":       lambda: qa_key_press(args.get("key", "")),
            "press_button":    lambda: qa_press_button(args.get("text", "")),
            "wait":            lambda: qa_wait(args.get("seconds", 1)),
            "vision_query":    lambda: qa_vision_query(
                args.get("image_path", ""),
                args.get("question", ""),
                args.get("model", "fast"),
            ),
            "get_game_state":  lambda: qa_get_game_state(),
            "wait_for_idle":   lambda: qa_wait_for_idle(
                args.get("timeout", 10.0),
                args.get("interval", 0.5),
            ),
            "poll_until":      lambda: qa_poll_until(
                args.get("condition_key", ""),
                args.get("condition_value"),
                args.get("timeout", 10.0),
                args.get("interval", 0.1),
                args.get("negate", False),
            ),
            "wait_until":      lambda: qa_wait_until(
                args.get("state_key", ""),
                args.get("target_value"),
                args.get("timeout", 10.0),
                args.get("interval", 0.1),
            ),
            "run_sequence":    lambda: qa_run_sequence(args.get("actions", [])),
            "kill_game":       lambda: qa_kill_game(),
            "create_bug_task": lambda: qa_create_bug_task(
                args.get("description", ""),
                args.get("evidence_path", ""),
                args.get("priority", 80),
                args.get("dependencies", None),
            ),
            "requeue_self":    lambda: qa_requeue_self(args.get("bug_task_ids", [])),
        })

    # Harness QA tools — synchronous checkpoint protocol
    if _rt.TASK_TYPE == "harness_qa" or (_rt.TASK_TYPE == "hybrid_qa" and _project_supports_harness()):
        dispatch.update({
            "harness_launch_game": lambda: harness_launch_game(
                str(workspace / project),
                _parse_extra_args(args.get("extra_args")),
            ),
            "harness_step":    lambda: harness_step(_resolve_harness_action(args), int(args.get("timeout", 30))),
            "harness_kill_game": lambda: harness_kill_game(),
            "harness_poll_state": lambda: harness_poll_state(int(args.get("timeout", 5))),
            "harness_inject": lambda: harness_inject(
                args.get("command") if isinstance(args.get("command"), dict)
                    else (__import__('json').loads(args["command"]) if isinstance(args.get("command"), str) and args["command"].strip().startswith("{")
                          else {k: v for k, v in args.items() if k not in ("timeout",)} if "command" in args
                          else args),
                int(args.get("timeout", 5)),
            ),
            "harness_take_screenshot": lambda: harness_take_screenshot(
                args.get("filename", f"data/harness_screenshot_{int(__import__('time').time())}.png"),
            ),
            "create_bug_task": lambda: qa_create_bug_task(
                args.get("description", ""),
                args.get("evidence_path", ""),
                args.get("priority", 80),
                args.get("dependencies", None),
            ),
            "requeue_self":    lambda: qa_requeue_self(args.get("bug_task_ids", [])),
        })

    # Aliases — model sometimes hallucinates tool names from other prompt variants
    _TOOL_ALIASES = {
        "harness_launch_game": "launch_game",
        "harness_get_state":   "get_game_state",
        "harness_screenshot":  "take_screenshot",
        "start_game":          "launch_game",
    }
    resolved_tool = _TOOL_ALIASES.get(tool, tool)
    if resolved_tool != tool:
        log(f"Tool alias: {tool} → {resolved_tool}")

    fn = dispatch.get(resolved_tool)
    if fn:
        result = fn()
        if resolved_tool == "broadcast_write" and isinstance(result, dict) and result.get("ok", True) is not False:
            _rt.RUN_BROADCAST_WRITE_COUNT += 1
        return result
    return {
        "ok": False,
        "error": f"Unknown tool: {tool}. Valid tools: {', '.join(dispatch.keys())}",
    }
