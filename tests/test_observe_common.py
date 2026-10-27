"""Unit tests for swarm/tools/observe_common.py -- pure functions, no TCP or
Godot needed. Regression guard for roadmap #10's observe-layer convergence.
"""

from swarm.tools.observe_common import (
    match_a11y_element,
    resolve_image_coords_from_model,
    screen_to_viewport,
    viewport_to_screen,
)


# ---------------------------------------------------------------------------
# screen_to_viewport / viewport_to_screen
# ---------------------------------------------------------------------------

def test_screen_to_viewport_no_offset_no_scaling():
    window = {"x": 0, "y": 0, "pixel_ratio": 1.0}
    assert screen_to_viewport(100, 200, window) == (100.0, 200.0)


def test_screen_to_viewport_with_offset():
    window = {"x": 50, "y": 30, "pixel_ratio": 1.0}
    assert screen_to_viewport(150, 130, window) == (100.0, 100.0)


def test_screen_to_viewport_with_pixel_ratio():
    window = {"x": 0, "y": 0, "pixel_ratio": 2.0}
    assert screen_to_viewport(100, 100, window) == (200.0, 200.0)


def test_viewport_to_screen_round_trips_with_screen_to_viewport():
    window = {"x": 40, "y": 20, "pixel_ratio": 1.5}
    vx, vy = screen_to_viewport(140, 120, window)
    sx, sy = viewport_to_screen(vx, vy, window)
    assert (sx, sy) == (140, 120)


def test_pixel_ratio_derived_from_viewport_w_when_not_set_directly():
    window = {"x": 0, "y": 0, "viewport_w": 2000, "w": 1000}
    assert screen_to_viewport(10, 10, window) == (20.0, 20.0)


def test_pixel_ratio_defaults_to_one_when_unknown():
    window = {}
    assert screen_to_viewport(10, 10, window) == (10.0, 10.0)
    assert viewport_to_screen(10, 10, window) == (10, 10)


# ---------------------------------------------------------------------------
# resolve_image_coords_from_model
# ---------------------------------------------------------------------------

def test_minimax_prefers_normalized_1000_when_in_range():
    x, y, mode = resolve_image_coords_from_model(500, 500, 1000, 800, "minimax")
    assert mode == "normalized-1000"
    assert (x, y) == (500, 400)


def test_minimax_falls_back_to_raw_pixels_when_out_of_normalized_range():
    # raw_y=1500 is out of the 0-1000 normalized range but within img bounds
    x, y, mode = resolve_image_coords_from_model(500, 1500, 1000, 2000, "minimax")
    assert mode == "raw-pixels"
    assert (x, y) == (500, 1500)


def test_non_minimax_prefers_raw_pixels_when_both_candidates_valid():
    x, y, mode = resolve_image_coords_from_model(500, 500, 1000, 800, "athena")
    assert mode == "raw-pixels"
    assert (x, y) == (500, 500)


def test_non_minimax_uses_normalized_hint_in_text():
    x, y, mode = resolve_image_coords_from_model(
        500, 500, 1000, 800, "athena", raw_text="coordinates are normalized 0-1000"
    )
    assert mode == "normalized-1000"
    assert (x, y) == (500, 400)


def test_fallback_clamped_when_neither_candidate_valid():
    x, y, mode = resolve_image_coords_from_model(5000, 5000, 1000, 800, "athena")
    assert mode == "fallback-clamped"
    assert (x, y) == (999, 799)


def test_fallback_clamped_when_image_dims_missing():
    x, y, mode = resolve_image_coords_from_model(100, 100, 0, 0, "minimax")
    assert mode == "fallback-clamped"
    assert (x, y) == (100, 100)


# ---------------------------------------------------------------------------
# match_a11y_element -- mode="scored" (qa_tools.py click_element tier 1)
# ---------------------------------------------------------------------------

def _tree(*elements):
    return list(elements)


def test_scored_exact_label_match_wins():
    tree = _tree(
        {"label": "Play", "role": "button", "visible": True},
        {"label": "Play Again", "role": "button", "visible": True},
    )
    result = match_a11y_element(tree, "Play", mode="scored")
    assert result["label"] == "Play"


def test_scored_prefers_button_role_bonus():
    tree = _tree(
        {"label": "settings menu", "role": "container", "visible": True},
        {"label": "settings", "role": "button", "visible": True},
    )
    result = match_a11y_element(tree, "settings", mode="scored")
    assert result["role"] == "button"


def test_scored_button_keyword_penalizes_non_button_role():
    tree = _tree(
        {"label": "quit button label text", "role": "label", "visible": True},
        {"label": "quit", "role": "button", "visible": True},
    )
    result = match_a11y_element(tree, "quit button", mode="scored")
    assert result["role"] == "button"


def test_scored_returns_none_when_no_positive_score():
    tree = _tree({"label": "unrelated", "role": "label", "visible": True})
    assert match_a11y_element(tree, "zzz_no_match_xyz", mode="scored") is None


def test_scored_qa_label_matches_too():
    tree = _tree({"label": "", "qa_label": "start", "role": "button", "visible": True})
    result = match_a11y_element(tree, "start", mode="scored")
    assert result["qa_label"] == "start"


def test_scored_ignores_invisible_when_require_visible_true():
    tree = _tree({"label": "Play", "role": "button", "visible": False})
    assert match_a11y_element(tree, "Play", mode="scored", require_visible=True) is None
    assert match_a11y_element(tree, "Play", mode="scored", require_visible=False) is not None


# ---------------------------------------------------------------------------
# match_a11y_element -- mode="substring" (playthrough_kit.py click_label)
# ---------------------------------------------------------------------------

def test_substring_case_insensitive_first_match():
    tree = _tree(
        {"label": "Start Game", "visible": True},
        {"label": "Settings", "visible": True},
    )
    result = match_a11y_element(tree, "start", mode="substring")
    assert result["label"] == "Start Game"


def test_substring_no_match_returns_none():
    tree = _tree({"label": "Settings", "visible": True})
    assert match_a11y_element(tree, "start", mode="substring") is None


def test_substring_skips_invisible_by_default():
    tree = _tree({"label": "Start", "visible": False})
    assert match_a11y_element(tree, "start", mode="substring", require_visible=True) is None


def test_substring_visible_defaults_true_when_key_absent():
    # entries without an explicit "visible" key are treated as visible
    tree = _tree({"label": "Start"})
    result = match_a11y_element(tree, "start", mode="substring", require_visible=True)
    assert result["label"] == "Start"


def test_match_a11y_element_rejects_unknown_mode():
    import pytest as _pytest
    with _pytest.raises(ValueError):
        match_a11y_element([], "x", mode="bogus")


def test_match_a11y_element_handles_non_list_input():
    assert match_a11y_element(None, "x", mode="scored") is None
    assert match_a11y_element({"not": "a list"}, "x", mode="substring") is None
