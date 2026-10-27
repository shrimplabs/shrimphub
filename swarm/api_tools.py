"""Controller-side tool backends for agent subprocesses.

Routes: POST /api/tools/web_search, POST /api/tools/fetch_url

fetch_url runs here because sandboxed agents have no outbound 443.  The
controller applies the egress rules in ``swarm.tools.core.fetch_url``: the
documentation allowlist (config ``fetch_url_allowed_hosts``), plus the exact
URLs this agent's own web_search returned in the last few hours -- so agents
can read any page search found them, but cannot pick an arbitrary host and
query string.

Agents no longer receive web-search API keys (BRAVE/TAVILY/SERPER) in their
environment.  Instead ``swarm.tools.core.web_search`` calls this endpoint on
localhost and the controller runs the search chain with keys that never leave
the server process:

    Tavily -> Brave -> Serper -> DuckDuckGo HTML (no key required)
"""

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from flask import g, jsonify, request

MAX_RESULTS_CAP = 5
_SEARCH_TIMEOUT = 10


def _tavily(query: str, max_results: int, key: str) -> list:
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps({"query": query, "max_results": max_results}).encode(),
        headers={"Content-Type": "application/json", "Api-Key": key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_SEARCH_TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", r.get("snippet", "")),
        }
        for r in data.get("results", [])[:max_results]
    ]


def _brave(query: str, max_results: int, key: str) -> list:
    q = urllib.parse.quote(query)
    req = urllib.request.Request(
        f"https://api.search.brave.com/res/v1/web/search?q={q}&count={max_results}",
        headers={"Accept": "application/json", "X-Subscription-Token": key},
    )
    with urllib.request.urlopen(req, timeout=_SEARCH_TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("description", r.get("snippet", "")),
        }
        for r in data.get("web", {}).get("results", [])[:max_results]
    ]


def _serper(query: str, max_results: int, key: str) -> list:
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=json.dumps({"q": query, "num": max_results}).encode(),
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_SEARCH_TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("link", ""),
            "snippet": r.get("snippet", ""),
        }
        for r in data.get("organic", [])[:max_results]
    ]


def _ddg_target(href: str) -> str:
    """DuckDuckGo HTML results link through //duckduckgo.com/l/?uddg=<url>;
    return the real destination so agents fetch the page, not the redirector."""
    href = (href or "").replace("&amp;", "&")
    if href.startswith("//"):
        href = "https:" + href
    parts = urllib.parse.urlsplit(href)
    if parts.netloc.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parts.query).get("uddg", [""])[0]
        if target:
            return target
    return href


def _duckduckgo(query: str, max_results: int) -> list:
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}&kl=us-en"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=_SEARCH_TIMEOUT) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    results = []
    for result_div in re.finditer(r'<div class="result__body">(.+?)</div>', html, re.DOTALL):
        div_html = result_div.group(1)
        a_match = re.search(r'<a class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', div_html)
        snippet_match = re.search(r'<a class="result__snippet"[^>]*>([^<]+)</a>', div_html)
        if a_match:
            results.append({
                "url": _ddg_target(a_match.group(1)),
                "title": re.sub(r'<[^>]+>', '', a_match.group(2)).strip(),
                "snippet": (snippet_match.group(1).strip() if snippet_match else ""),
            })
        if len(results) >= max_results:
            break
    if not results:
        for a_tag in re.finditer(r'<a class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', html):
            results.append({
                "url": _ddg_target(a_tag.group(1)),
                "title": re.sub(r'<[^>]+>', '', a_tag.group(2)).strip(),
                "snippet": "",
            })
            if len(results) >= max_results:
                break
    return results


def run_search_chain(query: str, max_results: int = 3, environ=None) -> dict:
    """Run the web search chain server-side.

    Falls back through Tavily -> Brave -> Serper -> DuckDuckGo.  Keys are read
    from ``environ`` (defaults to ``os.environ`` of the controller process).
    """
    environ = os.environ if environ is None else environ
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = 3
    max_results = max(1, min(max_results, MAX_RESULTS_CAP))

    keyed_backends = (
        ("TAVILY_API_KEY", _tavily),
        ("BRAVE_API_KEY", _brave),
        ("SERPER_API_KEY", _serper),
    )
    for env_name, backend in keyed_backends:
        key = (environ.get(env_name) or "").strip()
        if not key:
            continue
        try:
            results = backend(query, max_results, key)
            if results:
                return {"ok": True, "results": results}
        except Exception:
            continue

    try:
        results = _duckduckgo(query, max_results)
        if results:
            return {"ok": True, "results": results}
    except Exception:
        pass

    return {
        "ok": False,
        "error": "All search providers failed. Set BRAVE_API_KEY, TAVILY_API_KEY, or "
                 "SERPER_API_KEY in the controller's .env for reliable results.",
    }


SEARCH_URL_TTL_SECS = 4 * 3600
_SEARCH_URLS_PER_AGENT = 300
_search_urls: dict = {}          # agent_id -> {normalized url: expiry}
_search_urls_lock = threading.Lock()


def _agent_id() -> str:
    principal = getattr(g, "swarm_principal", None) or {}
    return principal.get("agent_id") or "" if principal.get("kind") == "agent" else ""


def remember_search_urls(agent_id: str, results: list) -> None:
    """Record the URLs a web_search returned to ``agent_id`` as fetchable."""
    from swarm.tools.core import _normalize_fetch_url
    if not agent_id:
        return
    now = time.time()
    with _search_urls_lock:
        urls = _search_urls.setdefault(agent_id, {})
        for r in results or []:
            u = _normalize_fetch_url((r or {}).get("url", ""))
            if u:
                urls[u] = now + SEARCH_URL_TTL_SECS
        for u in [u for u, exp in urls.items() if exp < now]:
            del urls[u]
        while len(urls) > _SEARCH_URLS_PER_AGENT:
            del urls[min(urls, key=urls.get)]


def approved_search_urls(agent_id: str) -> frozenset:
    now = time.time()
    with _search_urls_lock:
        return frozenset(u for u, exp in _search_urls.get(agent_id, {}).items() if exp >= now)


def register_routes(app, config=None):
    """Register controller-side tool routes on the Flask app."""
    config = config if config is not None else {}

    @app.route("/api/tools/web_search", methods=["POST"])
    def tools_web_search():
        data = request.get_json(silent=True) or {}
        query = data.get("query", "")
        if not str(query).strip():
            return jsonify({"ok": False, "error": "query is required"}), 400
        result = run_search_chain(query, data.get("max_results", 3))
        if result.get("ok"):
            remember_search_urls(_agent_id(), result.get("results"))
        return jsonify(result), 200

    @app.route("/api/tools/fetch_url", methods=["POST"])
    def tools_fetch_url():
        from swarm.tools.core import DEFAULT_FETCH_ALLOWED_HOSTS, fetch_url
        data = request.get_json(silent=True) or {}
        url = str(data.get("url", "") or "").strip()
        if not url:
            return jsonify({"ok": False, "error": "url is required"}), 400
        hosts = config.get("fetch_url_allowed_hosts")
        if isinstance(hosts, (list, tuple)) and hosts:
            allowed = [str(h).strip().lower().lstrip(".") for h in hosts if str(h).strip()]
        else:
            allowed = list(DEFAULT_FETCH_ALLOWED_HOSTS)
        result = fetch_url(
            url, bool(data.get("extract_text", True)),
            allowed_hosts=allowed, approved_urls=approved_search_urls(_agent_id()),
        )
        return jsonify(result), 200
