"""
swarm.runtime_helpers -- path normalization, file locking, API helpers,
                         project activity context, and lock-conflict handoff.

Extracted from agent_runtime.py. All functions read config from
agent_runtime at call-time via lazy import to avoid capturing stale values.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.request as _ur
from pathlib import Path


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _normalized_report_path(path: str) -> str:
    return (path or "").strip().replace("\\", "/").lstrip("./")


def _normalized_project_file_path(path: str) -> str:
    import swarm.agent_runtime as _rt
    from swarm.tools._shared import _project_root
    raw = (path or "").strip()
    if not raw:
        return ""
    try:
        project_root = Path(_project_root())
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


# ---------------------------------------------------------------------------
# Project activity context (for sibling-task coordination)
# ---------------------------------------------------------------------------

def _load_project_activity_context(limit: int = 8) -> str:
    import swarm.agent_runtime as _rt
    if not _rt.PROJECT:
        return ""
    try:
        db_path = Path(_rt.DATA_DIR) / "swarm.db"
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
                (_rt.PROJECT, _rt.TASK_ID, limit),
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
    import swarm.agent_runtime as _rt
    if not _rt.PROJECT or not _rt.TASK_ID:
        return False
    try:
        db_path = Path(_rt.DATA_DIR) / "swarm.db"
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
                (_rt.PROJECT, _rt.TASK_ID),
            ).fetchone()
        finally:
            conn.close()
        return row is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# File locking via API
# ---------------------------------------------------------------------------

def _lock_project_file(path: str) -> dict:
    import swarm.agent_runtime as _rt
    rel_path = _normalized_project_file_path(path)
    if not _rt.PROJECT or not _rt.TASK_ID or not rel_path:
        return {"ok": False, "error": "cannot lock empty file path"}
    try:
        payload = json.dumps({
            "file_path": rel_path,
            "agent_id": _rt.TASK_ID,
            "task_id": _rt.TASK_ID,
        }).encode()
        req = _ur.Request(
            f"http://localhost:{_rt.API_PORT}/api/projects/{_rt.PROJECT}/lock",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get("success"):
            _rt.CLAIMED_FILE_PATHS.add(rel_path)
            return {"ok": True, "file_path": rel_path}
        lock = result.get("lock") or {}
        owner = lock.get("task_id") or lock.get("locked_by")
        if owner and owner != _rt.TASK_ID:
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
        with _ur.urlopen(f"http://localhost:{_rt.API_PORT}/api/projects/{_rt.PROJECT}/locks", timeout=10) as resp:
            locks = json.loads(resp.read()).get("locks", {})
        lock = locks.get(rel_path)
        if lock and lock.get("locked_by") != _rt.TASK_ID:
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
    import swarm.agent_runtime as _rt
    if not _rt.PROJECT or not _rt.TASK_ID or not _rt.CLAIMED_FILE_PATHS:
        return
    for rel_path in list(_rt.CLAIMED_FILE_PATHS):
        try:
            payload = json.dumps({
                "file_path": rel_path,
                "agent_id": _rt.TASK_ID,
            }).encode()
            req = _ur.Request(
                f"http://localhost:{_rt.API_PORT}/api/projects/{_rt.PROJECT}/unlock",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _ur.urlopen(req, timeout=10):
                pass
        except Exception:
            pass
        finally:
            _rt.CLAIMED_FILE_PATHS.discard(rel_path)


# ---------------------------------------------------------------------------
# Low-level API helpers
# ---------------------------------------------------------------------------

def _api_get_json(path: str) -> dict:
    import swarm.agent_runtime as _rt
    with _ur.urlopen(f"http://localhost:{_rt.API_PORT}{path}", timeout=10) as resp:
        return json.loads(resp.read())


def _api_patch_json(path: str, payload: dict) -> dict:
    import swarm.agent_runtime as _rt
    body = json.dumps(payload).encode()
    req = _ur.Request(
        f"http://localhost:{_rt.API_PORT}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    with _ur.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _api_post_json(path: str, payload: dict) -> dict:
    import swarm.agent_runtime as _rt
    body = json.dumps(payload).encode()
    req = _ur.Request(
        f"http://localhost:{_rt.API_PORT}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _ur.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Archive check
# ---------------------------------------------------------------------------

def _is_archived(task_id: str) -> bool:
    """Return True if task_id is in the archived task history."""
    import swarm.orchestrator as _orc
    from swarm import db
    history_file = getattr(_orc, "HISTORY_FILE", None)
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
    try:
        task = db.task_get(task_id)
        if task is not None:
            return task.get("status") in ("completed", "failed", "cancelled")
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Lock-conflict handoff
# ---------------------------------------------------------------------------

def _spawn_lock_conflict_handoff(locked_path: str, owner_task_id: str) -> dict:
    import swarm.agent_runtime as _rt
    from swarm.branch_intent import format_branch_intent, branch_intent_metadata
    if not owner_task_id or owner_task_id == _rt.TASK_ID:
        return {"ok": False, "error": "missing valid owner task for lock handoff"}
    if _rt.LOCK_CONFLICT_HANDOFF and _rt.LOCK_CONFLICT_HANDOFF.get("followup_task_id"):
        return {"ok": True, **_rt.LOCK_CONFLICT_HANDOFF}

    try:
        current_task = (_api_get_json(f"/api/tasks/{_rt.TASK_ID}").get("task") or {})
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
        "id": _rt.TASK_ID,
        "type": _rt.TASK_TYPE,
        "description": _rt.TASK_DESC,
        "metadata": dict(_rt.TASK_METADATA or {}),
    }
    if current_task:
        merged_metadata = dict(intent_task.get("metadata") or {})
        merged_metadata.update(current_task.get("metadata") or {})
        intent_task.update(dict(current_task))
        intent_task["metadata"] = merged_metadata
        if not (intent_task.get("description") or "").strip():
            intent_task["description"] = _rt.TASK_DESC
        if not (intent_task.get("type") or "").strip():
            intent_task["type"] = _rt.TASK_TYPE

    handoff_desc = (
        f"CONTINUATION of task {_rt.TASK_ID} after lock conflict.\n\n"
        f"{format_branch_intent(intent_task, heading='ORIGINAL TASK OBJECTIVE')}\n\n"
        f"This work was blocked because `{locked_path}` is currently owned by sibling task `{owner_task_id}`.\n"
        f"Continue this task only after `{owner_task_id}` completes. Re-check HEAD first: the sibling may have already satisfied part of the requirement.\n"
        f"If the needed change is already present, validate and finish without duplicating work."
    )

    handoff_resp = _api_post_json(
        f"/api/projects/{_rt.PROJECT}/lock-conflict-handoff",
        {
            "blocked_task_id": _rt.TASK_ID,
            "owner_task_id":   owner_task_id,
            "locked_path":     locked_path,
            "task_type":       _rt.TASK_TYPE,
            "priority":        _rt.TASK_PRIORITY,
            "description":     handoff_desc,
            "dependencies":    inherited_deps,
            "metadata":        branch_intent_metadata(intent_task),
        },
    )
    was_created = handoff_resp.get("created", True)
    followup_task = handoff_resp.get("task") or {}
    followup_task_id = followup_task.get("id") or handoff_resp.get("task_id", "")

    result: dict = {
        "ok": bool(followup_task_id),
        "followup_task_id": followup_task_id,
        "created": was_created,
    }
    if followup_task_id:
        _rt.LOCK_CONFLICT_HANDOFF = result
    return result
