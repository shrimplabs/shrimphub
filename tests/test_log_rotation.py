"""Tests for swarm.log_rotation — signal extraction and rotation logic."""

from __future__ import annotations

import gzip
import json
import os
import threading
import time
from pathlib import Path

import pytest

import swarm.db as db
from swarm.log_rotation import extract_signals, rotate_logs, get_signals_summary


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm_test.db")
    yield
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


def _write_log(path: Path, content: str):
    path.write_text(content, encoding="utf-8")


def _age_file(path: Path, days: float):
    """Set mtime to N days ago."""
    old = time.time() - days * 86400
    os.utime(path, (old, old))


# ---------------------------------------------------------------------------
# _extract_and_store_signals — the finish-time persistence helper
# (regression guard: a missing `import os` here silently broke signal
#  extraction for every agent from 2026-07-04 until it was caught)
# ---------------------------------------------------------------------------

def test_extract_and_store_signals_persists_row(tmp_path):
    import swarm.agent_finish as af

    log = tmp_path / "agent_store_me.log"
    _write_log(log, """\
[Agent] Calling LLM... (loop 3/200)
[Agent] Executing tool: read_file
[Agent] Task complete!
""")
    # Seed a task so task_type resolves.
    db.task_upsert({
        "id": "sig-task-1", "project": "demo", "type": "feature",
        "description": "x", "priority": 50, "status": "completed",
        "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 3,
    })

    result = af._extract_and_store_signals(
        agent_id="agent-sig-1", task_id="sig-task-1",
        project="demo", log_path=str(log),
    )
    assert result["task_type"] == "feature"
    assert result["log_size_bytes"] > 0

    stored = db.agent_signals_get("agent-sig-1")
    assert stored is not None
    assert stored["project"] == "demo"
    assert stored["task_type"] == "feature"
    assert stored["terminal_status"] == "complete"


# ---------------------------------------------------------------------------
# extract_signals tests
# ---------------------------------------------------------------------------

FLAT_COMPLETE_LOG = """\
[Agent] Starting task: raccoon-city (feature)
[Agent] Calling LLM... (loop 1/200)
[Agent] [LLM] provider=minimax model=MiniMax-M3
[Agent] [LLM] in=5000 out=200
[Agent] [LLM] cache read=1000 write=500
[Agent] Executing tool: read_file
[Agent] Result: {'ok': True, 'content': 'hello'}
[Agent] Calling LLM... (loop 2/200)
[Agent] [LLM] cache read=1500 write=0
[Agent] Executing tool: patch_file
[Agent] Executing tool: run_tests
[Agent] WARNING: no tool calls and no TASK_COMPLETE -- nudging model to finish
[Agent] Calling LLM... (loop 3/200)
[Agent] LLM response: TASK_COMPLETE
[Agent] Task marked complete by LLM
[Agent] Task complete!
{"status": "success", "project": "raccoon-city", "task_id": "feat-123"}
"""

def test_extract_signals_complete_flat(tmp_path):
    log = tmp_path / "agent_abc.log"
    _write_log(log, FLAT_COMPLETE_LOG)
    sig = extract_signals(str(log))
    assert sig["terminal_status"] == "complete"
    assert sig["loop_count"] == 3
    assert sig["total_loops"] == 200
    assert sig["tool_call_count"] == 3
    tools = json.loads(sig["tool_sequence"])
    assert tools == ["read_file", "patch_file", "run_tests"]
    unique = json.loads(sig["unique_tools"])
    assert set(unique) == {"read_file", "patch_file", "run_tests"}
    assert sig["warning_count"] == 1
    assert sig["cache_read_total"] == 2500
    assert sig["cache_write_total"] == 500
    assert sig["is_pipeline"] == 0
    assert sig["compaction_count"] == 0
    assert sig["error_count"] == 0


def test_extract_signals_mechanism_fires(tmp_path):
    log = tmp_path / "agent_mech.log"
    _write_log(log, """\
[Agent] Calling LLM... (loop 5/200)
[Agent] [Mechanism] truncation_retry fired (1)
[Agent] [Mechanism] stall fired (1)
[Agent] Task complete!
[Agent] [MechanismSummary] {"stall": 1, "truncation_retry": 4}
""")
    sig = extract_signals(str(log))
    fires = json.loads(sig["mechanism_fires"])
    # The authoritative summary line wins over individual [Mechanism] lines.
    assert fires == {"stall": 1, "truncation_retry": 4}


def test_extract_signals_mechanism_fires_default_empty(tmp_path):
    log = tmp_path / "agent_nomech.log"
    _write_log(log, "[Agent] Calling LLM... (loop 1/200)\n[Agent] Task complete!\n")
    sig = extract_signals(str(log))
    assert sig["mechanism_fires"] == "{}"


PIPELINE_FAILED_LOG = """\
[Agent] [Pipeline] Starting: plan → scout → work → validate
[Agent] ============================================================
[Agent]   PHASE: PLAN  (1/4)
[Agent] ============================================================
[Agent] Calling LLM... (loop 1/10)
[Agent]   ✓ PLAN complete (12.3s)
[Agent] ============================================================
[Agent]   PHASE: WORK  (2/4)
[Agent] ============================================================
[Agent] Calling LLM... (loop 1/50)
[Agent] Executing tool: patch_file
[Agent] [Pipeline] Stopping — phase work signalled failure
[Agent] [Pipeline] Pipeline state for task feat-001 (feature):
  Errors: ['work: hit loop limit without WORK_COMPLETE']
[Agent] [Pipeline] Done. FAILED
{"status": "failed", "project": "iron-ember", "task_id": "feat-001"}
"""

def test_extract_signals_failed_pipeline(tmp_path):
    log = tmp_path / "agent_pipe.log"
    _write_log(log, PIPELINE_FAILED_LOG)
    sig = extract_signals(str(log))
    assert sig["terminal_status"] == "failed"
    assert sig["is_pipeline"] == 1
    phases = json.loads(sig["phases_completed"])
    assert "plan" in phases
    assert sig["loop_count"] == 1
    assert sig["tool_call_count"] == 1


CACHE_LOG = """\
[Agent] Calling LLM... (loop 1/200)
[Agent] [LLM] cache read=100 write=200
[Agent] Calling LLM... (loop 2/200)
[Agent] [LLM] read=300 write=0
[Agent] Calling LLM... (loop 3/200)
[Agent] [LLM] cache read=50 write=0
[Agent] Task complete!
"""

def test_extract_signals_cache_accumulation(tmp_path):
    log = tmp_path / "agent_cache.log"
    _write_log(log, CACHE_LOG)
    sig = extract_signals(str(log))
    assert sig["cache_read_total"] == 100 + 300 + 50
    assert sig["cache_write_total"] == 200


LOOP_LIMIT_LOG = """\
[Agent] Calling LLM... (loop 200/200)
[Agent] WARNING: hit loop limit -- exiting agent loop
"""

def test_extract_signals_loop_limit(tmp_path):
    """Flat agent that hits loop limit — no pipeline Done line, so loop_limit wins."""
    log = tmp_path / "agent_ll.log"
    _write_log(log, LOOP_LIMIT_LOG)
    sig = extract_signals(str(log))
    assert sig["terminal_status"] == "loop_limit"


COMPACTION_LOG = """\
[Agent] Calling LLM... (loop 10/200)
[Agent] [Compaction] Compressed 4 messages into summary (581 chars); conv was ~214430 tokens
[Agent] Calling LLM... (loop 50/200)
[Agent] [Compaction] Compressed 6 messages into summary (400 chars); conv was ~180000 tokens
[Agent] Task complete!
"""

def test_extract_signals_compaction(tmp_path):
    log = tmp_path / "agent_cmp.log"
    _write_log(log, COMPACTION_LOG)
    sig = extract_signals(str(log))
    assert sig["compaction_count"] == 2
    assert sig["terminal_status"] == "complete"


ERROR_LOG = """\
[Agent] Calling LLM... (loop 1/200)
[Agent] Executing tool: read_file
[Agent] Result: {'ok': False, 'error': 'File not found: scripts/player.gd'}
[Agent] Executing tool: run_command
[Agent] Result: {'ok': False, 'error': 'Command failed with exit code 1'}
[Agent] Task complete!
"""

def test_extract_signals_errors(tmp_path):
    log = tmp_path / "agent_err.log"
    _write_log(log, ERROR_LOG)
    sig = extract_signals(str(log))
    assert sig["error_count"] == 2
    snippets = json.loads(sig["error_snippets"])
    assert any("File not found" in s for s in snippets)
    assert any("Command failed" in s for s in snippets)


def test_extract_signals_empty_file(tmp_path):
    log = tmp_path / "agent_empty.log"
    _write_log(log, "")
    sig = extract_signals(str(log))
    assert sig["terminal_status"] == "unknown"
    assert sig["loop_count"] == 0
    assert sig["tool_call_count"] == 0


def test_extract_signals_missing_file(tmp_path):
    sig = extract_signals(str(tmp_path / "nonexistent.log"))
    assert "parse_error" in sig["terminal_status"]


# ---------------------------------------------------------------------------
# DB roundtrip
# ---------------------------------------------------------------------------

def test_agent_signals_db_roundtrip(tmp_path):
    log = tmp_path / "agent_abc.log"
    _write_log(log, FLAT_COMPLETE_LOG)
    sig = extract_signals(str(log))
    sig.update({
        "agent_id": "agent-001",
        "task_id": "feat-123",
        "project": "raccoon-city",
        "task_type": "feature",
        "extracted_at": "2026-07-04T00:00:00Z",
        "log_size_bytes": 500,
        "log_path": str(log),
    })
    db.agent_signals_upsert(sig)
    result = db.agent_signals_get("agent-001")
    assert result is not None
    assert result["terminal_status"] == "complete"
    assert result["project"] == "raccoon-city"
    assert result["tool_call_count"] == 3


def test_agent_signals_query_project_filter(tmp_path):
    for i, proj in enumerate(["project-a", "project-a", "project-b"]):
        db.agent_signals_upsert({
            "agent_id": f"agent-{i:03d}",
            "task_id": f"task-{i}",
            "project": proj,
            "task_type": "feature",
            "extracted_at": "2026-07-04T00:00:00Z",
            "terminal_status": "complete",
            "loop_count": i + 1,
            "total_loops": 200,
            "tool_sequence": "[]", "unique_tools": "[]", "tool_call_count": 0,
            "cache_read_total": 0, "cache_write_total": 0,
            "error_count": 0, "error_snippets": "[]",
            "warning_count": 0, "warning_types": "[]",
            "is_pipeline": 0, "phases_completed": "[]", "phase_failed": None,
            "compaction_count": 0, "log_size_bytes": 100, "log_path": "",
        })
    rows_a = db.agent_signals_query(project="project-a")
    rows_b = db.agent_signals_query(project="project-b")
    assert len(rows_a) == 2
    assert len(rows_b) == 1


def test_schema_idempotent():
    """Calling _evolve_schema twice should not raise."""
    conn = db._connect()
    db._evolve_schema(conn)
    db._evolve_schema(conn)


# ---------------------------------------------------------------------------
# rotate_logs tests
# ---------------------------------------------------------------------------

def _make_orchestrator_config(retention_days=30, action="delete", extract=False):
    import swarm.orchestrator as orc
    orc.LOG_RETENTION_DAYS  = retention_days
    orc.LOG_ROTATION_ACTION = action
    orc.LOG_EXTRACT_SIGNALS = extract


def test_rotate_disabled_when_zero(tmp_path):
    _make_orchestrator_config(retention_days=0)
    log = tmp_path / "agent_xyz.log"
    _write_log(log, FLAT_COMPLETE_LOG)
    _age_file(log, 60)
    result = rotate_logs(str(tmp_path), db)
    assert result.get("skipped") == "disabled"
    assert log.exists()


def test_rotate_deletes_old_logs(tmp_path):
    _make_orchestrator_config(retention_days=30, action="delete")
    old_log = tmp_path / "agent_old.log"
    new_log = tmp_path / "agent_new.log"
    _write_log(old_log, FLAT_COMPLETE_LOG)
    _write_log(new_log, FLAT_COMPLETE_LOG)
    _age_file(old_log, 40)  # older than 30d threshold
    # new_log stays at current mtime
    result = rotate_logs(str(tmp_path), db)
    assert not old_log.exists(), "Old log should be deleted"
    assert new_log.exists(), "Recent log should be kept"
    assert result["rotated"] >= 1


def test_rotate_keeps_recent_logs(tmp_path):
    _make_orchestrator_config(retention_days=30, action="delete")
    log = tmp_path / "agent_recent.log"
    _write_log(log, FLAT_COMPLETE_LOG)
    _age_file(log, 10)  # 10 days old, under threshold
    result = rotate_logs(str(tmp_path), db)
    assert log.exists()
    assert result["rotated"] == 0


def test_rotate_skips_active_agents(tmp_path, monkeypatch):
    _make_orchestrator_config(retention_days=30, action="delete")
    log = tmp_path / "agent_active123.log"
    _write_log(log, FLAT_COMPLETE_LOG)
    _age_file(log, 40)

    # Make agent appear active in DB
    import swarm.agent_lifecycle as al
    monkeypatch.setattr(al, "_active_handles", {"active123": {}})

    result = rotate_logs(str(tmp_path), db)
    assert log.exists(), "Active agent log should not be deleted"
    assert result["skipped_active"] >= 1


def test_rotate_compress(tmp_path):
    _make_orchestrator_config(retention_days=30, action="compress")
    log = tmp_path / "agent_compress_me.log"
    _write_log(log, FLAT_COMPLETE_LOG)
    _age_file(log, 40)

    result = rotate_logs(str(tmp_path), db)
    assert not log.exists(), "Original should be deleted after compress"
    # Find the gz file
    gz_files = list(tmp_path.rglob("agent_compress_me.log.gz"))
    assert gz_files, "Compressed file should exist in archives/"
    # Verify it's valid gzip
    with gzip.open(gz_files[0], "rb") as gz:
        content = gz.read().decode("utf-8")
    assert "Task complete!" in content


def test_rotate_compress_archive_dir_structure(tmp_path):
    """Archives should be under YYYY-MM/ subdirectories."""
    _make_orchestrator_config(retention_days=30, action="compress")
    log = tmp_path / "agent_structure_test.log"
    _write_log(log, FLAT_COMPLETE_LOG)
    _age_file(log, 40)
    rotate_logs(str(tmp_path), db)
    archive_dir = tmp_path / "archives"
    assert archive_dir.exists()
    month_dirs = list(archive_dir.iterdir())
    assert month_dirs, "Should have at least one YYYY-MM subdirectory"


# ---------------------------------------------------------------------------
# get_signals_summary
# ---------------------------------------------------------------------------

def test_signals_summary_empty():
    result = get_signals_summary(db)
    assert result == {"count": 0}


def test_signals_summary_with_data(tmp_path):
    for i in range(3):
        db.agent_signals_upsert({
            "agent_id": f"agent-{i}",
            "task_id": f"task-{i}",
            "project": "test-proj",
            "task_type": "feature",
            "extracted_at": "2026-07-04T00:00:00Z",
            "terminal_status": "complete" if i < 2 else "failed",
            "loop_count": (i + 1) * 10,
            "total_loops": 200,
            "tool_sequence": json.dumps(["read_file", "patch_file"]),
            "unique_tools": json.dumps(["read_file", "patch_file"]),
            "tool_call_count": 2,
            "cache_read_total": 1000,
            "cache_write_total": 200,
            "error_count": i,
            "error_snippets": "[]",
            "warning_count": 0,
            "warning_types": "[]",
            "is_pipeline": 0,
            "phases_completed": "[]",
            "phase_failed": None,
            "compaction_count": 0,
            "log_size_bytes": 500,
            "log_path": "",
        })
    summary = get_signals_summary(db, project="test-proj")
    assert summary["count"] == 3
    assert "complete" in summary["terminal_status"]
    assert summary["terminal_status"]["complete"] == 2
    assert summary["avg_loop_count"] == 20.0
    assert "read_file" in summary["top_tools"]
    assert summary["cache_hit_rate"] > 0
