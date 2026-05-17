import swarm_runner


def test_sanitize_learnings_text_filters_risky_cache_cleanup_advice():
    raw = "\n".join([
        "## run",
        "- delete .gd.uid files if GameManager errors appear",
        "- rm -f .godot/uid_cache.bin and temp/foo.gd.uid",
        "- use check_scripts.gd for validation",
    ])

    cleaned = swarm_runner._sanitize_learnings_text(raw)

    assert "delete .gd.uid files" not in cleaned
    assert "uid_cache.bin" not in cleaned
    assert "use check_scripts.gd for validation" in cleaned


def test_filter_learnings_text_keeps_relevant_and_generic_notes_only():
    raw = "\n".join([
        "## 2026-04-04 11:32 — completed (4 loops)",
        "- broadcast_write before first edit on shared files",
        "- broadcast.py does not exist; do not read it",
        "- math_utils.py already contains average(values)",
        "## 2026-04-04 11:28 — completed (3 loops)",
        "- tests/test_math_utils.py is the right validation target",
        "- string_utils.py did not need edits for the math task",
    ])

    filtered = swarm_runner._filter_learnings_text(
        raw,
        "Add median(values) to src/math_utils.py and tests/test_math_utils.py",
    )

    assert "broadcast_write before first edit" in filtered
    assert "math_utils.py already contains average(values)" in filtered
    assert "tests/test_math_utils.py is the right validation target" in filtered
    assert "broadcast.py does not exist" not in filtered
    assert "string_utils.py did not need edits" not in filtered
