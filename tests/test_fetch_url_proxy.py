"""fetch_url through the controller (POST /api/tools/fetch_url).

Sandboxed agents have no outbound 443, so the controller fetches for them.
It allows the documentation allowlist plus the exact URLs the calling
agent's own web_search returned -- never an arbitrary host the agent picks.
"""

from unittest.mock import MagicMock, patch

import pytest

import swarm.api_tools as api_tools
import swarm.tools.core as core
from swarm import db
from tests.test_api_authz import AGENT_ID, OTHER_AGENT_ID, _make_app, _seed, _teardown
from tests.test_fetch_url_guard import _opener_returning

OWN_TOKEN = {"X-Swarm-Agent-Token": "tok-own"}
OTHER_TOKEN = {"X-Swarm-Agent-Token": "tok-other"}
BLOG = "https://shaggydev.com/2022/02/13/advanced-state-machines-godot/"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv(core.FETCH_ALLOWED_HOSTS_ENV, raising=False)
    api_tools._search_urls.clear()
    app, saved = _make_app(tmp_path)
    _seed()
    yield app.test_client()
    api_tools._search_urls.clear()
    _teardown(saved)


def _search(client, headers, urls):
    results = [{"title": "t", "url": u, "snippet": ""} for u in urls]
    with patch.object(api_tools, "run_search_chain", return_value={"ok": True, "results": results}):
        r = client.post("/api/tools/web_search", json={"query": "q"}, headers=headers)
    assert r.status_code == 200


def _fetch(client, url, headers=OWN_TOKEN, body=b"<p>state machines</p>"):
    with patch("urllib.request.build_opener", return_value=_opener_returning(body)) as bo:
        r = client.post("/api/tools/fetch_url", json={"url": url}, headers=headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json(), bo


def test_agent_token_may_call_fetch_url(client):
    data, _ = _fetch(client, "https://docs.godotengine.org/en/stable/")
    assert data["ok"] is True and "UNTRUSTED WEB CONTENT" in data["content"]


def test_expanded_allowlist_covers_godot_forum(client):
    data, _ = _fetch(client, "https://forum.godotengine.org/t/some-thread/1234")
    assert data["ok"] is True


def test_off_allowlist_blocked_until_own_search_returns_it(client):
    data, bo = _fetch(client, BLOG)
    assert data["ok"] is False and data["blocked"] is True
    bo.assert_not_called()
    _search(client, OWN_TOKEN, [BLOG + "#section"])
    data, _ = _fetch(client, BLOG)
    assert data["ok"] is True, data


def test_search_approval_is_per_agent(client):
    _search(client, OTHER_TOKEN, [BLOG])
    data, bo = _fetch(client, BLOG, headers=OWN_TOKEN)
    assert data["blocked"] is True
    bo.assert_not_called()


def test_approval_is_exact_url_not_host(client):
    _search(client, OWN_TOKEN, [BLOG])
    for url in ["https://shaggydev.com/other-page",
                BLOG + "?leak=secret",
                "https://evil.shaggydev.com/2022/02/13/advanced-state-machines-godot/"]:
        data, bo = _fetch(client, url)
        assert data["blocked"] is True, url
        bo.assert_not_called()


def test_search_result_on_private_host_still_denied(client):
    _search(client, OWN_TOKEN, ["http://127.0.0.1:5001/api/config", "http://gitea.local:3000/x"])
    for url in ["http://127.0.0.1:5001/api/config", "http://gitea.local:3000/x"]:
        data, bo = _fetch(client, url)
        assert data["blocked"] is True, url
        bo.assert_not_called()


def test_approvals_expire(client, monkeypatch):
    _search(client, OWN_TOKEN, [BLOG])
    real = api_tools.time.time
    monkeypatch.setattr(api_tools.time, "time", lambda: real() + api_tools.SEARCH_URL_TTL_SECS + 1)
    data, _ = _fetch(client, BLOG)
    assert data["blocked"] is True


def test_operator_requests_get_allowlist_only(client):
    data, bo = _fetch(client, BLOG, headers={})
    assert data["blocked"] is True
    bo.assert_not_called()


def test_config_allowlist_is_honoured(tmp_path, monkeypatch):
    monkeypatch.delenv(core.FETCH_ALLOWED_HOSTS_ENV, raising=False)
    app, saved = _make_app(tmp_path, {"fetch_url_allowed_hosts": ["example.org"]})
    _seed()
    try:
        c = app.test_client()
        assert _fetch(c, "https://example.org/x")[0]["ok"] is True
        assert _fetch(c, "https://docs.godotengine.org/")[0]["blocked"] is True
    finally:
        _teardown(saved)


# ---- redirects -------------------------------------------------------------

def test_redirect_may_stay_on_approved_host_only():
    h = core._GuardedRedirectHandler(allowed_hosts=["docs.godotengine.org"], same_site_host="shaggydev.com")
    with patch("urllib.request.HTTPRedirectHandler.redirect_request", return_value="NEW"):
        assert h.redirect_request(MagicMock(), None, 301, "", {}, "https://shaggydev.com/new/") == "NEW"
        assert h.redirect_request(MagicMock(), None, 301, "", {}, "https://docs.godotengine.org/x") == "NEW"
    with pytest.raises(core.FetchBlocked):
        h.redirect_request(MagicMock(), None, 302, "", {}, "https://evil.example.com/")
    with pytest.raises(core.FetchBlocked):
        h.redirect_request(MagicMock(), None, 302, "", {}, "http://127.0.0.1/")


# ---- DuckDuckGo result links -----------------------------------------------

def test_duckduckgo_links_are_decoded_to_the_real_page():
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fshaggydev.com%2Fpost%2F&amp;rut=abc"
    assert api_tools._ddg_target(href) == "https://shaggydev.com/post/"
    assert api_tools._ddg_target("https://example.com/a") == "https://example.com/a"


# ---- agent-side client -------------------------------------------------------

def test_agent_client_logs_blocks_for_the_breaker(capsys, monkeypatch):
    monkeypatch.setattr(core, "_api_request", lambda *a, **k: {
        "ok": False, "blocked": True, "host": "evil.example.com",
        "error": "fetch_url blocked: host evil.example.com not in allowlist"})
    assert core.fetch_url_via_controller("https://evil.example.com/")["ok"] is False
    out = capsys.readouterr().out
    assert "[GUARD] blocked kind=fetch" in out and "host=evil.example.com" in out


def test_agent_client_never_opens_a_socket(monkeypatch):
    monkeypatch.setattr(core, "_api_request", lambda *a, **k: {"ok": True, "host": "docs.godotengine.org",
                                                               "content": "x"})
    with patch("urllib.request.build_opener") as bo:
        assert core.fetch_url_via_controller("https://docs.godotengine.org/")["ok"] is True
    bo.assert_not_called()


def test_dispatch_routes_fetch_url_through_controller():
    from swarm.tool_dispatch import _TOOL_REGISTRY
    spec = next(s for s in _TOOL_REGISTRY if s.name == "fetch_url")
    with patch.object(core, "fetch_url_via_controller", return_value={"ok": True}) as via, \
         patch("swarm.tool_dispatch.fetch_url_via_controller", via):
        spec.handler({"url": "https://docs.godotengine.org/"}, None, None)
    via.assert_called_once()
