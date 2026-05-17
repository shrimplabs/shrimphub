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


def test_closure_status_stalled_when_threshold_is_reached():
    status = derive_closure_status(
        {"profile": "python", "stall_count": 3, "open_regression_count": 2},
        {"autonomy": {"stall_threshold": 3}},
        verification={"boot_ok": True, "tests_ok": True, "critical_flows": {"main-flow": True}},
    )
    assert status == "stalled"
