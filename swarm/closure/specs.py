"""Normalization helpers for project closure specs.

This module is intentionally pure. It does not touch the DB or orchestrator.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


DEFAULT_AUTONOMY = {
    "repair_budget": 8,
    "stall_threshold": 3,
    "feature_freeze_on_red": True,
}

DEFAULT_GATES = {
    "boot_ok": False,
    "tests_ok": False,
    "critical_flow_count": 1,
    "max_open_regressions": 0,
}

GODOT_BOOT_COMMAND = "godot --headless --path . --quit"
GODOT_GUT_COMMAND = (
    "godot --headless --path . --script res://addons/gut/gut_cmdln.gd "
    "-- -gdir=res://test -ginclude_subdirs -gexit"
)

PROFILE_DEFAULTS = {
    "godot": {
        "boot": {"command": None, "cwd": None, "ready_check": {"type": "process"}},
        "verification": {
            "unit_test_command": None,
            "integration_test_command": None,
            # GUT-based smoke check — runs with full project context (autoloads registered).
            # Do NOT use `godot -s <script>` here; that runs without autoloads and causes
            # compile errors for any script that references autoload singletons.
            "smoke_checks": [{"id": "main-scene", "type": "command", "command": GODOT_GUT_COMMAND}],
        },
        "critical_flows": [{"id": "main-flow", "description": "Launch the project and reach the primary playable scene."}],
    },
    "python": {
        "boot": {"command": None, "cwd": None, "ready_check": {"type": "http"}},
        "verification": {
            "unit_test_command": None,
            "integration_test_command": None,
            "smoke_checks": [{"id": "service-smoke", "type": "command"}],
        },
        "critical_flows": [{"id": "main-flow", "description": "Start the service and exercise the primary request path."}],
    },
    "typescript": {
        "boot": {"command": None, "cwd": None, "ready_check": {"type": "http"}},
        "verification": {
            "unit_test_command": None,
            "integration_test_command": None,
            "smoke_checks": [{"id": "ui-smoke", "type": "browser"}],
        },
        "critical_flows": [{"id": "main-flow", "description": "Load the primary UI and exercise the main user journey."}],
    },
}


def normalize_closure_profile(profile: str | None) -> str:
    value = (profile or "python").strip().lower()
    if value in {"game", "godot-game", "godot_game"}:
        return "godot"
    if value in {"software", "programming", "app", "python-app", "python_app"}:
        return "python"
    if value in {"ts", "tsx", "js", "javascript", "node", "web", "react"}:
        return "typescript"
    if value in PROFILE_DEFAULTS:
        return value
    return "python"


def normalize_godot_command(command: str | None) -> str | None:
    if command is None or not isinstance(command, str):
        return command
    normalized = " ".join(command.strip().split())
    if normalized in {"gut --run --exit", "gut run", "gut"}:
        return GODOT_GUT_COMMAND
    return command


def default_project_spec(profile: str | None = None) -> dict[str, Any]:
    normalized_profile = normalize_closure_profile(profile)
    defaults = deepcopy(PROFILE_DEFAULTS[normalized_profile])
    return {
        "profile": normalized_profile,
        "mode": "build",
        "boot": defaults["boot"],
        "verification": defaults["verification"],
        "critical_flows": defaults["critical_flows"],
        "gates": deepcopy(DEFAULT_GATES),
        "autonomy": deepcopy(DEFAULT_AUTONOMY),
    }


def normalize_project_spec(raw_spec: Mapping[str, Any] | None, profile: str | None = None, *, strict: bool = False) -> dict[str, Any]:
    if raw_spec is None:
        raw_spec = {}
    if not isinstance(raw_spec, Mapping):
        raise ValueError("project closure spec must be a mapping")

    spec = default_project_spec(profile or raw_spec.get("profile"))

    mode = raw_spec.get("mode")
    if mode is not None:
        if mode not in {"build", "stabilize", "ship"}:
            if strict:
                raise ValueError(f"invalid closure mode: {mode}")
        else:
            spec["mode"] = mode

    boot = raw_spec.get("boot")
    if boot is not None:
        if not isinstance(boot, Mapping):
            if strict:
                raise ValueError("boot must be a mapping")
        else:
            normalized_boot = deepcopy(spec["boot"])
            for key in ("command", "cwd"):
                if key in boot:
                    value = boot.get(key)
                    if value is None or isinstance(value, str):
                        if key == "command" and spec["profile"] == "godot":
                            value = normalize_godot_command(value)
                        normalized_boot[key] = value
                    elif strict:
                        raise ValueError(f"boot.{key} must be a string or null")
            if "ready_check" in boot:
                ready_check = boot.get("ready_check")
                if ready_check is None:
                    normalized_boot["ready_check"] = None
                elif isinstance(ready_check, Mapping):
                    normalized_ready_check = dict(ready_check)
                    if spec["profile"] == "godot" and normalized_ready_check.get("type") == "command":
                        normalized_ready_check["command"] = normalize_godot_command(normalized_ready_check.get("command"))
                    normalized_boot["ready_check"] = normalized_ready_check
                elif strict:
                    raise ValueError("boot.ready_check must be a mapping or null")
            spec["boot"] = normalized_boot

    verification = raw_spec.get("verification")
    if verification is not None:
        if not isinstance(verification, Mapping):
            if strict:
                raise ValueError("verification must be a mapping")
        else:
            normalized_verification = deepcopy(spec["verification"])
            for key in ("unit_test_command", "integration_test_command"):
                value = verification.get(key)
                if value is None or isinstance(value, str):
                    if key in verification:
                        if spec["profile"] == "godot":
                            value = normalize_godot_command(value)
                        normalized_verification[key] = value
                elif strict:
                    raise ValueError(f"verification.{key} must be a string or null")
            smoke_checks = verification.get("smoke_checks")
            if smoke_checks is not None:
                if not isinstance(smoke_checks, list):
                    if strict:
                        raise ValueError("verification.smoke_checks must be a list")
                else:
                    normalized_smoke_checks = []
                    for item in smoke_checks:
                        if not isinstance(item, Mapping):
                            continue
                        normalized_item = dict(item)
                        if spec["profile"] == "godot" and normalized_item.get("type") == "command":
                            normalized_item["command"] = normalize_godot_command(normalized_item.get("command"))
                        normalized_smoke_checks.append(normalized_item)
                    normalized_verification["smoke_checks"] = normalized_smoke_checks
            spec["verification"] = normalized_verification

    critical_flows = raw_spec.get("critical_flows")
    if critical_flows is not None:
        if not isinstance(critical_flows, list):
            if strict:
                raise ValueError("critical_flows must be a list")
        else:
            spec["critical_flows"] = [dict(item) for item in critical_flows if isinstance(item, Mapping)]

    gates = raw_spec.get("gates")
    if gates is not None:
        if not isinstance(gates, Mapping):
            if strict:
                raise ValueError("gates must be a mapping")
        else:
            normalized_gates = deepcopy(spec["gates"])
            for key in ("boot_ok", "tests_ok"):
                if key in gates:
                    value = gates[key]
                    if isinstance(value, bool):
                        normalized_gates[key] = value
                    elif strict:
                        raise ValueError(f"gates.{key} must be a boolean")
            for key in ("critical_flow_count", "max_open_regressions"):
                if key in gates:
                    value = gates[key]
                    if isinstance(value, int) and value >= 0:
                        normalized_gates[key] = value
                    elif strict:
                        raise ValueError(f"gates.{key} must be a non-negative integer")
            spec["gates"] = normalized_gates

    autonomy = raw_spec.get("autonomy")
    if autonomy is not None:
        if not isinstance(autonomy, Mapping):
            if strict:
                raise ValueError("autonomy must be a mapping")
        else:
            normalized_autonomy = deepcopy(spec["autonomy"])
            for key in ("repair_budget", "stall_threshold"):
                if key in autonomy:
                    value = autonomy[key]
                    if isinstance(value, int) and value >= 0:
                        normalized_autonomy[key] = value
                    elif strict:
                        raise ValueError(f"autonomy.{key} must be a non-negative integer")
            if "feature_freeze_on_red" in autonomy:
                value = autonomy["feature_freeze_on_red"]
                if isinstance(value, bool):
                    normalized_autonomy["feature_freeze_on_red"] = value
                elif strict:
                    raise ValueError("autonomy.feature_freeze_on_red must be a boolean")
            spec["autonomy"] = normalized_autonomy

    return spec


def project_spec_from_project(project: Mapping[str, Any] | None) -> dict[str, Any]:
    if not project:
        return default_project_spec(None)
    return normalize_project_spec(project.get("closure_spec"), project.get("profile"))
