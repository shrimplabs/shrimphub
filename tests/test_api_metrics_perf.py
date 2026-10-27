"""/api/metrics must stay cheap (2026-09-30).

It walked the entire workspace (~77k dirs, ~6s) on every 30s dashboard poll
and its single ``grep`` over every agent log overflowed ARG_MAX, silently
reporting web_search_calls=0.
"""

import os

import pytest

import swarm.api_metrics as api_metrics
from tests.test_api_authz import _make_app, _seed, _teardown


@pytest.fixture()
def app(tmp_path):
    flask_app, saved = _make_app(tmp_path)
    _seed()
    yield flask_app, tmp_path
    _teardown(saved)


def test_knowledge_files_counted_at_project_roots_only(tmp_path):
    ws = tmp_path / "ws"
    for rel in ["game-a/AGENT_KNOWLEDGE.md", "game-b/AGENT_KNOWLEDGE.md",
                ".wt-game-a-1234/AGENT_KNOWLEDGE.md",       # agent worktree copy
                "game-a/archive/old/AGENT_KNOWLEDGE.md",     # nested copy
                ".hidden/AGENT_KNOWLEDGE.md"]:
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text("k\n")
    (ws / "game-c").mkdir()
    (ws / "loose.md").write_text("x")
    paths = api_metrics._knowledge_file_paths(str(ws))
    assert [os.path.basename(os.path.dirname(p)) for p in paths] == ["game-a", "game-b"]
    assert api_metrics._knowledge_file_paths(str(tmp_path / "missing")) == []


def test_log_count_batches_past_arg_max(tmp_path, monkeypatch):
    monkeypatch.setattr(api_metrics, "_GREP_BATCH", 3)
    logs = []
    for i in range(7):  # 3 + 3 + 1: includes a single-file batch
        f = tmp_path / f"agent_{i}.log"
        f.write_text("Executing tool: web_search\n" * (i % 3) + "other\n")
        logs.append(str(f))
    assert api_metrics._count_in_logs("Executing tool: web_search", logs) == sum(i % 3 for i in range(7))


def test_metrics_is_cached(app, monkeypatch):
    flask_app, _ = app
    calls = []
    real = api_metrics._knowledge_file_paths
    monkeypatch.setattr(api_metrics, "_knowledge_file_paths", lambda d: calls.append(d) or real(d))
    c = flask_app.test_client()
    a = c.get("/api/metrics").get_json()
    b = c.get("/api/metrics").get_json()
    assert a == b and len(calls) == 1
    real_time = api_metrics.time.time
    monkeypatch.setattr(api_metrics.time, "time", lambda: real_time() + api_metrics.METRICS_CACHE_SECS + 1)
    c.get("/api/metrics")
    assert len(calls) == 2


def test_knowledge_files_endpoint_lists_project_roots(app):
    flask_app, tmp_path = app
    ws = tmp_path / "workspace"
    (ws / "game-x").mkdir(parents=True, exist_ok=True)
    (ws / "game-x" / "AGENT_KNOWLEDGE.md").write_text("hello")
    (ws / ".wt-game-x-1" ).mkdir(exist_ok=True)
    (ws / ".wt-game-x-1" / "AGENT_KNOWLEDGE.md").write_text("dup")
    files = flask_app.test_client().get("/api/metrics/knowledge-files").get_json()["files"]
    assert [(f["project"], f["content"]) for f in files] == [("game-x", "hello")]
