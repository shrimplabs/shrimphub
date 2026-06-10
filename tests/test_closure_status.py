from swarm.closure.status import derive_closure_status, derive_gate_summary


def test_gate_summary_uses_spec_and_verification_results():
    summary = derive_gate_summary(
        {"profile": "python", "open_regression_count": 1},
        {
            "profile": "python",
            "mode": "build",
            "boot": {"command": None, "cwd": None, "ready_check": {"type": "http"}},
            "verification": {"unit_test_command": None, "integration_test_command": None, "smoke_checks": []},
            "critical_flows": [{"id": "main-flow", "description": "main"}],
            "gates": {"boot_ok": False, "tests_ok": False, "critical_flow_count": 1, "max_open_regressions": 2},
            "autonomy": {"repair_budget": 8, "stall_threshold": 3, "feature_freeze_on_red": True},
        },
        verification={
            "boot_ok": True,
            "tests_ok": False,
            "critical_flows": {"main-flow": True},
        },
    )

    assert summary["verification_seen"] is True
    assert summary["boot_ok"] is True
    assert summary["tests_ok"] is False
    assert summary["critical_flows_required"] == 1
    assert summary["critical_flows_passing"] == 1
    assert summary["critical_flows_ok"] is True
    assert summary["regressions_ok"] is True


def test_closure_status_defaults_to_yellow_without_verification():
    status = derive_closure_status({"profile": "godot", "open_regression_count": 0})
    assert status == "yellow"


def test_closure_status_red_when_boot_fails():
    status = derive_closure_status(
        {"profile": "python"},
        verification={"boot_ok": False, "tests_ok": True, "critical_flows": {"main-flow": True}},
    )
    assert status == "red"


def test_closure_status_frozen_when_red_and_policy_freezes_features():
    status = derive_closure_status(
        {"profile": "python"},
        {"mode": "stabilize", "autonomy": {"feature_freeze_on_red": True}},
        verification={"boot_ok": False, "tests_ok": True, "critical_flows": {"main-flow": True}},
    )
    assert status == "frozen"


def test_closure_status_green_when_required_gates_pass():
    status = derive_closure_status(
        {"profile": "typescript", "open_regression_count": 0, "last_verification_at": "2026-01-01T00:00:00"},
        verification={"boot_ok": True, "tests_ok": True, "critical_flows": {"main-flow": True}},
    )
    assert status == "green"


# ===========================================================================
# WS7 — Closure status consistency (stale-red reconciliation)
# ===========================================================================

def _reconcile_closure_status(closure_status: str, terminal_status: str, open_regressions: int) -> str:
    """Mirror of the reconciliation step added to validation._run_verification_checks."""
    if closure_status == "red" and terminal_status == "passed" and open_regressions == 0:
        return "yellow"
    return closure_status


def test_stale_red_reconciled_to_yellow_when_verification_passed_and_no_regressions():
    """If verification just passed and open_regression_count=0, closure_status cannot stay red."""
    # This is the exact run-4 symptom: gate spec has unexercised critical flows,
    # derive_closure_status returns red, but the run itself passed with 0 regressions.
    spec = {
        "critical_flows": [{"id": "flow-a"}, {"id": "flow-b"}],
        "gates": {"critical_flow_count": 2, "max_open_regressions": 0},
    }
    # Verification run that passes boot+tests but doesn't cover critical flows
    verification = {"boot_ok": True, "tests_ok": True}
    raw_status = derive_closure_status(
        {"profile": "godot", "open_regression_count": 0},
        spec,
        verification=verification,
    )
    # derive_closure_status alone would return red (critical flows not exercised)
    assert raw_status == "red"

    # Reconciliation must override to yellow
    reconciled = _reconcile_closure_status(raw_status, terminal_status="passed", open_regressions=0)
    assert reconciled == "yellow", f"Expected yellow after reconciliation, got {reconciled}"


def test_red_preserved_when_regressions_exist():
    """Reconciliation must NOT clear red if there are open regressions."""
    reconciled = _reconcile_closure_status("red", terminal_status="passed", open_regressions=1)
    assert reconciled == "red"


def test_red_preserved_when_terminal_status_failed():
    """Reconciliation must NOT clear red if the verification run itself failed."""
    reconciled = _reconcile_closure_status("red", terminal_status="failed", open_regressions=0)
    assert reconciled == "red"


def test_green_unaffected_by_reconciliation():
    """Reconciliation must not touch green status."""
    reconciled = _reconcile_closure_status("green", terminal_status="passed", open_regressions=0)
    assert reconciled == "green"


def test_yellow_unaffected_by_reconciliation():
    """Reconciliation must not touch yellow status."""
    reconciled = _reconcile_closure_status("yellow", terminal_status="passed", open_regressions=0)
    assert reconciled == "yellow"


def test_closure_status_stalled_when_threshold_is_reached():
    status = derive_closure_status(
        {"profile": "python", "stall_count": 3, "open_regression_count": 2},
        {"autonomy": {"stall_threshold": 3}},
        verification={"boot_ok": True, "tests_ok": True, "critical_flows": {"main-flow": True}},
    )
    assert status == "stalled"
