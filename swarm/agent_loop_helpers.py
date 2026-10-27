"""
Agent Loop Helpers

Stall detection, wrap-up messaging, continuation-task spawning, and context
compaction helpers shared by both the flat agent_runtime loop and the phase
pipeline work phase. Add shared mechanisms here so both paths stay in sync.
"""

import collections
import json
import urllib.request as _ur
from swarm.agent_token import agent_auth_headers

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
# Wrap-up Message Builder
# ---------------------------------------------------------------------------

def build_wrap_up_message(task_type: str, loops_left: int, has_mutations: bool) -> str:
    """Return the wrap-up nudge message appropriate for task_type.

    Called by both agent_runtime (flat loop) and work.py (phase loop) so the
    wording stays in sync automatically.
    """
    if task_type == "qa":
        return (
            f"WARNING: You have {loops_left} loops remaining. "
            "Stop testing immediately and wrap up:\n"
            "1. If you have already reproduced the same failure 2 or more times, do not investigate further.\n"
            "2. kill_game() if the game is still running.\n"
            "3. For every bug found: create_bug_task(description, evidence_path, priority, dependencies=[...]).\n"
            "   Chain bugs in the same system sequentially via dependencies. Different systems run in parallel.\n"
            "4. Call requeue_self(bug_task_ids=[...all ids...]).\n"
            "5. If no bugs: write QA_REPORT.md summarising what passed.\n"
            "6. TASK_COMPLETE\n"
            "Do not take more screenshots or play further."
        )
    if task_type == "bug":
        return (
            f"WARNING: You have {loops_left} loops remaining. "
            "Stop investigating and wrap up the fix now:\n"
            "1. Re-run the narrowest validation that proves the original bug is fixed.\n"
            "2. If that passes, run the broader required validation once.\n"
            "3. Summarise: repro, fix applied, targeted verification, broader verification.\n"
            "4. Commit all changes you have made (git add, git commit, git push).\n"
            "5. If the bug is not fully fixed, commit what you have with a clear message "
            "describing what still needs to be done.\n"
            "6. TASK_COMPLETE\n"
            "Do not start any new changes, and do not use grep/tail output as your only proof."
        )
    # feature, refactor, polish, art_pass — generic implementation wrap-up
    if has_mutations:
        return (
            f"WARNING: You have {loops_left} loops remaining in the work phase. "
            "Wrap up now:\n"
            "1. Run targeted validation to confirm your change works.\n"
            "2. git_commit all changes with a clear message.\n"
            "3. Output WORK_COMPLETE.\n"
            "Do not start new changes. Commit what you have."
        )
    return (
        f"WARNING: You have {loops_left} loops remaining and no commits yet. "
        "You must make a concrete change now:\n"
        "1. Pick the single most important file to edit and write it.\n"
        "2. git_commit immediately after.\n"
        "3. Output WORK_COMPLETE.\n"
        "Do not read more files or plan further."
    )


# ---------------------------------------------------------------------------
# Continuation Task Spawning
# ---------------------------------------------------------------------------

def spawn_continuation_task(
    task_id: str,
    task_type: str,
    task_desc: str,
    project: str,
    priority: int,
    managed_projects: list,
    conversation: list,
    api_port: int = 5001,
    project_path_override: str = "",
    worktree_branch: str = "",
    log_fn=log,
) -> str | None:
    """Spawn a continuation task when an agent hits the loop or context limit.

    Guards against deep chains, unmanaged projects, and existing continuations.
    Returns the new task ID on success, None if skipped or failed.
    Mirrors the logic in agent_runtime.py so both paths behave identically.
    """
    is_continuation = task_desc.startswith("CONTINUATION of task")
    chain_depth = task_id.count("bug-")
    deep_chain = chain_depth >= 3
    project_unmanaged = bool(managed_projects) and project not in managed_projects

    if is_continuation:
        log_fn("Loop/context limit on a continuation task — stopping chain")
        return None
    if deep_chain:
        log_fn(f"Loop/context limit on a deep chain task (depth {chain_depth}) — no continuation spawned")
        return None
    if project_unmanaged:
        log_fn(f"Loop/context limit but {project} is not in managed_projects — skipping continuation spawn")
        return None

    try:
        recent = conversation[-6:] if len(conversation) >= 6 else conversation
        summary_parts = [
            msg["content"][:400]
            for msg in recent
            if msg.get("role") == "assistant" and msg.get("content")
        ]
        remaining_context = "\n---\n".join(summary_parts[-2:]) if summary_parts else ""

        cont_desc = (
            f"CONTINUATION of task {task_id} (hit loop limit mid-task).\n\n"
            f"Original task: {task_desc[:500]}\n\n"
            f"The previous agent ran out of loops. It committed partial progress. "
            f"Read _swarm_progress.md in the project root for full context on what was done.\n"
            f"Then run the validation check to see what still needs fixing and continue.\n\n"
            f"Last agent progress:\n{remaining_context}"
        )
        meta: dict = {}
        if project_path_override:
            meta["worktree_path"] = project_path_override
            if worktree_branch:
                meta["worktree_branch"] = worktree_branch
                meta["worktree_inherited"] = True

        payload = json.dumps({
            "project": project,
            "type": task_type,
            "description": cont_desc,
            "priority": priority,
            "max_attempts": 5,
            "dependencies": [task_id],
            **({"metadata": meta} if meta else {}),
        }).encode()
        req = _ur.Request(
            f"http://localhost:{api_port}/api/tasks",
            data=payload,
            headers={"Content-Type": "application/json", **agent_auth_headers()},
            method="POST",
        )
        with _ur.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            new_id = result.get("task", {}).get("id", "?")
            log_fn(f"Continuation task created: {new_id}")
            return new_id
    except Exception as e:
        log_fn(f"WARNING: failed to spawn continuation task: {e}")
        return None


# ---------------------------------------------------------------------------
# Context Compaction
# ---------------------------------------------------------------------------

COMPACT_KEEP_TAIL = 4


def compact_conversation(
    conversation: list,
    system_prompt: str,
    compact_token_threshold: int,
    log_fn=log,
    compaction_provider: str | None = None,
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
        _compact_provider = compaction_provider or None
        summary_text, _, _thinking = call_llm(
            summary_prompt,
            [{"role": "user", "content": history_text}],
            provider=_compact_provider,
        )
        compacted = (
            conversation[:1]
            + [{"role": "user", "content": f"[CONTEXT SUMMARY — previous work compressed]\n{summary_text}"},
               {"role": "assistant", "content": "Understood. I'll continue from where I left off based on the summary."}]
            + trimmed_tail
        )
        _provider_note = f" via {_compact_provider}" if _compact_provider else ""
        log_fn(
            f"[Compaction] Compressed {len(to_summarise)} messages into summary "
            f"({len(summary_text)} chars){_provider_note}; conv was ~{_conv_token_estimate} tokens "
            f"(threshold {compact_token_threshold})"
        )
        return compacted
    except Exception as e:
        log_fn(f"[Compaction] Failed: {e} — continuing with full history")
        return conversation
