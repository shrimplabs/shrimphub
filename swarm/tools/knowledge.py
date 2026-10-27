"""
swarm.tools.knowledge — scratchpad, per-project knowledge, and shared knowledge tools.

Config vars in this module are set via _sync_knowledge_globals() in agent_runtime.py.
"""

import json
import os
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Config vars — set via _sync_knowledge_globals() in agent_runtime before use
# ---------------------------------------------------------------------------

PROJECT: str = ""
PROJECT_PATH_OVERRIDE: str = ""
WORKSPACE: Path = Path(".")
TASK_ID: str = "unknown"
DATA_DIR: str = "data"
API_PORT: int = 5001
READONLY: bool = False
TASK_TYPE: str = "feature"

# ---------------------------------------------------------------------------
# In-memory scratchpad (lives for the lifetime of this agent process)
# ---------------------------------------------------------------------------

SCRATCHPAD: list = []


# ---------------------------------------------------------------------------
# Helper — delegates to swarm.tools.core to avoid duplicating _project_root
# ---------------------------------------------------------------------------

def _project_root() -> str:
    """Return the effective project root (delegates to core for consistency)."""
    if PROJECT_PATH_OVERRIDE:
        return PROJECT_PATH_OVERRIDE
    return str(Path(WORKSPACE) / PROJECT)


# ---------------------------------------------------------------------------
# Scratchpad tools
# ---------------------------------------------------------------------------

def scratchpad_write(type: str = "note", content: str = "", files: list = None, key: str = None) -> dict:
    """Write a tagged note to the in-memory scratchpad.

    Args:
        type:    Category — "observation", "plan", "gotcha", "progress", or any label
        content: The note text
        files:   Optional file paths this note relates to (for filtered retrieval)
        key:     Optional named key for direct lookup

    Returns: {"ok": true, "index": N, "total": N}
    """
    import time as _time
    global SCRATCHPAD
    entry = {
        "type": type,
        "content": content,
        "files": files or [],
        "timestamp": _time.time(),
    }
    if key:
        entry["key"] = key
    SCRATCHPAD.append(entry)
    return {"ok": True, "index": len(SCRATCHPAD) - 1, "total": len(SCRATCHPAD)}


def scratchpad_read(files: list = None, type: str = None, key: str = None) -> dict:
    """Read notes from the in-memory scratchpad.

    Args:
        files: Return only entries mentioning at least one of these file paths
        type:  Filter to entries of this type (e.g. "gotcha", "plan")
        key:   Return the single entry stored under that named key

    Returns: {"ok": true, "entries": [...], "total": N}
    """
    entries = list(SCRATCHPAD)
    if key:
        entries = [e for e in entries if e.get("key") == key]
    if type:
        entries = [e for e in entries if e.get("type") == type]
    if files:
        file_set = set(files)
        entries = [e for e in entries if any(f in file_set for f in e.get("files", []))]
    return {"ok": True, "entries": entries, "total": len(entries)}


# ---------------------------------------------------------------------------
# Per-project knowledge (AGENT_KNOWLEDGE.md)
# ---------------------------------------------------------------------------

_KNOWLEDGE_INJECT_LIMIT = 20_000   # chars injected into task description (~5k tokens)
_KNOWLEDGE_COMPACT_THRESHOLD = 40_000  # chars before compaction runs on write


def read_agent_knowledge(project_path: str) -> str:
    """Read AGENT_KNOWLEDGE.md from project root if it exists.

    Returns at most _KNOWLEDGE_INJECT_LIMIT chars (the tail, i.e. most recent content).
    Older entries are preserved in the file but not injected to keep context lean.
    """
    knowledge_path = os.path.join(project_path, "AGENT_KNOWLEDGE.md")
    try:
        if os.path.exists(knowledge_path):
            from swarm.tools.core import _read_text_with_fallback
            text, _ = _read_text_with_fallback(knowledge_path)
            if len(text) <= _KNOWLEDGE_INJECT_LIMIT:
                return text
            # Truncate to last _KNOWLEDGE_INJECT_LIMIT chars, but start at an entry
            # boundary (---) so we don't inject a torn entry.
            tail = text[-_KNOWLEDGE_INJECT_LIMIT:]
            boundary = tail.find("\n---\n")
            if boundary != -1:
                tail = tail[boundary + 5:]
            total_entries = text.count("\n---\n") + 1
            injected_entries = tail.count("\n---\n") + 1
            header = f"[AGENT_KNOWLEDGE truncated: showing {injected_entries} most recent of {total_entries} entries]\n\n"
            return header + tail
    except Exception:
        pass
    return ""


def _compact_knowledge(knowledge_path: Path) -> bool:
    """LLM-compact AGENT_KNOWLEDGE.md when it exceeds the threshold.

    Reads the full file, calls the LLM to distill it into a single dense summary
    preserving all unique facts, then overwrites the file. Returns True on success.
    """
    try:
        text = knowledge_path.read_text(encoding="utf-8", errors="replace")
        if len(text) <= _KNOWLEDGE_COMPACT_THRESHOLD:
            return False  # nothing to do

        from swarm.llm_utils import call_llm
        sys_prompt = (
            "You are a knowledge base compactor. You will receive an AGENT_KNOWLEDGE.md file "
            "containing accumulated notes from multiple AI agents working on the same project. "
            "Your job is to distill it into a single compact, dense reference document. "
            "Rules: preserve ALL unique facts (file paths, class names, patterns, bug fixes, "
            "validation commands, exclusion lists, gotchas); merge duplicate or redundant entries "
            "into one authoritative statement; remove progress/status updates that are no longer "
            "actionable; keep the most recent version of any repeated fact; output plain markdown "
            "with no preamble, no commentary, just the distilled knowledge."
        )
        user_msg = (
            f"Compact the following AGENT_KNOWLEDGE.md file. "
            f"Return ONLY the distilled markdown — no preamble, no 'Here is...' framing.\n\n"
            f"{text}"
        )
        compacted, _, _thinking = call_llm(sys_prompt, [{"role": "user", "content": user_msg}])
        if not compacted or compacted.startswith("Error:") or compacted.startswith("API error"):
            return False

        # Write compacted version
        knowledge_path.write_text(compacted.strip() + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def update_validation_state(content: str) -> dict:
    """Overwrite VALIDATION_STATE.md with the current validation status.

    Unlike update_knowledge (which appends), this REPLACES the file each time
    so it always reflects the latest known-good state — no accumulation of
    redundant validation entries. Use this for:
      - Current validation command results (Scripts OK, Scenes OK, Main scene OK)
      - Exclusion lists (_swarm_check.gd patterns, scene skip lists)
      - Recurring gotchas (e.g. ghost file that keeps being deleted)
      - Validation commands (exact godot --headless invocations)
    Do NOT use this for structural facts (autoloads, class names, systems) —
    those belong in update_knowledge().
    """
    if READONLY:
        return {"ok": False, "error": "Read-only task: update_validation_state is disabled"}
    project_root = Path(_project_root())
    state_path = project_root / "VALIDATION_STATE.md"
    try:
        state_path.write_text(content.strip() + "\n", encoding="utf-8")
        return {"ok": True, "path": str(state_path)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def read_validation_state(project_path: str) -> str:
    """Read VALIDATION_STATE.md from project root if it exists."""
    state_path = os.path.join(project_path, "VALIDATION_STATE.md")
    try:
        if os.path.exists(state_path):
            from swarm.tools.core import _read_text_with_fallback
            text, _ = _read_text_with_fallback(state_path)
            # Hard cap — validation state should be compact by design
            if len(text) > 4000:
                return text[:4000] + "\n[VALIDATION_STATE truncated]"
            return text
    except Exception:
        pass
    return ""


def update_knowledge(content: str) -> dict:
    """Append an entry to the project's AGENT_KNOWLEDGE.md file.

    Creates the file if it does not exist. Entries are separated by "\\n---\\n".
    If the file exceeds _KNOWLEDGE_COMPACT_THRESHOLD chars after writing, triggers
    LLM compaction to keep the file lean for future agents.
    Use this for structural facts: key file locations, class names, API patterns,
    system architecture, gotchas. For validation status and exclusion lists,
    use update_validation_state() instead.
    """
    if READONLY:
        return {"ok": False, "error": "Read-only task: update_knowledge is disabled"}
    project_root = Path(_project_root())
    knowledge_path = project_root / "AGENT_KNOWLEDGE.md"
    try:
        if knowledge_path.exists() and knowledge_path.stat().st_size > 0:
            with open(knowledge_path, "a", encoding="utf-8") as f:
                f.write("\n---\n")
        with open(knowledge_path, "a", encoding="utf-8") as f:
            f.write(content.strip() + "\n")

        # Trigger compaction if file has grown too large
        compacted = False
        if knowledge_path.stat().st_size > _KNOWLEDGE_COMPACT_THRESHOLD:
            compacted = _compact_knowledge(knowledge_path)

        result: dict = {"ok": True, "path": str(knowledge_path)}
        if compacted:
            result["compacted"] = True
            result["new_size"] = knowledge_path.stat().st_size
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Shared knowledge (cross-project)
# ---------------------------------------------------------------------------

def get_task_context() -> dict:
    """Return a read-only snapshot of project state.

    Returns dict with:
      - active_agents: list of active agents for this PROJECT (excl. self)
      - recent_completed_tasks: last 5 completed tasks for this PROJECT
      - recent_commits: last 5 git commits

    All reads are in try/except — partial failures return empty lists.
    """
    import urllib.request as _ur_local

    result = {
        "active_agents": [],
        "recent_completed_tasks": [],
        "recent_commits": [],
    }

    try:
        _url = f"http://localhost:{API_PORT}/api/agents"
        with _ur_local.urlopen(_url, timeout=5) as _resp:
            _agents = json.loads(_resp.read().decode())
        _my_id = TASK_ID if TASK_ID and TASK_ID != "unknown" else None
        result["active_agents"] = [
            {"id": a.get("id"), "status": a.get("status"), "project": a.get("project")}
            for a in _agents
            if a.get("project") == PROJECT and a.get("status") == "active"
            and a.get("id") != _my_id
        ]
    except Exception:
        pass

    try:
        _data_dir = Path(DATA_DIR) if Path(DATA_DIR).is_absolute() else Path(_project_root()) / DATA_DIR
        _hist_path = _data_dir / "task-history.jsonl"
        if _hist_path.exists():
            _lines = _hist_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            _recent = []
            for _l in reversed(_lines[-50:]):
                try:
                    _entry = json.loads(_l)
                    if _entry.get("project") == PROJECT and _entry.get("status") == "completed":
                        _recent.append({
                            "id": _entry.get("id", ""),
                            "type": _entry.get("type", ""),
                            "description": (_entry.get("description", "") or "")[:80],
                            "completed_at": _entry.get("completed_at", ""),
                        })
                        if len(_recent) >= 5:
                            break
                except Exception:
                    continue
            result["recent_completed_tasks"] = _recent
    except Exception:
        pass

    try:
        _pr = Path(_project_root())
        _r = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True, cwd=str(_pr), timeout=5,
        )
        if _r.returncode == 0 and _r.stdout.strip():
            result["recent_commits"] = _r.stdout.strip().splitlines()
    except Exception:
        pass

    return result


def read_shared_knowledge(topic: str = "") -> dict:
    """Read from the cross-project shared knowledge base.

    If topic is provided, returns only sections containing that string.
    Caps response at 4000 characters.
    """
    try:
        _data_dir = Path(DATA_DIR) if Path(DATA_DIR).is_absolute() else Path(_project_root()) / DATA_DIR
        _path = _data_dir / "shared_knowledge.md"
        if not _path.exists():
            return {"ok": True, "content": ""}
        from swarm.tools.core import _read_text_with_fallback
        _text = _read_text_with_fallback(_path)[0]
        if topic:
            _lower_topic = topic.lower()
            _sep = "\n---\n"
            _sections = [s.strip() for s in _text.split(_sep) if _lower_topic in s.lower()]
            _text = _sep.join(_sections)
        if len(_text) > 4000:
            _text = _text[:4000] + "\n[...truncated]"
        return {"ok": True, "content": _text}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def update_shared_knowledge(content: str, topic: str = "") -> dict:
    """Append to the cross-project shared knowledge base.

    Prepends '[topic] ' to the content if topic is given.
    Uses fcntl.flock() for safe concurrent writes (Unix only).
    """
    if READONLY:
        return {"ok": False, "error": "Read-only task: update_shared_knowledge is disabled"}
    try:
        _data_dir = Path(DATA_DIR) if Path(DATA_DIR).is_absolute() else Path(_project_root()) / DATA_DIR
        _path = _data_dir / "shared_knowledge.md"
        _entry = ("[{" + topic + "}] " if topic else "") + content.strip()
        _sep = "\n---\n"
        _needs_sep = _path.exists() and _path.stat().st_size > 0
        import fcntl
        with open(_path, "a") as _f:
            fcntl.flock(_f.fileno(), fcntl.LOCK_EX)
            try:
                if _needs_sep:
                    _f.write(_sep)
                _f.write(_entry + "\n")
            finally:
                fcntl.flock(_f.fileno(), fcntl.LOCK_UN)
        return {"ok": True, "path": str(_path)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
