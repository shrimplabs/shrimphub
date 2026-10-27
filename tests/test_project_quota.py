"""Tests for per-project RPM/quota-budget persistence and soft enforcement
(roadmap #8, M5)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

import swarm.db as db
from swarm.model_policy import check_project_quota_cap, resolve_policy


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


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# --- db.py accessor round-trip ---


def test_project_tier_usage_increment_creates_row():
    db.project_tier_usage_increment("my-project", _today(), cheap=3, strong=1, default=0)

    row = db.project_tier_usage_get("my-project", _today())
    assert row["cheap_loops"] == 3
    assert row["strong_loops"] == 1
    assert row["default_loops"] == 0


def test_project_tier_usage_increment_accumulates():
    today = _today()
    db.project_tier_usage_increment("my-project", today, cheap=2, strong=1)
    db.project_tier_usage_increment("my-project", today, cheap=1, strong=4)

    row = db.project_tier_usage_get("my-project", today)
    assert row["cheap_loops"] == 3
    assert row["strong_loops"] == 5


def test_project_tier_usage_get_returns_none_when_absent():
    assert db.project_tier_usage_get("never-seen-project", _today()) is None


def test_project_tier_usage_recent_orders_by_day_desc():
    db.project_tier_usage_increment("my-project", "2026-09-01", strong=1)
    db.project_tier_usage_increment("my-project", "2026-09-03", strong=2)
    db.project_tier_usage_increment("my-project", "2026-09-02", strong=3)

    rows = db.project_tier_usage_recent("my-project", days=7)
    assert [r["day"] for r in rows] == ["2026-09-03", "2026-09-02", "2026-09-01"]


def test_project_tier_usage_scoped_per_project():
    today = _today()
    db.project_tier_usage_increment("project-a", today, strong=5)
    db.project_tier_usage_increment("project-b", today, strong=1)

    assert db.project_tier_usage_get("project-a", today)["strong_loops"] == 5
    assert db.project_tier_usage_get("project-b", today)["strong_loops"] == 1


# --- check_project_quota_cap ---


def test_check_project_quota_cap_uncapped_by_default():
    status = check_project_quota_cap("no-cap-project", {})
    assert status.over_cap is False
    assert status.cap is None


def test_check_project_quota_cap_under_budget():
    db.project_tier_usage_increment("capped-project", _today(), strong=5)
    config = {"project_quota_caps": {"capped-project": {"max_strong_loops_per_day": 100}}}

    status = check_project_quota_cap("capped-project", config)
    assert status.over_cap is False
    assert status.strong_loops_today == 5
    assert status.cap == 100
    assert status.pct_used == 5.0


def test_check_project_quota_cap_over_budget():
    db.project_tier_usage_increment("capped-project", _today(), strong=100)
    config = {"project_quota_caps": {"capped-project": {"max_strong_loops_per_day": 100}}}

    status = check_project_quota_cap("capped-project", config)
    assert status.over_cap is True


def test_check_project_quota_cap_never_raises_on_bad_config():
    status = check_project_quota_cap("weird-project", {"project_quota_caps": {"weird-project": {"max_strong_loops_per_day": "not-a-number"}}})
    assert status.over_cap is False


# --- resolve_policy quota-downgrade integration ---


def test_resolve_policy_downgrades_either_ceiling_when_over_cap():
    db.project_tier_usage_increment("capped-project", _today(), strong=100)
    config = {"project_quota_caps": {"capped-project": {"max_strong_loops_per_day": 100}}}

    decision = resolve_policy(
        config,
        project="capped-project",
        task_type="feature",  # DEFAULT_POLICY_TABLE gives tier="either" here
        phase="work",
        complexity="normal",
        default_provider="minimax",
    )

    assert decision.tier_ceiling == "fast"
    assert decision.reason == "policy_table_quota_capped"


def test_resolve_policy_never_downgrades_strong_ceiling_when_over_cap():
    # SAFETY: complexity="complex" forces tier="strong" -- budget pressure
    # must never override a deliberate quality decision like this.
    db.project_tier_usage_increment("capped-project", _today(), strong=100)
    config = {"project_quota_caps": {"capped-project": {"max_strong_loops_per_day": 100}}}

    decision = resolve_policy(
        config,
        project="capped-project",
        task_type="feature",
        phase="work",
        complexity="complex",
        default_provider="minimax",
    )

    assert decision.tier_ceiling == "strong"
    assert decision.reason == "policy_table"


def test_resolve_policy_unaffected_when_under_cap():
    db.project_tier_usage_increment("capped-project", _today(), strong=5)
    config = {"project_quota_caps": {"capped-project": {"max_strong_loops_per_day": 100}}}

    decision = resolve_policy(
        config,
        project="capped-project",
        task_type="feature",
        phase="work",
        complexity="normal",
        default_provider="minimax",
    )

    assert decision.tier_ceiling == "either"
    assert decision.reason == "policy_table"
