"""
Agent Loop Helpers

Stall detection and context compaction helpers extracted from agent_runtime.py.
"""

import collections
import json

from swarm.llm_utils import call_llm  # noqa: F401 — re-exported for callers that imported it from agent_runtime
from swarm.tools.core import log  # noqa: F401 — re-exported for callers that imported it from agent_runtime


# ---------------------------------------------------------------------------
# Stall Detection
# ---------------------------------------------------------------------------

_STALL_MESSAGE = (
    "You have called the same tool with identical arguments 3 times in a row "
    "and received the same result. This approach is not working. Stop and try "
    "a fundamentally different approach to solve the problem."
)


class StallDetector:
    """Detect when a single tool call repeats 3 times identically in a row.

    Usage in the agent loop::


        detector = StallDetector()
        ...
        if detector.record(tool_name, tool_args):
            conversation.append({"role": "user", "content": StallDetector.injected_message()})
        ...
        if len(tool_calls) == 1:
            detector.append((tool_calls[0].get("tool"), json.dumps(tool_calls[0].get("args"), sort_keys=True)))
    """

    def __init__(self) -> None:
        self._deque: collections.deque = collections.deque(maxlen=3)

    def record(self, tool_name: str, args: dict) -> bool:
        """Return True if stall is detected (same tool+args seen 3 times).

        Clears the internal deque after detecting so the same warning isnt
        injected on every subsequent loop tick.
        """
        entry = (tool_name, json.dumps(args, sort_keys=True))
        self._deque.append(entry)
        if len(self._deque) == 3 and len(set(self._deque)) == 1:
            self._deque.clear()
            return True
        return False

    def check(self) -> bool:
        """Return True if stall is currently detected (deque is full and all equal).

        Clears the deque on detection so the warning is only injected once per stall.
        """
        if len(self._deque) == 3 and len(set(self._deque)) == 1:
            self._deque.clear()
            return True
        return False

    def append(self, entry: tuple) -> None:
        """Append a (tool_name, args_json) tuple to the history deque."""
        self._deque.append(entry)

    @staticmethod
    def injected_message() -> str:
        """Message to inject into the conversation when a stall is detected."""
        return _STALL_MESSAGE


# ---------------------------------------------------------------------------
# Context Compaction
# ---------------------------------------------------------------------------

COMPACT_KEEP_TAIL = 4


def compact_conversation(
    conversation: list,
    system_prompt: str,
    compact_token_threshold: int,
    log_fn=log,
) -> list:
    """Compress the middle of a conversation when it exceeds the token threshold.


    Summarises all messages except the system prompt and the last N messages,
    replacing them with a single compact summary + acknowledgement pair.


    Returns the compacted conversation list (may be the original if compaction
    was not triggered or failed).

    Args:
        conversation: current conversation list
        system_prompt: used only for length estimation
        compact_token_threshold: estimated-token count above which compaction fires
        log_fn: logging function (defaults to swarm.tools.core.log)
    """
    _conv_token_estimate = sum(
        len(m["content"]) if isinstance(m["content"], str) else len(str(m["content"]))
        for m in conversation
    ) // 2

    if _conv_token_estimate <= compact_token_threshold:
        return conversation

    to_summarise = conversation[1:-COMPACT_KEEP_TAIL]
    raw_tail = conversation[-COMPACT_KEEP_TAIL:]
    trimmed_tail = []
    for msg in raw_tail:
        content = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
        if msg["role"] == "user" and len(content) > 2000:
            content = content[:2000] + "\n[... trimmed for compaction ...]"
        trimmed_tail.append({**msg, "content": content})

    history_text = "\n\n".join(
        f"[{m['role'].upper()}]: {m['content'] if isinstance(m['content'], str) else str(m['content'])}"
        for m in to_summarise
    )

    summary_prompt = (
        "You are summarising an AI agent's work session to compress its context.\n"
        "Produce a concise but complete summary covering:\n"
        "- What the task is and the current goal\n"
        "- What files have been read and their key contents\n"
        "- What changes have already been made (files written/edited)\n"
        "- What problems or bugs were found\n"
        "- What still needs to be done\n"
        "Be specific — include file names, function names, variable names, and error messages. "
        "The agent will use this summary as its only memory of prior work."
    )

    try:
        summary_text, _, _thinking = call_llm(summary_prompt, [{"role": "user", "content": history_text}])
        compacted = (
            conversation[:1]
            + [{"role": "user", "content": f"[CONTEXT SUMMARY — previous work compressed]\n{summary_text}"},
               {"role": "assistant", "content": "Understood. I'll continue from where I left off based on the summary."}]
            + trimmed_tail
        )
        log_fn(
            f"[Compaction] Compressed {len(to_summarise)} messages into summary "
            f"({len(summary_text)} chars); conv was ~{_conv_token_estimate} tokens "
            f"(threshold {compact_token_threshold})"
        )
        return compacted
    except Exception as e:
        log_fn(f"[Compaction] Failed: {e} — continuing with full history")
        return conversation
