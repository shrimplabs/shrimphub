"""Tests for the ToolSpec registry in swarm.tool_dispatch.

Covers:
- ToolSpec structure and registry population
- Required-arg validation driven from registry
- Normal tool availability (all task types)
- QA-only tool availability (qa / art_pass / hybrid_qa)
- Harness QA tool availability (harness_qa / hybrid_qa)
- Alias dispatch (model hallucinations map to canonical tools)
- _TOOL_REQUIRED_ARGS backward-compat view
"""

from unittest.mock import patch, MagicMock

import pytest

from swarm.tool_dispatch import (
    ToolSpec,
    _TOOL_REGISTRY,
    _TOOL_REQUIRED_ARGS,
    _registry_by_name,
    validate_tool_call,
)


# ---------------------------------------------------------------------------
# ToolSpec structure
# ---------------------------------------------------------------------------

class TestToolSpec:
    def test_registry_is_non_empty(self):
        assert len(_TOOL_REGISTRY) > 0

    def test_each_spec_has_name_and_handler(self):
        for spec in _TOOL_REGISTRY:
            assert spec.name, f"ToolSpec missing name: {spec}"
            assert callable(spec.handler), f"ToolSpec {spec.name} handler not callable"

    def test_required_args_is_list(self):
        for spec in _TOOL_REGISTRY:
            assert isinstance(spec.required_args, list), f"{spec.name}.required_args is not a list"

    def test_task_types_is_set(self):
        for spec in _TOOL_REGISTRY:
            assert isinstance(spec.task_types, set), f"{spec.name}.task_types is not a set"

    def test_aliases_is_list(self):
        for spec in _TOOL_REGISTRY:
            assert isinstance(spec.aliases, list), f"{spec.name}.aliases is not a list"


# ---------------------------------------------------------------------------
# Required-args index (backward compat)
# ---------------------------------------------------------------------------

class TestRequiredArgsIndex:
    def test_index_populated(self):
        assert len(_TOOL_REQUIRED_ARGS) > 0

    def test_read_file_requires_path(self):
        assert "path" in _TOOL_REQUIRED_ARGS["read_file"]

    def test_write_file_requires_path_and_content(self):
        req = _TOOL_REQUIRED_ARGS["write_file"]
        assert "path" in req
        assert "content" in req

    def test_create_task_requires_description(self):
        assert "description" in _TOOL_REQUIRED_ARGS["create_task"]

    def test_broadcast_read_has_no_required_args(self):
        assert _TOOL_REQUIRED_ARGS.get("broadcast_read", []) == []


# ---------------------------------------------------------------------------
# validate_tool_call — driven from registry
# ---------------------------------------------------------------------------

class TestValidateToolCall:
    def test_missing_tool_field(self):
        err = validate_tool_call({"args": {}})
        assert "missing" in err.lower()

    def test_args_not_dict(self):
        err = validate_tool_call({"tool": "read_file", "args": "not-a-dict"})
        assert "JSON object" in err

    def test_missing_required_arg(self):
        err = validate_tool_call({"tool": "read_file", "args": {}})
        assert "path" in err

    def test_valid_call_returns_empty_string(self):
        err = validate_tool_call({"tool": "read_file", "args": {"path": "/some/file.txt"}})
        assert err == ""

    def test_unknown_tool_passes_validation(self):
        # Validation only checks required args for known tools; unknown tools
        # are rejected at dispatch time, not validation time.
        err = validate_tool_call({"tool": "nonexistent_tool", "args": {}})
        assert err == ""

    def test_create_tasks_file_aware_requires_tasks(self):
        err = validate_tool_call({"tool": "create_tasks_file_aware", "args": {}})
        assert "tasks" in err

    def test_patch_file_requires_three_args(self):
        err = validate_tool_call({"tool": "patch_file", "args": {"path": "f.gd"}})
        assert err != ""  # missing old and new


# ---------------------------------------------------------------------------
# Registry lookup (canonical + alias)
# ---------------------------------------------------------------------------

class TestRegistryByName:
    def test_canonical_tools_are_in_registry(self):
        reg = _registry_by_name()
        for name in ("read_file", "write_file", "create_task", "broadcast_write"):
            assert name in reg, f"{name} not found in registry"

    def test_alias_maps_to_canonical_spec(self):
        reg = _registry_by_name()
        # start_game is an alias for launch_game
        if "start_game" in reg:
            assert reg["start_game"].name == "launch_game"

    def test_harness_get_state_alias_maps_to_get_game_state(self):
        reg = _registry_by_name()
        if "harness_get_state" in reg:
            assert reg["harness_get_state"].name == "get_game_state"


# ---------------------------------------------------------------------------
# Normal tool availability (task_types empty = all task types)
# ---------------------------------------------------------------------------

class TestNormalToolAvailability:
    def test_read_file_has_no_task_type_restriction(self):
        reg = _registry_by_name()
        assert reg["read_file"].task_types == set()

    def test_write_file_has_no_task_type_restriction(self):
        reg = _registry_by_name()
        assert reg["write_file"].task_types == set()

    def test_create_task_has_no_task_type_restriction(self):
        reg = _registry_by_name()
        assert reg["create_task"].task_types == set()


# ---------------------------------------------------------------------------
# QA-only tool availability
# ---------------------------------------------------------------------------

class TestQAToolAvailability:
    def test_vision_query_restricted_to_qa_types(self):
        reg = _registry_by_name()
        spec = reg.get("vision_query")
        assert spec is not None, "vision_query not in registry"
        assert "qa" in spec.task_types
        assert "art_pass" in spec.task_types

    def test_take_screenshot_restricted_to_qa_types(self):
        reg = _registry_by_name()
        spec = reg.get("take_screenshot")
        assert spec is not None
        assert "qa" in spec.task_types

    def test_create_bug_task_available_in_qa(self):
        reg = _registry_by_name()
        # At least one spec named create_bug_task should include qa
        specs = [s for s in _TOOL_REGISTRY if s.name in ("create_bug_task", "create_bug_task_harness")]
        assert any("qa" in s.task_types for s in specs)

    def test_normal_tools_not_restricted_to_qa(self):
        reg = _registry_by_name()
        # run_command should not be QA-only
        spec = reg.get("run_command")
        assert spec is not None
        assert not spec.task_types or "qa" not in spec.task_types or len(spec.task_types) > 1


# ---------------------------------------------------------------------------
# Harness QA tool availability
# ---------------------------------------------------------------------------

class TestHarnessToolAvailability:
    def test_harness_step_restricted_to_harness_types(self):
        reg = _registry_by_name()
        spec = reg.get("harness_step")
        assert spec is not None, "harness_step not in registry"
        assert "harness_qa" in spec.task_types or "hybrid_qa" in spec.task_types

    def test_harness_kill_game_restricted_to_harness_types(self):
        reg = _registry_by_name()
        spec = reg.get("harness_kill_game")
        assert spec is not None
        assert "harness_qa" in spec.task_types or "hybrid_qa" in spec.task_types

    def test_harness_launch_game_not_available_for_normal_tasks(self):
        reg = _registry_by_name()
        spec = reg.get("harness_launch_game")
        assert spec is not None
        # Should NOT be available for feature tasks
        assert "feature" not in spec.task_types


# ---------------------------------------------------------------------------
# Godot-only tool
# ---------------------------------------------------------------------------

class TestGodotOnlyTools:
    def test_launch_game_base_spec_is_godot_only(self):
        # The base (non-QA-override) launch_game spec must be godot_only.
        # The QA override (task_types=_QA_TYPES) is not godot_only — it is
        # restricted by task_type instead.
        base_specs = [s for s in _TOOL_REGISTRY if s.name == "launch_game" and s.godot_only]
        assert base_specs, "No godot_only launch_game spec found in registry"

    def test_get_game_state_is_godot_only(self):
        reg = _registry_by_name()
        spec = reg.get("get_game_state")
        assert spec is not None
        assert spec.godot_only is True
