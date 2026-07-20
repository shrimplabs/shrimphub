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
