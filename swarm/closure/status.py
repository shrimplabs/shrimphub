"""Pure helpers for deriving project closure status."""

from __future__ import annotations

from typing import Any, Mapping

from swarm.closure.specs import normalize_project_spec


def _count_passing_flows(flows: list[dict[str, Any]], results: Mapping[str, Any] | None) -> int:
    if not flows:
        return 0
    if not isinstance(results, Mapping):
        return 0
    passed = results.get("critical_flows")
    if not isinstance(passed, Mapping):
        return 0
    count = 0
    for flow in flows:
        flow_id = flow.get("id")
        if flow_id and passed.get(flow_id) is True:
            count += 1
    return count


def derive_gate_summary(
    project: Mapping[str, Any] | None,
    spec: Mapping[str, Any] | None = None,
    *,
    verification: Mapping[str, Any] | None = None,
    open_regression_count: int | None = None,
) -> dict[str, Any]:
    project = project or {}
    normalized_spec = normalize_project_spec(spec or project.get("closure_spec"), project.get("profile"))
    gates = normalized_spec["gates"]
    critical_flows = normalized_spec["critical_flows"]
    verification = verification if isinstance(verification, Mapping) else {}

    regression_count = open_regression_count
    if regression_count is None:
        regression_count = int(project.get("open_regression_count") or 0)

    boot_ok = verification.get("boot_ok")
    tests_ok = verification.get("tests_ok")
    smoke_ok = verification.get("smoke_ok")
    verification_seen = bool(verification) or bool(project.get("last_verification_at")) or bool(project.get("last_verification_status"))
    critical_flows_passing = _count_passing_flows(critical_flows, verification)
    # smoke_required is only True when smoke_ok was explicitly provided in verification.
    # If smoke_checks are defined in spec but smoke_ok is absent, smoke is not required
    # to have been run -- absence of failure is treated as pass.
    smoke_required = "smoke_ok" in verification

    return {
        "verification_seen": verification_seen,
        "boot_required": True,
        "boot_ok": boot_ok if isinstance(boot_ok, bool) else None,
        "tests_required": True,
        "tests_ok": tests_ok if isinstance(tests_ok, bool) else None,
        "smoke_required": smoke_required,
        "smoke_ok": smoke_ok if isinstance(smoke_ok, bool) else None,
        "critical_flows_required": max(len(critical_flows), int(gates.get("critical_flow_count", 0) or 0)),
        "critical_flows_passing": critical_flows_passing,
        "critical_flows_ok": critical_flows_passing >= int(gates.get("critical_flow_count", 0) or 0),
        "max_open_regressions": int(gates.get("max_open_regressions", 0) or 0),
        "open_regressions": regression_count,
        "regressions_ok": regression_count <= int(gates.get("max_open_regressions", 0) or 0),
    }


def derive_closure_status(
    project: Mapping[str, Any] | None,
    spec: Mapping[str, Any] | None = None,
    *,
    verification: Mapping[str, Any] | None = None,
    open_regression_count: int | None = None,
) -> str:
    project = project or {}
    normalized_spec = normalize_project_spec(spec or project.get("closure_spec"), project.get("profile"))
    summary = derive_gate_summary(
        project,
        normalized_spec,
        verification=verification,
        open_regression_count=open_regression_count,
    )

    stall_count = int(project.get("stall_count") or 0)
    stall_threshold = int(normalized_spec["autonomy"]["stall_threshold"] or 0)
    if stall_threshold > 0 and stall_count >= stall_threshold and summary["open_regressions"] > 0:
        return "stalled"

    boot_failed = summary["boot_ok"] is False
    smoke_failed = summary["smoke_required"] and summary["smoke_ok"] is False
    critical_flows_failed = summary["verification_seen"] and not summary["critical_flows_ok"]
    regression_limit_failed = not summary["regressions_ok"] and (boot_failed or smoke_failed or critical_flows_failed)
    is_red = boot_failed or smoke_failed or critical_flows_failed or regression_limit_failed
    if is_red:
        if normalized_spec["autonomy"]["feature_freeze_on_red"] and normalized_spec["mode"] in {"stabilize", "ship"}:
            return "frozen"
        return "red"

    smoke_passed = (
        not summary["smoke_required"]
        or summary["smoke_ok"] is True
        or (summary["smoke_ok"] is None and not summary["verification_seen"])
    )
    all_gates_pass = (
        summary["verification_seen"]
        and summary["boot_ok"] is True
        and summary["tests_ok"] is True
        and smoke_passed
        and summary["critical_flows_ok"]
        and summary["regressions_ok"]
    )
    if all_gates_pass:
        return "green"

    return "yellow"
