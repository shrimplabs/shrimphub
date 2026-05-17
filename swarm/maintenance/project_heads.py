"""Project-head maintenance helpers."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterable, Optional

from swarm.integrity import is_continuity_eligible_task, is_valid_project_head


def _dedupe(ids: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in ids:
        if not isinstance(item, str):
            continue
        task_id = item.strip()
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        out.append(task_id)
    return out


def get_project_head(db: Any, project_name: str) -> Optional[str]:
    if not project_name:
        return None
    proj = db.project_get(project_name)
    if not proj:
        return None
    head = proj.get("head_task_id")
    if not head:
        return None
    task = db.task_get(head)
    if task is None:
        task = db.task_get_completed_record(head)
    if is_valid_project_head(task, project_name):
        return head
    return None


def _history_file() -> Path:
    try:
        import swarm.orchestrator as _orc
        return Path(getattr(_orc, "DATA_DIR")) / "task-history.jsonl"
    except Exception:
        return Path(__file__).resolve().parent.parent.parent / "data" / "task-history.jsonl"


def _task_timestamp(task: dict) -> str:
    return (
        task.get("completed")
        or task.get("created")
        or task.get("started")
        or ""
    )


def infer_project_tail(db: Any, project_name: str) -> Optional[str]:
    """Best-effort chain anchor for projects missing head_task_id."""
    genesis_id = f"{project_name}-genesis"
    live = db.task_get_by_project(project_name) or []
    preferred_live = [task for task in live if is_continuity_eligible_task(task)]
    non_genesis_live = [task for task in preferred_live if task.get("id") != genesis_id]
    if non_genesis_live:
        preferred_live = non_genesis_live
    if preferred_live:
        live_sorted = sorted(preferred_live, key=_task_timestamp)
        for task in reversed(live_sorted):
            task_id = task.get("id")
            if task_id:
                return task_id

    hist = _history_file()
    if not hist.exists():
        return None

    latest: Optional[dict] = None
    with hist.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("project") != project_name:
                continue
            if not is_continuity_eligible_task(rec):
                continue
            if rec.get("id") == genesis_id:
                continue
            if latest is None or _task_timestamp(rec) >= _task_timestamp(latest):
                latest = rec
    if latest:
        return latest.get("id")
    return None


def ensure_project_head(db: Any, project_name: str) -> Optional[str]:
    """Return a project's head, creating a genesis anchor when needed."""
    if not project_name:
        return None
    proj = db.project_get(project_name)
    if not proj:
        return None
    head = get_project_head(db, project_name)
    if head:
        return head

    inferred = infer_project_tail(db, project_name)
    if inferred and (db.task_get(inferred) or db.task_get_completed_record(inferred)):
        db.project_upsert({**proj, "head_task_id": inferred})
        return inferred

    genesis_id = f"{project_name}-genesis"
    if not db.task_get(genesis_id):
        now = datetime.now().isoformat()
        db.task_upsert({
            "id": genesis_id,
            "project": project_name,
            "type": "feature",
            "description": "Project registered - genesis anchor",
            "status": "completed",
            "created": now,
            "completed": now,
            "priority": 50,
        })
        try:
            db.task_record_completed(genesis_id, project=project_name)
        except Exception:
            pass

    db.project_upsert({**proj, "head_task_id": genesis_id})
    return genesis_id


def reconcile_project_head(db: Any, project_name: str) -> dict:
    """Repair and report the canonical head for a project."""
    if not project_name:
        return {"ok": False, "error": "project_name required"}
    proj = db.project_get(project_name)
    if not proj:
        return {"ok": False, "error": f"Unknown project '{project_name}'"}

    stored_head = proj.get("head_task_id")
    valid_head = get_project_head(db, project_name)
    if valid_head:
        return {
            "ok": True,
            "project": project_name,
            "head_task_id": valid_head,
            "previous_head_task_id": stored_head,
            "action": "unchanged" if stored_head == valid_head else "repaired",
            "source": "stored",
        }

    inferred = infer_project_tail(db, project_name)
    if inferred and (db.task_get(inferred) or db.task_get_completed_record(inferred)):
        db.project_upsert({**proj, "head_task_id": inferred})
        return {
            "ok": True,
            "project": project_name,
            "head_task_id": inferred,
            "previous_head_task_id": stored_head,
            "action": "repaired",
            "source": "live_or_history_tail",
        }

    genesis_id = ensure_project_head(db, project_name)
    return {
        "ok": True,
        "project": project_name,
        "head_task_id": genesis_id,
        "previous_head_task_id": stored_head,
        "action": "created_genesis" if stored_head != genesis_id else "unchanged",
        "source": "genesis",
    }


def set_project_head(
    db: Any,
    project_name: str,
    head_task_id: Optional[str],
    *,
    repair_if_missing: bool = False,
) -> Optional[str]:
    """Persist a validated project head."""
    if not project_name:
        raise ValueError("project_name required")
    proj = db.project_get(project_name)
    if not proj:
        raise ValueError(f"Unknown project '{project_name}'")
    if not head_task_id:
        if repair_if_missing:
            return ensure_project_head(db, project_name)
        db.project_upsert({**proj, "head_task_id": None})
        return None

    task = db.task_get(head_task_id) or db.task_get_completed_record(head_task_id)
    if task is None:
        raise ValueError(
            f"Invalid head '{head_task_id}' for project '{project_name}': task does not exist"
        )
    if not is_valid_project_head(task, project_name):
        raise ValueError(
            f"Invalid head '{head_task_id}' for project '{project_name}': "
            "task must belong to the project and be continuity-eligible"
        )
    db.project_upsert({**proj, "head_task_id": head_task_id})
    return head_task_id
