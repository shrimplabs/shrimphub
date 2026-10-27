"""Telemetry route handlers for the Swarm API.

Thin Flask blueprint over swarm.telemetry_db. Implements the privacy guarantees
from internal/docs/telemetry-spec.md:

- The route handlers never read or persist ``request.remote_addr``. The IP is
  dropped before the event dict is built, so no storage write can accidentally
  contain it.
- Per-session ingest is capped at 1000 events/minute. Above that threshold,
  events are dropped silently (200 OK, no error body) so malformed clients do
  not surface errors in game logs.
- The position heatmap aggregates samples into 32px cells and only exposes
  cells with >=5 samples. Raw per-session position data is never returned.

This module is registered by ``swarm/api.py`` via ``register(app, data_dir)``
(task feature-395920652-0331). When called with a ``data_dir``, the telemetry
DB at ``<data_dir>/telemetry.db`` is initialised on first use so the routes
return default/zero aggregates on a cold DB rather than raising.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Optional, Tuple

from flask import jsonify, request

from swarm import telemetry_db

logger = logging.getLogger(__name__)

# Per-session sliding-window timestamp deque. Keyed by session_id.
# Values are monotonic second timestamps of accepted ingest calls.
_RATE_BUCKETS: Dict[str, Deque[float]] = defaultdict(deque)
_RATE_LIMIT_PER_MINUTE = 1000
_RATE_WINDOW_SECONDS = 60.0
# Grid cell size for the position heatmap (per telemetry-spec.md).
_HEATMAP_CELL_PX = 32
# Minimum samples per cell before it is exposed in the heatmap response.
_HEATMAP_MIN_CELL_SAMPLES = 5


def _rate_limit_allows(session_id: Optional[str]) -> bool:
    """Return True if *session_id* is below the 1000 events/minute cap."""
    if not session_id:
        # No session_id == treat as unrate-limited single-event stream.
        return True
    bucket = _RATE_BUCKETS[session_id]
    now = time.monotonic()
    cutoff = now - _RATE_WINDOW_SECONDS
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= _RATE_LIMIT_PER_MINUTE:
        return False
    bucket.append(now)
    return True


def _scrub_event(raw: Any) -> Optional[Dict]:
    """Validate and sanitise a single event payload.

    Returns ``None`` if *raw* is not an object/dict. The returned dict never
    contains ``request.remote_addr`` or any IP field -- callers must NOT have
    added one. We also defensively strip any keys that look like IPs or
    addresses.
    """
    if not isinstance(raw, dict):
        return None
    blocked = {
        "remote_addr",
        "ip",
        "ip_address",
        "client_ip",
        "x_forwarded_for",
        "forwarded_for",
        "address",
        "session_ip",
    }
    return {k: v for k, v in raw.items() if k not in blocked}



def _ingest_event(event: Dict[str, Any]) -> Tuple[bool, str]:
    """Ingest a single sanitised event.

    Returns ``(accepted, reason)``.
    """
    session_id = event.get("session") or event.get("session_id")
    if not _rate_limit_allows(session_id):
        return False, "rate_limit"
    try:
        telemetry_db.event_insert(event)
    except Exception as exc:  # pragma: no cover -- defensive
        logger.warning("telemetry_db.event_insert raised: %s", exc)
        return False, "storage_error"
    return True, "ok"


def _ingest_post():
    """POST /api/telemetry -- accept one event or a batched array."""
    body = request.get_json(silent=True)
    accepted = 0
    dropped = 0
    if isinstance(body, list):
        events_raw = body
    elif isinstance(body, dict):
        events_raw = [body]
    else:
        events_raw = []

    for raw in events_raw:
        event = _scrub_event(raw)
        if event is None:
            dropped += 1
            continue
        ok, _reason = _ingest_event(event)
        if ok:
            accepted += 1
        else:
            dropped += 1
    # Note: we deliberately do NOT echo the dropped count or any error reason
    # back to the client -- silent drop is part of the spec.
    return jsonify({"accepted": accepted}), 200


def _session_summary(game: str, limit: int = 100) -> list:
    """Return per-session summaries (no raw event stream)."""
    conn = telemetry_db._get_conn()
    rows = conn.execute(
        """
        SELECT session,
               MAX(CASE WHEN event = 'quit' THEN t ELSE NULL END) AS playtime,
               SUM(CASE WHEN event = 'player_died' THEN 1 ELSE 0 END) AS deaths,
               MAX(CASE WHEN event = 'victory' THEN 1 ELSE 0 END) AS victory,
               MAX(CASE WHEN event = 'quit' THEN scene ELSE NULL END) AS quit_scene
        FROM events
        WHERE game = ? AND session IS NOT NULL
        GROUP BY session
        ORDER BY MAX(received) DESC
        LIMIT ?
        """,
        (game, int(limit)),
    ).fetchall()
    out = []
    for row in rows:
        out.append({
            "session": row[0],
            "playtime_s": float(row[1]) if row[1] is not None else 0.0,
            "deaths": int(row[2] or 0),
            "victory": bool(row[3]),
            "quit_scene": row[4],
        })
    return out



def _heatmap_for_scene(game: str, scene: str) -> Dict:
    """Aggregate Tier 2 position_sample events for *scene* into a 32px grid.

    Cells with fewer than ``_HEATMAP_MIN_CELL_SAMPLES`` are dropped.
    """
    conn = telemetry_db._get_conn()
    rows = conn.execute(
        """
        SELECT data
        FROM events
        WHERE game = ? AND scene = ? AND event = 'position_sample'
        """,
        (game, scene),
    ).fetchall()
    import json as _json
    cell_counts: Dict[Tuple[int, int], int] = {}
    for (data_blob,) in rows:
        if not data_blob:
            continue
        try:
            payload = _json.loads(data_blob)
        except (TypeError, ValueError):
            continue
        x = payload.get("x")
        y = payload.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        cx = int(x) // _HEATMAP_CELL_PX
        cy = int(y) // _HEATMAP_CELL_PX
        cell_counts[(cx, cy)] = cell_counts.get((cx, cy), 0) + 1
    cells = [
        {
            "cell_x": cx * _HEATMAP_CELL_PX,
            "cell_y": cy * _HEATMAP_CELL_PX,
            "count": cnt,
        }
        for (cx, cy), cnt in cell_counts.items()
        if cnt >= _HEATMAP_MIN_CELL_SAMPLES
    ]
    cells.sort(key=lambda c: (-c["count"], c["cell_y"], c["cell_x"]))
    return {"scene": scene, "cells": cells}


def _summary_for_game(game: str) -> Dict:
    """Build the aggregate JSON shape from telemetry-spec.md ``/summary``."""
    sessions_total = telemetry_db.session_counts(game).get("session_count", 0)
    victory_info = telemetry_db.victory_rate(game)
    median_info = telemetry_db.median_playtime(game)
    top_quits = telemetry_db.top_quit_scenes(game, limit=5)
    top_causes = telemetry_db.top_death_causes(game, limit=5)
    funnel = telemetry_db.scene_funnel(game)

    # sessions_last_7d / sessions_last_30d = distinct sessions seen in the
    # last 7 / 30 days by received ts. Both are used by the dashboard
    # telemetry panel (per telemetry-spec.md "Session count 7d / 30d / all time").
    conn = telemetry_db._get_conn()
    now_ts = int(time.time())
    cutoff_7d = now_ts - 7 * 24 * 3600
    cutoff_30d = now_ts - 30 * 24 * 3600
    row = conn.execute(
        "SELECT COUNT(DISTINCT session) FROM events "
        "WHERE game = ? AND received >= ?",
        (game, cutoff_7d),
    ).fetchone()
    sessions_last_7d = int((row or [0])[0] or 0)
    row = conn.execute(
        "SELECT COUNT(DISTINCT session) FROM events "
        "WHERE game = ? AND received >= ?",
        (game, cutoff_30d),
    ).fetchone()
    sessions_last_30d = int((row or [0])[0] or 0)

    # median_deaths -- median of per-session deaths for sessions that have any
    # player_died event. Computed directly so we keep the aggregate shape.
    row = conn.execute(
        """
        SELECT session, COUNT(*) AS c
        FROM events
        WHERE game = ? AND event = 'player_died'
        GROUP BY session
        ORDER BY c
        """,
        (game,),
    ).fetchall()
    death_counts = sorted(int(r[1]) for r in row)
    if not death_counts:
        median_deaths = 0.0
    else:
        n = len(death_counts)
        mid = n // 2
        if n % 2 == 1:
            median_deaths = float(death_counts[mid])
        else:
            median_deaths = (death_counts[mid - 1] + death_counts[mid]) / 2.0

    # Compute quit-scene percentages for top_quit_scenes.
    total_quits = sum(item["count"] for item in top_quits) or 1
    top_quit_scenes_out = [
        {
            "scene": item["scene"],
            "quit_count": item["count"],
            "pct": round(item["count"] / total_quits, 4),
        }
        for item in top_quits
    ]

    return {
        "game": game,
        "sessions_total": sessions_total,
        "sessions_last_7d": sessions_last_7d,
        "sessions_last_30d": sessions_last_30d,
        "median_playtime_s": median_info.get("median_playtime_s", 0.0),
        "victory_rate": victory_info.get("victory_rate", 0.0),
        "median_deaths": round(median_deaths, 3),
        "top_quit_scenes": top_quit_scenes_out,
        "top_death_causes": [
            {"cause": item["cause"], "count": item["count"]}
            for item in top_causes
        ],
        "funnel": [
            {"scene": item["scene"], "count": item["count"]}
            for item in funnel
        ],
    }



def _reset_for_tests() -> None:
    """Drop in-memory rate-limit buckets. Test fixture hook."""
    _RATE_BUCKETS.clear()


def register(app, data_dir=None) -> None:
    """Register telemetry routes on the Flask *app*.

    When *data_dir* is provided, the telemetry DB at
    ``<data_dir>/telemetry.db`` is initialised on first request if it has not
    been initialised yet, so the endpoints return default/zero aggregates on
    a cold DB rather than raising ``RuntimeError``. When *data_dir* is None,
    the caller is responsible for having called ``telemetry_db.init(path)``
    beforehand.
    """

    if data_dir is not None:
        from pathlib import Path
        _db_path_candidate = Path(data_dir) / "telemetry.db"
        if not telemetry_db._initialized or telemetry_db._db_path != _db_path_candidate:
            try:
                telemetry_db.init(_db_path_candidate)
            except Exception as exc:  # pragma: no cover -- defensive
                logger.warning("telemetry_db.init failed for %s: %s", _db_path_candidate, exc)

    @app.route("/api/telemetry", methods=["POST"])
    def ingest_telemetry():
        return _ingest_post()

    @app.route("/api/telemetry/<game>/summary", methods=["GET"])
    def telemetry_summary(game: str):
        return jsonify(_summary_for_game(game))

    @app.route("/api/telemetry/<game>/heatmap", methods=["GET"])
    def telemetry_heatmap(game: str):
        scene = request.args.get("scene", "")
        if not scene:
            return jsonify({"error": "missing scene parameter"}), 400
        return jsonify(_heatmap_for_scene(game, scene))

    @app.route("/api/telemetry/<game>/sessions", methods=["GET"])
    def telemetry_sessions(game: str):
        try:
            limit = int(request.args.get("limit", "100"))
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 1000))
        return jsonify({"sessions": _session_summary(game, limit=limit)})
