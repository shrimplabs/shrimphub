"""Tests for POST /api/tools/web_search (swarm/api_tools.py) and the agent-side proxy."""
import json
import os
import threading
from unittest.mock import MagicMock, patch

import pytest

from swarm import db


@pytest.fixture()
def app(tmp_path):
    """Fresh Flask test app with an isolated DB (same pattern as tests/test_api.py)."""
    _api_key_vars = ("MINIMAX_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "KIMI_API_KEY")
    _saved_keys = {k: os.environ.pop(k, None) for k in _api_key_vars}
    os.environ["MINIMAX_API_KEY"] = "test-key-do-not-use"
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm_test.db")

    from swarm.api import create_app
    flask_app = create_app(
        config={
            "workspace": str(tmp_path / "workspace"),
            "max_active_agents": 3,
            "lock_project": False,
            "disable_monitor": True,
            "disable_remote_repo": True,
            "project_creation_retry_rounds": 0,
            "llm_provider": "minimax",
            "managed_projects": [],
            "paused_projects": [],
        },
        data_dir=tmp_path / "data",
        config_file=tmp_path / "config.json",  # REQUIRED: never touch the real config.json
    )
    flask_app.config["TESTING"] = True
    yield flask_app

    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None
    for k, v in _saved_keys.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture()
def client(app):
    return app.test_client()


class TestWebSearchRoute:
    def test_route_calls_search_chain_and_returns_results(self, client):
        fake = {"ok": True, "results": [{"title": "T", "url": "https://docs.godotengine.org", "snippet": "s"}]}
        with patch("swarm.api_tools.run_search_chain", return_value=fake) as chain:
            resp = client.post("/api/tools/web_search", json={"query": "godot move_and_slide", "max_results": 2})
        assert resp.status_code == 200
        assert resp.get_json() == fake
        chain.assert_called_once_with("godot move_and_slide", 2)

    def test_route_defaults_max_results(self, client):
        with patch("swarm.api_tools.run_search_chain", return_value={"ok": True, "results": []}) as chain:
            client.post("/api/tools/web_search", json={"query": "q"})
        chain.assert_called_once_with("q", 3)

    def test_route_rejects_missing_query(self, client):
        with patch("swarm.api_tools.run_search_chain") as chain:
            resp = client.post("/api/tools/web_search", json={})
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False
        chain.assert_not_called()

    def test_route_propagates_chain_failure(self, client):
        with patch("swarm.api_tools.run_search_chain", return_value={"ok": False, "error": "All search providers failed."}):
            resp = client.post("/api/tools/web_search", json={"query": "q"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is False


class TestSearchChain:
    def test_keys_come_from_supplied_environ_not_agent(self):
        """Tavily is used when its key is in the controller environ; the result shape is normalised."""
        from swarm.api_tools import run_search_chain
        payload = json.dumps({"results": [{"title": "A", "url": "https://a", "content": "c"}]}).encode()
        m = MagicMock(); m.__enter__ = lambda s: s; m.__exit__ = MagicMock(return_value=False); m.read.return_value = payload
        with patch("urllib.request.urlopen", return_value=m) as urlopen:
            out = run_search_chain("q", 3, environ={"TAVILY_API_KEY": "tk"})
        assert out == {"ok": True, "results": [{"title": "A", "url": "https://a", "snippet": "c"}]}
        req = urlopen.call_args[0][0]
        assert req.get_header("Api-key") == "tk"

    def test_caps_max_results_at_five(self):
        from swarm.api_tools import run_search_chain
        results = [{"title": str(i), "url": f"https://{i}", "content": ""} for i in range(10)]
        m = MagicMock(); m.__enter__ = lambda s: s; m.__exit__ = MagicMock(return_value=False)
        m.read.return_value = json.dumps({"results": results}).encode()
        with patch("urllib.request.urlopen", return_value=m):
            out = run_search_chain("q", 50, environ={"TAVILY_API_KEY": "tk"})
        assert len(out["results"]) == 5


class TestAgentSideProxy:
    def test_agent_web_search_calls_controller_endpoint_without_keys(self):
        """swarm.tools.core.web_search must go through /api/tools/web_search and never read search keys."""
        import swarm.tools.core as core
        fake = {"ok": True, "results": [{"title": "T", "url": "u", "snippet": "s"}]}
        with patch("swarm.tools.core._api_request", return_value=fake) as api, \
             patch.dict(os.environ, {"TAVILY_API_KEY": "should-not-be-used"}), \
             patch("urllib.request.urlopen", side_effect=AssertionError("direct network call")):
            out = core.web_search("godot", max_results=9)
        assert out == fake
        api.assert_called_once_with(
            "POST", "/api/tools/web_search", payload={"query": "godot", "max_results": 5}, timeout=45,
        )

    def test_agent_web_search_returns_error_when_controller_unreachable(self):
        import swarm.tools.core as core
        with patch("swarm.tools.core._api_request", side_effect=OSError("connection refused")):
            out = core.web_search("godot")
        assert out["ok"] is False
        assert "web_search unavailable" in out["error"]
