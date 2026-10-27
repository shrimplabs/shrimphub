"""Persistence-oriented helpers for VerificationRun records."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Mapping
from uuid import uuid4

from swarm import db


VALID_RUN_TYPES = {"post_task", "periodic", "pre_release", "recovery", "manual"}
VALID_RUN_STATUSES = {"pending", "running", "passed", "failed", "error", "cancelled"}
ACTIVE_RUN_STATUSES = {"pending", "running"}
MAX_STRING_LENGTH = 2000
MAX_LIST_ITEMS = 50
MAX_DICT_ITEMS = 50
DEFAULT_VERIFICATION_THROTTLE_SECONDS = 15
DEFAULT_VERIFICATION_WINDOW_SECONDS = 300
DEFAULT_VERIFICATION_MAX_RUNS_PER_WINDOW = 6

DEFAULT_RESULTS = {
    "boot_ok": None,
    "tests_ok": None,
    "smoke_ok": None,
    "critical_flows": {},
    "errors": [],
}


def _trim_string(value: str) -> str:
    return value[:MAX_STRING_LENGTH]


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _sanitize_json(value: Any, *, max_depth: int = 4) -> Any:
    if max_depth <= 0:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return _trim_string(value) if isinstance(value, str) else value
        return str(value)[:MAX_STRING_LENGTH]
    if isinstance(value, str):
        return _trim_string(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_DICT_ITEMS:
                break
            out[str(key)] = _sanitize_json(item, max_depth=max_depth - 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [
            _sanitize_json(item, max_depth=max_depth - 1)
            for item in list(value)[:MAX_LIST_ITEMS]
        ]
    return _trim_string(str(value))


def _normalize_run_type(run_type: str | None) -> str:
    value = (run_type or "post_task").strip().lower()
    return value if value in VALID_RUN_TYPES else "post_task"


def _normalize_run_status(status: str | None) -> str:
    value = (status or "pending").strip().lower()
    return value if value in VALID_RUN_STATUSES else "pending"


def _normalize_fingerprints(fingerprints: Any) -> list[str]:
    if fingerprints is None:
        return []
    if isinstance(fingerprints, str):
        fingerprints = [fingerprints]
    if not isinstance(fingerprints, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in fingerprints[:MAX_LIST_ITEMS]:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(_trim_string(value))
    return out


def normalize_verification_results(results: Any) -> dict[str, Any]:
    normalized = deepcopy(DEFAULT_RESULTS)
    if not isinstance(results, Mapping):
        return normalized
    if "boot_ok" in results:
        normalized["boot_ok"] = results["boot_ok"] if isinstance(results["boot_ok"], bool) else None
    if "tests_ok" in results:
        normalized["tests_ok"] = results["tests_ok"] if isinstance(results["tests_ok"], bool) else None
    if "smoke_ok" in results:
        normalized["smoke_ok"] = results["smoke_ok"] if isinstance(results["smoke_ok"], bool) else None
    critical_flows = results.get("critical_flows")
    if isinstance(critical_flows, Mapping):
        normalized["critical_flows"] = {
            str(key): value
            for key, value in list(critical_flows.items())[:MAX_DICT_ITEMS]
            if isinstance(value, bool)
        }
    errors = results.get("errors")
    if isinstance(errors, list):
        normalized["errors"] = [
            _trim_string(item)
            for item in errors[:MAX_LIST_ITEMS]
            if isinstance(item, str) and item.strip()
        ]
    for key, value in results.items():
        if key in normalized:
            continue
        normalized[str(key)] = _sanitize_json(value)
    return normalized


def normalize_verification_run(record: Mapping[str, Any]) -> dict[str, Any]:
    project = (record.get("project") or "").strip()
    if not project:
        raise ValueError("verification run requires project")

    run_id = (record.get("id") or "").strip() or f"vr-{uuid4().hex[:12]}"
    now = datetime.now().isoformat()
    status = _normalize_run_status(record.get("status"))
    created_at = record.get("created_at") or now
    started_at = record.get("started_at")
    completed_at = record.get("completed_at")

    normalized = {
        "id": run_id,
        "project": project,
        "trigger_task_id": record.get("trigger_task_id"),
        "run_type": _normalize_run_type(record.get("run_type")),
        "status": status,
        "created_at": created_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "results_json": normalize_verification_results(record.get("results_json")),
        "artifacts_json": _sanitize_json(record.get("artifacts_json") or {}),
        "fingerprints_json": _normalize_fingerprints(record.get("fingerprints_json")),
        "metadata_json": _sanitize_json(record.get("metadata_json") or {}),
    }
    if status == "running" and not normalized["started_at"]:
        normalized["started_at"] = now
    if status in {"passed", "failed", "error", "cancelled"} and not normalized["completed_at"]:
        normalized["completed_at"] = now
    return normalized


def create_verification_run(
    project: str,
    *,
    trigger_task_id: str | None = None,
    run_type: str = "post_task",
    status: str = "pending",
    results: Mapping[str, Any] | None = None,
    artifacts: Mapping[str, Any] | None = None,
    fingerprints: list[str] | None = None,
    metadata: Mapping[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    run = normalize_verification_run({
        "id": run_id,
        "project": project,
        "trigger_task_id": trigger_task_id,
        "run_type": run_type,
        "status": status,
        "results_json": results or {},
        "artifacts_json": artifacts or {},
        "fingerprints_json": fingerprints or [],
        "metadata_json": metadata or {},
    })
    db.verification_run_upsert(run)
    return db.verification_run_get(run["id"])


def load_verification_run(run_id: str) -> dict[str, Any] | None:
    return db.verification_run_get(run_id)


def list_verification_runs(project: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    return db.verification_run_list_by_project(project, limit=limit)


def describe_verification_guard(
    project: str,
    *,
    run_type: str = "post_task",
    trigger_task_id: str | None = None,
    now: datetime | None = None,
    throttle_seconds: int = DEFAULT_VERIFICATION_THROTTLE_SECONDS,
    window_seconds: int = DEFAULT_VERIFICATION_WINDOW_SECONDS,
    max_runs_per_window: int = DEFAULT_VERIFICATION_MAX_RUNS_PER_WINDOW,
) -> dict[str, Any]:
    now = now or datetime.now()
    normalized_run_type = _normalize_run_type(run_type)
    runs = list_verification_runs(project, limit=max(max_runs_per_window * 3, 20))
    relevant = [run for run in runs if run.get("run_type") == normalized_run_type]
    active = [run for run in relevant if run.get("status") in ACTIVE_RUN_STATUSES]
    duplicate_inflight = next(
        (
            run for run in active
            if _same_trigger(run.get("trigger_task_id"), trigger_task_id)
        ),
        None,
    )
    if duplicate_inflight is None and active:
        duplicate_inflight = active[0]

    latest_same_trigger = next(
        (
            run for run in relevant
            if _same_trigger(run.get("trigger_task_id"), trigger_task_id)
        ),
        None,
    )
    latest_run = relevant[0] if relevant else None

    recent_window_start = now - timedelta(seconds=max(window_seconds, 0))
    recent_runs = [
        run for run in relevant
        if (_parse_timestamp(run.get("created_at")) or _parse_timestamp(run.get("started_at")) or now) >= recent_window_start
    ]

    throttled = False
    throttle_reason = None
    throttle_target = latest_same_trigger if isinstance(trigger_task_id, str) and trigger_task_id.strip() else latest_run
    if throttle_target and throttle_seconds > 0:
        latest_seen = (
            _parse_timestamp(throttle_target.get("completed_at"))
            or _parse_timestamp(throttle_target.get("started_at"))
            or _parse_timestamp(throttle_target.get("created_at"))
        )
        if latest_seen and (now - latest_seen).total_seconds() < throttle_seconds:
            throttled = True
            throttle_reason = "recent_duplicate"

    budget_exhausted = max_runs_per_window > 0 and len(recent_runs) >= max_runs_per_window
    if budget_exhausted and throttle_reason is None:
        throttle_reason = "run_budget_exhausted"

    return {
        "project": project,
        "run_type": normalized_run_type,
        "trigger_task_id": trigger_task_id,
        "active_run_count": len(active),
        "recent_run_count": len(recent_runs),
        "duplicate_run_id": duplicate_inflight.get("id") if duplicate_inflight else None,
        "latest_run_id": latest_run.get("id") if latest_run else None,
        "latest_matching_run_id": latest_same_trigger.get("id") if latest_same_trigger else None,
        "throttled": throttled or budget_exhausted,
        "throttle_reason": throttle_reason,
        "window_seconds": window_seconds,
        "throttle_seconds": throttle_seconds,
        "max_runs_per_window": max_runs_per_window,
    }


def mark_verification_run_started(run_id: str) -> dict[str, Any]:
    db.verification_run_update(run_id, {
        "status": "running",
        "started_at": datetime.now().isoformat(),
    })
    run = db.verification_run_get(run_id)
    if run is None:
        raise ValueError(f"verification run not found: {run_id}")
    return run


def complete_verification_run(
    run_id: str,
    *,
    status: str,
    results: Mapping[str, Any] | None = None,
    artifacts: Mapping[str, Any] | None = None,
    fingerprints: list[str] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    existing = db.verification_run_get(run_id)
    if existing is None:
        raise ValueError(f"verification run not found: {run_id}")

    now = datetime.now().isoformat()
    normalized_results = normalize_verification_results(results if results is not None else existing.get("results_json"))
    normalized_artifacts = _sanitize_json(artifacts if artifacts is not None else existing.get("artifacts_json") or {})
    normalized_fingerprints = _normalize_fingerprints(
        fingerprints if fingerprints is not None else existing.get("fingerprints_json")
    )
    normalized_metadata = _sanitize_json(metadata if metadata is not None else existing.get("metadata_json") or {})
    normalized_status = _normalize_run_status(status)
    if normalized_status not in {"passed", "failed", "error", "cancelled"}:
        raise ValueError("completed verification run requires terminal status")

    db.verification_run_update(run_id, {
        "status": normalized_status,
        "completed_at": now,
        "results_json": normalized_results,
        "artifacts_json": normalized_artifacts,
        "fingerprints_json": normalized_fingerprints,
        "metadata_json": normalized_metadata,
    })

    project = existing["project"]
    db.project_update(project, {
        "last_verification_at": now,
        "last_verification_status": normalized_status,
    })
    return db.verification_run_get(run_id)


def _same_trigger(left: Any, right: Any) -> bool:
    left_value = left.strip() if isinstance(left, str) else ""
    right_value = right.strip() if isinstance(right, str) else ""
    return left_value == right_value
