"""Regression identity and upsert helpers for closure verification failures."""

from __future__ import annotations

import re
from datetime import datetime
from hashlib import sha1
from typing import Any, Mapping

from swarm import db


def normalize_regression_fingerprints(run: Mapping[str, Any] | None) -> list[str]:
    if not run:
        return []
    provided = run.get("fingerprints_json")
    if isinstance(provided, list) and provided:
        out: list[str] = []
        seen: set[str] = set()
        for item in provided:
            if not isinstance(item, str):
                continue
            value = _normalize_fingerprint(item)
            if value and value not in seen:
                seen.add(value)
                out.append(value)
        return out

    results = run.get("results_json")
    if not isinstance(results, Mapping):
        return []

    derived: list[str] = []
    if results.get("boot_ok") is False:
        derived.append("boot:failed")
    if results.get("tests_ok") is False:
        derived.append("tests:failed")
    if results.get("smoke_ok") is False:
        derived.append("smoke:failed")

    critical_flows = results.get("critical_flows")
    if isinstance(critical_flows, Mapping):
        for flow_id, passed in critical_flows.items():
            if passed is False and isinstance(flow_id, str) and flow_id.strip():
                derived.append(f"flow:{_normalize_token(flow_id)}:failed")

    errors = results.get("errors")
    if isinstance(errors, list):
        for error in errors[:5]:
            if not isinstance(error, str):
                continue
            normalized_error = _normalize_error(error)
            if normalized_error:
                derived.append(f"error:{normalized_error}")

    out: list[str] = []
    seen: set[str] = set()
    for item in derived:
        normalized = _normalize_fingerprint(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def resolve_regressions_for_passing_run(run_id: str) -> list[str]:
    """Close open regressions whose fingerprints are absent from a passing run.

    Called when a verification run status is 'passed'. Any open regression
    whose fingerprint did not appear in this run's failure set is resolved.
    Returns the list of regression IDs that were closed.
    """
    run = db.verification_run_get(run_id)
    if not run or run.get("status") != "passed":
        return []

    project = run["project"]
    failing_fingerprints = set(normalize_regression_fingerprints(run))
    now = run.get("completed_at") or datetime.now().isoformat()

    resolved: list[str] = []
    for regression in db.regression_list_by_project(project, status="open"):
        if regression.get("fingerprint") not in failing_fingerprints:
            db.regression_update(regression["id"], {
                "status": "resolved",
                "resolved_at": now,
                "resolved_by_run_id": run_id,
            })
            resolved.append(regression["id"])

    if resolved:
        refresh_project_recurrence_state(project)
    return resolved


def upsert_regressions_for_run(run_id: str) -> list[dict[str, Any]]:
    run = db.verification_run_get(run_id)
    if not run:
        raise ValueError(f"verification run not found: {run_id}")
    if run.get("status") not in {"failed", "error"}:
        return []

    fingerprints = normalize_regression_fingerprints(run)
    project = run["project"]
    now = run.get("completed_at") or datetime.now().isoformat()
    results = run.get("results_json") if isinstance(run.get("results_json"), Mapping) else {}
    artifacts = run.get("artifacts_json") if isinstance(run.get("artifacts_json"), Mapping) else {}

    upserted: list[dict[str, Any]] = []
    for fingerprint in fingerprints:
        existing = db.regression_find_open(project, fingerprint)
        if existing:
            db.regression_update(existing["id"], {
                "last_seen_at": now,
                "occurrences": int(existing.get("occurrences") or 0) + 1,
                "source_run_id": run_id,
                "details_json": {
                    "results": results,
                    "artifacts": artifacts,
                    "last_run_status": run.get("status"),
                },
            })
            upserted.append(db.regression_get(existing["id"]))
            continue

        regression_id = _regression_id(project, fingerprint)
        db.regression_upsert({
            "id": regression_id,
            "project": project,
            "fingerprint": fingerprint,
            "status": "open",
            "severity": _severity_for_fingerprint(fingerprint),
            "first_seen_at": now,
            "last_seen_at": now,
            "occurrences": 1,
            "source_run_id": run_id,
            "details_json": {
                "results": results,
                "artifacts": artifacts,
                "last_run_status": run.get("status"),
            },
        })
        upserted.append(db.regression_get(regression_id))

    refresh_project_recurrence_state(project)
    return [item for item in upserted if item is not None]


def summarize_project_recurrence(project: str, *, stall_threshold: int = 3) -> dict[str, Any]:
    open_regressions = db.regression_list_by_project(project, status="open")
    sorted_open = sorted(
        open_regressions,
        key=lambda item: (
            int(item.get("occurrences") or 0),
            item.get("last_seen_at") or "",
            item.get("fingerprint") or "",
        ),
        reverse=True,
    )
    max_occurrences = max((int(item.get("occurrences") or 0) for item in open_regressions), default=0)
    summarized = [_summarize_open_regression(item, stall_threshold=stall_threshold) for item in sorted_open]
    one_off = [item for item in summarized if item["occurrences"] < 2]
    recurring = [item for item in summarized if item["occurrences"] >= 2]
    stalled_candidates = [item for item in summarized if item["occurrences"] >= stall_threshold]
    repair_loops = [item for item in summarized if item["has_active_repair"]]
    return {
        "project": project,
        "open_regression_count": len(open_regressions),
        "stall_count": max_occurrences,
        "max_occurrences": max_occurrences,
        "one_off_regressions": one_off,
        "recurring_regressions": recurring,
        "repair_loop_regressions": repair_loops,
        "stalled_candidates": stalled_candidates,
        "top_fingerprints": [item.get("fingerprint") for item in summarized[:5]],
        "should_mark_stalled": bool(stalled_candidates),
    }


def refresh_project_recurrence_state(project: str, *, stall_threshold: int = 3) -> dict[str, Any]:
    summary = summarize_project_recurrence(project, stall_threshold=stall_threshold)
    updates: dict[str, Any] = {
        "open_regression_count": summary["open_regression_count"],
        "stall_count": summary["stall_count"],
    }
    # When all regressions are gone, clear stalled/frozen status back to green.
    # This allows expansion tasks to resume without waiting for a passing verification run.
    if summary["open_regression_count"] == 0:
        proj_row = db.project_get(project)
        if proj_row and (proj_row.get("closure_status") or "") in {"stalled", "frozen"}:
            updates["closure_status"] = "green"
    db.project_update(project, updates)
    return summary


def resolve_regressions_for_linked_task(task_id: str, project: str) -> list[str]:
    """Resolve any open regressions whose linked_task_id matches this completed task.

    Called when a bug/repair task completes successfully. Closes regressions that
    were waiting on this specific task, then refreshes the project's recurrence state.
    This unblocks expansion tasks without requiring a full passing verification run.
    Returns the list of regression IDs that were closed.
    """
    now = datetime.now().isoformat()
    resolved: list[str] = []
    for regression in db.regression_list_by_project(project, status="open"):
        if regression.get("linked_task_id") == task_id:
            db.regression_update(regression["id"], {
                "status": "resolved",
                "resolved_at": now,
                "resolved_by_run_id": f"task:{task_id}",
            })
            resolved.append(regression["id"])
    if resolved:
        refresh_project_recurrence_state(project)
    return resolved


def _normalize_fingerprint(value: str) -> str:
    parts = [_normalize_token(part) for part in value.split(":")]
    parts = [part for part in parts if part]
    return ":".join(parts[:4])


def _normalize_token(value: str) -> str:
    compact = value.strip().lower()
    compact = re.sub(r"\s+", "-", compact)
    compact = re.sub(r"[^a-z0-9._-]", "", compact)
    return compact[:80]


def _normalize_error(error: str) -> str:
    value = error.strip().lower()
    value = re.sub(r"\b\d+\b", "<n>", value)
    value = re.sub(r"0x[0-9a-f]+", "<hex>", value)
    value = re.sub(r"\s+", " ", value)
    return _normalize_token(value)[:120]


def _severity_for_fingerprint(fingerprint: str) -> str:
    if fingerprint.startswith("boot:") or fingerprint.startswith("flow:"):
        return "high"
    if fingerprint.startswith("tests:") or fingerprint.startswith("smoke:"):
        return "medium"
    return "medium"


def _regression_id(project: str, fingerprint: str) -> str:
    digest = sha1(f"{project}:{fingerprint}".encode("utf-8")).hexdigest()[:12]
    return f"reg-{digest}"


def _summarize_open_regression(regression: Mapping[str, Any], *, stall_threshold: int) -> dict[str, Any]:
    linked_task_id = regression.get("linked_task_id")
    linked_task = db.task_get(linked_task_id) if isinstance(linked_task_id, str) and linked_task_id.strip() else None
    linked_task_status = linked_task.get("status") if linked_task else None
    occurrences = int(regression.get("occurrences") or 0)
    return {
        "id": regression.get("id"),
        "fingerprint": regression.get("fingerprint"),
        "severity": regression.get("severity") or "medium",
        "occurrences": occurrences,
        "linked_task_id": linked_task_id,
        "linked_task_status": linked_task_status,
        "has_active_repair": linked_task_status in {"pending", "in_progress"},
        "has_repair_history": bool(linked_task_id),
        "is_recurring": occurrences >= 2,
        "is_stalled_candidate": occurrences >= stall_threshold,
        "last_seen_at": regression.get("last_seen_at"),
    }
