"""
Agent Runtime

All executable logic for swarm agent processes.  This module is imported by
thin wrapper scripts that the orchestrator generates per-task.

The wrapper sets module-level config variables, then calls main().

Example wrapper:
    import sys; sys.path.insert(0, "/path/to/swarm-controller")
    import swarm.agent_runtime as rt
    rt.WORKSPACE = "/path/to/workspace"
    rt.PROJECT   = "my-project"
    ...
    sys.exit(rt.main())
"""

import json
import atexit
import collections as _collections
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request as _ur
from pathlib import Path
from swarm import constants, db
from swarm.provider_utils import LLM_PROVIDERS
from swarm.agent_loop_helpers import StallDetector, compact_conversation  # noqa: E402
from swarm.llm_utils import call_llm, parse_tool_calls, MCPClient  # noqa: E402
from swarm import qa_tools  # noqa: E402
from swarm.branch_intent import format_branch_intent, branch_intent_metadata  # noqa: E402
from swarm.qa_tools import (  # noqa: E402
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

# Import tool functions from submodules
from swarm.tools.core import (  # noqa: F401
    log, _project_root, _safe_cwd, run,
    run_command, git_commit, git_push,
    mcp_call_tool, mcp_list_tools,
    rag_query, web_search, fetch_url,
    broadcast_read, broadcast_write, delegate_helper,
)

from swarm.tools.files import (  # noqa: F401, E402
    read_file, list_files, search_code, get_file_stats, get_file_outline,
    read_file_range, patch_file, write_file, append_file,
)

from swarm.tools.tasks import (  # noqa: F401
    create_task, create_tasks_file_aware, create_tasks, delegate_task_batch,
    list_tasks, list_subtasks,
    annotate_downstream_tasks, split_task, prune_task, insert_dependency, set_task_complexity,
)
from swarm.tools.knowledge import (  # noqa: F401
    scratchpad_write, scratchpad_read,
    read_agent_knowledge, update_knowledge,
    get_task_context, read_shared_knowledge, update_shared_knowledge,
)


# ---------------------------------------------------------------------------
# Config variables — set by the wrapper before calling main()
# ---------------------------------------------------------------------------

WORKSPACE: Path = Path(".")
DATA_DIR: str = "data"   # path to the swarm data directory; set by wrapper
PROJECT: str = ""
PROJECT_PATH_OVERRIDE: str = ""  # when set, overrides WORKSPACE/PROJECT (used with git worktrees)
WORKTREE_BRANCH: str = ""        # git branch name inside the worktree (empty when not in worktree)
TASK_TYPE: str = "feature"
TASK_DESC: str = ""
TASK_ID: str = "unknown"
TASK_PRIORITY: int = 50
MAX_LINES: int = 5000
IGNORE_DIRS: set = {"addons", ".git", ".godot"}
# These are overridden by the wrapper script; constants provide fallback defaults
MAX_TOOL_LOOPS: int = constants.MAX_TOOL_LOOPS
API_PORT: int = constants.API_PORT
QA_MAX_CYCLES: int = constants.QA_MAX_CYCLES
IGNORE_EXTENSIONS: set = constants.IGNORE_EXTENSIONS

MCP_SERVERS: dict = {}
MANAGED_PROJECTS: list = []  # set by wrapper; empty = all projects allowed
READONLY: bool = False  # when True, git_commit / git_push / write_file are disabled
QA_CONFIG: dict = {}    # vision provider config for QA agents (set by wrapper)
QA_CYCLE: int = 0       # which requeue cycle this QA task is on (0 = first run)
TASK_METADATA: dict = {}
RUN_BROADCAST_WRITE_COUNT: int = 0
CLAIMED_FILE_PATHS: set[str] = set()
LOCK_CONFLICT_HANDOFF: dict | None = None

# Task-type prompts — set by the wrapper
FEATURE_SYSTEM: str = ""
FEATURE_USER: str = ""
BUG_SYSTEM: str = ""
BUG_USER: str = ""
POLISH_SYSTEM: str = ""
POLISH_USER: str = ""
PYTHON_FEATURE_SYSTEM: str = ""
PYTHON_FEATURE_USER: str = ""
PYTHON_BUG_SYSTEM: str = ""
PYTHON_BUG_USER: str = ""
PYTHON_REFACTOR_SYSTEM: str = ""
PYTHON_REFACTOR_USER: str = ""
PYTHON_PLAN_SYSTEM: str = ""
PYTHON_PLAN_USER: str = ""
PLAN_SYSTEM: str = ""
PLAN_USER: str = ""
MANAGER_SYSTEM: str = ""
MANAGER_USER: str = ""
PROJECT_CREATE_SYSTEM: str = ""
PROJECT_CREATE_USER: str = ""
QA_SYSTEM: str = ""
QA_USER: str = ""
AUDIT_SYSTEM: str = ""
AUDIT_USER: str = ""
AUDIT_LEARNINGS_SYSTEM: str = ""
AUDIT_LEARNINGS_USER: str = ""
TRIAGE_SYSTEM: str = ""
TRIAGE_USER: str = ""
PROJECT_PLAN_SYSTEM: str = ""
PROJECT_PLAN_USER: str = ""
ART_PASS_SYSTEM: str = ""
ART_PASS_USER: str = ""
RESEARCH_SYSTEM: str = ""
RESEARCH_USER: str = ""
HARNESS_QA_SYSTEM: str = ""
HARNESS_QA_USER: str = ""
HYBRID_QA_SYSTEM: str = ""
HYBRID_QA_USER: str = ""

# LLM provider config — set by the wrapper
LLM_PROVIDER: str = "minimax"

# EXPERIMENT: meta-investigation — a short out-of-band LLM call that fires when
# the same error string repeats 3+ times in run_command output across non-consecutive
# loops. It reads relevant files, probes the environment, then injects a hint into
# the main conversation. Disable via config.json: "meta_investigation": false
META_INVESTIGATION_ENABLED: bool = True
# Provider used for meta-investigation calls — defaults to the same provider as the
# main agent. Override via config.json: "meta_investigation_provider": "claude"
META_INVESTIGATION_PROVIDER: str = ""

# Runtime state
system_prompt: str = ""
user_prompt: str = ""
mcp_client = None


def _get_provider_runtime_limits() -> tuple[int, int]:
    cfg = dict(LLM_PROVIDERS.get(LLM_PROVIDER, LLM_PROVIDERS.get("minimax", {})))
    context_window = int(cfg.get("context_window", 120_000))
    max_output_tokens = int(cfg.get("max_tokens", 8_096))
    return context_window, max_output_tokens


def _get_compaction_threshold() -> int:
    context_window, max_output_tokens = _get_provider_runtime_limits()
    # Leave headroom for tool responses, system prompt growth, and a full model answer.
    reserve = max(12_000, min(max_output_tokens + 8_000, context_window // 4))
    threshold = context_window - reserve
    # Planners benefit from larger repo context before compaction; other task types stay safer.
    if TASK_TYPE == "project_plan":
        threshold = min(context_window - 20_000, threshold + 12_000)
    return max(60_000, threshold)
SCRATCHPAD: list = []  # NOTE: actual scratchpad lives in swarm.tools.knowledge.SCRATCHPAD


def _project_supports_harness() -> bool:
    project_root = Path(PROJECT_PATH_OVERRIDE) if PROJECT_PATH_OVERRIDE else (WORKSPACE / PROJECT)
    return (project_root / "autoload" / "test_harness.gd").exists()


def _parse_extra_args(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        txt = raw.strip()
        if not txt:
            return []
        try:
            parsed = __import__("json").loads(txt)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        return [txt]
    return []


def _resolve_harness_action(args: dict) -> dict:
    raw = args.get("action")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        txt = raw.strip()
        if txt.startswith("{"):
            try:
                parsed = __import__("json").loads(txt)
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


def _normalized_report_path(path: str) -> str:
    return (path or "").strip().replace("\\", "/").lstrip("./")


def _normalized_project_file_path(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        return ""
    try:
        project_root = _project_root()
        root_resolved = project_root.resolve()
        raw_path = Path(raw)
        if raw_path.is_absolute():
            candidate = raw_path.resolve()
        else:
            candidate = (root_resolved / raw_path).resolve()
        if os.path.commonpath([str(root_resolved), str(candidate)]) == str(root_resolved):
            return candidate.relative_to(root_resolved).as_posix()
        return candidate.as_posix().lstrip("./")
    except Exception:
        return Path(raw).as_posix().lstrip("./")


def _load_project_activity_context(limit: int = 8) -> str:
    if not PROJECT:
        return ""
    try:
        db_path = Path(DATA_DIR) / "swarm.db"
        if not db_path.exists():
            return ""
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                select id, type, status, dependencies, description
                from tasks
                where project = ?
                  and id != ?
                  and status in ('in_progress', 'pending')
                order by
                  case status when 'in_progress' then 0 else 1 end,
                  created asc
                limit ?
                """,
                (PROJECT, TASK_ID, limit),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return ""

        active: list[str] = []
        pending: list[str] = []
        for row in rows:
            desc = (row["description"] or "").splitlines()[0].strip()
            short = desc[:100] + ("..." if len(desc) > 100 else "")
            try:
                deps = json.loads(row["dependencies"] or "[]")
            except Exception:
                deps = []
            item = f"{row['id']} ({row['type']}) — {short or 'no description'}"
            if deps:
                item += f" | deps: {', '.join(deps[:3])}"
                if len(deps) > 3:
                    item += ", ..."
            if row["status"] == "in_progress":
                active.append(item)
            else:
                pending.append(item)

        lines = [
            "## Live Project Activity",
            "Other tasks on this project may be running in parallel. Coordinate to avoid duplicate work, shared-file collisions, and repeated broad validation.",
        ]
        if active:
            lines.append("Active sibling tasks:")
            lines.extend(f"- {item}" for item in active)
        if pending:
            lines.append("Nearby pending tasks:")
            lines.extend(f"- {item}" for item in pending[: max(0, limit - len(active))])
        lines.append(
            "Use broadcast_read() early and before shared-file edits or broad validation. "
            "Use broadcast_write() as a bounded checkpoint: one early shared-file claim if needed, one finding when you discover a blocker/root cause that affects siblings, and one final handoff when you finish or create bug/recovery follow-up. "
            "Do not turn broadcasts into routine progress chatter."
        )
        return "\n".join(lines)
    except Exception:
        return ""


def _has_active_sibling_tasks() -> bool:
    if not PROJECT or not TASK_ID:
        return False
    try:
        db_path = Path(DATA_DIR) / "swarm.db"
        if not db_path.exists():
            return False
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                """
                select 1
                from tasks
                where project = ?
                  and id != ?
                  and status = 'in_progress'
                limit 1
                """,
                (PROJECT, TASK_ID),
            ).fetchone()
        finally:
            conn.close()
        return row is not None
    except Exception:
        return False


def _lock_project_file(path: str) -> dict:
    rel_path = _normalized_project_file_path(path)
    if not PROJECT or not TASK_ID or not rel_path:
        return {"ok": False, "error": "cannot lock empty file path"}
    try:
        payload = json.dumps({
            "file_path": rel_path,
            "agent_id": TASK_ID,
            "task_id": TASK_ID,
        }).encode()
        req = _ur.Request(
            f"http://localhost:{API_PORT}/api/projects/{PROJECT}/lock",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get("success"):
            CLAIMED_FILE_PATHS.add(rel_path)
            return {"ok": True, "file_path": rel_path}
        lock = result.get("lock") or {}
        owner = lock.get("task_id") or lock.get("locked_by")
        if owner and owner != TASK_ID:
            return {
                "ok": False,
                "error": f"file '{rel_path}' is currently locked by {owner}",
                "locked_by": lock.get("locked_by"),
                "task_id": lock.get("task_id"),
                "file_path": lock.get("file_path") or rel_path,
            }
    except Exception:
        pass

    try:
        with _ur.urlopen(f"http://localhost:{API_PORT}/api/projects/{PROJECT}/locks", timeout=10) as resp:
            locks = json.loads(resp.read()).get("locks", {})
        lock = locks.get(rel_path)
        if lock and lock.get("locked_by") != TASK_ID:
            owner = lock.get("task_id") or lock.get("locked_by") or "another task"
            return {
                "ok": False,
                "error": f"file '{rel_path}' is currently locked by {owner}",
                "locked_by": lock.get("locked_by"),
                "task_id": lock.get("task_id"),
            }
    except Exception:
        pass

    return {"ok": False, "error": f"failed to lock file '{rel_path}'"}


def _unlock_claimed_files() -> None:
    if not PROJECT or not TASK_ID or not CLAIMED_FILE_PATHS:
        return
    for rel_path in list(CLAIMED_FILE_PATHS):
        try:
            payload = json.dumps({
                "file_path": rel_path,
                "agent_id": TASK_ID,
            }).encode()
            req = _ur.Request(
                f"http://localhost:{API_PORT}/api/projects/{PROJECT}/unlock",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _ur.urlopen(req, timeout=10):
                pass
        except Exception:
            pass
        finally:
            CLAIMED_FILE_PATHS.discard(rel_path)


def _is_archived(task_id: str) -> bool:
    """Return True if task_id is in the archived task history (completed/failed/cancelled and pruned from DB)."""
    import swarm.orchestrator as _orc
    history_file = getattr(_orc, 'HISTORY_FILE', None)
    if history_file is None:
        try:
            import swarm.orchestrator as _o2
            history_file = getattr(_o2, 'HISTORY_FILE', None)
        except Exception:
            pass
    if history_file and history_file.exists():
        try:
            for line in history_file.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get("id") == task_id:
                    return True
        except Exception:
            pass
    # Fallback: check DB directly (tasks may still be in DB if prune hasn't run)
    try:
        task = db.task_get(task_id)
        if task is not None:
            return task.get("status") in ("completed", "failed", "cancelled")
    except Exception:
        pass
    return False


def _api_get_json(path: str) -> dict:
    with _ur.urlopen(f"http://localhost:{API_PORT}{path}", timeout=10) as resp:
        return json.loads(resp.read())


def _api_patch_json(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = _ur.Request(
        f"http://localhost:{API_PORT}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    with _ur.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _api_post_json(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = _ur.Request(
        f"http://localhost:{API_PORT}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _ur.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _spawn_lock_conflict_handoff(locked_path: str, owner_task_id: str) -> dict:
    global LOCK_CONFLICT_HANDOFF
    if not owner_task_id or owner_task_id == TASK_ID:
        return {"ok": False, "error": "missing valid owner task for lock handoff"}
    if LOCK_CONFLICT_HANDOFF and LOCK_CONFLICT_HANDOFF.get("followup_task_id"):
        return {"ok": True, **LOCK_CONFLICT_HANDOFF}

    try:
        current_task = (_api_get_json(f"/api/tasks/{TASK_ID}").get("task") or {})
    except Exception:
        current_task = {}
    inherited_deps: list[str] = []
    seen_deps: set[str] = set()
    for dep in current_task.get("dependencies") or []:
        if not isinstance(dep, str):
            continue
        dep_id = dep.strip()
        if not dep_id or dep_id in seen_deps:
            continue
        seen_deps.add(dep_id)
        inherited_deps.append(dep_id)
    if owner_task_id not in seen_deps:
        inherited_deps.append(owner_task_id)

    intent_task = {
        "id": TASK_ID,
        "type": TASK_TYPE,
        "description": TASK_DESC,
        "metadata": dict(TASK_METADATA or {}),
    }
    if current_task:
        merged_metadata = dict(intent_task.get("metadata") or {})
        merged_metadata.update(current_task.get("metadata") or {})
        intent_task.update(dict(current_task))
        intent_task["metadata"] = merged_metadata
        if not (intent_task.get("description") or "").strip():
            intent_task["description"] = TASK_DESC
        if not (intent_task.get("type") or "").strip():
            intent_task["type"] = TASK_TYPE

    handoff_desc = (
        f"CONTINUATION of task {TASK_ID} after lock conflict.\n\n"
        f"{format_branch_intent(intent_task, heading='ORIGINAL TASK OBJECTIVE')}\n\n"
        f"This work was blocked because `{locked_path}` is currently owned by sibling task `{owner_task_id}`.\n"
        f"Continue this task only after `{owner_task_id}` completes. Re-check HEAD first: the sibling may have already satisfied part of the requirement.\n"
        f"If the needed change is already present, validate and finish without duplicating work."
    )

    # Atomic check-or-create: server serialises concurrent callers for the same
    # (project, owner_task_id), so all contenders get back the same continuation.
    handoff_resp = _api_post_json(
        f"/api/projects/{PROJECT}/lock-conflict-handoff",
        {
            "blocked_task_id": TASK_ID,
            "owner_task_id":   owner_task_id,
            "locked_path":     locked_path,
            "task_type":       TASK_TYPE,
            "priority":        TASK_PRIORITY,
            "description":     handoff_desc,
            "dependencies":    inherited_deps,
            "metadata":        branch_intent_metadata(intent_task),
        },
    )
    was_created = handoff_resp.get("created", True)
    followup_task = handoff_resp.get("task") or {}
    followup_id = followup_task.get("id")
    if not was_created:
        log(f"[Swarm] Lock conflict: reusing existing continuation {followup_id} for owner {owner_task_id}")
    if not followup_id:
        return {"ok": False, "error": "failed to create follow-up task"}

    try:
        dependents = _api_get_json(f"/api/tasks/{TASK_ID}/dependents").get("dependents", [])
    except Exception:
        dependents = []
    reparented: list[str] = []
    for dep in dependents:
        dep_id = dep.get("id")
        if not dep_id:
            continue
        try:
            dep_task = _api_get_json(f"/api/tasks/{dep_id}").get("task") or {}
            deps = list(dep_task.get("dependencies") or [])
            new_deps = [followup_id if d == TASK_ID else d for d in deps]
            _api_patch_json(f"/api/tasks/{dep_id}", {"dependencies": new_deps})
            reparented.append(dep_id)
        except Exception as exc:
            log(f"WARNING: failed to reparent dependent {dep_id} to {followup_id}: {exc}")

    try:
        current_meta = dict(TASK_METADATA or {})
        current_meta.update({
            "lock_conflict_handoff_to": followup_id,
            "lock_conflict_blocked_file": locked_path,
            "lock_conflict_blocked_by": owner_task_id,
            "lock_conflict_reparented_dependents": reparented,
        })
        _api_patch_json(f"/api/tasks/{TASK_ID}", {"metadata": current_meta})
    except Exception as exc:
        log(f"WARNING: failed to persist lock conflict metadata on {TASK_ID}: {exc}")

    LOCK_CONFLICT_HANDOFF = {
        "followup_task_id": followup_id,
        "blocked_file_path": locked_path,
        "blocked_by_task_id": owner_task_id,
        "reparented_dependents": reparented,
    }
    return {"ok": True, **LOCK_CONFLICT_HANDOFF}


def _task_tool_authority_error(tool: str, reason: str) -> dict:
    return {"ok": False, "error": f"{TASK_TYPE} task cannot use {tool}: {reason}"}


def _tool_authority_denial(tool: str, args: dict) -> dict | None:
    mutating_tools = {"write_file", "patch_file", "append_file", "git_commit", "git_push"}

    if TASK_TYPE in ("plan", "python_plan") and tool in mutating_tools:
        return _task_tool_authority_error(tool, "planning tasks are read-only")

    if tool == "delegate_helper" and TASK_TYPE not in {"feature", "bug", "refactor", "polish", "audit", "research", "triage"}:
        return _task_tool_authority_error(
            tool,
            "this task type does not support transient helper delegation",
        )

    if tool == "delegate_task_batch" and TASK_TYPE not in {"feature", "bug", "refactor", "polish"}:
        return _task_tool_authority_error(
            tool,
            "this task type does not support structured child-task delegation",
        )
    if tool == "delegate_task_batch" and TASK_METADATA.get("delegation_batch_id"):
        return _task_tool_authority_error(
            tool,
            "nested structured child-task delegation is disabled in the initial rollout",
        )

    if TASK_TYPE == "project_plan" and tool in (mutating_tools | {"run_command", "create_task", "create_tasks"}):
        return _task_tool_authority_error(
            tool,
            "project planners must inspect the repo and delegate through create_tasks_file_aware() only",
        )

    if TASK_TYPE in ("qa", "hybrid_qa", "harness_qa"):
        if tool in {"patch_file", "append_file", "git_commit", "git_push", "run_command", "create_task", "create_tasks_file_aware"}:
            return _task_tool_authority_error(
                tool,
                "QA tasks are read-only testers; file follow-ups should go through create_bug_task/requeue_self",
            )
        if tool == "write_file":
            report_path = _normalized_report_path(args.get("path", ""))
            if report_path != "QA_REPORT.md":
                return _task_tool_authority_error(tool, "QA tasks may only write QA_REPORT.md")

    if TASK_TYPE == "triage":
        if tool in {"patch_file", "append_file", "git_commit", "git_push", "create_task", "create_tasks_file_aware"}:
            return _task_tool_authority_error(
                tool,
                "triage tasks are read-only except for bug filing and the triage report",
            )
        if tool == "write_file":
            report_path = _normalized_report_path(args.get("path", ""))
            if report_path != "TRIAGE_REPORT.md":
                return _task_tool_authority_error(tool, "triage may only write TRIAGE_REPORT.md")

    if TASK_TYPE == "research":
        if tool in {"patch_file", "append_file", "git_push"}:
            return _task_tool_authority_error(tool, "research tasks may record findings but must not implement code changes")
        if tool == "write_file":
            report_path = _normalized_report_path(args.get("path", ""))
            if not report_path.startswith("research/") or not report_path.endswith(".md"):
                return _task_tool_authority_error(tool, "research findings must be written under research/*.md")

    if TASK_TYPE == "audit" and tool in mutating_tools:
        return _task_tool_authority_error(tool, "audit tasks may inspect and use API calls, but must not edit repo files or commit")

    if TASK_METADATA.get("is_recovery_task") and tool in {"create_task", "create_tasks_file_aware"}:
        return _task_tool_authority_error(
            tool,
            "recovery tasks must repair the branch directly or fail into canonical continuation; they cannot spawn arbitrary child work",
        )

    if (
        tool in {"write_file", "patch_file", "append_file"}
        and TASK_TYPE in {"feature", "bug", "refactor", "polish"}
        and RUN_BROADCAST_WRITE_COUNT <= 0
        and _has_active_sibling_tasks()
    ):
        path = _normalized_report_path(args.get("path", ""))
        return _task_tool_authority_error(
            tool,
            "active sibling tasks are running on this project; before your first edit, call "
            f"broadcast_write() with a one-line shared-file claim for '{path or 'the files you will touch'}'",
        )

    return None


def _load_task_metadata() -> dict:
    try:
        with _ur.urlopen(f"http://localhost:{API_PORT}/api/tasks", timeout=10) as resp:
            all_tasks = json.loads(resp.read()).get("tasks", [])
        task = next((t for t in all_tasks if t.get("id") == TASK_ID), None)
        return dict(task.get("metadata") or {}) if task else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Module sync helpers — propagate wrapper-set config to tool submodules
# (same pattern as _sync_qa_tools_globals)
# ---------------------------------------------------------------------------

def _sync_qa_tools_globals():
    """Sync config from agent_runtime.py to qa_tools.py module."""
    qa_tools.WORKSPACE = WORKSPACE
    qa_tools.DATA_DIR = DATA_DIR
    qa_tools.PROJECT = PROJECT
    qa_tools.PROJECT_PATH_OVERRIDE = PROJECT_PATH_OVERRIDE
    qa_tools.TASK_TYPE = TASK_TYPE
    qa_tools.API_PORT = API_PORT
    qa_tools.QA_CONFIG = QA_CONFIG
    qa_tools.QA_CYCLE = QA_CYCLE
    qa_tools.QA_MAX_CYCLES = QA_MAX_CYCLES
    qa_tools.mcp_client = mcp_client


def _sync_core_globals():
    """Sync config from agent_runtime.py to swarm.tools.core module and _shared config store."""
    import swarm.tools.core as _core
    from swarm.tools import _shared
    _core.WORKSPACE = WORKSPACE
    _core.DATA_DIR = DATA_DIR
    _core.PROJECT = PROJECT
    _core.PROJECT_PATH_OVERRIDE = PROJECT_PATH_OVERRIDE
    _core.WORKTREE_BRANCH = WORKTREE_BRANCH
    _core.TASK_TYPE = TASK_TYPE
    _core.TASK_ID = TASK_ID
    _core.TASK_PRIORITY = TASK_PRIORITY
    _core.MAX_LINES = MAX_LINES
    _core.IGNORE_DIRS = IGNORE_DIRS
    _core.IGNORE_EXTENSIONS = IGNORE_EXTENSIONS
    _core.MAX_TOOL_LOOPS = MAX_TOOL_LOOPS
    _core.API_PORT = API_PORT
    _core.MCP_SERVERS = MCP_SERVERS
    _core.MANAGED_PROJECTS = MANAGED_PROJECTS
    _core.READONLY = READONLY
    _core.mcp_client = mcp_client
    _shared.TASK_TYPE = TASK_TYPE
    _shared.WORKSPACE = WORKSPACE
    _shared.PROJECT = PROJECT
    _shared.PROJECT_PATH_OVERRIDE = PROJECT_PATH_OVERRIDE
    # Sync task tool config
    import swarm.tools.tasks as _tasks
    _tasks.PROJECT = PROJECT
    _tasks.TASK_TYPE = TASK_TYPE
    _tasks.TASK_ID = TASK_ID
    _tasks.TASK_PRIORITY = TASK_PRIORITY
    _tasks.API_PORT = API_PORT


def _sync_knowledge_globals():
    """Sync config from agent_runtime.py to swarm.tools.knowledge module."""
    import swarm.tools.knowledge as _knowledge
    _knowledge.WORKSPACE = WORKSPACE
    _knowledge.DATA_DIR = DATA_DIR
    _knowledge.PROJECT = PROJECT
    _knowledge.PROJECT_PATH_OVERRIDE = PROJECT_PATH_OVERRIDE
    _knowledge.TASK_ID = TASK_ID
    _knowledge.API_PORT = API_PORT
    _knowledge.READONLY = READONLY
    _knowledge.TASK_TYPE = TASK_TYPE


# ---------------------------------------------------------------------------
# Tool call parsing & validation
# ---------------------------------------------------------------------------

# Required args for each tool — used by validate_tool_call to catch malformed
# calls before executing. Maps tool name → list of required non-empty arg names.
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
# Tool dispatch
# ---------------------------------------------------------------------------

def execute_tool(tool_call: dict) -> dict:
    global RUN_BROADCAST_WRITE_COUNT
    tool = tool_call.get("tool", "")
    args = tool_call.get("args", {})
    log(f"Executing tool: {tool}")

    denied = _tool_authority_denial(tool, args)
    if denied:
        return denied

    if TASK_TYPE == "project_plan" and tool in {"create_task", "create_tasks"}:
        return {
            "ok": False,
            "error": (
                "project_plan must use create_tasks_file_aware() once with the full task list. "
                "Do not use create_task() or create_tasks() for project planning."
            ),
        }

    if tool in {"write_file", "patch_file", "append_file"}:
        rel_path = _normalized_project_file_path(args.get("path", ""))
        if rel_path and rel_path not in CLAIMED_FILE_PATHS:
            lock_result = _lock_project_file(args.get("path", ""))
            if not lock_result.get("ok"):
                owner = lock_result.get("task_id") or lock_result.get("locked_by")
                owner_text = f" by {owner}" if owner else ""
                handoff = {}
                if owner:
                    try:
                        handoff = _spawn_lock_conflict_handoff(rel_path, owner)
                    except Exception as exc:
                        log(f"WARNING: failed to create lock conflict handoff for {TASK_ID}: {exc}")
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
    # Agents use these to launch the game and read structured state after making
    # changes, giving them a lightweight sanity check without needing vision.
    _godot_project_file = (
        Path(PROJECT_PATH_OVERRIDE) if PROJECT_PATH_OVERRIDE else (WORKSPACE / PROJECT)
    ) / "project.godot"
    if _godot_project_file.exists() and not READONLY:
        dispatch.update({
            "launch_game":    lambda: launch_game_headless(args.get("project_path", str(WORKSPACE / PROJECT))),
            "get_game_state": lambda: qa_get_game_state(),
            "wait":           lambda: qa_wait(args.get("seconds", 1)),
            "kill_game":      lambda: qa_kill_game(),
        })

    # QA-only tools (vision-led game interaction)
    if TASK_TYPE in ("qa", "art_pass", "hybrid_qa"):
        dispatch.update({
            "focus_game":      lambda: qa_focus_game(),
            "position_window": lambda: qa_position_window(),
            "launch_game":     lambda: (
                harness_launch_game(
                    args.get("project_path", str(WORKSPACE / PROJECT)),
                    _parse_extra_args(args.get("extra_args")),
                )
                if TASK_TYPE == "hybrid_qa" and _project_supports_harness() and _parse_extra_args(args.get("extra_args"))
                else qa_launch_game(args.get("project_path", str(WORKSPACE / PROJECT)))
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
    if TASK_TYPE == "harness_qa" or (TASK_TYPE == "hybrid_qa" and _project_supports_harness()):
        dispatch.update({
            "harness_launch_game": lambda: harness_launch_game(
                str(WORKSPACE / PROJECT),
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
            RUN_BROADCAST_WRITE_COUNT += 1
        return result
    return {
        "ok": False,
        "error": f"Unknown tool: {tool}. Valid tools: {', '.join(dispatch.keys())}",
    }


# ---------------------------------------------------------------------------
# EXPERIMENT: Meta-investigation
# ---------------------------------------------------------------------------

def _run_meta_investigation(repeated_error: str, loop_history: list[str], task_desc: str) -> str:
    """Out-of-band LLM call that investigates a repeatedly-seen error.

    Gets a compact view of what the agent has tried, can read files and run
    commands itself, then returns a short hint to inject into the main loop.

    loop_history: list of (tool, args_summary, error_snippet) strings from
    recent loops where the error appeared.
    """
    # Prefer the real project path; fall back gracefully if override doesn't exist
    _override = Path(PROJECT_PATH_OVERRIDE) if PROJECT_PATH_OVERRIDE else None
    project_root = (_override if _override and _override.exists() else None) or (WORKSPACE / PROJECT)
    log(f"[Meta] project_root={project_root} (exists={project_root.exists()})")

    # Give the investigator a read_file and run_command it can use
    def _mini_read(path: str) -> str:
        try:
            p = Path(path) if Path(path).is_absolute() else project_root / path
            if not p.exists():
                return f"[not found: {p}]"
            return p.read_text(encoding="utf-8", errors="replace")[:3000]
        except Exception as e:
            return f"[read error: {e}]"

    def _mini_run(cmd: str) -> str:
        try:
            # On Windows use cmd /c explicitly to avoid shell=True's ambiguous quoting
            if sys.platform == "win32":
                args = ["cmd", "/c", cmd]
                r = subprocess.run(args, capture_output=True, timeout=20,
                                   encoding=sys.stdout.encoding or "utf-8", errors="replace")
            else:
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20)
            out = (r.stdout or "") + (r.stderr or "")
            return out[:2000] if out.strip() else "[no output]"
        except Exception as e:
            return f"[run error: {e}]"

    def _mini_list_dir(path: str, pattern: str = "**/*") -> str:
        """Python-native directory listing — no shell, works on all platforms."""
        try:
            p = Path(path) if Path(path).is_absolute() else project_root / path
            if not p.exists():
                return f"[not found: {p}]"
            entries = sorted(p.rglob(pattern) if "**" in pattern else p.glob(pattern))
            lines = [str(e.relative_to(p)) for e in entries[:200]]
            suffix = f"\n... ({len(entries) - 200} more)" if len(entries) > 200 else ""
            return "\n".join(lines) + suffix if lines else "[empty directory]"
        except Exception as e:
            return f"[list error: {e}]"

    # --- Seed context: last 30 lines of this agent's log ---
    _log_tail = ""
    try:
        _log_path = Path(DATA_DIR) / f"agent_{TASK_ID}.log"
        if _log_path.exists():
            _lines = _log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            _log_tail = "\n".join(_lines[-30:])
    except Exception:
        pass

    # --- Seed context: project file tree (always provided up-front) ---
    _dir_listing = _mini_list_dir(str(project_root))

    _is_windows = sys.platform == "win32"
    history_text = "\n".join(f"  - {h}" for h in loop_history[-8:])
    _cmd_note = (
        "\nIMPORTANT — this agent runs on Windows. For shell commands use simple "
        "cmd.exe syntax (no &&, no Unix pipes). Prefer [LIST_DIR] for directory "
        "listings and [READ_FILE] for file contents instead of shell commands.\n"
    ) if _is_windows else ""
    sys_prompt = (
        "You are a debugging investigator. An AI agent has been stuck on the same "
        "error for many loops. Your job is to figure out WHY the fix isn't working "
        "and produce a short, specific hint (2-5 sentences) to redirect the agent.\n\n"
        "You have access to three tools:\n"
        "[READ_FILE] <absolute_or_relative_path>\n"
        "[LIST_DIR] <absolute_or_relative_path>  — lists all files recursively (Python-native, always works)\n"
        "[RUN_CMD] <shell command>  — run a shell command\n"
        + _cmd_note +
        "\nUse them to probe the environment. Then write your hint starting with HINT:"
    )
    user_msg = (
        f"TASK: {task_desc[:500]}\n\n"
        f"REPEATED ERROR (seen 3+ times):\n{repeated_error}\n\n"
        f"WHAT THE AGENT HAS TRIED (recent loops):\n{history_text}\n\n"
        f"PROJECT ROOT: {project_root}\n"
        f"PROJECT FILE TREE:\n{_dir_listing[:3000]}\n\n"
        + (f"RECENT AGENT LOG (last 30 lines):\n{_log_tail}\n\n" if _log_tail else "")
        + "Investigate why the fix isn't working. Use [READ_FILE] to inspect specific files, "
        "[LIST_DIR] for subdirectories, [RUN_CMD] for commands. Then write your HINT."
    )

    # Use the designated investigation provider (default: claude) for better reasoning.
    # Falls back silently to the main provider if the key isn't available.
    _inv_provider = META_INVESTIGATION_PROVIDER or LLM_PROVIDER
    log(f"[Meta] using provider={_inv_provider}")

    conversation = [{"role": "user", "content": user_msg}]
    hint = ""

    for inv_loop in range(8):  # max 8 investigator loops
        response, _ = call_llm(sys_prompt, conversation, provider=_inv_provider)
        conversation.append({"role": "assistant", "content": response})

        tool_output_parts = []
        for m in re.finditer(r'\[READ_FILE\]\s*(.+)', response):
            path = m.group(1).strip()
            content = _mini_read(path)
            log(f"[Meta] inv-loop {inv_loop + 1} READ_FILE {path} → {len(content)} chars")
            tool_output_parts.append(f"[READ_FILE {path}]\n{content}")

        for m in re.finditer(r'\[LIST_DIR\]\s*(.+)', response):
            path = m.group(1).strip()
            listing = _mini_list_dir(path)
            log(f"[Meta] inv-loop {inv_loop + 1} LIST_DIR {path} → {len(listing)} chars")
            tool_output_parts.append(f"[LIST_DIR {path}]\n{listing}")

        for m in re.finditer(r'\[RUN_CMD\]\s*(.+)', response):
            cmd = m.group(1).strip()
            output = _mini_run(cmd)
            log(f"[Meta] inv-loop {inv_loop + 1} RUN_CMD {cmd[:80]} → {output[:120].strip()}")
            tool_output_parts.append(f"[RUN_CMD {cmd}]\n{output}")

        # Extract hint — accept HINT: label or any of these fallback prefixes
        for _marker in ("HINT:", "NOTE:", "DIAGNOSIS:", "The issue is", "The problem is", "The root cause"):
            if _marker in response:
                hint = response[response.index(_marker):response.index(_marker) + 800].strip()
                break

        if tool_output_parts and not hint:
            loops_used = inv_loop + 1
            loops_left = 8 - loops_used
            if loops_left <= 2:
                pressure = (
                    f"\n\n⚠️ FINAL {loops_left} LOOP(S) REMAINING out of 8. "
                    "Stop exploring — you have enough information. Write your HINT: now."
                )
            else:
                pressure = (
                    f"\n\n[Investigation loop {loops_used}/8 — {loops_left} remaining. "
                    "If you have enough to diagnose the issue, write your HINT: now instead of continuing.]"
                )
            conversation.append({"role": "user", "content": "\n\n".join(tool_output_parts) + pressure})
        else:
            # No more tool calls (or hint already found) — if still no hint, use last response
            if not hint and response.strip():
                hint = response.strip()[:800]
                log("[Meta] No HINT: marker — using full response as hint")
            break

    if hint:
        log(f"[Meta] Full hint:\n{hint}")
    else:
        log(f"[Meta] No hint produced after {inv_loop + 1} investigator loops")

    return hint or f"[Meta-investigator found no clear cause for repeated error: {repeated_error[:120]}]"


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------

def main() -> int:
    global system_prompt, user_prompt, mcp_client, TASK_METADATA, RUN_BROADCAST_WRITE_COUNT, CLAIMED_FILE_PATHS, LOCK_CONFLICT_HANDOFF

    TASK_METADATA = _load_task_metadata()
    RUN_BROADCAST_WRITE_COUNT = 0
    CLAIMED_FILE_PATHS = set()
    LOCK_CONFLICT_HANDOFF = None

    # Sync config to all tool modules
    _sync_core_globals()
    _sync_knowledge_globals()
    _sync_qa_tools_globals()

    # Ensure any Godot process launched by this agent is killed when we exit,
    # regardless of which return path is taken (TASK_COMPLETE, loop limit, crash).
    atexit.register(qa_tools.kill_game)
    atexit.register(qa_tools.harness_kill_game)


    # Guard: if a worktree path was specified but doesn't exist, fail immediately.
    # Without this, agents resolve relative paths against the dead override path,
    # silently create phantom directories, and produce untracked work.
    if PROJECT_PATH_OVERRIDE and not Path(PROJECT_PATH_OVERRIDE).exists():
        log(f"FATAL: worktree path does not exist: {PROJECT_PATH_OVERRIDE}")
        log("The worktree was likely cleaned up before this agent started. Failing fast.")
        return 1

    project_path = _project_root()

    # If this is a continuation task, load the progress file into context
    _progress_context = ""
    if TASK_DESC.startswith("CONTINUATION of task"):
        _prog_file = Path(project_path) / "_swarm_progress.md"
        if _prog_file.exists():
            try:
                _progress_context = _prog_file.read_text()[:3000]
                log("Loaded progress context from _swarm_progress.md")
            except Exception:
                pass

    # Load .env if present
    env_file = Path(WORKSPACE) / "swarm-controller" / ".env"
    if env_file.exists():
        for line in env_file.read_text().strip().splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                os.environ[key] = val

    # Init MCP if configured
    if MCP_SERVERS:
        mcp_client = MCPClient(MCP_SERVERS, cwd=project_path)
        log(f"MCP configured with servers: {list(MCP_SERVERS.keys())}")
        # Re-sync after mcp_client is set
        _sync_core_globals()
        _sync_qa_tools_globals()

    # Select prompts based on project type
    is_python = (
        os.path.exists(os.path.join(project_path, "requirements.txt"))
        or os.path.exists(os.path.join(project_path, "pyproject.toml"))
    )
    is_typescript = (
        os.path.exists(os.path.join(project_path, "package.json"))
        and (
            os.path.exists(os.path.join(project_path, "tsconfig.json"))
            or os.path.exists(os.path.join(project_path, "tsconfig.app.json"))
        )
    )

    if TASK_TYPE == "plan":
        system_prompt, user_prompt = PLAN_SYSTEM, PLAN_USER
    elif TASK_TYPE == "python_plan":
        system_prompt, user_prompt = PYTHON_PLAN_SYSTEM, PYTHON_PLAN_USER
    elif is_python:
        if TASK_TYPE == "feature":
            system_prompt, user_prompt = PYTHON_FEATURE_SYSTEM, PYTHON_FEATURE_USER
        elif TASK_TYPE == "bug":
            system_prompt, user_prompt = PYTHON_BUG_SYSTEM, PYTHON_BUG_USER
        elif TASK_TYPE == "refactor" and PYTHON_REFACTOR_SYSTEM:
            system_prompt, user_prompt = PYTHON_REFACTOR_SYSTEM, PYTHON_REFACTOR_USER
        else:
            # polish / any other type on a Python project: treat as feature
            system_prompt, user_prompt = PYTHON_FEATURE_SYSTEM, PYTHON_FEATURE_USER
    else:
        if TASK_TYPE == "manager":
            system_prompt, user_prompt = MANAGER_SYSTEM, MANAGER_USER
        elif TASK_TYPE == "project_create":
            system_prompt, user_prompt = PROJECT_CREATE_SYSTEM, PROJECT_CREATE_USER
        elif TASK_TYPE == "qa":
            system_prompt, user_prompt = QA_SYSTEM, QA_USER
        elif TASK_TYPE == "art_pass":
            system_prompt, user_prompt = ART_PASS_SYSTEM, ART_PASS_USER
        elif TASK_TYPE == "audit":
            system_prompt, user_prompt = AUDIT_SYSTEM, AUDIT_USER
        elif TASK_TYPE == "audit_learnings":
            system_prompt, user_prompt = AUDIT_LEARNINGS_SYSTEM, AUDIT_LEARNINGS_USER
        elif TASK_TYPE == "triage":
            system_prompt, user_prompt = TRIAGE_SYSTEM, TRIAGE_USER
        elif TASK_TYPE == "project_plan":
            system_prompt, user_prompt = PROJECT_PLAN_SYSTEM, PROJECT_PLAN_USER
        elif TASK_TYPE == "research":
            system_prompt, user_prompt = RESEARCH_SYSTEM, RESEARCH_USER
        elif TASK_TYPE == "harness_qa":
            system_prompt, user_prompt = HARNESS_QA_SYSTEM, HARNESS_QA_USER
        elif TASK_TYPE == "hybrid_qa":
            system_prompt, user_prompt = HYBRID_QA_SYSTEM, HYBRID_QA_USER
        elif TASK_TYPE == "feature":
            system_prompt, user_prompt = FEATURE_SYSTEM, FEATURE_USER
        elif TASK_TYPE == "bug":
            system_prompt, user_prompt = BUG_SYSTEM, BUG_USER
        elif TASK_TYPE == "polish":
            system_prompt, user_prompt = POLISH_SYSTEM, POLISH_USER
        else:
            system_prompt, user_prompt = FEATURE_SYSTEM, FEATURE_USER

    # Append progress context to system prompt for continuation tasks
    if _progress_context:
        system_prompt = system_prompt + f"\n\n## Previous Agent Progress\n{_progress_context}"

    # Sibling-coordination context: only for tasks that write shared files and can collide
    _COLLIDABLE_TASK_TYPES = {"feature", "bug", "polish", "art_pass", "refactor"}
    if TASK_TYPE in _COLLIDABLE_TASK_TYPES:
        _project_activity_context = _load_project_activity_context()
        if _project_activity_context:
            system_prompt = system_prompt + f"\n\n{_project_activity_context}"

    _delegation_hint_lines = []
    _desc_lower = (TASK_DESC or "").lower()
    if "delegate_helper" in _desc_lower:
        _delegation_hint_lines.append(
            "This task explicitly requires the normal swarm tool `delegate_helper(...)`. "
            "Call `delegate_helper` directly as a tool call. Do not use mcp_list_tools or mcp_call_tool to look for it."
        )
    if "delegate_task_batch" in _desc_lower:
        _delegation_hint_lines.append(
            "This task explicitly requires the normal swarm tool `delegate_task_batch(...)`. "
            "Call `delegate_task_batch` directly as a tool call. Do not treat it like an MCP server."
        )
    if _delegation_hint_lines:
        system_prompt = system_prompt + "\n\n## Delegation Tool Reminder\n" + "\n".join(
            f"- {line}" for line in _delegation_hint_lines
        )

    log(f"Starting task: {PROJECT} ({TASK_TYPE})")
    log(f"Description: {TASK_DESC}")

    # Pull latest (skip for virtual/qa tasks and worktree agents on their own branches)
    _skip_pull_types = {"manager", "project_create", "qa", "audit", "triage", "project_plan", "python_plan", "plan", "research", "harness_qa", "hybrid_qa"}
    if TASK_TYPE not in _skip_pull_types and not PROJECT_PATH_OVERRIDE:
        log("Pulling latest...")
        code, out, err = run("git pull origin main --ff-only")
        if code != 0:
            run("git branch --set-upstream-to=origin/main main")
            code2, out2, err2 = run("git pull origin main --ff-only")
            log("Git pull successful" if code2 == 0 else f"Git pull: {err2[:200]}")

    # Find largest .gd file (refactor context only)
    main_file = None
    max_file_lines = 0
    if TASK_TYPE == "refactor" and not is_python and not is_typescript:
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for f in files:
                if f.endswith(".gd"):
                    fp = os.path.join(root, f)
                    try:
                        lines = len(open(fp).readlines())
                        if lines > max_file_lines:
                            max_file_lines = lines
                            main_file = os.path.relpath(fp, project_path)
                    except Exception:
                        pass

        if main_file:
            log(f"Largest file: {main_file} ({max_file_lines} lines)")
            content = read_file(main_file)
            if content.get("ok"):
                log(f"Read {len(content.get('content', ''))} chars from {main_file}")

    # Build the legacy Godot refactor prompt only for Godot projects.
    if TASK_TYPE == "refactor" and not is_python and not is_typescript:
        system_prompt = f"""You are an expert Godot game developer. Use tool calls to modify the codebase — do not just describe changes.

Available tools (use [TOOL_CALL]{{"tool": "name", "args": {{...}}}}[/TOOL_CALL] format):
- list_files(path): List files and directories
- read_file(path, offset=0, limit=0): Read file contents (full file; use offset/limit in lines for large files)
- search_code(query): Search for a pattern across .gd files
- get_file_stats(path): Get line count and size
- write_file(path, content): Write content to a file
- run_command(command): Run a shell command
- git_commit(message): Stage all changes and commit
- git_push(): Push commits to remote

REFACTOR RULES — follow these exactly:

PHASE 1 — PLAN (if refactor.md does not exist, or all items are done):
1. Use grep -n and sed to read all oversized files and identify logical sections to extract (systems, managers, UI, constants, etc.). Note the approximate start/end line numbers for each section.
2. Write (or overwrite) refactor.md at the project root with a fresh checklist including line ranges:
   # Refactor Plan: Sprint <N>
   ## Files to reduce
   - [ ] scripts/foo_system.gd — lines 450-620 of source.gd (festival + event logic)
   - [ ] scripts/bar_manager.gd — lines 621-790 of source.gd (guild management)
   ...
3. git_commit("Refactor: create sprint <N> plan") then git_push().

PHASE 2 — EXECUTE (read refactor.md to find next unchecked item):
4. Read refactor.md to find the next unchecked `- [ ]` item.
5. Find the exact line numbers of the section to extract.
6. Write the new extracted file using write_file.
7. Delete those lines from the source file using sed in-place — do NOT rewrite the whole file:
   run_command: sed -i '<start>,<end>d' /path/to/source.gd
   This is mandatory. The source file MUST have fewer lines after every extraction.
8. Verify the source shrank: run_command: wc -l /path/to/source.gd
9. In refactor.md, change `- [ ]` to `- [x]` for the completed item.
10. git_commit with a message stating what was extracted and the before/after line count, then git_push().
11. Repeat from step 4 until ALL files are under {MAX_LINES} lines.

CRITICAL: Step 7 (sed -i delete) is not optional.
NEVER touch files in: {', '.join(sorted(IGNORE_DIRS))}.
NEVER modify files with extensions: {', '.join(sorted(IGNORE_EXTENSIONS))}.

Say TASK_COMPLETE only when every .gd file outside ignored dirs is under {MAX_LINES} lines and all commits are pushed."""

        # Check for existing refactor.md
        refactor_md_path = os.path.join(project_path, "refactor.md")
        if os.path.exists(refactor_md_path):
            with open(refactor_md_path) as f:
                refactor_md_content = f.read()
            if "- [ ]" in refactor_md_content:
                resume_context = (
                    f"\nrefactor.md exists with pending items — resume it:\n```\n{refactor_md_content[:3000]}\n```\n"
                    f"Find the next unchecked `- [ ]` item and execute it. Skip straight to Phase 2."
                )
            else:
                oversized = []
                for root, dirs, files in os.walk(project_path):
                    dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
                    for f in files:
                        if f.endswith(".gd"):
                            try:
                                lc = len(open(os.path.join(root, f)).readlines())
                                if lc > MAX_LINES:
                                    oversized.append((os.path.relpath(os.path.join(root, f), project_path), lc))
                            except Exception:
                                pass
                if oversized:
                    oversized_list = "\n".join(f"  - {p} ({l_cnt} lines)" for p, l_cnt in sorted(oversized, key=lambda x: -x[1]))
                    resume_context = (
                        f"\nrefactor.md exists but all items are checked off. However these files are still over {MAX_LINES} lines:\n{oversized_list}\n"
                        f"Previous plan for reference:\n```\n{refactor_md_content[:1500]}\n```\n"
                        f"Start Phase 1: write a new sprint plan in refactor.md covering these files, then execute it."
                    )
                else:
                    resume_context = (
                        f"\nrefactor.md exists and all items are checked off, and all files are under {MAX_LINES} lines.\n"
                        f"Say TASK_COMPLETE immediately."
                    )
        else:
            resume_context = "\nNo refactor.md found — start with Phase 1: analyse oversized files and write the plan."

        user_prompt = (
            f"Project: {PROJECT}\n"
            f"Task: reduce all .gd files to under {MAX_LINES} lines.\n"
            f"Project path: {project_path}\n"
            f"Largest file right now: {main_file} ({max_file_lines} lines)\n"
            f"{resume_context}\n\n"
            f"Work through the phases. After every extraction: shrink the source file, update refactor.md, commit, push."
        )

    # Tool loop
    conversation = [{"role": "user", "content": user_prompt}]
    tool_loop_count = 0

    # Track output from the last batch of run_command tool calls so we can
    # intercept TASK_COMPLETE when validation errors are still present.
    _last_run_outputs: list = []

    # Patterns in run_command stdout/stderr that indicate the task is NOT done
    _FAILURE_PATTERNS = [
        "SCRIPT ERROR:",
        "SCENE ERROR:",
        "Missing autoload:",
        " FAILED",       # GUT: "  2 FAILED"
        "tests failed",  # GUT summary line
        "ERROR: Parse Error",
        "error: ",       # generic compiler errors
    ]

    def _has_validation_failures(outputs: list) -> list[str]:
        """Return any failure lines found in recent run_command outputs."""
        hits = []
        for out in outputs:
            for line in out.splitlines():
                if any(pat in line for pat in _FAILURE_PATTERNS):
                    hits.append(line.strip())
        return hits

    context_limit_hit = False
    loop_limit_hit = False
    task_complete_hit = False
    lock_handoff_hit: dict | None = None
    _wrap_up_injected = False
    _no_tool_call_nudged = False
    _malformed_retries: dict = {}  # loop_count → retry attempts for malformed tool calls

    # Token tracking
    total_input_tokens = 0
    total_output_tokens = 0

    stall_detector = StallDetector()
    # EXPERIMENT: meta-investigation — track recurring error strings across loops
    _error_counts: _collections.Counter = _collections.Counter()
    _error_loop_history: dict[str, list[str]] = {}  # error_key → list of loop summaries
    _meta_investigated: set[str] = set()  # errors already investigated (don't repeat)

    while tool_loop_count < MAX_TOOL_LOOPS:
        log(f"Calling LLM... (loop {tool_loop_count + 1}/{MAX_TOOL_LOOPS})")
        compact_token_threshold = _get_compaction_threshold()

        if stall_detector.check() and not _wrap_up_injected:
            log("WARNING: stall detected — same tool called 3 times with identical args")
            conversation.append({"role": "user", "content": StallDetector.injected_message()})
        # EXPERIMENT: meta-investigation — fire when same error seen 3+ times
        if META_INVESTIGATION_ENABLED and not _wrap_up_injected:
            for err_key, count in _error_counts.items():
                if count >= 3 and err_key not in _meta_investigated:
                    _meta_investigated.add(err_key)
                    log(f"[Meta] Repeated error ({count}x): {err_key[:80]} — launching investigation")
                    history = _error_loop_history.get(err_key, [])
                    try:
                        hint = _run_meta_investigation(err_key, history, TASK_DESC)
                        log("[Meta] Investigation complete")
                        conversation.append({"role": "user", "content": (
                            f"[INVESTIGATOR NOTE — out-of-band analysis of your repeated error]\n\n"
                            f"{hint}\n\n"
                            "Take this into account before your next tool call."
                        )})
                    except Exception as e:
                        log(f"[Meta] Investigation failed: {e}")
                    break  # one investigation per loop tick

        # Wrap-up nudge at loop 110 for QA and bug tasks
        if (TASK_TYPE in ("qa", "bug")
                and not _wrap_up_injected
                and tool_loop_count >= MAX_TOOL_LOOPS - 10):
            loops_left = MAX_TOOL_LOOPS - tool_loop_count
            if TASK_TYPE == "qa":
                wrap_up_msg = (
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
            else:  # bug
                wrap_up_msg = (
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
            conversation.append({"role": "user", "content": wrap_up_msg})
            _wrap_up_injected = True

        system_with_budget = f"[Loop {tool_loop_count + 1}/{MAX_TOOL_LOOPS}]\n" + system_prompt
        response, tokens = call_llm(system_with_budget, conversation)
        total_input_tokens += tokens.get("input", 0)
        total_output_tokens += tokens.get("output", 0)
        log(f"[LLM] in={tokens['input']} out={tokens['output']}")
        # Write live token counts so dashboard can display them without waiting for agent exit
        try:
            _tok_file = Path(DATA_DIR) / f"agent_{TASK_ID}_tokens.json"
            _conv_est = sum(
                len(m["content"]) if isinstance(m["content"], str) else len(str(m["content"]))
                for m in conversation
            ) // 2
            _tok_file.write_text(json.dumps({
                "input": total_input_tokens,
                "output": total_output_tokens,
                "total": total_input_tokens + total_output_tokens,
                "conv_estimate": _conv_est,
                "compact_threshold": compact_token_threshold,
            }))
        except Exception:
            pass

        # Detect context window exceeded — spawn continuation task and exit cleanly
        if "context window exceeds limit" in response or (
            "invalid_request_error" in response and "context" in response.lower()
        ):
            log("WARNING: context window limit hit — spawning continuation task")
            context_limit_hit = True
            break

        log(f"LLM response: {response[:3000]}{'...' if len(response) > 3000 else ''}")
        conversation.append({"role": "assistant", "content": response})

        # Strip tool call blocks before checking for TASK_COMPLETE so that
        # tool arguments containing "TASK_COMPLETE" don't trigger a false early exit.
        _response_sans_tools = re.sub(
            r'\[TOOL_CALL\].*?\[/TOOL_CALL\]', '', response, flags=re.DOTALL
        )
        _response_sans_tools = re.sub(
            r'<tool_call>.*?</tool_call>', '', _response_sans_tools, flags=re.DOTALL
        )
        _has_task_complete = "TASK_COMPLETE" in _response_sans_tools

        tool_calls = parse_tool_calls(response)

        # Validate tool calls before executing — catch missing required args
        # and inject a targeted correction WITHOUT burning a loop counter slot.
        # Capped at 2 retries per loop position to prevent infinite correction loops.
        if tool_calls:
            _errors = [validate_tool_call(tc) for tc in tool_calls]
            _bad = [(tc, err) for tc, err in zip(tool_calls, _errors) if err]
            if _bad:
                _retries = _malformed_retries.get(tool_loop_count, 0)
                if _retries < 2:
                    _malformed_retries[tool_loop_count] = _retries + 1
                    _corrections = "\n".join(f"- {err}" for _, err in _bad)
                    log("WARNING: malformed tool call(s), injecting correction (retry {_retries + 1}/2)")
                    conversation.append({"role": "user", "content": (
                        f"Fix these tool call errors and try again:\n{_corrections}"
                    )})
                    # Don't increment tool_loop_count — this retry is free
                    continue
                else:
                    log("WARNING: malformed tool call(s) after 2 retries — executing anyway")

        if not tool_calls:
            has_open = "[TOOL_CALL]" in response or "<tool_call>" in response
            has_close = "[/TOOL_CALL]" in response or "</tool_call>" in response
            if has_open and not has_close:
                log("WARNING: response truncated — injecting targeted retry")
                conversation.append({"role": "user", "content": (
                    "Your response was cut off before the tool call closed. "
                    "Write a shorter tool call — avoid large write_file blocks. "
                    "Try again with a concise tool call."
                )})
                tool_loop_count += 1
                continue
            if has_open and has_close:
                log("WARNING: tool call tags present but JSON invalid — asking to retry")
                conversation.append({"role": "user", "content": (
                    "Your tool call could not be parsed. "
                    "Use exactly this format with valid JSON:\n"
                    '[TOOL_CALL]{"tool": "tool_name", "args": {"key": "value"}}[/TOOL_CALL]'
                )})
                tool_loop_count += 1
                continue
            if "write_file" in response.lower():
                log("write_file mentioned but not parsed — asking to retry")
                conversation.append({"role": "user", "content": "Please write smaller files (under 40 lines) one at a time."})
                tool_loop_count += 1
                continue
            # No tool calls — check for TASK_COMPLETE before nudging
            if _has_task_complete:
                failures = _has_validation_failures(_last_run_outputs)
                if failures:
                    log(f"TASK_COMPLETE blocked — validation failures still present: {failures[:3]}")
                    feedback = (
                        "TASK_COMPLETE rejected. Your most recent command output still contains errors "
                        "that must be fixed before the task can be marked complete:\n\n"
                        + "\n".join(f"  {line}" for line in failures[:10])
                        + "\n\nFix all errors, re-run validation, and only say TASK_COMPLETE "
                        "when every check passes cleanly. Use the smallest targeted validation first, "
                        "then broader validation. Do not rely on grep/tail-only output as final proof."
                    )
                    conversation.append({"role": "user", "content": feedback})
                    _last_run_outputs = []
                    tool_loop_count += 1
                    continue
                log("Task marked complete by LLM")
                task_complete_hit = True
                break
            # No tool calls and no TASK_COMPLETE — give it one nudge.
            if not _no_tool_call_nudged:
                _no_tool_call_nudged = True
                log("WARNING: no tool calls and no TASK_COMPLETE — nudging model to finish")
                conversation.append({"role": "user", "content": (
                    "Your response contained no tool calls and no TASK_COMPLETE. "
                    "If you have finished your work, output TASK_COMPLETE on its own line now. "
                    "If you still have work to do, use a tool call to continue. "
                    "For bug tasks, prefer: reproduce -> smallest fix -> targeted verification -> broader verification."
                )})
                tool_loop_count += 1
                continue
            log("No valid tool calls found after nudge — marking task failed")
            break

        tool_results = []
        _last_run_outputs = []   # reset each loop; only keep the latest batch
        _no_tool_call_nudged = False  # reset — model is back to using tools
        for tc in tool_calls:
            log(f"Tool call: {tc}")
            result = execute_tool(tc)
            log(f"Result: {str(result)[:200]}")
            tool_results.append(f"Tool {tc.get('tool', '?')}: {json.dumps(result)}")
            if isinstance(result, dict) and result.get("lock_conflict_handoff_created"):
                lock_handoff_hit = result
                break
            # Accumulate run_command outputs for the TASK_COMPLETE guard
            if tc.get("tool") == "run_command" and isinstance(result, dict):
                combined = (result.get("stdout") or "") + (result.get("stderr") or "")
                if combined.strip():
                    _last_run_outputs.append(combined)
                    # EXPERIMENT: track recurring errors for meta-investigation
                    if META_INVESTIGATION_ENABLED:
                        for line in combined.splitlines():
                            line = line.strip()
                            if any(pat in line for pat in _FAILURE_PATTERNS) and len(line) > 10:
                                # Normalise: strip file paths so same error from diff files merges
                                err_key = re.sub(r'res://\S+|C:\\\\[^\s]+|/[\w/.]+\.gd', '<file>', line)[:120]
                                _error_counts[err_key] += 1
                                summary = f"loop {tool_loop_count + 1}: {tc.get('args', {}).get('command', '')[:60]} → {line[:80]}"
                                _error_loop_history.setdefault(err_key, []).append(summary)

        # Stall detection: record single tool call for stall tracking
        if len(tool_calls) == 1:
            tc = tool_calls[0]
            stall_detector.append((tc.get("tool", ""), json.dumps(tc.get("args", {}), sort_keys=True)))

        conversation.append({"role": "user", "content": "\n".join(tool_results)})

        if lock_handoff_hit:
            log(
                "Lock conflict handoff created — stopping current task after sequencing "
                f"follow-up {lock_handoff_hit.get('followup_task_id')} behind "
                f"{lock_handoff_hit.get('blocked_by_task_id')}"
            )
            task_complete_hit = True
            break

        # Check TASK_COMPLETE after executing tools (not before), so tool calls
        # that appear in the same response as TASK_COMPLETE still run.
        if _has_task_complete:
            failures = _has_validation_failures(_last_run_outputs)
            if failures:
                log(f"TASK_COMPLETE blocked — validation failures still present: {failures[:3]}")
                feedback = (
                    "TASK_COMPLETE rejected. Your most recent command output still contains errors "
                    "that must be fixed before the task can be marked complete:\n\n"
                    + "\n".join(f"  {line}" for line in failures[:10])
                    + "\n\nFix all errors, re-run validation, and only say TASK_COMPLETE "
                    "when every check passes cleanly. Use the smallest targeted validation first, "
                    "then broader validation. Do not rely on grep/tail-only output as final proof."
                )
                conversation.append({"role": "user", "content": feedback})
                _last_run_outputs = []
                tool_loop_count += 1
                continue
            log("Task marked complete by LLM")
            task_complete_hit = True
            break

        # Context compaction: when conversation grows large, summarise the middle
        conversation = compact_conversation(
            conversation, system_prompt, compact_token_threshold, log
        )

    if tool_loop_count >= MAX_TOOL_LOOPS and not context_limit_hit:
        loop_limit_hit = True
        task_complete_hit = True  # loop limit counts as a success exit (continuation spawned)

    # If context limit was hit, commit progress then spawn a continuation task.
    if context_limit_hit:
        try:
            code, out, _ = run("git status --porcelain")
            if code == 0 and out.strip():
                git_commit(f"WIP: partial progress on {TASK_ID} (context limit)")
                git_push()
        except Exception as e:
            log(f"WARNING: WIP commit failed: {e}")
        import json as _json
        is_continuation = TASK_DESC.startswith("CONTINUATION of task")
        _project_unmanaged = bool(MANAGED_PROJECTS) and PROJECT not in MANAGED_PROJECTS
        if is_continuation:
            log("Context limit hit on a continuation task — stopping chain, marking done")
            _unlock_claimed_files()
            print(_json.dumps({"status": "success", "project": PROJECT, "task_id": TASK_ID, "note": "context_limit_continuation_end"}))
            return 0
        if _project_unmanaged:
            log(f"Context limit hit but {PROJECT} is not in managed_projects — skipping continuation spawn")
            _unlock_claimed_files()
            print(_json.dumps({"status": "success", "project": PROJECT, "task_id": TASK_ID, "note": "context_limit_unmanaged_no_continuation"}))
            return 0
        try:
            git_context_parts = []
            try:
                proj_root = _project_root()
                _, diff_stat, _ = run("git diff HEAD~5..HEAD --stat")
                if not diff_stat.strip():
                    _, diff_stat, _ = run("git diff --stat")
                if diff_stat.strip():
                    git_context_parts.append(f"### Files changed (git diff stat):\n{diff_stat.strip()[:1500]}")
                _, git_log, _ = run("git log --oneline -8")
                if git_log.strip():
                    git_context_parts.append(f"### Recent commits:\n{git_log.strip()}")
                _, staged, _ = run("git diff --cached --stat")
                if staged.strip():
                    git_context_parts.append(f"### Staged but uncommitted:\n{staged.strip()[:500]}")
            except Exception as e:
                log(f"WARNING: git context gathering failed: {e}")
            git_context = "\n\n".join(git_context_parts)

            recent_assistant = [m["content"] for m in conversation if m.get("role") == "assistant" and m.get("content")]
            last_thoughts = "\n---\n".join(c[:600] for c in recent_assistant[-3:]) if recent_assistant else ""

            try:
                progress_file = Path(proj_root) / "_swarm_progress.md"
                all_assistant = [m["content"] for m in conversation if m.get("role") == "assistant" and m.get("content")]
                progress_content = (
                    f"# Swarm Progress — {TASK_ID}\n\n"
                    f"## Original Task\n{TASK_DESC[:1000]}\n\n"
                    f"## Git Evidence\n{git_context}\n\n"
                    f"## What Was Done (agent turns)\n"
                    + "\n\n---\n\n".join(f"**Turn {i+1}:**\n{c[:600]}" for i, c in enumerate(all_assistant[-10:]))
                )
                progress_file.write_text(progress_content)
                log(f"Progress saved to {progress_file}")
            except Exception as e:
                log(f"WARNING: could not write progress file: {e}")

            cont_desc = (
                f"CONTINUATION of task {TASK_ID} (hit context limit mid-task).\n\n"
                f"## Original task\n{TASK_DESC[:800]}\n\n"
                f"## What the previous agent did\n{git_context}\n\n"
                f"## Where it left off\n{last_thoughts}\n\n"
                f"## How to continue\n"
                f"1. Read _swarm_progress.md in the project root for the full agent turn history.\n"
                f"2. Run the appropriate validation (e.g. `godot --headless --script res://check_scripts.gd` or `python -m py_compile`) to see current error state.\n"
                f"3. Review `git diff HEAD~3..HEAD` to understand what has already been changed.\n"
                f"4. Continue fixing what remains — do NOT redo work already committed.\n"
            )
            cont_meta: dict = {}
            if PROJECT_PATH_OVERRIDE:
                cont_meta["worktree_path"] = PROJECT_PATH_OVERRIDE
                if WORKTREE_BRANCH:
                    cont_meta["worktree_branch"] = WORKTREE_BRANCH
                    cont_meta["worktree_inherited"] = True
            payload = _json.dumps({
                "project": PROJECT,
                "type": TASK_TYPE,
                "description": cont_desc,
                "priority": TASK_PRIORITY,
                "max_attempts": 5,
                "dependencies": [TASK_ID],
                **({"metadata": cont_meta} if cont_meta else {}),
            }).encode()
            req = _ur.Request(
                f"http://localhost:{API_PORT}/api/tasks",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _ur.urlopen(req, timeout=10) as resp:
                result = _json.loads(resp.read())
                new_id = result.get("task", {}).get("id", "?")
                log(f"Continuation task created: {new_id}" + (f" (inheriting worktree {PROJECT_PATH_OVERRIDE})" if PROJECT_PATH_OVERRIDE else ""))
        except Exception as e:
            log(f"WARNING: failed to spawn continuation task: {e}")
        log("Context limit — exiting after partial progress")
        _unlock_claimed_files()
        print(_json.dumps({"status": "success", "project": PROJECT, "task_id": TASK_ID, "note": "context_limit_continuation_spawned"}))
        return 0

    # ---------------------------------------------------------------------------
    # Post-completion graph reflection loop
    # Runs outside the main tool budget — does NOT count toward MAX_TOOL_LOOPS.
    # Allowed tools: list_tasks + the 5 adaptive graph mutators only.
    # Cap: 40 calls. Nudge at 35 to wrap up.
    # Skipped for read-only, qa, manager, project_create, audit, triage, project_plan.
    # ---------------------------------------------------------------------------
    _REFLECTION_SKIP_TYPES = {"manager", "project_create", "qa", "audit", "triage", "project_plan", "harness_qa"}
    _REFLECTION_ALLOWED_TOOLS = {
        "list_tasks", "annotate_downstream_tasks", "split_task",
        "prune_task", "insert_dependency", "set_task_complexity",
    }
    _REFLECTION_MAX = 40
    _REFLECTION_NUDGE_AT = 35

    if task_complete_hit and not loop_limit_hit and not context_limit_hit \
            and not READONLY and TASK_TYPE not in _REFLECTION_SKIP_TYPES:
        try:
            # Gather context: diff stat from the just-completed work
            _ref_diff = ""
            try:
                _, _ref_diff, _ = run("git diff HEAD~1..HEAD --stat")
                if not _ref_diff.strip():
                    _, _ref_diff, _ = run("git diff --stat")
            except Exception:
                pass

            _ref_system = (
                "You are in the GRAPH REFLECTION phase. Your implementation work is done.\n"
                "Your job now is to review the downstream task queue and improve it based on what you just learned.\n\n"
                "TOOL CALL FORMAT — you MUST use this exact format for every tool call:\n"
                "[TOOL_CALL]{\"tool\": \"tool_name\", \"args\": {\"key\": \"value\"}}[/TOOL_CALL]\n\n"
                "ALLOWED TOOLS (ONLY these — no file reads, no writes, no git):\n"
                "- list_tasks(project): list all tasks for the project with their IDs and status\n"
                "- annotate_downstream_tasks(findings, task_ids): prepend context to downstream pending tasks\n"
                "- split_task(task_id, replacement_tasks): replace a pending downstream task with smaller pieces\n"
                "- prune_task(task_id, reason): mark a downstream task completed if your work made it redundant\n"
                "- insert_dependency(from_task_id, to_task_id): add an ordering constraint between two pending tasks\n"
                "- set_task_complexity(task_id, complexity, reason): tag a task 'simple' or 'complex'\n\n"
                "WORKFLOW:\n"
                "1. Call list_tasks() to see all tasks for the project (pending, in_progress, completed)\n"
                "2. Reflect on what you built and what that means for pending tasks\n"
                "3. Make only changes that are genuinely useful — do nothing if the plan looks correct\n"
                "4. When done, output REFLECTION_COMPLETE on its own line\n\n"
                "IMPORTANT — how annotate_downstream_tasks works:\n"
                "- It only annotates tasks that are BOTH pending AND transitively depend on YOUR task\n"
                "- Most tasks in list_tasks() are parallel branches — they will NOT be annotated even if pending\n"
                "- If it returns annotated=0, that means none of your direct dependents are pending (they may\n"
                "  already be in_progress or there are no downstream tasks). Do NOT retry — just finish.\n"
                "- Call it ONCE with your findings. If it annotated 0, accept that and output REFLECTION_COMPLETE.\n\n"
                "Do NOT: read files, write files, run commands, commit, or do any implementation work.\n"
                "Do NOT: annotate every task — only ones where you have specific, concrete information to add."
            )

            _ref_user_seed = (
                f"Task just completed: {TASK_DESC[:600]}\n\n"
                + (f"Files changed:\n{_ref_diff.strip()[:800]}\n\n" if _ref_diff.strip() else "")
                + f"Project: {PROJECT}\n\n"
                "Now call list_tasks() to see the downstream queue, then decide what (if anything) to change."
            )

            _ref_conv = [
                {"role": "user", "content": _ref_user_seed},
            ]
            _ref_loop = 0
            _ref_nudge_injected = False

            log("[Reflection] Starting post-completion graph reflection loop")

            while _ref_loop < _REFLECTION_MAX:
                # Nudge at 35
                if _ref_loop >= _REFLECTION_NUDGE_AT and not _ref_nudge_injected:
                    _ref_conv.append({
                        "role": "user",
                        "content": (
                            f"You have used {_ref_loop} of {_REFLECTION_MAX} reflection calls. "
                            "Finish up now. If you have any remaining tool calls to make, make them, "
                            "then output REFLECTION_COMPLETE."
                        ),
                    })
                    _ref_nudge_injected = True

                _ref_resp, _ref_tokens = call_llm(_ref_system, _ref_conv)
                total_input_tokens += _ref_tokens.get("input", 0)
                total_output_tokens += _ref_tokens.get("output", 0)

                _ref_conv.append({"role": "assistant", "content": _ref_resp})

                if "REFLECTION_COMPLETE" in _ref_resp:
                    log(f"[Reflection] Complete after {_ref_loop + 1} calls")
                    break

                _ref_tool_calls = parse_tool_calls(_ref_resp)

                # Filter to allowed tools only — silently drop disallowed calls
                _ref_allowed = [tc for tc in _ref_tool_calls if tc.get("tool") in _REFLECTION_ALLOWED_TOOLS]
                _ref_blocked = [tc.get("tool") for tc in _ref_tool_calls if tc.get("tool") not in _REFLECTION_ALLOWED_TOOLS]
                if _ref_blocked:
                    log(f"[Reflection] Blocked disallowed tool calls: {_ref_blocked}")

                if not _ref_allowed and not _ref_tool_calls:
                    # No tool calls — nudge once then bail
                    _ref_conv.append({
                        "role": "user",
                        "content": "No tool calls detected. If you are done, output REFLECTION_COMPLETE.",
                    })
                    _ref_loop += 1
                    continue

                if not _ref_allowed:
                    # All calls were blocked
                    _ref_conv.append({
                        "role": "user",
                        "content": (
                            f"Those tools are not available in reflection mode: {_ref_blocked}. "
                            "Use only: list_tasks, annotate_downstream_tasks, split_task, "
                            "prune_task, insert_dependency, set_task_complexity. "
                            "If you have nothing more to do, output REFLECTION_COMPLETE."
                        ),
                    })
                    _ref_loop += 1
                    continue

                _ref_results = []
                for tc in _ref_allowed:
                    result = execute_tool(tc)
                    _ref_results.append(f"[{tc['tool']}] → {json.dumps(result)[:8000]}")
                    log(f"[Reflection] Tool {tc['tool']} → {str(result)[:200]}")

                _ref_conv.append({
                    "role": "user",
                    "content": "\n".join(_ref_results),
                })
                _ref_loop += 1

            if _ref_loop >= _REFLECTION_MAX:
                log(f"[Reflection] Hit call cap ({_REFLECTION_MAX}) — exiting")

        except Exception as _ref_exc:
            log(f"[Reflection] ERROR (non-fatal): {_ref_exc}")

    # Auto-commit any remaining uncommitted changes
    if TASK_TYPE in ("manager", "project_create", "qa", "audit", "triage", "project_plan"):
        pass  # No git commit needed for these task types
    elif READONLY:
        log("Read-only task — skipping auto-commit")
    else:
        try:
            code, out, err = run("git status --porcelain")
            if code == 0 and out.strip():
                changed = [line[3:].strip() for line in out.strip().splitlines() if line.strip()]
                new_files = [f for f in changed if not f.endswith(".md")]
                if new_files:
                    names = ", ".join(os.path.basename(f) for f in new_files[:4])
                    if len(new_files) > 4:
                        names += f" (+{len(new_files) - 4} more)"
                    auto_msg = f"Refactor: update {names}"
                else:
                    auto_msg = f"Refactor: update plan for {PROJECT}"
                result = git_commit(auto_msg)
                if result.get("ok"):
                    git_push()
        except Exception as e:
            log(f"WARNING: auto-commit failed (non-fatal): {e}")

    # If the loop limit was hit without TASK_COMPLETE, spawn a continuation task
    if loop_limit_hit and TASK_TYPE not in ("qa", "manager", "project_create", "audit", "triage", "project_plan") and not READONLY:
        is_continuation = TASK_DESC.startswith("CONTINUATION of task")
        _project_unmanaged = bool(MANAGED_PROJECTS) and PROJECT not in MANAGED_PROJECTS
        if is_continuation:
            log("Loop limit hit on a continuation task — stopping chain, marking done")
        elif _project_unmanaged:
            log(f"Loop limit hit but {PROJECT} is not in managed_projects — skipping continuation spawn")
        else:
            try:
                recent = conversation[-6:] if len(conversation) >= 6 else conversation
                summary_parts = [
                    msg["content"][:400]
                    for msg in recent
                    if msg.get("role") == "assistant" and msg.get("content")
                ]
                remaining_context = "\n---\n".join(summary_parts[-2:]) if summary_parts else ""
                progress_path = None
                try:
                    proj_root = _project_root()
                    progress_file = Path(proj_root) / "_swarm_progress.md"
                    all_assistant = [m["content"] for m in conversation if m.get("role") == "assistant" and m.get("content")]
                    progress_content = (
                        f"# Swarm Progress — {TASK_ID}\n\n"
                        f"## Original Task\n{TASK_DESC[:1000]}\n\n"
                        f"## What Was Done\n"
                        + "\n\n---\n\n".join(f"**Turn {i+1}:**\n{c[:600]}" for i, c in enumerate(all_assistant[-8:]))
                    )
                    progress_file.write_text(progress_content)
                    progress_path = str(progress_file)
                    log(f"Progress saved to {progress_path}")
                except Exception as e:
                    log(f"WARNING: could not write progress file: {e}")
                cont_desc = (
                    f"CONTINUATION of task {TASK_ID} (hit loop limit mid-task).\n\n"
                    f"Original task: {TASK_DESC[:500]}\n\n"
                    f"The previous agent ran out of loops. It committed partial progress. "
                    f"Read _swarm_progress.md in the project root for full context on what was done.\n"
                    f"Then run the validation check to see what still needs fixing and continue.\n\n"
                    f"Last agent progress:\n{remaining_context}"
                )
                loop_cont_meta: dict = {}
                if PROJECT_PATH_OVERRIDE:
                    loop_cont_meta["worktree_path"] = PROJECT_PATH_OVERRIDE
                    if WORKTREE_BRANCH:
                        loop_cont_meta["worktree_branch"] = WORKTREE_BRANCH
                        loop_cont_meta["worktree_inherited"] = True
                payload = json.dumps({
                    "project": PROJECT,
                    "type": TASK_TYPE,
                    "description": cont_desc,
                    "priority": TASK_PRIORITY,
                    "max_attempts": 5,
                    **({"metadata": loop_cont_meta} if loop_cont_meta else {}),
                }).encode()
                req = _ur.Request(
                    f"http://localhost:{API_PORT}/api/tasks",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with _ur.urlopen(req, timeout=10) as resp:
                    result = json.loads(resp.read())
                    new_id = result.get("task", {}).get("id", "?")
                    log(f"Continuation task created: {new_id}" + (f" (inheriting worktree {PROJECT_PATH_OVERRIDE})" if PROJECT_PATH_OVERRIDE else ""))
            except Exception as e:
                log(f"WARNING: failed to spawn continuation task: {e}")

    # Write token usage to file for orchestrator to pick up
    try:
        token_data = {
            "input": total_input_tokens,
            "output": total_output_tokens,
            "total": total_input_tokens + total_output_tokens,
        }
        token_file = Path(DATA_DIR) / f"agent_{TASK_ID}_tokens.json"
        token_file.write_text(json.dumps(token_data))
    except Exception as e:
        log(f"WARNING: failed to write token file: {e}")
        token_data = {"input": 0, "output": 0, "total": 0}

    if task_complete_hit:
        log("Task complete!")
        _unlock_claimed_files()
        print(json.dumps({"status": "success", "project": PROJECT, "task_id": TASK_ID,
                          "input_tokens": total_input_tokens, "output_tokens": total_output_tokens}))
        return 0
    else:
        log("Task ended without TASK_COMPLETE — marking as failed")
        _unlock_claimed_files()
        print(json.dumps({"status": "failed", "project": PROJECT, "task_id": TASK_ID,
                          "note": "no_task_complete",
                          "input_tokens": total_input_tokens, "output_tokens": total_output_tokens}))
        return 1
