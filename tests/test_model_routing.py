from swarm.model_routing import choose_adaptive_flat_provider


def test_adaptive_flat_uses_strong_on_first_loop():
    decision = choose_adaptive_flat_provider(
        {"enabled": True, "fast_provider": "mini-fast", "strong_provider": "mini-strong"},
        default_provider="mini-strong",
        task_type="feature",
        loop_index=0,
        last_tools=[],
    )

    assert decision.provider is None
    assert decision.tier == "strong"
    assert decision.reason == "first_loop_sets_intent"


def test_adaptive_flat_uses_fast_after_read_only_tools():
    decision = choose_adaptive_flat_provider(
        {"enabled": True, "fast_provider": "mini-fast", "strong_provider": "mini-strong"},
        default_provider="mini-strong",
        task_type="feature",
        loop_index=1,
        last_tools=["read_file", "search_code"],
    )

    assert decision.provider == "mini-fast"
    assert decision.tier == "cheap"
    assert decision.reason == "read_only_followup"


def test_adaptive_flat_returns_to_strong_after_write_or_command():
    decision = choose_adaptive_flat_provider(
        {"enabled": True, "fast_provider": "mini-fast", "strong_provider": "mini-strong"},
        default_provider="mini-strong",
        task_type="feature",
        loop_index=3,
        last_tools=["patch_file"],
    )

    assert decision.provider is None
    assert decision.tier == "strong"
    assert decision.reason == "prior_strong_tool"


def test_adaptive_flat_caps_consecutive_cheap_loops():
    decision = choose_adaptive_flat_provider(
        {
            "enabled": True,
            "fast_provider": "mini-fast",
            "strong_provider": "mini-strong",
            "max_consecutive_cheap_loops": 2,
        },
        default_provider="mini-strong",
        task_type="feature",
        loop_index=4,
        last_tools=["read_file"],
        consecutive_cheap_loops=2,
    )

    assert decision.provider is None
    assert decision.tier == "strong"
    assert decision.reason == "cheap_loop_cap"


def test_adaptive_flat_keeps_qualitative_tasks_strong_after_probe():
    decision = choose_adaptive_flat_provider(
        {"enabled": True, "fast_provider": "mini-fast", "strong_provider": "mini-strong"},
        default_provider="mini-strong",
        task_type="art_pass",
        loop_index=2,
        last_tools=["vision_query"],
    )

    assert decision.provider is None
    assert decision.tier == "strong"
    assert decision.reason == "strong_task_type"


def test_adaptive_flat_lookahead_routes_fast_for_read_only_next_tools():
    # next_tools takes priority over last_tools — agent just requested reads,
    # so the *next* LLM call should be on the fast provider.
    decision = choose_adaptive_flat_provider(
        {"enabled": True, "fast_provider": "athena", "strong_provider": "minimax"},
        default_provider="minimax",
        task_type="feature",
        loop_index=2,
        last_tools=["patch_file"],       # trailing signal says strong
        next_tools=["read_file", "search_code"],  # lookahead says cheap
    )

    assert decision.provider == "athena"
    assert decision.tier == "cheap"
    assert decision.reason == "lookahead_read_only"


def test_adaptive_flat_lookahead_routes_strong_for_write_next_tools():
    # Agent just requested a write — force strong regardless of last_tools.
    decision = choose_adaptive_flat_provider(
        {"enabled": True, "fast_provider": "athena", "strong_provider": "minimax"},
        default_provider="minimax",
        task_type="feature",
        loop_index=3,
        last_tools=["read_file"],        # trailing signal says cheap
        next_tools=["write_file"],       # lookahead overrides to strong
    )

    assert decision.provider is None
    assert decision.tier == "strong"
    assert decision.reason == "lookahead_strong_tool"


def test_adaptive_flat_lookahead_falls_back_to_last_tools_when_next_empty():
    # No next_tools yet (e.g. TASK_COMPLETE loop) — fall back to trailing signal.
    decision = choose_adaptive_flat_provider(
        {"enabled": True, "fast_provider": "athena", "strong_provider": "minimax"},
        default_provider="minimax",
        task_type="feature",
        loop_index=3,
        last_tools=["read_file", "list_files"],
        next_tools=[],
    )

    assert decision.provider == "athena"
    assert decision.tier == "cheap"
    assert decision.reason == "read_only_followup"


# --- strong_bias / tier_ceiling (roadmap #8 policy-layer overrides) ---


def test_strong_bias_overrides_strong_task_types_membership():
    # "feature" is NOT in STRONG_TASK_TYPES, but an explicit strong_bias=True
    # (as a policy layer would pass for a complexity="complex" feature task)
    # forces the same strong_task_type behavior as a qualitative task type.
    decision = choose_adaptive_flat_provider(
        {"enabled": True, "fast_provider": "athena", "strong_provider": "minimax"},
        default_provider="minimax",
        task_type="feature",
        loop_index=2,
        last_tools=["vision_query"],
        strong_bias=True,
    )

    assert decision.tier == "strong"
    assert decision.reason == "strong_task_type"


def test_strong_bias_false_relaxes_strong_task_types_membership():
    # "bug" IS in STRONG_TASK_TYPES, but an explicit strong_bias=False (as a
    # policy layer would pass for a complexity="simple" bug task) lets the
    # normal read/write tool-lookahead heuristic run unconstrained.
    decision = choose_adaptive_flat_provider(
        {"enabled": True, "fast_provider": "athena", "strong_provider": "minimax"},
        default_provider="minimax",
        task_type="bug",
        loop_index=2,
        last_tools=["read_file"],
        strong_bias=False,
    )

    assert decision.provider == "athena"
    assert decision.tier == "cheap"
    assert decision.reason == "read_only_followup"


def test_strong_bias_none_preserves_legacy_strong_task_types_behavior():
    # Default (strong_bias=None) must reproduce the pre-graduation behavior
    # exactly for every existing caller that doesn't pass the new kwargs.
    decision = choose_adaptive_flat_provider(
        {"enabled": True, "fast_provider": "athena", "strong_provider": "minimax"},
        default_provider="minimax",
        task_type="art_pass",
        loop_index=2,
        last_tools=["vision_query"],
    )

    assert decision.tier == "strong"
    assert decision.reason == "strong_task_type"


def test_fast_ceiling_relaxes_first_loop_intent_setting():
    decision = choose_adaptive_flat_provider(
        {"enabled": True, "fast_provider": "athena", "strong_provider": "minimax"},
        default_provider="minimax",
        task_type="feature",
        loop_index=0,
        last_tools=[],
        strong_bias=False,
        tier_ceiling="fast",
    )

    assert decision.provider == "athena"
    assert decision.tier == "cheap"
    assert decision.reason == "first_loop_sets_intent_fast_ceiling"


def test_fast_ceiling_relaxes_cheap_loop_cap():
    decision = choose_adaptive_flat_provider(
        {
            "enabled": True,
            "fast_provider": "athena",
            "strong_provider": "minimax",
            "max_consecutive_cheap_loops": 2,
        },
        default_provider="minimax",
        task_type="feature",
        loop_index=4,
        last_tools=["read_file"],
        consecutive_cheap_loops=2,
        strong_bias=False,
        tier_ceiling="fast",
    )

    assert decision.provider == "athena"
    assert decision.tier == "cheap"
    assert decision.reason == "cheap_loop_cap_fast_ceiling"


def test_fast_ceiling_does_not_override_strong_tool_lookahead():
    # SAFETY-CRITICAL: a quota-driven downgrade (strong_bias=False,
    # tier_ceiling="fast") must never weaken the STRONG_TOOLS tool-lookahead
    # branch. A write/commit call must always force strong, regardless of any
    # budget-cap downgrade applied upstream by swarm.model_policy.
    decision = choose_adaptive_flat_provider(
        {"enabled": True, "fast_provider": "athena", "strong_provider": "minimax"},
        default_provider="minimax",
        task_type="bug",
        loop_index=5,
        last_tools=["read_file"],
        next_tools=["git_commit"],
        strong_bias=False,
        tier_ceiling="fast",
    )

    assert decision.provider is None
    assert decision.tier == "strong"
    assert decision.reason == "lookahead_strong_tool"


def test_fast_ceiling_does_not_override_prior_strong_tool():
    decision = choose_adaptive_flat_provider(
        {"enabled": True, "fast_provider": "athena", "strong_provider": "minimax"},
        default_provider="minimax",
        task_type="bug",
        loop_index=5,
        last_tools=["write_file"],
        strong_bias=False,
        tier_ceiling="fast",
    )

    assert decision.provider is None
    assert decision.tier == "strong"
    assert decision.reason == "prior_strong_tool"
