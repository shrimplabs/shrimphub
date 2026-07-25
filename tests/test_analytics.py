"""Tests for swarm.analytics aggregate queries (roadmap #7)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import swarm.db as db
from swarm import analytics


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


def _task(tid, project, ttype, status="completed", **meta_and_fields):
    attempts = meta_and_fields.pop("attempts", 0)
    metadata = meta_and_fields.pop("metadata", {})
    db.task_upsert({
        "id": tid, "project": project, "type": ttype,
        "description": "x", "priority": 50, "status": status,
        "dependencies": [], "metadata": metadata,
        "attempts": attempts, "max_attempts": 3,
        "completed": meta_and_fields.pop("completed", "2026-07-01T00:00:00"),
    })


def _agent(aid, project, cost=0.0, tin=0, tout=0, loops=0):
    # agent_upsert writes the base row + input/output tokens; cost, loop_count,
    # and cache columns are written separately via agent_update_status (mirrors
    # how _mark_agent_finished populates them in production).
    db.agent_upsert({
        "id": aid, "project": project, "task_type": "feature",
        "status": "completed", "input_tokens": tin, "output_tokens": tout,
        "metadata": {},
    })
    db.agent_update_status(aid, "completed",
                           estimated_cost_usd=cost, loop_count=loops)


# ---------------------------------------------------------------------------
# value / repair -- reproduce the run-11 hand analysis shape
# ---------------------------------------------------------------------------

def test_value_repair_ratio_basic():
    # 8 value tasks, ~2.8 repair -> pick counts that yield 2.8 exactly.
    # 14 value / 5 repair = 2.8.
    for i in range(14):
        _task(f"v{i}", "run11-art", "feature")
    for i in range(5):
        _task(f"r{i}", "run11-art", "bug", metadata={"is_validation_bug": True})

    result = analytics.value_repair(db, project="run11-art")
    assert result["value_tasks"] == 14
    assert result["repair_tasks"] == 5
    assert result["value_repair_ratio"] == 2.8


def test_repair_counts_retried_bugs_not_just_validation():
    _task("v1", "p", "feature")
    _task("r1", "p", "bug", attempts=2)              # retried -> repair
    _task("r2", "p", "bug", metadata={"is_validation_bug": True})  # validation -> repair
    _task("b_clean", "p", "bug", attempts=1)         # first-try bug -> NOT repair
    result = analytics.value_repair(db, project="p")
    assert result["value_tasks"] == 1
    assert result["repair_tasks"] == 2


def test_value_repair_only_counts_completed():
    _task("v1", "p", "feature", status="completed")
    _task("v2", "p", "feature", status="failed")     # excluded
    result = analytics.value_repair(db, project="p")
    assert result["value_tasks"] == 1


def test_value_repair_by_project_sorted_desc():
    db.project_upsert({"name": "hi", "head_task_id": None})
    db.project_upsert({"name": "lo", "head_task_id": None})
    for i in range(6):
        _task(f"hi{i}", "hi", "feature")
    _task("hi_r", "hi", "bug", metadata={"is_validation_bug": True})   # 6/1 = 6.0
    for i in range(2):
        _task(f"lo{i}", "lo", "feature")
    _task("lo_r", "lo", "bug", metadata={"is_validation_bug": True})   # 2/1 = 2.0
    rows = analytics.value_repair_by_project(db)
    ratios = [r["value_repair_ratio"] for r in rows]
    assert ratios == sorted(ratios, reverse=True)
    assert rows[0]["project"] == "hi"


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------

def test_overview_cost_and_counts(tmp_path):
    _task("c1", "p", "feature", status="completed")
    _task("c2", "p", "feature", status="completed")
    _task("f1", "p", "bug", status="failed")
    _agent("a1", "p", cost=0.50, tin=1000, tout=200, loops=10)
    _agent("a2", "p", cost=1.50, tin=3000, tout=400, loops=20)

    result = analytics.overview(db, tmp_path, project="p")
    assert result["tasks_completed"] == 2
    assert result["tasks_failed"] == 1
    assert result["total_cost_usd"] == 2.0
    assert result["avg_cost_per_completed_task"] == 1.0
    assert result["avg_loops"] == 15.0


def test_overview_includes_jsonl_fallback(tmp_path):
    _task("c1", "p", "feature")
    _agent("a1", "p", cost=1.0)
    # Archived agent only in JSONL (pruned from DB)
    (tmp_path / "agent-history.jsonl").write_text(
        json.dumps({"id": "old", "project": "p", "estimated_cost_usd": 2.0,
                    "input_tokens": 0, "output_tokens": 0, "loop_count": 5}) + "\n"
    )
    result = analytics.overview(db, tmp_path, project="p")
    assert result["total_cost_usd"] == 3.0  # live + archived


# ---------------------------------------------------------------------------
# deaths / mechanisms (agent_signals-backed, degrade to empty)
# ---------------------------------------------------------------------------

def _signal(aid, project, status, task_type="feature", loops=0, errors=None, mechs=None):
    db.agent_signals_upsert({
        "agent_id": aid, "task_id": aid + "-t", "project": project,
        "task_type": task_type, "extracted_at": "2026-07-01T00:00:00",
        "terminal_status": status, "loop_count": loops, "total_loops": 200,
        "tool_sequence": "[]", "unique_tools": "[]", "tool_call_count": 0,
        "cache_read_total": 0, "cache_write_total": 0,
        "error_count": len(errors or []), "error_snippets": json.dumps(errors or []),
        "warning_count": 0, "warning_types": "[]",
        "is_pipeline": 0, "phases_completed": "[]", "phase_failed": None,
        "compaction_count": 0, "log_size_bytes": 100, "log_path": "",
        "mechanism_fires": json.dumps(mechs or {}),
    })


def test_deaths_empty_when_no_signals():
    assert analytics.deaths(db)["count"] == 0


def test_deaths_aggregates(tmp_path):
    _signal("a1", "p", "complete", loops=30)
    _signal("a2", "p", "loop_limit", loops=200, errors=["File not found: x.gd"])
    _signal("a3", "p", "failed", loops=50, errors=["File not found: x.gd"])
    d = analytics.deaths(db, project="p")
    assert d["count"] == 3
    assert d["terminal_status"]["complete"] == 1
    assert d["terminal_status"]["loop_limit"] == 1
    assert d["top_errors"][0][0].startswith("File not found")
    assert d["top_errors"][0][1] == 2


def test_mechanisms_empty_when_no_column_data():
    _signal("a1", "p", "complete")  # mechanism_fires = {}
    m = analytics.mechanisms(db, project="p")
    assert m["mechanisms"] == {}
    # completion rate still computed on the with/without split
    assert m["completion_rate_without_any_mechanism"] == 1.0


def test_mechanisms_efficacy_split():
    # 2 runs fired truncation_retry: 1 completed, 1 failed -> 0.5
    _signal("a1", "p", "complete", mechs={"truncation_retry": 3})
    _signal("a2", "p", "failed", mechs={"truncation_retry": 8})
    # 1 run no mechanism, completed -> 1.0
    _signal("a3", "p", "complete", mechs={})
    m = analytics.mechanisms(db, project="p")
    assert m["mechanisms"]["truncation_retry"]["fired_runs"] == 2
    assert m["mechanisms"]["truncation_retry"]["completion_rate"] == 0.5
    assert m["completion_rate_without_any_mechanism"] == 1.0


# ---------------------------------------------------------------------------
# ship candidates
# ---------------------------------------------------------------------------

def test_ship_candidates_godot_only_and_ranked(tmp_path):
    ws = tmp_path / "ws"
    # Godot project A: clean
    (ws / "game-a").mkdir(parents=True)
    (ws / "game-a" / "project.godot").write_text("")
    # Godot project B: has validation bugs (less shippable)
    (ws / "game-b").mkdir(parents=True)
    (ws / "game-b" / "project.godot").write_text("")
    # Non-Godot project: excluded
    (ws / "pylib").mkdir(parents=True)

    db.project_upsert({"name": "game-a", "head_task_id": None})
    db.project_upsert({"name": "game-b", "head_task_id": None})
    db.project_upsert({"name": "pylib", "head_task_id": None})

    _task("a1", "game-a", "feature", status="completed")
    for i in range(3):
        _task(f"b{i}", "game-b", "bug", status="completed",
              metadata={"is_validation_bug": True})

    rows = analytics.ship_candidates(db, tmp_path, ws)
    names = [r["project"] for r in rows]
    assert "pylib" not in names
    assert set(names) == {"game-a", "game-b"}
    # game-a (0 val bugs) ranks ahead of game-b (3)
    assert names[0] == "game-a"


# ---------------------------------------------------------------------------
# cost breakdown
# ---------------------------------------------------------------------------

def _agent_with_cost(aid, project, task_id, cost=0.0, tin=0, tout=0,
                     model="", provider=""):
    """Seed an agent with task_id, model, provider, and cost (for cost() tests)."""
    db.agent_upsert({
        "id": aid, "project": project, "task_type": "feature",
        "task_id": task_id, "status": "completed",
        "input_tokens": tin, "output_tokens": tout,
        "metadata": {},
    })
    db.agent_update_status(aid, "completed",
                           estimated_cost_usd=cost,
                           model=model, provider=provider)


def test_cost_zero_when_no_agents(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    result = analytics.cost(db, data_dir)
    assert result["total_cost_usd"] == 0.0
    assert result["agents_counted"] == 0
    assert result["by_project"] == []
    assert result["by_project_task_type"] == []
    assert result["by_model"] == []
    assert result["by_provider"] == []
    assert result["cost_per_completed_task"] == 0.0


def test_cost_groups_by_project_and_task_type(tmp_path):
    _task("t1", "alpha", "feature", status="completed")
    _task("t2", "alpha", "bug", status="completed")
    _task("t3", "beta", "feature", status="completed")

    _agent_with_cost("a1", "alpha", "t1", cost=1.50, tin=1000, tout=500,
                     model="claude-opus-4.7", provider="anthropic")
    _agent_with_cost("a2", "alpha", "t2", cost=0.50, tin=200, tout=100,
                     model="claude-opus-4.7", provider="anthropic")
    _agent_with_cost("a3", "beta", "t3", cost=2.00, tin=3000, tout=1500,
                     model="MiniMax-m3", provider="MiniMax")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    result = analytics.cost(db, data_dir)

    assert result["total_cost_usd"] == 4.0
    assert result["agents_counted"] == 3
    # alpha has 2 completed tasks, $2.00 total -> $1.00/task
    assert result["cost_per_completed_task"] == round(4.0 / 3, 4)

    # by_project sorted by cost desc
    projs = {p["project"]: p for p in result["by_project"]}
    assert projs["beta"]["cost_usd"] == 2.0
    assert projs["alpha"]["cost_usd"] == 2.0
    assert projs["alpha"]["agents"] == 2
    assert projs["alpha"]["tokens_in"] == 1200
    assert projs["alpha"]["tokens_out"] == 600

    # by_project_task_type has 3 rows (alpha/feature, alpha/bug, beta/feature)
    keys = {(r["project"], r["task_type"]) for r in result["by_project_task_type"]}
    assert keys == {("alpha", "feature"), ("alpha", "bug"), ("beta", "feature")}
    bug_row = next(r for r in result["by_project_task_type"]
                   if r["project"] == "alpha" and r["task_type"] == "bug")
    assert bug_row["cost_usd"] == 0.5

    # by_model / by_provider aggregated
    models = {m["model"]: m for m in result["by_model"]}
    assert models["claude-opus-4.7"]["cost_usd"] == 2.0
    assert models["claude-opus-4.7"]["agents"] == 2
    assert models["MiniMax-m3"]["cost_usd"] == 2.0
    providers = {p["provider"]: p for p in result["by_provider"]}
    assert providers["anthropic"]["cost_usd"] == 2.0
    assert providers["MiniMax"]["cost_usd"] == 2.0


def test_cost_project_filter(tmp_path):
    _task("t1", "alpha", "feature", status="completed")
    _task("t2", "beta", "feature", status="completed")
    _agent_with_cost("a1", "alpha", "t1", cost=1.0, model="m")
    _agent_with_cost("a2", "beta", "t2", cost=5.0, model="m")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    result = analytics.cost(db, data_dir, project="alpha")
    assert result["project"] == "alpha"
    assert result["total_cost_usd"] == 1.0
    assert result["agents_counted"] == 1
    assert all(p["project"] == "alpha" for p in result["by_project"])


def test_cost_includes_archived_agents(tmp_path):
    _task("t1", "alpha", "feature", status="completed")
    _agent_with_cost("a1", "alpha", "t1", cost=1.0, model="m")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # archived row for the same project (no longer in live agents table)
    (data_dir / "agent-history.jsonl").write_text(
        json.dumps({
            "id": "archived-1", "project": "alpha", "task_id": "t1",
            "estimated_cost_usd": 2.5, "input_tokens": 100, "output_tokens": 50,
            "model": "m", "provider": "p", "loop_count": 10,
        }) + "\n"
    )
    result = analytics.cost(db, data_dir)
    # live + archived
    assert result["total_cost_usd"] == 3.5
    assert result["agents_counted"] == 2


def test_cost_skips_zero_agents(tmp_path):
    _task("t1", "alpha", "feature", status="completed")
    # agent with no cost and no tokens => skip
    db.agent_upsert({
        "id": "noop", "project": "alpha", "task_type": "feature",
        "task_id": "t1", "status": "completed",
        "input_tokens": 0, "output_tokens": 0, "metadata": {},
    })
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    result = analytics.cost(db, data_dir)
    assert result["total_cost_usd"] == 0.0
    assert result["agents_counted"] == 0


# ---------------------------------------------------------------------------
# death-cause buckets
# ---------------------------------------------------------------------------

def test_deaths_buckets_empty_when_no_signals():
    d = analytics.deaths(db)
    assert d["cause_buckets"] == {
        "loop_limit": 0, "no_task_complete": 0, "validation_fail": 0, "other": 0,
    }


def test_deaths_buckets_classify_all_four():
    # loop_limit: loop_count >= 195
    _signal("a1", "p", "failed", loops=200, errors=["oops"])
    # no_task_complete: failed with low loops and no phase_failed
    _signal("a2", "p", "failed", loops=30)
    # validation_fail: needs phase_failed set -> use direct upsert below
    db.agent_signals_upsert({
        "agent_id": "a3", "task_id": "a3-t", "project": "p",
        "task_type": "qa", "extracted_at": "2026-07-01T00:00:00",
        "terminal_status": "failed", "loop_count": 100, "total_loops": 100,
        "tool_sequence": "[]", "unique_tools": "[]", "tool_call_count": 0,
        "cache_read_total": 0, "cache_write_total": 0,
        "error_count": 2, "error_snippets": "[\"x\"]",
        "warning_count": 0, "warning_types": "[]",
        "is_pipeline": 1, "phases_completed": "[\"llm\"]",
        "phase_failed": "validate",
        "compaction_count": 0, "log_size_bytes": 100, "log_path": "",
        "mechanism_fires": "{}",
    })
    # other: parse_error
    db.agent_signals_upsert({
        "agent_id": "a4", "task_id": "a4-t", "project": "p",
        "task_type": "feature", "extracted_at": "2026-07-01T00:00:00",
        "terminal_status": "parse_error:Something", "loop_count": 5, "total_loops": 5,
        "tool_sequence": "[]", "unique_tools": "[]", "tool_call_count": 0,
        "cache_read_total": 0, "cache_write_total": 0,
        "error_count": 0, "error_snippets": "[]",
        "warning_count": 0, "warning_types": "[]",
        "is_pipeline": 0, "phases_completed": "[]", "phase_failed": None,
        "compaction_count": 0, "log_size_bytes": 100, "log_path": "",
        "mechanism_fires": "{}",
    })
    # successful completion - doesn't count as a death
    _signal("a5", "p", "complete", loops=40)

    d = analytics.deaths(db, project="p")
    assert d["cause_buckets"]["loop_limit"] == 1
    assert d["cause_buckets"]["no_task_complete"] == 1
    assert d["cause_buckets"]["validation_fail"] == 1
    assert d["cause_buckets"]["other"] == 1
    # count includes the successful row but cause_buckets excludes it
    assert d["count"] == 5


def test_deaths_buckets_all_in_one_bucket():
    # 5 loop_limit deaths
    for i in range(5):
        _signal(f"a{i}", "p", "loop_limit", loops=200)
    d = analytics.deaths(db, project="p")
    assert d["cause_buckets"]["loop_limit"] == 5
    assert d["cause_buckets"]["no_task_complete"] == 0
    assert d["cause_buckets"]["validation_fail"] == 0
    assert d["cause_buckets"]["other"] == 0
