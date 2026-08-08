"""Experimental model routing policies for continuous agent loops."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


READ_ONLY_TOOLS = {
    "list_files",
    "search_code",
    "read_file",
    "read_file_range",
    "get_file_stats",
    "get_file_outline",
    "list_tasks",
    "list_subtasks",
    "get_task_context",
    "scratchpad_read",
    "read_agent_knowledge",
    "read_shared_knowledge",
    "rag_query",
    "web_search",
    "fetch_url",
    "broadcast_read",
}

STRONG_TOOLS = {
    "write_file",
    "patch_file",
    "append_file",
    "run_command",
    "run_python",
    "git_commit",
    "git_push",
    "create_task",
    "create_tasks",
    "create_subtask",
    "create_tasks_file_aware",
    "delegate_task_batch",
    "annotate_downstream_tasks",
    "split_task",
    "prune_task",
    "insert_dependency",
    "set_task_complexity",
    "vision_query",
    "take_screenshot",
    "screenshot_burst",
    "launch_game",
    "kill_game",
    "get_game_state",
    "qa_run_harness",
}

STRONG_TASK_TYPES = {
    "art_pass",
    "polish",
    "qa",
    "harness_qa",
    "hybrid_qa",
    "scenario_qa",
    "bug",
}


@dataclass(frozen=True)
class RoutingDecision:
    provider: str | None
    tier: str
    reason: str


def _provider_or_none(provider: str, default_provider: str) -> str | None:
    provider = (provider or "").strip()
    if not provider or provider == default_provider:
        return None
    return provider


def choose_adaptive_flat_provider(
    config: dict[str, Any] | None,
    *,
    default_provider: str,
    fast_provider: str = "",
    strong_provider: str = "",
    task_type: str = "",
    loop_index: int = 0,
    last_tools: list[str] | tuple[str, ...] | None = None,
    next_tools: list[str] | tuple[str, ...] | None = None,
    consecutive_cheap_loops: int = 0,
) -> RoutingDecision:
    """Choose a provider for the next flat-loop LLM call.

    Routing priority:
    1. next_tools (lookahead): tools the agent just requested — zero-lag signal.
       If the current LLM response requested only read-only tools, route the
       next call to the fast provider immediately. If it requested any strong
       tool, stay on strong.
    2. last_tools (trailing fallback): tools executed in the previous loop.
       Used when next_tools is unavailable (e.g. first loop after a write).

    The policy stays conservative for intent-setting, edits, validation, vision,
    completion, and qualitative task types.
    """

    cfg = dict(config or {})
    if not cfg.get("enabled", False):
        return RoutingDecision(None, "default", "adaptive_flat_disabled")

    fast = str(cfg.get("fast_provider") or fast_provider or "").strip()
    strong = str(cfg.get("strong_provider") or strong_provider or default_provider).strip()
    max_cheap = int(cfg.get("max_consecutive_cheap_loops", cfg.get("max_cheap_loops", 3)) or 3)
    cheap_after_first = bool(cfg.get("cheap_after_first_loop", True))

    if not fast or fast == strong:
        return RoutingDecision(_provider_or_none(strong, default_provider), "strong", "no_distinct_fast_provider")

    if loop_index <= 0 and cheap_after_first:
        return RoutingDecision(_provider_or_none(strong, default_provider), "strong", "first_loop_sets_intent")

    if consecutive_cheap_loops >= max_cheap:
        return RoutingDecision(_provider_or_none(strong, default_provider), "strong", "cheap_loop_cap")

    # Lookahead: route based on what the agent just requested (zero-lag).
    # next_tools is set from the parsed tool calls of the current LLM response
    # before they are executed, so it predicts what the *next* loop needs.
    requested = [str(t) for t in (next_tools or []) if t]
    if requested:
        if task_type in STRONG_TASK_TYPES:
            # Qualitative tasks stay strong unless it's a pure read probe.
            if all(t in READ_ONLY_TOOLS for t in requested) and consecutive_cheap_loops == 0:
                return RoutingDecision(_provider_or_none(fast, default_provider), "cheap", "lookahead_qualitative_read")
            return RoutingDecision(_provider_or_none(strong, default_provider), "strong", "strong_task_type")
        if any(t in STRONG_TOOLS for t in requested):
            return RoutingDecision(_provider_or_none(strong, default_provider), "strong", "lookahead_strong_tool")
        if all(t in READ_ONLY_TOOLS for t in requested):
            return RoutingDecision(_provider_or_none(fast, default_provider), "cheap", "lookahead_read_only")
        return RoutingDecision(_provider_or_none(strong, default_provider), "strong", "lookahead_mixed_tools")

    # Trailing fallback: use tools executed in the previous loop.
    tools = [str(t) for t in (last_tools or []) if t]

    if task_type in STRONG_TASK_TYPES:
        if tools and all(t in READ_ONLY_TOOLS for t in tools) and consecutive_cheap_loops == 0:
            return RoutingDecision(_provider_or_none(fast, default_provider), "cheap", "qualitative_read_only_probe")
        return RoutingDecision(_provider_or_none(strong, default_provider), "strong", "strong_task_type")

    if not tools:
        return RoutingDecision(_provider_or_none(strong, default_provider), "strong", "no_prior_tool_signal")

    if any(t in STRONG_TOOLS for t in tools):
        return RoutingDecision(_provider_or_none(strong, default_provider), "strong", "prior_strong_tool")

    if all(t in READ_ONLY_TOOLS for t in tools):
        return RoutingDecision(_provider_or_none(fast, default_provider), "cheap", "read_only_followup")

    return RoutingDecision(_provider_or_none(strong, default_provider), "strong", "unknown_tool_signal")
