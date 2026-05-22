"""
swarm.runtime_config -- provider limits, compaction threshold, and harness helpers.

Extracted from agent_runtime.py. These utilities are self-contained and do not
depend on the full agent_runtime config blob, making them safe to import from
other modules without creating circular dependencies.

All functions read config from agent_runtime at call-time via lazy import to
avoid capturing stale values.
"""

from __future__ import annotations

import json
from pathlib import Path


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
