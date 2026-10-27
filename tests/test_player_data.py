"""Tests for swarm.player_data -- telemetry-aware prompt augmentation.

Covers the "Feed into Swarm Prompts" section of internal/docs/telemetry-spec.md.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


from swarm import player_data


def _reset_telemetry_db():
    """Force telemetry_db back to an uninitialised state for test isolation.

    Uses the module's own reset helper rather than assigning private
    attributes: telemetry_db keeps one connection per thread, so clearing
    _db_path alone leaves a live handle pointing at the previous tmp_path
    database and the next init() reuses it.
    """
    from swarm import telemetry_db
    telemetry_db._reset_for_tests()


import pytest


@pytest.fixture(autouse=True)
def _telemetry_isolation():
    _reset_telemetry_db()
    yield
    _reset_telemetry_db()


def _make_state(*, task_type="feature", project="mygame", workspace=None):
    return SimpleNamespace(
        task_type=task_type,
        project=project,
        workspace=workspace or "/tmp/someworkspace",
        project_path="/tmp/someworkspace",
    )


# ---------------------------------------------------------------------------
# build_player_data_block
# ---------------------------------------------------------------------------


class TestBuildPlayerDataBlock:
    def test_returns_empty_when_no_data_dir(self, tmp_path):
        # No telemetry DB exists; function should gracefully return "".
        assert player_data.build_player_data_block("mygame", data_dir=tmp_path) == ""

    def test_returns_empty_when_game_blank(self, tmp_path):
        assert player_data.build_player_data_block("", data_dir=tmp_path) == ""

    def test_returns_empty_when_no_sessions(self, tmp_path):
        # Telemetry DB exists but has no rows for the game.
        db_path = tmp_path / "telemetry.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS events (game TEXT, event_type TEXT, scene TEXT, cause TEXT);"
            )
            conn.commit()
        finally:
            conn.close()
        assert player_data.build_player_data_block("mygame", data_dir=tmp_path) == ""

    def test_renders_block_when_sessions_present(self, tmp_path):
        from swarm import telemetry_db
        db_path = tmp_path / "telemetry.db"
        if db_path.exists():
            db_path.unlink()
        # Use telemetry_db.init + event_insert to get a properly-shaped DB.
        telemetry_db.init(db_path)
        try:
            # 3 distinct sessions, with various events.
            for sid in ("s1", "s2", "s3"):
                telemetry_db.event_insert({
                    "game": "mygame",
                    "session": sid,
                    "event": "session_start",
                    "scene": "Start",
                })
            # s1 quits at Level1, s2 dies on spike_trap, s3 wins.
            telemetry_db.event_insert({
                "game": "mygame", "session": "s1", "event": "quit",
                "scene": "Level1", "t": 30.0,
            })
            telemetry_db.event_insert({
                "game": "mygame", "session": "s2", "event": "player_died",
                "scene": "Level1", "data": {"cause": "spike_trap"},
            })
            telemetry_db.event_insert({
                "game": "mygame", "session": "s3", "event": "victory", "scene": "End",
            })
        finally:
            telemetry_db._reset_for_tests()

        block = player_data.build_player_data_block("mygame", data_dir=tmp_path)
        assert "## Player Data (last 30 days)" in block
        assert "Sessions:" in block
        assert "Victory rate:" in block
        assert "Top quit point:" in block
        assert "Top death cause:" in block
        assert "Prioritize tasks" in block

    def test_handles_telemetry_init_failure(self, tmp_path):
        # data_dir exists but DB file is corrupted -> graceful empty string.
        db_path = tmp_path / "telemetry.db"
        db_path.write_bytes(b"not a sqlite file")
        assert player_data.build_player_data_block("mygame", data_dir=tmp_path) == ""


# ---------------------------------------------------------------------------
# resolve_data_dir_for_state
# ---------------------------------------------------------------------------


class TestResolveDataDirForState:
    def test_prefers_workspace_relative_data_dir(self, tmp_path, monkeypatch):
        # Workspace/data/telemetry.db exists -> use that.
        ws = tmp_path / "ws"
        data = ws / "data"
        data.mkdir(parents=True)
        (data / "telemetry.db").touch()

        state = _make_state(workspace=str(ws))
        resolved = player_data.resolve_data_dir_for_state(state)
        assert resolved == data

    def test_returns_none_when_no_telemetry(self, tmp_path, monkeypatch):
        # No workspace DB, no agent_runtime DATA_DIR DB -> None.
        monkeypatch.setattr(
            "swarm.agent_runtime.DATA_DIR", str(tmp_path / "nope"),
        )
        state = _make_state(workspace=str(tmp_path / "nows"))
        # Force the agent_runtime fallback to also fail (no telemetry.db there).
        assert player_data.resolve_data_dir_for_state(state) is None

    def test_falls_back_to_agent_runtime_data_dir(self, tmp_path):
        # No workspace DB, but agent_runtime DATA_DIR has one.
        ws = tmp_path / "nows"
        ws.mkdir()
        state = _make_state(workspace=str(ws))

        from swarm import agent_runtime

        original = agent_runtime.DATA_DIR
        agent_runtime.DATA_DIR = str(tmp_path)
        (tmp_path / "telemetry.db").touch()
        try:
            resolved = player_data.resolve_data_dir_for_state(state)
            assert resolved == tmp_path
        finally:
            agent_runtime.DATA_DIR = original

    def test_returns_none_when_state_workspace_broken(self):
        # State with no usable workspace and no DATA_DIR.
        state = SimpleNamespace(task_type="feature", project="x", workspace=None)
        # The resolver should not raise.
        result = player_data.resolve_data_dir_for_state(state)
        # May return None or a path depending on agent_runtime.DATA_DIR.
        # What matters is it does not raise.
        assert result is None or isinstance(result, Path)


# ---------------------------------------------------------------------------
# player_data_block_for_task
# ---------------------------------------------------------------------------


class TestPlayerDataBlockForTask:
    def test_eligible_task_type_with_data(self, tmp_path):
        ws = tmp_path / "ws"
        data = ws / "data"
        data.mkdir(parents=True)
        db_path = data / "telemetry.db"
        from swarm import telemetry_db
        telemetry_db.init(db_path)
        telemetry_db.event_insert({
            "game": "mygame", "session": "s1", "event": "quit",
            "scene": "Level1", "t": 60.0,
        })

        state = _make_state(workspace=str(ws), project="mygame", task_type="polish")
        block = player_data.player_data_block_for_task(state)
        assert "## Player Data" in block

    def test_ineligible_task_type_returns_empty(self, tmp_path):
        ws = tmp_path / "ws"
        data = ws / "data"
        data.mkdir(parents=True)
        (data / "telemetry.db").touch()  # DB exists but task_type blocks emission

        state = _make_state(workspace=str(ws), project="mygame", task_type="qa")
        assert player_data.player_data_block_for_task(state) == ""

    def test_missing_project_returns_empty(self, tmp_path):
        ws = tmp_path / "ws"
        data = ws / "data"
        data.mkdir(parents=True)
        (data / "telemetry.db").touch()

        state = _make_state(workspace=str(ws), project="", task_type="feature")
        assert player_data.player_data_block_for_task(state) == ""

    def test_broken_state_returns_empty(self):
        # State with no project_path, no workspace, no project -- must not raise.
        state = SimpleNamespace(task_type="feature")
        assert player_data.player_data_block_for_task(state) == ""

    def test_exception_inside_telemetry_returns_empty(self, tmp_path):
        # Force telemetry_db helpers to throw to exercise the safety net.
        ws = tmp_path / "ws"
        data = ws / "data"
        data.mkdir(parents=True)
        (data / "telemetry.db").touch()

        state = _make_state(workspace=str(ws), project="mygame", task_type="feature")

        with mock.patch(
            "swarm.player_data._summary_dict",
            side_effect=RuntimeError("simulated"),
        ):
            assert player_data.player_data_block_for_task(state) == ""


# ---------------------------------------------------------------------------
# _format_block -- pure function behaviour
# ---------------------------------------------------------------------------


class TestFormatBlock:
    def test_zero_sessions_emits_nothing(self):
        out = player_data._format_block({
            "game": "x",
            "sessions_total": 0,
            "victory_rate": 0.0,
            "median_playtime_s": 0.0,
            "top_quit_scene": None,
            "top_quit_count": 0,
            "top_death_cause": None,
            "top_death_count": 0,
        })
        assert out == ""

    def test_sessions_with_no_quit_or_death(self):
        out = player_data._format_block({
            "game": "x",
            "sessions_total": 4,
            "victory_rate": 0.5,
            "median_playtime_s": 90.0,
            "top_quit_scene": None,
            "top_quit_count": 0,
            "top_death_cause": None,
            "top_death_count": 0,
        })
        assert "Top quit point: unknown" in out
        assert "Top death cause: unknown" in out
        assert "Sessions: 4" in out
        assert "1m30s" in out  # 90 seconds
        assert "Victory rate: 50%" in out
