"""Tests for _build_player_data_block and the player_data prompt section.

The Player Data (last 30 days) block in prompts/project_plan.yaml is fed by
``_build_player_data_block(project)`` in swarm_runner.py. This file pins the
behaviour:

* No telemetry data -> the block string is empty (no-op for project_plan).
* Telemetry data present -> the rendered prompt contains the section header,
  the per-metric bullets, and the "Prioritize tasks" instruction line.
* Format follows the spec exactly: Sessions | Victory rate | Median playtime,
  Top quit point with percentage, Top death cause with count.
"""

import sqlite3
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_telemetry_db():
    """Ensure each test starts with a fresh telemetry_db module state."""
    from swarm import telemetry_db as _tdb
    _tdb._reset_for_tests()
    yield
    _tdb._reset_for_tests()


def _init_telemetry_db(tmp_path: Path) -> Path:
    """Init swarm/telemetry_db.py against a temp DB and return its path."""
    from swarm import telemetry_db as _tdb
    db_path = tmp_path / "telemetry.db"
    _tdb.init(db_path)
    return db_path


def _seed_events(events):
    """Insert the given list of dicts into the telemetry DB."""
    from swarm import telemetry_db as _tdb
    for ev in events:
        _tdb.event_insert(ev)


# ---------------------------------------------------------------------------
# _build_player_data_block -- format & gating
# ---------------------------------------------------------------------------

class TestBuildPlayerDataBlock:
    def test_returns_empty_string_when_no_telemetry_db(self, tmp_path, monkeypatch):
        """Missing/empty DB -> empty block (no-op)."""
        import swarm_runner
        # Point DATA_DIR at an empty tmp dir so the helper can't init a real DB
        monkeypatch.setattr(swarm_runner, "DATA_DIR", tmp_path)
        # Don't init telemetry_db -- simulate the cold-DB case
        from swarm import telemetry_db as _tdb
        _tdb._reset_for_tests()
        # Even if a DB file were present, it should be empty
        assert swarm_runner._build_player_data_block("some-game") == ""

    def test_returns_empty_when_sessions_zero(self, tmp_path):
        """A DB with no session rows returns empty -- dark/no-op."""
        import swarm_runner
        _init_telemetry_db(tmp_path)
        assert swarm_runner._build_player_data_block("empty-game") == ""

    def test_format_matches_spec_for_populated_db(self, tmp_path):
        """Populated DB -> block contains all four spec'd lines."""
        import swarm_runner
        _init_telemetry_db(tmp_path)

        # Insert 142 sessions, ~31% victories, ~312s median playtime.
        # Seed 5 sessions with quits on Level_3 and deaths by enemy.
        events = []
        for i in range(5):
            events.append({
                "game": "my-game",
                "session": f"s{i}",
                "event": "quit",
                "scene": "Level_3",
                "t": 312.0 + i,
            })
        for v in ("victor-1", "victor-2", "victor-3"):
            events.append({"game": "my-game", "session": v, "event": "victory"})
        for i in range(10):
            events.append({
                "game": "my-game",
                "session": f"d{i}",
                "event": "player_died",
                "data": {"cause": "enemy"},
            })
        _seed_events(events)

        block = swarm_runner._build_player_data_block("my-game")
        assert block.startswith("## Player Data (last 30 days)")
        assert "Sessions:" in block
        assert "Victory rate:" in block
        assert "Median playtime:" in block
        assert "Top quit point:" in block
        assert "Top death cause:" in block
        assert "Prioritize tasks that address" in block

    def test_quit_scene_and_death_cause_drive_instruction(self, tmp_path):
        """The instruction line names the top quit scene + top death cause."""
        import swarm_runner
        _init_telemetry_db(tmp_path)
        _seed_events([
            {"game": "g", "session": "q1", "event": "quit", "scene": "Boss_Fight",
             "t": 100.0},
            {"game": "g", "session": "d1", "event": "player_died",
             "data": {"cause": "lava"}},
        ])
        block = swarm_runner._build_player_data_block("g")
        assert "Boss_Fight" in block
        assert "lava" in block
        # The instruction line should reference both
        assert "Boss_Fait drop-off" not in block  # typo guard
        assert "Boss_Fight drop-off" in block
        assert "lava difficulty" in block

    def test_format_uses_m_ss_for_playtime(self, tmp_path):
        """Median playtime renders as e.g. '5m12s', zero-padded seconds."""
        import swarm_runner
        _init_telemetry_db(tmp_path)
        # Two quit events so median = (60 + 372) / 2 = 216s = 3m36s
        _seed_events([
            {"game": "p", "session": "s1", "event": "quit", "t": 60.0},
            {"game": "p", "session": "s2", "event": "quit", "t": 372.0},
        ])
        block = swarm_runner._build_player_data_block("p")
        assert "3m36s" in block

    def test_falls_back_gracefully_on_uninitialized_db(self, tmp_path, monkeypatch):
        """If telemetry_db.init fails, return empty rather than raise."""
        import swarm_runner
        from swarm import telemetry_db as _tdb
        _tdb._reset_for_tests()

        # Monkeypatch init to raise
        def _boom(*a, **kw):
            raise RuntimeError("simulated init failure")
        monkeypatch.setattr(_tdb, "init", _boom)
        assert swarm_runner._build_player_data_block("any-game") == ""


# ---------------------------------------------------------------------------
# project_plan.yaml rendering -- conditional block behaviour
# ---------------------------------------------------------------------------

class TestProjectPlanPromptRendering:
    def test_prompt_renders_section_when_player_data_provided(self, tmp_path):
        """Passing player_data into project_plan surfaces the section + instruction."""
        import swarm_runner
        player_data = (
            "## Player Data (last 30 days)\n"
            "- Sessions: 142 | Victory rate: 31% | Median playtime: 5m12s\n"
            "- Top quit point: Level_3 (20% of sessions quit here)\n"
            "- Top death cause: enemy (412 deaths)\n"
            "\nPrioritize tasks that address the Level_3 drop-off and enemy difficulty."
        )
        system, _user = swarm_runner._load_prompt(
            "project_plan",
            project="my-game",
            description="",
            project_path=str(tmp_path),
            project_path_arg='"dummy"',
            godot_bin="godot",
            godot_command='"godot"',
            godot_status="",
            prompt_intent_variant="baseline",
            task_id="t-1",
            player_data=player_data,
        )
        assert "## Player Data (last 30 days)" in system
        assert "Sessions: 142" in system
        assert "Top quit point: Level_3" in system
        assert "Top death cause: enemy (412 deaths)" in system
        assert "Prioritize tasks that address the Level_3 drop-off" in system

    def test_prompt_absent_section_when_player_data_empty(self, tmp_path):
        """Empty player_data -> the conditional block is dark / no-op."""
        import swarm_runner
        system, _user = swarm_runner._load_prompt(
            "project_plan",
            project="my-game",
            description="",
            project_path=str(tmp_path),
            project_path_arg='"dummy"',
            godot_bin="godot",
            godot_command='"godot"',
            godot_status="",
            prompt_intent_variant="baseline",
            task_id="t-1",
            player_data="",
        )
        assert "## Player Data (last 30 days)" not in system
        assert "PLAYER DATA (telemetry, last 30 days)" not in system
        # STEP 1 should still be present
        assert "STEP 1: UNDERSTAND THE VISION" in system

    def test_prompt_absent_section_when_player_data_missing(self, tmp_path):
        """No player_data kwarg at all -> block absent (graceful undefined)."""
        import swarm_runner
        system, _user = swarm_runner._load_prompt(
            "project_plan",
            project="my-game",
            description="",
            project_path=str(tmp_path),
            project_path_arg='"dummy"',
            godot_bin="godot",
            godot_command='"godot"',
            godot_status="",
            prompt_intent_variant="baseline",
            task_id="t-1",
        )
        assert "## Player Data (last 30 days)" not in system
