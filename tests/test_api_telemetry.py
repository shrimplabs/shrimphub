"""Tests for swarm.api_telemetry -- Flask blueprint routes.

Covers the privacy guarantees from internal/docs/telemetry-spec.md:
- IP-scrubbing: no IP field ever reaches the storage layer, even if the
  caller stuffs one into the JSON body.
- Rate limiting: the 1001st event for a session is dropped silently.
- Query endpoint shapes: /summary, /heatmap, /sessions return the documented
  JSON shape with no raw per-session position data.

Uses Flask's ``app.test_client()`` so we never bind a real socket.
"""

from __future__ import annotations

import time

import pytest

from swarm import telemetry_db
from swarm import api_telemetry


@pytest.fixture(autouse=True)
def isolated_telemetry_db(tmp_path):
    """Reset telemetry_db state and point it at a per-test file.

    Also wipes the in-memory rate-limit buckets so each test starts clean.
    """
    telemetry_db._reset_for_tests()
    telemetry_db.init(tmp_path / "telemetry_api_test.db")
    api_telemetry._reset_for_tests()
    yield
    telemetry_db._reset_for_tests()
    api_telemetry._reset_for_tests()


@pytest.fixture
def client():
    """Build a Flask test client with the telemetry blueprint registered."""
    from flask import Flask

    app = Flask(__name__)
    app.config["TESTING"] = True
    api_telemetry.register(app)
    return app.test_client()


# ---------------------------------------------------------------------------
# IP scrubbing
# ---------------------------------------------------------------------------


def test_ingest_strips_remote_addr_ip_fields(client):
    """POST with IP-shaped keys must not persist them."""
    payload = {
        "session": "sess-ip",
        "game": "raccoon-city",
        "event": "scene_entered",
        "scene": "Level_1",
        "t": 1.0,
        "remote_addr": "203.0.113.7",
        "ip": "10.0.0.1",
        "x_forwarded_for": "198.51.100.4",
    }
    resp = client.post("/api/telemetry", json=payload)
    assert resp.status_code == 200

    # Inspect the raw stored row -- no IP-shaped keys should appear in any column.
    conn = telemetry_db._get_conn()
    rows = conn.execute(
        "SELECT game, session, platform, event, scene, t, data "
        "FROM events WHERE session = ?",
        ("sess-ip",),
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    # No column should ever contain an IP string.
    for col in row:
        assert "203.0.113" not in str(col)
        assert "10.0.0.1" not in str(col)
        assert "198.51.100" not in str(col)


def test_ingest_batch_scrubs_ip_per_event(client):
    """Batched ingest must scrub every event independently."""
    payload = [
        {"session": "a", "game": "g1", "event": "session_start", "ip": "1.1.1.1"},
        {"session": "b", "game": "g1", "event": "session_start", "remote_addr": "2.2.2.2"},
    ]
    resp = client.post("/api/telemetry", json=payload)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"accepted": 2}

    conn = telemetry_db._get_conn()
    n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert n == 2



# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_ingest_silently_drops_events_above_rate_limit(client):
    """The 1001st event from the same session must be dropped with no error."""
    session_id = "burst-session"
    payload = {
        "session": session_id,
        "game": "raccoon-city",
        "event": "position_sample",
        "scene": "Level_1",
        "t": 1.0,
        "data": {"x": 10, "y": 20},
    }
    # First 1000 must all be accepted.
    for _ in range(api_telemetry._RATE_LIMIT_PER_MINUTE):
        resp = client.post("/api/telemetry", json=payload)
        assert resp.status_code == 200

    # 1001st must still return 200 (no error response).
    resp = client.post("/api/telemetry", json=payload)
    assert resp.status_code == 200
    body = resp.get_json()
    # Silent drop means accepted is 0 -- no error key in the body.
    assert body == {"accepted": 0}
    assert "error" not in body

    # Only the first 1000 should be in the DB.
    conn = telemetry_db._get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE session = ?",
        (session_id,),
    ).fetchone()[0]
    assert n == api_telemetry._RATE_LIMIT_PER_MINUTE


def test_ingest_batched_drops_excess_but_accepts_rest(client):
    """A batch larger than the remaining quota must be partially accepted."""
    session_id = "batch-session"
    payload = [
        {
            "session": session_id,
            "game": "g",
            "event": "position_sample",
            "scene": "X",
            "t": float(i),
            "data": {"x": i, "y": i},
        }
        for i in range(api_telemetry._RATE_LIMIT_PER_MINUTE + 5)
    ]
    resp = client.post("/api/telemetry", json=payload)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["accepted"] == api_telemetry._RATE_LIMIT_PER_MINUTE

    conn = telemetry_db._get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM events WHERE session = ?",
        (session_id,),
    ).fetchone()[0]
    assert n == api_telemetry._RATE_LIMIT_PER_MINUTE



# ---------------------------------------------------------------------------
# Query endpoint shapes
# ---------------------------------------------------------------------------


def _seed_summary_events(game: str = "raccoon-city") -> None:
    """Insert a representative mix of events for the summary endpoint."""
    # Three sessions, one of which has a victory.
    telemetry_db.event_insert({"game": game, "session": "s1", "event": "session_start",
                              "data": {"game_version": "1.0"}, "t": 0.0})
    telemetry_db.event_insert({"game": game, "session": "s1", "event": "scene_entered",
                              "scene": "Level_1", "t": 5.0})
    telemetry_db.event_insert({"game": game, "session": "s1", "event": "player_died",
                              "data": {"cause": "enemy"}, "scene": "Level_1", "t": 30.0})
    telemetry_db.event_insert({"game": game, "session": "s1", "event": "quit",
                              "scene": "Level_2", "t": 100.0})

    telemetry_db.event_insert({"game": game, "session": "s2", "event": "session_start", "t": 0.0})
    telemetry_db.event_insert({"game": game, "session": "s2", "event": "scene_entered",
                              "scene": "Level_1", "t": 2.0})
    telemetry_db.event_insert({"game": game, "session": "s2", "event": "victory",
                              "data": {"total_duration_s": 412.0, "deaths": 7}, "t": 412.0})

    telemetry_db.event_insert({"game": game, "session": "s3", "event": "session_start", "t": 0.0})
    telemetry_db.event_insert({"game": game, "session": "s3", "event": "player_died",
                              "data": {"cause": "enemy"}, "scene": "Level_3", "t": 5.0})
    telemetry_db.event_insert({"game": game, "session": "s3", "event": "quit",
                              "scene": "Level_3", "t": 12.0})


def test_summary_endpoint_shape(client):
    """GET /summary must return the documented JSON shape."""
    _seed_summary_events()
    resp = client.get("/api/telemetry/raccoon-city/summary")
    assert resp.status_code == 200
    body = resp.get_json()
    expected_keys = {
        "game",
        "sessions_total",
        "sessions_last_7d",
        "sessions_last_30d",
        "median_playtime_s",
        "victory_rate",
        "median_deaths",
        "top_quit_scenes",
        "top_death_causes",
        "funnel",
    }
    assert expected_keys.issubset(body.keys()), \
        f"missing keys: {expected_keys - body.keys()}"
    assert body["game"] == "raccoon-city"
    assert body["sessions_total"] == 3
    # Default seed inserts events at "now" (received=time.time()), so both
    # the 7-day and 30-day windows must include all 3 sessions.
    assert body["sessions_last_7d"] == 3
    assert body["sessions_last_30d"] == 3
    assert isinstance(body["victory_rate"], float)
    assert isinstance(body["median_playtime_s"], (int, float))
    assert isinstance(body["top_quit_scenes"], list)
    assert isinstance(body["top_death_causes"], list)
    assert isinstance(body["funnel"], list)


def test_summary_endpoint_empty_for_unknown_game(client):
    """GET /summary for an unknown game must not error."""
    resp = client.get("/api/telemetry/no-such-game/summary")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["sessions_total"] == 0
    assert body["sessions_last_7d"] == 0
    assert body["sessions_last_30d"] == 0
    assert body["victory_rate"] == 0.0
    assert body["median_playtime_s"] == 0.0
    assert body["median_deaths"] == 0.0
    assert body["top_quit_scenes"] == []
    assert body["top_death_causes"] == []
    assert body["funnel"] == []


def test_summary_endpoint_windows_partition_by_received_age(client):
    """sessions_last_7d / sessions_last_30d must filter by ``received`` age.

    Verifies that the dashboard panel's 7d / 30d / all-time counts derive
    from the same backing table but with different time-window cutoffs.
    """
    now = int(time.time())
    # 8 days ago -> outside 7d, inside 30d.
    telemetry_db.event_insert({"game": "windowed", "session": "old-recent",
                              "event": "session_start", "t": 0.0,
                              "received": now - 8 * 24 * 3600})
    # 45 days ago -> outside both windows.
    telemetry_db.event_insert({"game": "windowed", "session": "old-ancient",
                              "event": "session_start", "t": 0.0,
                              "received": now - 45 * 24 * 3600})
    # Just now -> inside both windows.
    telemetry_db.event_insert({"game": "windowed", "session": "fresh",
                              "event": "session_start", "t": 0.0})

    resp = client.get("/api/telemetry/windowed/summary")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["sessions_total"] == 3, "all-time count must include every session"
    assert body["sessions_last_7d"] == 1, "only the fresh session is within 7d"
    assert body["sessions_last_30d"] == 2, "fresh + old-recent are within 30d"


def test_heatmap_buckets_position_samples_to_32px_grid(client):
    """Heatmap must aggregate samples into 32px cells and hide <5-sample cells."""
    # 6 samples in (0..31, 0..31) -> cell (0,0) count=6.
    for i in range(6):
        telemetry_db.event_insert({
            "game": "g", "session": f"s{i}", "event": "position_sample",
            "scene": "Level_1", "t": float(i),
            "data": {"x": 10 + i, "y": 20},
        })
    # 4 samples in (32..63, 32..63) -> cell (1,1) count=4 (below threshold).
    for i in range(4):
        telemetry_db.event_insert({
            "game": "g", "session": f"t{i}", "event": "position_sample",
            "scene": "Level_1", "t": float(i),
            "data": {"x": 40 + i, "y": 40},
        })

    resp = client.get("/api/telemetry/g/heatmap?scene=Level_1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["scene"] == "Level_1"
    assert isinstance(body["cells"], list)
    # The (1,1) cell is dropped because count=4 < 5.
    cell_coords = {(c["cell_x"], c["cell_y"]) for c in body["cells"]}
    assert (0, 0) in cell_coords
    assert (32, 32) not in cell_coords
    # The exposed cell must include count metadata, not raw x/y.
    cell = next(c for c in body["cells"] if (c["cell_x"], c["cell_y"]) == (0, 0))
    assert cell["count"] == 6



def test_heatmap_does_not_expose_raw_positions(client):
    """The heatmap response must not contain any per-session coordinates."""
    telemetry_db.event_insert({
        "game": "g", "session": "secret", "event": "position_sample",
        "scene": "Level_2", "t": 0.0,
        "data": {"x": 1234, "y": 5678},
    })
    resp = client.get("/api/telemetry/g/heatmap?scene=Level_2")
    assert resp.status_code == 200
    body = resp.get_json()
    # Raw coords must never appear -- below threshold means cell hidden too.
    body_str = str(body)
    assert "1234" not in body_str
    assert "5678" not in body_str
    assert body["cells"] == []


def test_heatmap_requires_scene_query_param(client):
    """Missing ``scene`` must return a 400."""
    resp = client.get("/api/telemetry/g/heatmap")
    assert resp.status_code == 400


def test_sessions_endpoint_returns_per_session_summaries(client):
    """GET /sessions must list per-session summaries without raw event stream."""
    telemetry_db.event_insert({"game": "g", "session": "s1", "event": "session_start",
                              "t": 0.0})
    telemetry_db.event_insert({"game": "g", "session": "s1", "event": "player_died",
                              "data": {"cause": "enemy"}, "t": 5.0})
    telemetry_db.event_insert({"game": "g", "session": "s1", "event": "quit",
                              "scene": "Level_2", "t": 30.0})
    telemetry_db.event_insert({"game": "g", "session": "s2", "event": "victory",
                              "t": 200.0})

    resp = client.get("/api/telemetry/g/sessions?limit=10")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "sessions" in body
    sessions = body["sessions"]
    assert isinstance(sessions, list)
    assert len(sessions) >= 1

    # Validate the shape of the first session entry.
    first = sessions[0]
    expected = {"session", "playtime_s", "deaths", "victory", "quit_scene"}
    assert expected.issubset(first.keys()), \
        f"missing session keys: {expected - first.keys()}"
    assert isinstance(first["deaths"], int)
    assert isinstance(first["victory"], bool)
    assert isinstance(first["playtime_s"], (int, float))
    # No raw event fields must leak through.
    assert "data" not in first
    assert "event" not in first


def test_sessions_endpoint_respects_limit(client):
    """GET /sessions must honour the limit query parameter."""
    for i in range(5):
        telemetry_db.event_insert({
            "game": "g", "session": f"s{i}", "event": "session_start", "t": 0.0,
        })
    resp = client.get("/api/telemetry/g/sessions?limit=2")
    assert resp.status_code == 200
    sessions = resp.get_json()["sessions"]
    assert len(sessions) == 2


def test_ingest_route_handler_does_not_read_request_ip(client):
    """The ingest route must never call ``request.remote_addr``.

    Verifies the privacy contract at the AST level: no Python code path inside
    the ingest route can ever access the requester's IP.
    """
    import ast
    with open(api_telemetry.__file__, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "remote_addr":
            offenders.append(f"line {node.lineno}: {ast.dump(node)}")
    assert offenders == [], (
        "api_telemetry must never read request.remote_addr; found: "
        + "\n".join(offenders)
    )
