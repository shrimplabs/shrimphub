from swarm.closure.specs import (
    GODOT_GUT_COMMAND,
    default_project_spec,
    normalize_closure_profile,
    normalize_godot_command,
    normalize_project_spec,
    project_spec_from_project,
)


def test_profile_aliases_normalize_to_supported_profiles():
    assert normalize_closure_profile("godot-game") == "godot"
    assert normalize_closure_profile("software") == "python"
    assert normalize_closure_profile("react") == "typescript"
    assert normalize_closure_profile("unknown-value") == "python"


def test_default_project_spec_uses_profile_defaults():
    godot = default_project_spec("godot")
    python = default_project_spec("python")
    typescript = default_project_spec("typescript")

    assert godot["profile"] == "godot"
    assert godot["verification"]["smoke_checks"][0]["type"] == "command"
    assert godot["verification"]["smoke_checks"][0]["id"] == "boot"
    assert godot["verification"]["smoke_checks"][0]["command"] == "godot --headless --path . --quit"
    assert python["verification"]["smoke_checks"][0]["type"] == "command"
    assert typescript["verification"]["smoke_checks"][0]["type"] == "browser"
    assert python["mode"] == "build"
    assert python["gates"]["critical_flow_count"] == 1
    assert python["autonomy"]["feature_freeze_on_red"] is True


def test_godot_gut_command_normalizes_legacy_bare_gut():
    assert normalize_godot_command("gut --run --exit") == GODOT_GUT_COMMAND

    spec = normalize_project_spec(
        {
            "verification": {
                "unit_test_command": "gut --run --exit",
                "smoke_checks": [{"id": "gut", "type": "command", "command": "gut --run --exit"}],
            },
        },
        "godot",
    )

    assert spec["verification"]["unit_test_command"] == GODOT_GUT_COMMAND
    assert spec["verification"]["smoke_checks"][0]["command"] == GODOT_GUT_COMMAND


def test_normalize_project_spec_merges_partial_input():
    spec = normalize_project_spec(
        {
            "mode": "stabilize",
            "boot": {"command": "npm run dev"},
            "verification": {"unit_test_command": "npm test"},
            "gates": {"max_open_regressions": 2},
            "autonomy": {"repair_budget": 12},
        },
        "typescript",
    )

    assert spec["profile"] == "typescript"
    assert spec["mode"] == "stabilize"
    assert spec["boot"]["command"] == "npm run dev"
    assert spec["boot"]["ready_check"]["type"] == "http"
    assert spec["verification"]["unit_test_command"] == "npm test"
    assert spec["verification"]["smoke_checks"][0]["type"] == "browser"
    assert spec["gates"]["max_open_regressions"] == 2
    assert spec["autonomy"]["repair_budget"] == 12


def test_normalize_project_spec_handles_malformed_sections_safely():
    spec = normalize_project_spec(
        {
            "mode": "bad-mode",
            "boot": "not-a-dict",
            "verification": {"smoke_checks": "bad"},
            "critical_flows": "bad",
            "gates": {"critical_flow_count": "bad"},
        },
        "python",
    )

    assert spec["mode"] == "build"
    assert spec["boot"]["ready_check"]["type"] == "http"
    assert spec["verification"]["smoke_checks"][0]["type"] == "command"
    assert spec["critical_flows"][0]["id"] == "main-flow"
    assert spec["gates"]["critical_flow_count"] == 1


def test_normalize_project_spec_strict_mode_fails_clearly():
    try:
        normalize_project_spec({"mode": "bad-mode"}, "python", strict=True)
    except ValueError as exc:
        assert "invalid closure mode" in str(exc)
    else:
        raise AssertionError("strict mode should reject invalid mode")


def test_project_spec_from_project_uses_project_profile_and_saved_spec():
    spec = project_spec_from_project({
        "profile": "godot",
        "closure_spec": {
            "mode": "ship",
            "verification": {
                "smoke_checks": [{"id": "scene", "type": "godot_scene"}],
            },
        },
    })

    assert spec["profile"] == "godot"
    assert spec["mode"] == "ship"
    assert spec["verification"]["smoke_checks"] == [{"id": "scene", "type": "godot_scene"}]
