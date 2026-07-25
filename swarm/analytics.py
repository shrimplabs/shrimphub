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
    """Completed/failed counts, cost, tokens, avg loops -- global or per-project."""
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


def value_repair_by_project(db, projects: Optional[list] = None) -> list[dict]:
    """value/repair for every project that has completed tasks, ratio descending.

    If ``projects`` is given, only those projects are included.
    """
    all_projects = sorted(db.project_get_all().keys())
    if projects is not None:
        all_projects = [p for p in all_projects if p in set(projects)]
    rows = [value_repair(db, p) for p in all_projects]
    rows = [r for r in rows if r["value_tasks"] or r["repair_tasks"]]
    rows.sort(key=lambda r: r["value_repair_ratio"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# 2b. Cost breakdown (project / task_type / model / provider)
# ---------------------------------------------------------------------------

def cost(db, data_dir: Path, project: Optional[str] = None) -> dict:
    """Aggregate estimated_cost_usd over live + archived agents, grouped by
    project / task_type / model / provider.

    Joins ``agents`` with ``tasks`` via ``task_id`` to recover the task_type
    (not stored on the agent row). The task_type lookup is best-effort: if a
    task row was pruned, the agent still contributes to totals/by_project,
    but ``task_type`` falls back to ``"unknown"``.

    Returns a flat dict the dashboard can render directly without further
    aggregation. Zero-value rows are dropped to keep the payload small.
    """
    # task_type lookup keyed by task_id (rows pruned from tasks are skipped)
    type_by_id: dict = {}
    proj_by_id: dict = {}
    completed_proj_by_id: dict = {}
    for t in db.task_get_all():
        tid = t.get("id") or ""
        if not tid:
            continue
        type_by_id[tid] = t.get("type") or "unknown"
        proj_by_id[tid] = t.get("project") or ""
        if t.get("status") == "completed":
            completed_proj_by_id[t.get("project") or ""] = (
                completed_proj_by_id.get(t.get("project") or "", 0) + 1
            )

    by_project: dict = defaultdict(lambda: {"cost": 0.0, "agents": 0, "tokens_in": 0, "tokens_out": 0})
    by_project_type: dict = defaultdict(lambda: {"cost": 0.0, "agents": 0})
    by_model: dict = defaultdict(lambda: {"cost": 0.0, "agents": 0})
    by_provider: dict = defaultdict(lambda: {"cost": 0.0, "agents": 0})
    total_cost = 0.0
    total_agents = 0

    def _account(row: dict, source: str):
        nonlocal total_cost, total_agents
        if project and row.get("project") and row.get("project") != project:
            return
        # archived rows may not have project; skip them under project filter
        if project and not row.get("project"):
            return
        cost_val = row.get("estimated_cost_usd") or 0.0
        if not cost_val and not row.get("input_tokens") and not row.get("output_tokens"):
            # skip pure-noop rows so they don't inflate agent counts
            return
        proj = row.get("project") or "(no-project)"
        ttype = type_by_id.get(row.get("task_id") or "", "unknown")
        model = row.get("model") or "(unknown)"
        provider = row.get("provider") or "(unknown)"

        by_project[proj]["cost"] += cost_val
        by_project[proj]["agents"] += 1
        by_project[proj]["tokens_in"] += row.get("input_tokens") or 0
        by_project[proj]["tokens_out"] += row.get("output_tokens") or 0

        by_project_type[(proj, ttype)]["cost"] += cost_val
        by_project_type[(proj, ttype)]["agents"] += 1

        by_model[model]["cost"] += cost_val
        by_model[model]["agents"] += 1

        by_provider[provider]["cost"] += cost_val
        by_provider[provider]["agents"] += 1

        total_cost += cost_val
        total_agents += 1

    for row in db.agent_get_all():
        _account(row, "live")
    for row in _iter_agent_history(data_dir):
        _account(row, "archive")

    # Completed-task count, used to compute cost_per_completed_task
    if project:
        completed_total = completed_proj_by_id.get(project, 0)
    else:
        completed_total = sum(completed_proj_by_id.values())

    def _round(d):
        return {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in d.items()}

    return {
        "project": project or "(all)",
        "total_cost_usd": round(total_cost, 4),
        "agents_counted": total_agents,
        "cost_per_completed_task": round(total_cost / completed_total, 4) if completed_total else 0.0,
        "by_project": [
            {"project": p, "cost_usd": round(v["cost"], 4), "agents": v["agents"],
             "tokens_in": v["tokens_in"], "tokens_out": v["tokens_out"]}
            for p, v in sorted(by_project.items(), key=lambda kv: -kv[1]["cost"])
            if v["cost"] > 0 or v["agents"] > 0
        ],
        "by_project_task_type": [
            {"project": p, "task_type": t, "cost_usd": round(v["cost"], 4), "agents": v["agents"]}
            for (p, t), v in sorted(by_project_type.items(), key=lambda kv: (-kv[1]["cost"], kv[0][0]))
            if v["cost"] > 0 or v["agents"] > 0
        ],
        "by_model": [
            {"model": m, "cost_usd": round(v["cost"], 4), "agents": v["agents"]}
            for m, v in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"])
            if v["cost"] > 0 or v["agents"] > 0
        ],
        "by_provider": [
            {"provider": p, "cost_usd": round(v["cost"], 4), "agents": v["agents"]}
            for p, v in sorted(by_provider.items(), key=lambda kv: -kv[1]["cost"])
            if v["cost"] > 0 or v["agents"] > 0
        ],
    }


# ---------------------------------------------------------------------------
# 3. Where agents die (from agent_signals)
# ---------------------------------------------------------------------------

def _classify_death(row: dict) -> str:
    """Map a single agent_signals row to one of the four dashboard buckets.

    - ``loop_limit``: hit the configured loop ceiling (loop_count >= 195).
    - ``no_task_complete``: agent exited cleanly but the run did not register
      a TASK_COMPLETE marker (``terminal_status`` of ``complete`` is reserved
      for runs that *did* print TASK_COMPLETE; everything else that didn't).
      Heuristic: ``terminal_status in {"failed", "unknown", "parse_error:*"}
      AND loop_count < 195``.
    - ``validation_fail``: post-task validation failed -- ``phase_failed`` is
      set (pipeline agents) or ``error_count > 0`` with the task_type being
      a validation/QA family. Non-pipeline agents with errors still fall into
      "other" because we can't distinguish a mid-run tool error from a
      validation failure at the row level.
    - ``other``: anything that doesn't match the above (parse_errors, etc.).
    """
    lc = row.get("loop_count") or 0
    ts = row.get("terminal_status") or ""
    if lc >= 195 or ts == "loop_limit":
        return "loop_limit"
    if ts == "complete":
        return "loop_limit" if lc >= 195 else "other"  # successful -> not a death
    # ts in {failed, unknown, parse_error:*}
    if row.get("phase_failed") or (row.get("error_count") and (row.get("task_type") or "") in {"qa", "harness_qa", "hybrid_qa"}):
        return "validation_fail"
    if row.get("error_count") and lc >= 80:
        # high-loop + errors but no phase_failed -> likely validation reset
        return "validation_fail"
    if ts == "failed":
        # failed without clear validation signal -> treat as no_task_complete
        return "no_task_complete"
    return "other"


def deaths(db, project: Optional[str] = None) -> dict:
    rows = db.agent_signals_query(project=project, limit=10000)
    if not rows:
        return {
            "count": 0,
            "terminal_status": {},
            "cause_buckets": {
                "loop_limit": 0,
                "no_task_complete": 0,
                "validation_fail": 0,
                "other": 0,
            },
            "avg_loop_by_type": {},
            "top_errors": [],
        }

    status = Counter()
    loops_by_type: dict[str, list[int]] = defaultdict(list)
    errors = Counter()
    cause_buckets = Counter({
        "loop_limit": 0,
        "no_task_complete": 0,
        "validation_fail": 0,
        "other": 0,
    })

    for r in rows:
        ts = r.get("terminal_status") or "unknown"
        status[ts] += 1
        lc = r.get("loop_count") or 0
        if lc:
            loops_by_type[r.get("task_type") or "unknown"].append(lc)
        for snip in json.loads(r.get("error_snippets") or "[]"):
            errors[snip[:100]] += 1
        # Only count as a "death" if terminal_status wasn't a successful complete
        if ts != "complete":
            cause_buckets[_classify_death(r)] += 1

    return {
        "count": len(rows),
        "terminal_status": dict(status),
        "cause_buckets": dict(cause_buckets),
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

def ship_candidates(db, data_dir: Path, workspace: Path, projects: Optional[list] = None) -> list[dict]:
    """Rank Godot projects by shippability signals.

    Signals per project: closure_status, validation-bug rate over the last N
    tasks, and unverified-completion count (Phase A's completion_evidence).
    Lower repair/unverified => more shippable.
    """
    try:
        from swarm.closure.status import derive_closure_status
    except Exception:
        derive_closure_status = None

    project_filter = set(projects) if projects is not None else None
    out = []
    for name in sorted(db.project_get_all().keys()):
        if project_filter is not None and name not in project_filter:
            continue
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

        pending = sum(1 for t in tasks if t.get("status") == "pending")
        has_bot = any(t.get("type") == "playthrough_bot" and t.get("status") == "completed" for t in tasks)
        bot_pending = any(t.get("type") == "playthrough_bot" and t.get("status") == "pending" for t in tasks)

        out.append({
            "project": name,
            "closure_status": (closure.get("status") if isinstance(closure, dict) else closure) or "unknown",
            "validation_bugs_last50": val_bugs,
            "unverified_completions": unverified,
            "recent_task_sample": len(recent),
            "pending_tasks": pending,
            "playthrough_bot": "done" if has_bot else ("pending" if bot_pending else "none"),
        })

    # Rank: bot done first, then fewest validation bugs + unverified + pending tasks.
    out.sort(key=lambda r: (
        0 if r["playthrough_bot"] == "done" else (1 if r["playthrough_bot"] == "pending" else 2),
        r["validation_bugs_last50"] + r["unverified_completions"],
        r["pending_tasks"],
    ))
    return out


def research_feeder_roi(db, project: Optional[str] = None) -> dict:
    """How often does a research feeder diagnosis actually unblock the original task?

    Returns counts and unblock rate across all research feeder tasks.
    """
    import json as _json

    conn = db._connect()
    q = "SELECT id, project, status, metadata FROM tasks WHERE type='research' AND metadata LIKE '%feeds_into_task_id%'"
    params: list = []
    if project:
        q += " AND project=?"
        params.append(project)
    feeders = conn.execute(q, params).fetchall()

    total = 0
    unblocked = 0
    still_failed = 0
    still_pending = 0
    by_project: dict = {}

    for f in feeders:
        try:
            meta = _json.loads(f[3]) if f[3] else {}
        except Exception:
            continue
        orig_id = meta.get("feeds_into_task_id")
        if not orig_id:
            continue
        orig = conn.execute("SELECT status, project FROM tasks WHERE id=?", (orig_id,)).fetchone()
        if not orig:
            continue
        total += 1
        proj = f[1]
        if proj not in by_project:
            by_project[proj] = {"total": 0, "unblocked": 0, "failed": 0}
        by_project[proj]["total"] += 1
        if orig[0] == "completed":
            unblocked += 1
            by_project[proj]["unblocked"] += 1
        elif orig[0] in ("failed", "cancelled"):
            still_failed += 1
            by_project[proj]["failed"] += 1
        else:
            still_pending += 1

    rate = round(unblocked / total, 3) if total else None
    return {
        "total_feeders": total,
        "unblocked": unblocked,
        "still_failed": still_failed,
        "still_pending": still_pending,
        "unblock_rate": rate,
        "by_project": sorted(
            [{"project": p, **v, "rate": round(v["unblocked"] / v["total"], 2) if v["total"] else 0}
             for p, v in by_project.items()],
            key=lambda r: -r["total"],
        )[:20],
    }
