"""swarm.player_data -- convert telemetry aggregates into prompt-friendly text.

Per internal/docs/telemetry-spec.md "Feed into Swarm Prompts":
- ``build_player_data_block(game, data_dir=None)`` returns a compact text
  block describing player behaviour for the given game (sessions, victory
  rate, median playtime, top quit scene, top death cause).
- Returns "" when there is no telemetry yet (cold DB, uninitialised, or
  zero sessions) so callers can include the return value unconditionally
  without an "if" check.
- Reads directly from ``swarm.telemetry_db`` (no HTTP round trip) so it
  can be called from both the dashboard chat path and the work-prompt
  construction path.

The output format mirrors ``swarm_runner._build_player_data_block`` so
prompt content is consistent between fill_slots-driven project_plan
rendering and runtime work / chat prompt augmentation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional


# Minimum sessions before we emit a block. A single smoke-test event
# shouldn't pollute agent context with "1 session, 0.0% victory".
_MIN_SESSIONS_FOR_BLOCK = 1


def _ensure_telemetry_initialised(data_dir: Optional[Path]) -> bool:
    """Initialise ``telemetry_db`` against ``data_dir/telemetry.db`` if possible.

    Returns True when telemetry_db is ready to answer queries, False when
    ``data_dir`` is None, the file doesn't exist yet, or init fails.
    """
    if data_dir is None:
        return False
    from swarm import telemetry_db  # local import to avoid cycles

    candidate = Path(data_dir) / "telemetry.db"
    if not candidate.exists():
        return False
    if telemetry_db._initialized and telemetry_db._db_path == candidate:
        return True
    try:
        telemetry_db.init(candidate)
        return True
    except Exception:
        return False


def _summary_dict(game: str) -> Dict[str, Any]:
    """Return a minimal aggregate dict from telemetry_db.

    Mirrors the shape produced by ``api_telemetry._summary_for_game`` but
    only pulls the columns we render, and never touches ``request``.
    """
    from swarm import telemetry_db

    sessions_total = int(
        (telemetry_db.session_counts(game) or {}).get("session_count", 0) or 0
    )
    victory_info = telemetry_db.victory_rate(game) or {}
    median_info = telemetry_db.median_playtime(game) or {}
    top_quits = telemetry_db.top_quit_scenes(game, limit=1) or []
    top_causes = telemetry_db.top_death_causes(game, limit=1) or []

    return {
        "game": game,
        "sessions_total": sessions_total,
        "victory_rate": float(victory_info.get("victory_rate", 0.0) or 0.0),
        "median_playtime_s": float(median_info.get("median_playtime_s", 0.0) or 0.0),
        "top_quit_scene": top_quits[0]["scene"] if top_quits else None,
        "top_quit_count": int(top_quits[0]["count"]) if top_quits else 0,
        "top_death_cause": top_causes[0]["cause"] if top_causes else None,
        "top_death_count": int(top_causes[0]["count"]) if top_causes else 0,
    }


def _format_block(s: Dict[str, Any]) -> str:
    """Render the canonical Player Data block from a summary dict."""
    sessions = s["sessions_total"]
    if sessions < _MIN_SESSIONS_FOR_BLOCK:
        return ""

    vr_pct = int(round(s["victory_rate"] * 100))
    total_secs = int(round(s["median_playtime_s"]))
    minutes = total_secs // 60
    seconds = total_secs % 60

    if s["top_quit_scene"]:
        quit_scene = s["top_quit_scene"]
        quit_count = s["top_quit_count"]
        quit_pct = int(round((quit_count / sessions) * 100)) if sessions else 0
        quit_line = (
            f"- Top quit point: {quit_scene} ({quit_pct}% of sessions quit here)"
        )
    else:
        quit_scene = "unknown"
        quit_line = "- Top quit point: unknown (no quit events recorded)"

    if s["top_death_cause"]:
        death_cause = s["top_death_cause"]
        death_count = s["top_death_count"]
        death_line = f"- Top death cause: {death_cause} ({death_count} deaths)"
    else:
        death_cause = "unknown"
        death_line = "- Top death cause: unknown (no player_died events recorded)"

    header = (
        "## Player Data (last 30 days)\n"
        f"- Sessions: {sessions} | Victory rate: {vr_pct}% | "
        f"Median playtime: {minutes}m{seconds:02d}s"
    )
    instruction = (
        f"\nPrioritize tasks that address the {quit_scene} drop-off and "
        f"{death_cause} difficulty."
    )
    return f"{header}\n{quit_line}\n{death_line}\n{instruction}"


def build_player_data_block(game: str, data_dir: Optional[Path] = None) -> str:
    """Build a canonical Player Data block for one game project.

    Returns an empty string when telemetry is not yet initialised or when
    there are no recorded sessions for ``game``. Callers can include the
    return value unconditionally; absence-of-data naturally degrades to
    "no section at all" as required by the spec.
    """
    if not game:
        return ""
    if not _ensure_telemetry_initialised(data_dir):
        return ""
    try:
        s = _summary_dict(game)
    except Exception:
        return ""
    return _format_block(s)


def resolve_data_dir_for_state(state) -> Optional[Path]:
    """Best-effort resolve a Path to the swarm data dir from a TaskState-like.

    Looks at ``state.workspace`` first (used by the work phase); falls back to
    ``swarm.agent_runtime.DATA_DIR``; returns ``None`` if neither is usable.
    The function never raises -- callers can safely pass the result straight
    into ``build_player_data_block``.
    """
    try:
        ws = getattr(state, "workspace", None)
        if ws:
            ws_path = Path(ws)
            # The workspace is the project root; telemetry lives under
            # <workspace>/data, not <workspace>/../data.
            candidate = ws_path / "data" / "telemetry.db"
            if candidate.exists():
                return ws_path / "data"
    except Exception:
        pass
    try:
        from swarm import agent_runtime as _rt
        candidate = Path(_rt.DATA_DIR) / "telemetry.db"
        if candidate.exists():
            return Path(_rt.DATA_DIR)
    except Exception:
        return None
    return None


# Task types whose work profile benefits from player-behaviour context.
# Mirrors ``_WORK_PROFILE_TASK_TYPES`` in swarm.phases.work.
_PLAYER_DATA_TASK_TYPES = frozenset({
    "art_pass", "polish", "feature", "bug", "refactor",
})


def player_data_block_for_task(state) -> str:
    """Return a Player Data block for the given task state, or "" if not relevant.

    Eligibility:
    - ``state.task_type`` is in ``_PLAYER_DATA_TASK_TYPES``.
    - ``state.project`` is set (used as the telemetry ``game`` key).
    - Telemetry DB exists and has at least one session for that project.

    Any failure (missing fields, broken telemetry db, exception) returns ""
    so this helper can be called unconditionally from prompt builders.
    """
    try:
        task_type = getattr(state, "task_type", "")
        if task_type not in _PLAYER_DATA_TASK_TYPES:
            return ""
        project = getattr(state, "project", "")
        if not project:
            return ""
        data_dir = resolve_data_dir_for_state(state)
        if data_dir is None:
            return ""
        return build_player_data_block(project, data_dir=data_dir)
    except Exception:
        return ""
