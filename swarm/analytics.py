"""
swarm.analytics -- read-only aggregate queries over the tasks / agents /
agent_signals tables for the dashboard Analytics panel (roadmap #7).

Every function is a pure query: it takes the db module and optional filters,
returns plain dicts/lists. No mutation, no side effects. The API layer
(swarm/api_analytics.py) is a thin wrapper over these.

Design notes:
- Tasks are permanent in the DB (completed/failed rows are never deleted;
  8,364 pre-migration rows were backfilled 2026-07-04), so task-based metrics
  read the tasks table directly.
- Agent rows ARE pruned to data/agent-history.jsonl after finish, so
  cost/token metrics read the live agents table plus the JSONL fallback.
- agent_signals is populated at finish time when config.log_extract_signals
  is on; death/mechanism metrics degrade gracefully to empty when it's off.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

# Task-type taxonomy for value/repair analysis.
_VALUE_TYPES = {"feature", "polish", "art_pass"}
_REPAIR_TYPES = {"bug"}


def _iter_agent_history(data_dir: Path):
    """Yield archived agent rows from agent-history.jsonl (pruned from DB)."""
    hist = data_dir / "agent-history.jsonl"
    if not hist.exists():
        return
    for line in hist.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


# ---------------------------------------------------------------------------
# 1. Overview
# ---------------------------------------------------------------------------

def overview(db, data_dir: Path, project: Optional[str] = None) -> dict:
    """Completed/failed counts, cost, tokens, avg loops — global or per-project."""
    tasks = db.task_get_by_project(project) if project else db.task_get_all()

    completed = [t for t in tasks if t.get("status") == "completed"]
    failed = [t for t in tasks if t.get("status") == "failed"]

    # Cost + tokens: live agents table, then archived JSONL, scoped to project.
    total_cost = 0.0
    total_in = total_out = 0
    loop_counts: list[int] = []
    agent_count = 0

    def _account(row: dict):
        nonlocal total_cost, total_in, total_out, agent_count
        if project and row.get("project") != project:
            return
        total_cost += row.get("estimated_cost_usd") or 0.0
        total_in += row.get("input_tokens") or 0
        total_out += row.get("output_tokens") or 0
        lc = row.get("loop_count")
        if lc:
            loop_counts.append(lc)
        agent_count += 1

    for row in db.agent_get_all():
        _account(row)
    for row in _iter_agent_history(data_dir):
        _account(row)

    return {
        "project": project or "(all)",
        "tasks_completed": len(completed),
        "tasks_failed": len(failed),
        "agents_counted": agent_count,
        "total_cost_usd": round(total_cost, 4),
        "avg_cost_per_completed_task": round(total_cost / len(completed), 4) if completed else 0.0,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "avg_loops": round(sum(loop_counts) / len(loop_counts), 1) if loop_counts else 0.0,
    }


# ---------------------------------------------------------------------------
# 2. Value / repair ratio (reproduces the run-11 hand analysis)
# ---------------------------------------------------------------------------

def _is_repair(task: dict) -> bool:
    if task.get("type") not in _REPAIR_TYPES:
        return False
    meta = task.get("metadata") or {}
    return bool(meta.get("is_validation_bug")) or (task.get("attempts") or 0) > 1


def value_repair(db, project: Optional[str] = None) -> dict:
    """value = completed feature/polish/art_pass; repair = validation/retried bugs.

    Ratio > 1 means the project produced more forward-progress work than
    rework. Run-11's art arm was ~2.8x.
    """
    tasks = db.task_get_by_project(project) if project else db.task_get_all()
    completed = [t for t in tasks if t.get("status") == "completed"]

    value = sum(1 for t in completed if t.get("type") in _VALUE_TYPES)
    repair = sum(1 for t in completed if _is_repair(t))

    return {
        "project": project or "(all)",
        "value_tasks": value,
        "repair_tasks": repair,
        "value_repair_ratio": round(value / repair, 2) if repair else (float(value) if value else 0.0),
    }


def value_repair_by_project(db) -> list[dict]:
    """value/repair for every project that has completed tasks, ratio descending."""
    projects = sorted(db.project_get_all().keys())
    rows = [value_repair(db, p) for p in projects]
    rows = [r for r in rows if r["value_tasks"] or r["repair_tasks"]]
    rows.sort(key=lambda r: r["value_repair_ratio"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# 3. Where agents die (from agent_signals)
# ---------------------------------------------------------------------------

def deaths(db, project: Optional[str] = None) -> dict:
    rows = db.agent_signals_query(project=project, limit=10000)
    if not rows:
        return {"count": 0, "terminal_status": {}, "avg_loop_by_type": {}, "top_errors": []}

    status = Counter()
    loops_by_type: dict[str, list[int]] = defaultdict(list)
    errors = Counter()

    for r in rows:
        status[r.get("terminal_status") or "unknown"] += 1
        lc = r.get("loop_count") or 0
        if lc:
            loops_by_type[r.get("task_type") or "unknown"].append(lc)
        for snip in json.loads(r.get("error_snippets") or "[]"):
            errors[snip[:100]] += 1

    return {
        "count": len(rows),
        "terminal_status": dict(status),
        "avg_loop_by_type": {
            t: round(sum(v) / len(v), 1) for t, v in loops_by_type.items() if v
        },
        "top_errors": errors.most_common(10),
    }


# ---------------------------------------------------------------------------
# 4. Recovery-mechanism efficacy (needs Phase B's mechanism_fires column)
# ---------------------------------------------------------------------------

def mechanisms(db, project: Optional[str] = None) -> dict:
    """For each recovery mechanism: how often it fires, and the completion rate
    of runs where it fired vs didn't. Answers 'do our reflexes actually help?'

    Reads the mechanism_fires JSON column added in Phase B. Returns an empty
    report (not an error) if that column/data isn't present yet.
    """
    rows = db.agent_signals_query(project=project, limit=10000)
    if not rows:
        return {"count": 0, "mechanisms": {}}

    # completion rate baseline
    def _completed(r):
        return (r.get("terminal_status") or "") == "complete"

    per_mech_fired = defaultdict(lambda: {"runs": 0, "completed": 0})
    with_any = {"runs": 0, "completed": 0}
    without_any = {"runs": 0, "completed": 0}

    for r in rows:
        try:
            fires = json.loads(r.get("mechanism_fires") or "{}")
        except Exception:
            fires = {}
        done = _completed(r)
        if fires:
            with_any["runs"] += 1
            with_any["completed"] += int(done)
        else:
            without_any["runs"] += 1
            without_any["completed"] += int(done)
        for mech, n in fires.items():
            if n:
                per_mech_fired[mech]["runs"] += 1
                per_mech_fired[mech]["completed"] += int(done)

    def _rate(d):
        return round(d["completed"] / d["runs"], 3) if d["runs"] else None

    return {
        "count": len(rows),
        "completion_rate_with_any_mechanism": _rate(with_any),
        "completion_rate_without_any_mechanism": _rate(without_any),
        "mechanisms": {
            m: {"fired_runs": d["runs"], "completion_rate": _rate(d)}
            for m, d in sorted(per_mech_fired.items())
        },
    }


# ---------------------------------------------------------------------------
# 5. Ship candidates (which Godot game is closest to releasable)
# ---------------------------------------------------------------------------

def ship_candidates(db, data_dir: Path, workspace: Path) -> list[dict]:
    """Rank Godot projects by shippability signals.

    Signals per project: closure_status, validation-bug rate over the last N
    tasks, and unverified-completion count (Phase A's completion_evidence).
    Lower repair/unverified => more shippable.
    """
    try:
        from swarm.closure.status import derive_closure_status
    except Exception:
        derive_closure_status = None

    out = []
    for name in sorted(db.project_get_all().keys()):
        proj_dir = workspace / name
        if not (proj_dir / "project.godot").exists():
            continue  # Godot projects only

        tasks = db.task_get_by_project(name)
        recent = sorted(
            [t for t in tasks if t.get("status") in ("completed", "failed")],
            key=lambda t: t.get("completed") or "",
            reverse=True,
        )[:50]
        val_bugs = sum(
            1 for t in recent
            if t.get("type") == "bug" and (t.get("metadata") or {}).get("is_validation_bug")
        )
        unverified = sum(
            1 for t in tasks
            if (t.get("metadata") or {}).get("completion_evidence", {}).get("unverified")
        )

        closure = None
        if derive_closure_status:
            try:
                closure = derive_closure_status(db, name)
            except Exception:
                closure = None

        out.append({
            "project": name,
            "closure_status": (closure.get("status") if isinstance(closure, dict) else closure) or "unknown",
            "validation_bugs_last50": val_bugs,
            "unverified_completions": unverified,
            "recent_task_sample": len(recent),
        })

    # Rank: fewest validation bugs + unverified completions first.
    out.sort(key=lambda r: (r["validation_bugs_last50"] + r["unverified_completions"]))
    return out
