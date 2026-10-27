"""
Tests for the web search chain (swarm/api_tools.py, server-side) and
fetch_url() (swarm/tools/core.py, agent-side, egress-guarded).

The search chain moved behind POST /api/tools/web_search so that search API
keys never enter agent subprocesses; the agent-side ``web_search`` is a thin
proxy covered in tests/test_api_tools.py.
"""
import json
import os
from unittest.mock import MagicMock, patch


import swarm.agent_runtime as rt
from swarm.api_tools import run_search_chain


class MockResponse:
    def __init__(self, data):
        self._data = data if isinstance(data, bytes) else json.dumps(data).encode()

    def read(self, n=-1):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class TestWebSearch:
    """Tests for the server-side search chain."""

    def test_web_search_returns_expected_structure_on_mocked_response(self):
        """run_search_chain returns expected structure on a mocked Tavily response."""
        mock_results = {
            "results": [
                {"title": "Godot 4 CharacterBody2D", "url": "https://godotengine.org", "content": "move_and_slide() documentation"},
                {"title": "CharacterBody2D API", "url": "https://docs.godotengine.org", "content": "velocity property"},
            ]
        }
        with patch("urllib.request.urlopen", return_value=MockResponse(mock_results)):
            result = run_search_chain("Godot 4 CharacterBody2D move_and_slide", 3, environ={"TAVILY_API_KEY": "test-key"})

        assert result["ok"] is True
        assert "results" in result
        assert len(result["results"]) == 2
        assert result["results"][0]["title"] == "Godot 4 CharacterBody2D"

    def test_web_search_returns_false_on_network_error_without_raising(self):
        """run_search_chain returns ok=false on network error without raising."""
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            result = run_search_chain("test query", 3, environ={"TAVILY_API_KEY": "test-key"})

        assert result["ok"] is False
        assert "error" in result

    def test_duckduckgo_fallback_when_no_api_keys(self):
        """DuckDuckGo HTML scraper fallback is used when no API keys set."""
        mock_html = b"""
        <html><body>
        <a class="result__a" href="https://godotengine.org">Godot Engine</a>
        <span class="result__snippet">Game development engine</span>
        <a class="result__a" href="https://docs.godotengine.org">GDScript documentation</a>
        <span class="result__snippet">Official GDScript docs</span>
        </body></html>
        """
        with patch("urllib.request.urlopen", return_value=MockResponse(mock_html)):
            result = run_search_chain("Godot tutorial", 3, environ={})

        assert result["ok"] is True
        assert len(result["results"]) >= 1
        assert result["results"][0]["url"] == "https://godotengine.org"

    def test_hard_cap_results_at_5(self):
        """Hard cap results at 5 regardless of argument."""
        many_results = {"results": [{"title": f"Result {i}", "url": f"https://example.com/{i}", "content": f"Content {i}"} for i in range(10)]}
        with patch("urllib.request.urlopen", return_value=MockResponse(many_results)):
            result = run_search_chain("test", 10, environ={"TAVILY_API_KEY": "test-key"})

        assert result["ok"] is True
        assert len(result["results"]) == 5

    def test_agent_side_web_search_never_reads_search_keys(self):
        """The agent-side tool proxies to the controller; keys in its env are irrelevant."""
        with patch("swarm.tools.core._api_request", return_value={"ok": True, "results": []}) as api, \
             patch.dict(os.environ, {"TAVILY_API_KEY": "leaked?"}), \
             patch("urllib.request.urlopen", side_effect=AssertionError("must not hit network")):
            result = rt.web_search("q", max_results=2)
        assert result["ok"] is True
        assert api.call_args[0][1] == "/api/tools/web_search"


class TestFetchUrl:
    """Tests for fetch_url() function (allowlisted host, mocked opener)."""

    URL = "https://docs.godotengine.org/en/stable/classes/class_node.html"

    def _opener(self, content: bytes):
        opener = MagicMock()
        opener.open.return_value = MockResponse(content)
        return opener

    def test_fetch_url_strips_html_tags_when_extract_text_true(self):
        """fetch_url converts HTML to markdown when extract_text=True."""
        html = b"<html><head><style>/* css */</style></head><body><h1>Title</h1><p>Paragraph text</p></body></html>"

        with patch("urllib.request.build_opener", return_value=self._opener(html)):
            result = rt.fetch_url(self.URL, extract_text=True)

        assert result["ok"] is True
        body = result["content"].split("\n", 1)[1].rsplit("\n", 1)[0]   # strip untrusted framing
        assert "<" not in body                # No raw HTML tags in markdown output
        assert "Title" in body                # h1 becomes # Title in markdown
        assert "Paragraph" in body

    def test_fetch_url_returns_plain_content_when_extract_text_false(self):
        """fetch_url returns raw HTML when extract_text=False."""
        html = b"<html><body><p>Hello</p></body></html>"

        with patch("urllib.request.build_opener", return_value=self._opener(html)):
            result = rt.fetch_url(self.URL, extract_text=False)

        assert result["ok"] is True
        assert "Hello" in result["content"]

    def test_fetch_url_returns_truncated_content_when_page_exceeds_8000_chars(self):
        """fetch_url truncates content beyond 8000 chars."""
        long_html = b"<html><body>" + b"x" * 10000 + b"</body></html>"

        with patch("urllib.request.build_opener", return_value=self._opener(long_html)):
            result = rt.fetch_url(self.URL)

        assert result["ok"] is True
        assert result["truncated"] is True
        assert result["length"] <= 8000 + 100  # truncation suffix is small

    def test_fetch_url_returns_error_on_failure(self):
        """fetch_url returns ok=false on failure."""
        opener = MagicMock()
        opener.open.side_effect = Exception("Connection error")
        with patch("urllib.request.build_opener", return_value=opener):
            result = rt.fetch_url(self.URL)

        assert result["ok"] is False
        assert "error" in result

    def test_fetch_url_blocks_non_allowlisted_host(self):
        with patch("urllib.request.build_opener") as build_opener:
            result = rt.fetch_url("https://example.com")
        assert result["ok"] is False
        assert "not in allowlist" in result["error"]
        build_opener.assert_not_called()
