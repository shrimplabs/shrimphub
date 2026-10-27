from swarm.model_policy import (
    DEFAULT_POLICY_TABLE,
    resolve_policy,
    legacy_fallback_provider,
)
from swarm.model_routing import choose_adaptive_flat_provider


def test_resolve_policy_falls_back_to_default_table_when_no_config():
    decision = resolve_policy(
        {},
        project="brand-new-project",
        task_type="feature",
        phase="work",
        complexity="normal",
        default_provider="minimax",
    )

    assert decision.tier_ceiling == "either"
    assert decision.adaptive_enabled is False  # opt-in only; see DEFAULT_POLICY_TABLE comment
    assert decision.reason == "policy_table"


def test_resolve_policy_reproduces_strong_task_types_for_bug_at_normal_complexity():
    decision = resolve_policy(
        {},
        project="brand-new-project",
        task_type="bug",
        phase="work",
        complexity="normal",
        default_provider="minimax",
    )

    assert decision.tier_ceiling == "strong"
    assert decision.adaptive_enabled is False


def test_resolve_policy_complex_forces_strong_even_for_non_strong_task_type():
    # "feature" is not in legacy STRONG_TASK_TYPES, but a complex feature task
    # should still get a strong ceiling under the wildcard default entry.
    decision = resolve_policy(
        {},
        project="brand-new-project",
        task_type="feature",
        phase="work",
        complexity="complex",
        default_provider="minimax",
    )

    assert decision.tier_ceiling == "strong"
    assert decision.adaptive_enabled is False


def test_resolve_policy_project_override_wins_over_default_table():
    config = {
        "model_policy": {
            "projects": {
                "my-project": {
                    "bug": {"work": {"normal": {"tier": "fast", "adaptive": True}}}
                }
            }
        }
    }

    decision = resolve_policy(
        config,
        project="my-project",
        task_type="bug",
        phase="work",
        complexity="normal",
        default_provider="minimax",
    )

    assert decision.tier_ceiling == "fast"
    assert decision.adaptive_enabled is True


def test_resolve_policy_legacy_fixed_provider_collapses_to_no_adaptivity():
    # A project with an old-style _work_provider override and no model_policy
    # entry of its own must reproduce today's single-fixed-provider behavior,
    # even for a task_type that DEFAULT_POLICY_TABLE would otherwise mark
    # adaptive (e.g. "feature").
    config = {
        "project_pipelines": {
            "legacy-project": {"_work_provider": "claude"},
        }
    }

    decision = resolve_policy(
        config,
        project="legacy-project",
        task_type="feature",
        phase="work",
        complexity="normal",
        default_provider="minimax",
    )

    assert decision.adaptive_enabled is False
    assert decision.fast_provider == "claude"
    assert decision.strong_provider == "claude"
    assert decision.reason == "legacy_fixed_provider"


def test_resolve_policy_explicit_model_policy_entry_overrides_legacy_provider():
    # If a project has BOTH a legacy _work_provider key AND an explicit
    # model_policy entry, the explicit entry wins (opting the project into
    # the new declarative table).
    config = {
        "project_pipelines": {
            "migrating-project": {"_work_provider": "claude"},
        },
        "model_policy": {
            "projects": {
                "migrating-project": {
                    "bug": {"work": {"normal": {"tier": "fast", "adaptive": True}}}
                }
            }
        },
    }

    decision = resolve_policy(
        config,
        project="migrating-project",
        task_type="bug",
        phase="work",
        complexity="normal",
        default_provider="minimax",
    )

    assert decision.tier_ceiling == "fast"
    assert decision.adaptive_enabled is True
    assert decision.reason == "policy_table"


def test_resolve_policy_flat_phase_never_collapses_to_legacy_provider():
    # phase="flat" (the adaptive-flat loop) must never collapse to the
    # single-fixed-provider fallback, even if the project happens to also
    # have an unrelated legacy provider key set (e.g. _flat_provider).
    config = {
        "project_pipelines": {
            "flat-project": {"_flat_provider": "claude"},
        }
    }

    decision = resolve_policy(
        config,
        project="flat-project",
        task_type="feature",
        phase="flat",
        complexity="normal",
        default_provider="minimax",
    )

    assert decision.reason == "policy_table"
    # adaptive_enabled is irrelevant to the flat-loop path (M3 never reads it,
    # only tier_ceiling/strong_bias) -- what matters here is that reason is
    # "policy_table", not "legacy_fixed_provider".


def test_legacy_fallback_provider_prefers_project_over_global():
    config = {"work_provider": "minimax-global"}
    project_cfg = {"_work_provider": "minimax-project"}

    assert legacy_fallback_provider(config, project_cfg, "work") == "minimax-project"


def test_legacy_fallback_provider_falls_back_to_global():
    config = {"work_provider": "minimax-global"}

    assert legacy_fallback_provider(config, {}, "work") == "minimax-global"


def test_legacy_fallback_provider_returns_none_when_unconfigured():
    assert legacy_fallback_provider({}, {}, "work") is None


# --- M3 integration: resolve_policy -> choose_adaptive_flat_provider parity ---


def _route_via_policy(task_type, last_tools=None, next_tools=None, complexity="normal", loop_index=2, consecutive_cheap_loops=0):
    """Mirror agent_runtime.py's flat-loop call site exactly."""
    loop_model_routing = {"enabled": True, "fast_provider": "athena", "strong_provider": "minimax"}
    policy = resolve_policy(
        {"model_policy": {}, "loop_model_routing": loop_model_routing},
        project="unconfigured-project",
        task_type=task_type,
        phase="flat",
        complexity=complexity,
        default_provider="minimax",
    )
    strong_bias = (
        True if policy.tier_ceiling == "strong"
        else False if policy.tier_ceiling == "fast"
        else None
    )
    return choose_adaptive_flat_provider(
        loop_model_routing,
        default_provider="minimax",
        fast_provider="athena",
        strong_provider="minimax",
        task_type=task_type,
        loop_index=loop_index,
        last_tools=last_tools or [],
        next_tools=next_tools or [],
        consecutive_cheap_loops=consecutive_cheap_loops,
        strong_bias=strong_bias,
        tier_ceiling=policy.tier_ceiling,
    )


def _route_legacy(task_type, last_tools=None, next_tools=None, loop_index=2, consecutive_cheap_loops=0):
    """Pre-graduation call shape: no strong_bias/tier_ceiling passed at all."""
    loop_model_routing = {"enabled": True, "fast_provider": "athena", "strong_provider": "minimax"}
    return choose_adaptive_flat_provider(
        loop_model_routing,
        default_provider="minimax",
        fast_provider="athena",
        strong_provider="minimax",
        task_type=task_type,
        loop_index=loop_index,
        last_tools=last_tools or [],
        next_tools=next_tools or [],
        consecutive_cheap_loops=consecutive_cheap_loops,
    )


def test_m3_wiring_is_byte_identical_to_legacy_for_every_strong_task_type():
    legacy_strong_task_types = {
        "art_pass", "polish", "qa", "harness_qa", "hybrid_qa", "scenario_qa", "bug",
    }
    scenarios = [
        {"last_tools": ["vision_query"]},
        {"last_tools": ["read_file"]},
        {"next_tools": ["write_file"]},
        {"next_tools": ["read_file", "list_files"]},
        {"last_tools": [], "next_tools": []},
    ]
    for task_type in legacy_strong_task_types:
        for scenario in scenarios:
            via_policy = _route_via_policy(task_type, **scenario)
            via_legacy = _route_legacy(task_type, **scenario)
            assert via_policy == via_legacy, f"{task_type} {scenario}: {via_policy} != {via_legacy}"


def test_m3_wiring_is_byte_identical_to_legacy_for_non_strong_task_types():
    non_strong_task_types = ["feature", "refactor", "research", "plan", "audit"]
    scenarios = [
        {"last_tools": ["read_file"]},
        {"next_tools": ["write_file"]},
        {"next_tools": ["read_file"]},
        {"loop_index": 0},
    ]
    for task_type in non_strong_task_types:
        for scenario in scenarios:
            via_policy = _route_via_policy(task_type, **scenario)
            via_legacy = _route_legacy(task_type, **scenario)
            assert via_policy == via_legacy, f"{task_type} {scenario}: {via_policy} != {via_legacy}"


def test_m3_wiring_respects_complex_complexity_override():
    # "feature" is not in legacy STRONG_TASK_TYPES, so at complexity="normal"
    # a read-only trailing tool routes fast via the ordinary (non-bias) path.
    decision_normal = _route_via_policy("feature", last_tools=["read_file"], complexity="normal")
    assert decision_normal.tier == "cheap"
    assert decision_normal.reason == "read_only_followup"

    # complexity="complex" is a genuinely NEW capability the legacy call site
    # never had: it routes "feature" through the strong-bias path instead
    # (still dipping to fast for a pure read probe, but via the
    # qualitative-task-type carve-out, not the ordinary heuristic).
    decision_complex = _route_via_policy("feature", last_tools=["read_file"], complexity="complex")
    assert decision_complex.tier == "cheap"
    assert decision_complex.reason == "qualitative_read_only_probe"

    # With a non-read-only trailing tool, the complexity override becomes
    # visible in the tier itself, not just the reason.
    decision_complex_write = _route_via_policy("feature", last_tools=["patch_file"], complexity="complex")
    decision_normal_write = _route_via_policy("feature", last_tools=["patch_file"], complexity="normal")
    assert decision_complex_write.tier == "strong"
    assert decision_complex_write.reason == "strong_task_type"
    assert decision_normal_write.tier == "strong"
    assert decision_normal_write.reason == "prior_strong_tool"


def test_default_policy_table_covers_all_strong_task_types_with_strong_normal():
    # Parity guard: every task type that today's STRONG_TASK_TYPES set forces
    # strong for must resolve to tier="strong" at complexity="normal" here too.
    legacy_strong_task_types = {
        "art_pass", "polish", "qa", "harness_qa", "hybrid_qa", "scenario_qa", "bug",
    }
    for task_type in legacy_strong_task_types:
        entry = DEFAULT_POLICY_TABLE.get(task_type, {}).get("*", {}).get("normal")
        assert entry is not None, f"missing DEFAULT_POLICY_TABLE entry for {task_type}"
        assert entry["tier"] == "strong", f"{task_type} should default to strong at normal complexity"
