"""Tests for swarm.telemetry_db -- init, event_insert, aggregate helpers.

Mirrors the isolated_db autouse fixture pattern from tests/test_analytics.py
to keep each test pointing at its own tmp_path telemetry.db.
"""

from __future__ import annotations

import json
import time

import pytest

from swarm import telemetry_db


@pytest.fixture(autouse=True)
def isolated_telemetry_db(tmp_path):
    """Reset module state, point telemetry_db at a fresh tmp file each test."""
    telemetry_db._reset_for_tests()
    telemetry_db.init(tmp_path / "telemetry_test.db")
    yield
    telemetry_db._reset_for_tests()


# ---------------------------------------------------------------------------
# init / schema
# ---------------------------------------------------------------------------


def test_init_creates_events_table_and_indexes(tmp_path):
    """init() must create the events table + three indexes via sqlite_master."""
    conn = telemetry_db._get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "events" in tables

    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
    ).fetchall()}
    assert "idx_events_game" in indexes
    assert "idx_events_session" in indexes
    assert "idx_events_event_game" in indexes


def test_init_enables_wal_mode(tmp_path):
    """PRAGMA journal_mode must report 'wal' (case-insensitive)."""
    conn = telemetry_db._get_conn()
    row = conn.execute("PRAGMA journal_mode").fetchone()
    assert str(row[0]).lower() == "wal"


def test_init_is_idempotent():
    """Calling init twice must not raise or duplicate schema objects."""
    telemetry_db.init("/tmp/test_telemetry_idempotent.db")
    telemetry_db.init("/tmp/test_telemetry_idempotent.db")  # second call
    conn = telemetry_db._get_conn()
    n_tables = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='events'"
    ).fetchone()[0]
    assert n_tables == 1


# ---------------------------------------------------------------------------
# event_insert
# ---------------------------------------------------------------------------


def test_event_insert_persists_row_with_correct_columns():
    """Inserting an event round-trips with correct column values."""
    new_id = telemetry_db.event_insert({
        "game": "raccoon-city",
        "session": "sess-1",
        "platform": "macos",
        "event": "scene_entered",
        "scene": "Level_2",
        "t": 142.3,
        "data": {"from_scene": "MainMenu"},
    })
    assert isinstance(new_id, int) and new_id > 0

    conn = telemetry_db._get_conn()
    row = conn.execute(
        "SELECT game, session, platform, event, scene, t, data "
        "FROM events WHERE id = ?",
        (new_id,),
    ).fetchone()
    assert row[0] == "raccoon-city"
    assert row[1] == "sess-1"
    assert row[2] == "macos"
    assert row[3] == "scene_entered"
    assert row[4] == "Level_2"
    assert abs(float(row[5]) - 142.3) < 1e-9
    # data must be a JSON-encoded string.
    assert isinstance(row[6], str)
    parsed = json.loads(row[6])
    assert parsed == {"from_scene": "MainMenu"}


def test_event_insert_fills_received_with_unix_timestamp():
    """received must be set to a sane current unix timestamp."""
    before = int(time.time())
    telemetry_db.event_insert({"game": "g", "event": "session_start"})
    after = int(time.time())

    row = telemetry_db._get_conn().execute(
        "SELECT received FROM events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] is not None
    # Sanity range: between 2020 and 2100 -- catches both 0 and far-future bugs.
    assert 1_577_836_800 <= int(row[0]) <= 4_102_444_800
    assert before <= int(row[0]) <= after


def test_event_insert_preserves_string_data_verbatim():
    """String data values are stored as-is, not double-encoded."""
    telemetry_db.event_insert({
        "game": "g", "event": "custom",
        "data": '{"key":"raw"}',
    })
    row = telemetry_db._get_conn().execute(
        "SELECT data FROM events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == '{"key":"raw"}'


def test_event_insert_defaults_missing_keys_to_null():
    """Columns not in the dict must end up NULL."""
    telemetry_db.event_insert({"game": "g", "event": "session_start"})
    row = telemetry_db._get_conn().execute(
        "SELECT session, platform, scene, t FROM events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None
    assert row[3] is None


# ---------------------------------------------------------------------------
# Aggregate helpers -- small hand-seeded dataset, scoped to "alpha"
# ---------------------------------------------------------------------------


def _seed_alpha_dataset():
    """Seed two sessions of game 'alpha' with a known shape.

    Sessions:
      sess-a: starts, dies (enemy), retries, completes level, quits at 90s
      sess-b: starts, dies twice (trap, enemy), wins, quits at 240s
    """
    events = [
        # sess-a
        {"game": "alpha", "session": "sess-a", "event": "session_start",
         "platform": "macos", "scene": "MainMenu", "t": 0.0,
         "data": {"game_version": "1.0.0"}},
        {"game": "alpha", "session": "sess-a", "event": "scene_entered",
         "scene": "Level_2", "t": 10.0,
         "data": {"from_scene": "MainMenu"}},
        {"game": "alpha", "session": "sess-a", "event": "player_died",
         "scene": "Level_2", "t": 30.0, "data": {"cause": "enemy"}},
        {"game": "alpha", "session": "sess-a", "event": "level_complete",
         "scene": "Level_2", "t": 60.0,
         "data": {"level": "Level_2", "duration_s": 50.0, "attempts": 1}},
        {"game": "alpha", "session": "sess-a", "event": "quit",
         "scene": "Level_3", "t": 90.0, "data": {"scene": "Level_3"}},
        # sess-b
        {"game": "alpha", "session": "sess-b", "event": "session_start",
         "platform": "windows", "scene": "MainMenu", "t": 0.0,
         "data": {}},
        {"game": "alpha", "session": "sess-b", "event": "player_died",
         "scene": "Level_1", "t": 20.0, "data": {"cause": "trap"}},
        {"game": "alpha", "session": "sess-b", "event": "player_died",
         "scene": "Level_2", "t": 50.0, "data": {"cause": "enemy"}},
        {"game": "alpha", "session": "sess-b", "event": "victory",
         "scene": "Final", "t": 220.0,
         "data": {"total_duration_s": 220.0, "deaths": 2}},
        {"game": "alpha", "session": "sess-b", "event": "quit",
         "scene": "MainMenu", "t": 240.0, "data": {"scene": "MainMenu"}},
        # Noise game -- different game so we can verify scoping.
        {"game": "beta", "session": "sess-noise", "event": "victory",
         "scene": "Boss", "t": 100.0, "data": {}},
    ]
    for ev in events:
        telemetry_db.event_insert(ev)


def test_session_counts_scoped_to_game():
    _seed_alpha_dataset()
    out = telemetry_db.session_counts("alpha")
    assert out == {"game": "alpha", "session_count": 2}
    # noise game beta has its own session -- not counted under alpha.
    assert telemetry_db.session_counts("beta")["session_count"] == 1
    # unknown game -> 0.
    assert telemetry_db.session_counts("does-not-exist")["session_count"] == 0


def test_victory_rate_basic():
    _seed_alpha_dataset()
    out = telemetry_db.victory_rate("alpha")
    assert out["game"] == "alpha"
    assert out["sessions"] == 2
    assert out["victories"] == 1  # only sess-b has a victory event
    assert out["victory_rate"] == 0.5


def test_victory_rate_handles_zero_sessions():
    """Division by zero must yield 0.0, not crash."""
    out = telemetry_db.victory_rate("nobody-played")
    assert out["victories"] == 0
    assert out["sessions"] == 0
    assert out["victory_rate"] == 0.0


def test_median_playtime_odd_sample_count():
    """Single quit -> median equals that one t value."""
    telemetry_db.event_insert({
        "game": "g", "session": "s", "event": "quit",
        "scene": "X", "t": 17.5, "data": {"playtime_s": 17.5},
    })
    out = telemetry_db.median_playtime("g")
    assert out["median_playtime_s"] == 17.5
    assert out["samples"] == 1


def test_median_playtime_even_sample_count():
    """Two quit events -> median is the average of the two."""
    for t in (10.0, 30.0):
        telemetry_db.event_insert({
            "game": "g", "session": f"s-{t}", "event": "quit",
            "scene": "X", "t": t, "data": {"playtime_s": t},
        })
    out = telemetry_db.median_playtime("g")
    assert out["median_playtime_s"] == 20.0
    assert out["samples"] == 2


def test_median_playtime_no_quits_returns_zero():
    telemetry_db.event_insert({
        "game": "g", "session": "s", "event": "session_start",
    })
    out = telemetry_db.median_playtime("g")
    assert out["median_playtime_s"] == 0.0
    assert out["samples"] == 0


def test_top_quit_scenes_descending_by_count():
    _seed_alpha_dataset()
    out = telemetry_db.top_quit_scenes("alpha")
    # alpha has one quit each on Level_3 and MainMenu.
    assert out == [
        {"scene": "Level_3", "count": 1},
        {"scene": "MainMenu", "count": 1},
    ]


def test_top_quit_scenes_with_limit_truncates():
    """If more scenes than limit, only the top N are returned."""
    for i in range(7):
        telemetry_db.event_insert({
            "game": "g", "session": f"s{i}", "event": "quit",
            "scene": f"Scene_{i % 3}", "t": float(i),
        })
    # Scene_0 / Scene_1 appear 3x / 2x / 2x; limit=2 keeps the top two.
    out = telemetry_db.top_quit_scenes("g", limit=2)
    assert len(out) == 2
    assert out[0]["count"] >= out[1]["count"]


def test_top_death_causes_groups_by_blob_cause():
    _seed_alpha_dataset()
    out = telemetry_db.top_death_causes("alpha")
    # alpha: 3 deaths -> enemy x2, trap x1
    counts = {row["cause"]: row["count"] for row in out}
    assert counts == {"enemy": 2, "trap": 1}


def test_top_death_causes_falls_back_to_scene_when_cause_missing():
    """If data blob has no cause key, fall back to scene column."""
    telemetry_db.event_insert({
        "game": "g", "session": "s", "event": "player_died",
        "scene": "BossRoom", "t": 1.0, "data": {"unrelated": True},
    })
    out = telemetry_db.top_death_causes("g")
    assert out == [{"cause": "BossRoom", "count": 1}]


def test_scene_funnel_groups_by_scene():
    _seed_alpha_dataset()
    out = telemetry_db.scene_funnel("alpha")
    # Convert to {scene: count} for easier assertion.
    counts = {row["scene"]: row["count"] for row in out}
    # Scenes present in alpha: MainMenu (2), Level_1 (1), Level_2 (4), Level_3 (1), Final (1).
    assert counts["Level_2"] == 4
    assert counts["Level_3"] == 1
    assert counts["Final"] == 1
    # Returned in scene-name order.
    scenes = [r["scene"] for r in out]
    assert scenes == sorted(scenes)


# ---------------------------------------------------------------------------
# Thread safety
#
# Regression tests for a bug found the first time the September telemetry code
# ran on a live server: telemetry_db held ONE module-level sqlite3.Connection,
# so /api/telemetry/<game>/summary raised
#   sqlite3.ProgrammingError: SQLite objects created in a thread can only be
#   used in that same thread
# as soon as a Flask worker thread read a DB that the monitor thread had
# opened.  Connections are now thread-local, mirroring swarm/db.py.
# ---------------------------------------------------------------------------

def _call_in_thread(fn, *args):
    """Run fn in a fresh thread; re-raise whatever it raised, else return."""
    import threading

    box = {}

    def runner():
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "worker thread hung"
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_read_from_another_thread_after_write_on_this_one():
    """The exact shape of the live 500: write here, aggregate over there."""
    telemetry_db.event_insert({
        "game": "g", "session": "s1", "event": "session_start",
        "scene": "MainMenu", "t": 0.0, "data": {},
    })
    counts = _call_in_thread(telemetry_db.session_counts, "g")
    assert counts["session_count"] == 1


def test_write_from_another_thread_is_visible_here():
    """The reverse direction: a worker writes, the main thread reads."""
    _call_in_thread(telemetry_db.event_insert, {
        "game": "g", "session": "s2", "event": "session_start",
        "scene": "MainMenu", "t": 0.0, "data": {},
    })
    assert telemetry_db.session_counts("g")["session_count"] == 1


@pytest.mark.parametrize(
    "fn_name",
    [
        "session_counts",
        "victory_rate",
        "median_playtime",
        "top_quit_scenes",
        "top_death_causes",
        "scene_funnel",
    ],
)
def test_every_aggregate_is_callable_cross_thread(fn_name):
    """Any one of these can be the first call a Flask worker makes."""
    telemetry_db.event_insert({
        "game": "g", "session": "s", "event": "session_start",
        "scene": "MainMenu", "t": 0.0, "data": {},
    })
    _call_in_thread(getattr(telemetry_db, fn_name), "g")


def test_concurrent_readers_and_writers_do_not_error():
    """WAL plus per-thread handles should take simultaneous traffic."""
    import threading

    errors = []

    def writer(index):
        try:
            for step in range(10):
                telemetry_db.event_insert({
                    "game": "g", "session": f"s{index}", "event": "scene_enter",
                    "scene": f"L{step}", "t": float(step), "data": {},
                })
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(10):
                telemetry_db.session_counts("g")
                telemetry_db.scene_funnel("g")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"concurrent access raised: {errors[:3]}"
    assert telemetry_db.session_counts("g")["session_count"] == 3
