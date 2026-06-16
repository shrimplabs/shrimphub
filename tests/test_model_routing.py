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
