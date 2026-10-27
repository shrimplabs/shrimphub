"""swarm.telemetry_db -- anonymous play-telemetry SQLite layer.

Stores events emitted by Godot clients (see internal/docs/telemetry-spec.md).
Deliberately uses its own connection to a separate ``data/telemetry.db``
file (WAL mode) so telemetry writes do not contend with ``swarm.db`` writes
on the orchestrator's hot path.

Public API:
    init(path)                              - open / create the DB + schema
    event_insert(event_dict)                - insert one event row
    session_counts(game)                    - COUNT(DISTINCT session)
    victory_rate(game)                      - victories / session_count
    median_playtime(game)                   - median of quit-event t values
    top_quit_scenes(game, limit=5)          - GROUP BY scene on quit events
    top_death_causes(game, limit=5)         - GROUP BY data.cause on deaths
    scene_funnel(game)                      - per-scene event counts

The module is dependency-light (stdlib sqlite3 + json only).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

# Per-thread connections, mirroring swarm/db.py.  Flask serves each request on
# a worker thread while the monitor thread writes events, so a single shared
# sqlite3.Connection raises "SQLite objects created in a thread can only be
# used in that same thread" as soon as two threads touch it -- which is exactly
# what /api/telemetry/<game>/summary did the first time it was called on a
# live server.  Connections are tracked so _reset_for_tests() can close them.
_db_path: Optional[Path] = None
_initialized: bool = False
_local = threading.local()
_all_conns: list[sqlite3.Connection] = []
_conns_lock = threading.Lock()

# Bumped whenever the connection pool is torn down (init() re-pointed at a
# different file, or _reset_for_tests()).  A thread's cached handle is only
# valid while its stamped generation matches; otherwise it reopens.  Clearing
# _local.conn alone would only fix the thread doing the clearing and leave
# every other thread holding a closed handle to the previous database.
_generation: int = 0

# Whitelist of column names that event_insert will accept from a dict.
_EVENT_COLS = (
    "game",
    "session",
    "platform",
    "event",
    "scene",
    "t",
    "data",
)


def _get_conn() -> sqlite3.Connection:
    """Return this thread's connection, opening it lazily.

    One connection per thread, not one per process.  WAL mode makes concurrent
    readers cheap, so the extra handles cost little and remove the cross-thread
    failure entirely.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "generation", None) != _generation:
        # Pool was torn down by another thread; this handle is closed/stale.
        conn = None
        _local.conn = None

    if conn is None:
        if _db_path is None:
            raise RuntimeError("telemetry_db.init(path) must be called first")
        conn = sqlite3.connect(str(_db_path), isolation_level=None, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
        _local.generation = _generation
        with _conns_lock:
            _all_conns.append(conn)
    return conn


def init(path) -> None:
    """Open (or create) the telemetry DB at *path* and ensure schema exists.

    Idempotent for the same path: repeat calls reuse the open connection.

    Re-pointing at a *different* path closes the cached connections first.
    Without that, ``_db_path`` would advance to the new file while
    ``_get_conn()`` kept handing back a connection to the old one, so
    ``_create_schema`` would run against the previous database and later
    queries would hit "no such column" on the new, still-empty file.  Tests
    that init two tmp_path databases in one process do exactly this.
    """
    global _db_path, _initialized
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    new_path = Path(os.path.realpath(str(path)))
    if _db_path is not None and Path(os.path.realpath(str(_db_path))) != new_path:
        _close_all_conns()
        _initialized = False

    _db_path = path
    conn = _get_conn()
    _create_schema(conn)
    _initialized = True


def _close_all_conns() -> None:
    """Close every thread's connection and invalidate all cached handles."""
    global _generation
    with _conns_lock:
        for conn in _all_conns:
            try:
                conn.close()
            except Exception:
                pass
        _all_conns.clear()
        _generation += 1
    _local.conn = None


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            game      TEXT,
            session   TEXT,
            platform  TEXT,
            event     TEXT,
            scene     TEXT,
            t         REAL,
            data      TEXT,
            received  INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_game ON events (game)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events (session)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_event_game ON events (event, game)"
    )


def event_insert(event_dict: dict) -> int:
    """Insert one event row. Returns the new row's id.

    Accepts any subset of the whitelisted columns from a dict. ``data``
    is JSON-encoded if it is a dict/list; string values are stored verbatim.
    ``received`` is filled with ``int(time.time())`` if absent.
    """
    conn = _get_conn()
    row = {}
    for key in _EVENT_COLS:
        if key in event_dict:
            row[key] = event_dict[key]

    # Encode data blob.
    if "data" in row and isinstance(row["data"], (dict, list)):
        row["data"] = json.dumps(row["data"], separators=(",", ":"))

    received = int(event_dict.get("received") or time.time())
    row["received"] = received

    placeholders = ", ".join(["?"] * (len(_EVENT_COLS) + 1))
    # Build column list in a stable order matching placeholders.
    cols = list(_EVENT_COLS) + ["received"]
    values = [row.get(c) for c in cols]

    cur = conn.execute(
        f"INSERT INTO events ({', '.join(cols)}) VALUES ({placeholders})",
        values,
    )
    return cur.lastrowid or 0


# ---------------------------------------------------------------------------
# Aggregate query helpers -- all scope to a single game.
# Return plain dicts / lists; never mutate.
# ---------------------------------------------------------------------------


def session_counts(game: str) -> dict:
    """Count distinct sessions seen for *game*."""
    conn = _get_conn()
    cur = conn.execute(
        "SELECT COUNT(DISTINCT session) FROM events WHERE game = ?", (game,)
    )
    row = cur.fetchone()
    count = row[0] if row else 0
    return {"game": game, "session_count": int(count or 0)}


def victory_rate(game: str) -> dict:
    """Return victory_count / session_count for *game*. 0.0 if no sessions."""
    conn = _get_conn()
    cur = conn.execute(
        "SELECT COUNT(*) FROM events WHERE game = ? AND event = 'victory'",
        (game,),
    )
    victories = int((cur.fetchone() or [0])[0] or 0)

    sc = session_counts(game)
    sessions = sc["session_count"]
    rate = (victories / sessions) if sessions else 0.0
    return {
        "game": game,
        "sessions": sessions,
        "victories": victories,
        "victory_rate": round(rate, 4),
    }


def median_playtime(game: str) -> dict:
    """Median of the ``t`` value across quit events for *game*."""
    conn = _get_conn()
    cur = conn.execute(
        "SELECT t FROM events WHERE game = ? AND event = 'quit' AND t IS NOT NULL ORDER BY t",
        (game,),
    )
    values = [float(r[0]) for r in cur.fetchall() if r[0] is not None]
    if not values:
        return {"game": game, "median_playtime_s": 0.0, "samples": 0}
    n = len(values)
    mid = n // 2
    if n % 2 == 1:
        median = values[mid]
    else:
        median = (values[mid - 1] + values[mid]) / 2.0
    return {
        "game": game,
        "median_playtime_s": round(median, 3),
        "samples": n,
    }


def top_quit_scenes(game: str, limit: int = 5) -> list:
    """Top scenes by quit-event count for *game*. Descending."""
    conn = _get_conn()
    cur = conn.execute(
        """
        SELECT scene, COUNT(*) AS c
        FROM events
        WHERE game = ? AND event = 'quit'
        GROUP BY scene
        ORDER BY c DESC, scene ASC
        LIMIT ?
        """,
        (game, limit),
    )
    return [{"scene": row[0], "count": int(row[1])} for row in cur.fetchall()]


def top_death_causes(game: str, limit: int = 5) -> list:
    """Top death-cause buckets for *game*. Descending by count.

    Prefers ``data`` blob's ``cause`` key (json_extract). If the blob is
    missing or has no ``cause``, falls back to the row's ``scene`` column
    so we still get useful grouping for clients that omit data.cause.
    """
    conn = _get_conn()
    cur = conn.execute(
        """
        SELECT
            COALESCE(NULLIF(json_extract(data, '$.cause'), ''), scene) AS cause,
            COUNT(*) AS c
        FROM events
        WHERE game = ? AND event = 'player_died'
        GROUP BY cause
        ORDER BY c DESC, cause ASC
        LIMIT ?
        """,
        (game, limit),
    )
    return [{"cause": row[0], "count": int(row[1])} for row in cur.fetchall()]


def scene_funnel(game: str) -> list:
    """Per-scene event counts for *game* in scene-name order."""
    conn = _get_conn()
    cur = conn.execute(
        """
        SELECT scene, COUNT(*) AS c
        FROM events
        WHERE game = ? AND scene IS NOT NULL
        GROUP BY scene
        ORDER BY scene ASC
        """,
        (game,),
    )
    return [{"scene": row[0], "count": int(row[1])} for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    """Drop module state. Tests call this between cases via the autouse fixture.

    Closes every thread's connection, not just the calling thread's, so a test
    that touched the DB from a worker thread does not leave a handle open on
    the previous tmp_path database file.
    """
    global _db_path, _initialized
    _close_all_conns()
    _db_path = None
    _initialized = False
