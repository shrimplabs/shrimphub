"""
Tests for web_search() and fetch_url() in swarm/agent_runtime.py.
"""
import os
from unittest.mock import MagicMock, patch


import swarm.agent_runtime as rt


class TestWebSearch:
    """Tests for web_search() function."""

    def test_web_search_returns_expected_structure_on_mocked_response(self):
        """web_search returns expected structure on a mocked HTTP response."""
        import json

        # Mock Tavily response
        mock_results = {
            "results": [
                {"title": "Godot 4 CharacterBody2D", "url": "https://godotengine.org", "content": "move_and_slide() documentation"},
                {"title": "CharacterBody2D API", "url": "https://docs.godotengine.org", "content": "velocity property"},
            ]
        }

        class MockResponse:
            def __init__(self, data):
                self._data = json.dumps(data).encode()

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = MockResponse(mock_results)
            with patch.dict(os.environ, {"TAVILY_API_KEY": "test-key"}):
                result = rt.web_search("Godot 4 CharacterBody2D move_and_slide", max_results=3)

        assert result["ok"] is True
        assert "results" in result
        assert len(result["results"]) == 2
        assert result["results"][0]["title"] == "Godot 4 CharacterBody2D"

    def test_web_search_returns_false_on_network_error_without_raising(self):
        """web_search returns ok=false on network error without raising."""
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            with patch.dict(os.environ, {"TAVILY_API_KEY": "test-key"}):
                result = rt.web_search("test query")

        assert result["ok"] is False
        assert "error" in result

    def test_duckduckgo_fallback_when_no_api_keys(self):
        """DuckDuckGo HTML scraper fallback is used when no API keys set."""

        # DDG HTML scraper returns HTML with result__a / result__snippet CSS classes
        mock_html = b"""
        <html><body>
        <a class="result__a" href="https://godotengine.org">Godot Engine</a>
        <span class="result__snippet">Game development engine</span>
        <a class="result__a" href="https://docs.godotengine.org">GDScript documentation</a>
        <span class="result__snippet">Official GDScript docs</span>
        </body></html>
        """

        class MockResponse:
            def read(self):
                return mock_html

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = MockResponse()
            with patch.dict(os.environ, {"TAVILY_API_KEY": "", "BRAVE_API_KEY": "", "SERPER_API_KEY": ""}):
                result = rt.web_search("Godot tutorial")

        assert result["ok"] is True
        assert len(result["results"]) >= 1
        assert result["results"][0]["url"] == "https://godotengine.org"

    def test_hard_cap_results_at_5(self):
        """Hard cap results at 5 regardless of argument."""
        import json

        class MockResponse:
            def __init__(self, data):
                self._data = json.dumps(data).encode()

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        # Return 10 results, request 10, should still be capped at 5
        many_results = {"results": [{"title": f"Result {i}", "url": f"https://example.com/{i}", "content": f"Content {i}"} for i in range(10)]}

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = MockResponse(many_results)
            with patch.dict(os.environ, {"TAVILY_API_KEY": "test-key"}):
                result = rt.web_search("test", max_results=10)

        assert result["ok"] is True
        assert len(result["results"]) == 5


class TestFetchUrl:
    """Tests for fetch_url() function."""

    def _make_urlopen_mock(self, content: bytes):
        """Return a context-manager mock for urllib.request.urlopen."""
        m = MagicMock()
        m.__enter__ = lambda s: s
        m.__exit__ = MagicMock(return_value=False)
        m.read.return_value = content
        return m

    def test_fetch_url_strips_html_tags_when_extract_text_true(self):
        """fetch_url converts HTML to markdown when extract_text=True."""
        html = b"<html><head><style>/* css */</style></head><body><h1>Title</h1><p>Paragraph text</p></body></html>"

        with patch("urllib.request.urlopen", return_value=self._make_urlopen_mock(html)):
            result = rt.fetch_url("https://example.com", extract_text=True)

        assert result["ok"] is True
        assert "<" not in result["content"]   # No raw HTML tags in markdown output
        assert "Title" in result["content"]   # h1 becomes # Title in markdown
        assert "Paragraph" in result["content"]

    def test_fetch_url_returns_plain_content_when_extract_text_false(self):
        """fetch_url returns raw HTML when extract_text=False."""
        html = b"<html><body><p>Hello</p></body></html>"

        with patch("urllib.request.urlopen", return_value=self._make_urlopen_mock(html)):
            result = rt.fetch_url("https://example.com", extract_text=False)

        assert result["ok"] is True
        assert "Hello" in result["content"]

    def test_fetch_url_returns_truncated_content_when_page_exceeds_8000_chars(self):
        """fetch_url truncates content beyond 8000 chars."""
        long_html = b"<html><body>" + b"x" * 10000 + b"</body></html>"

        with patch("urllib.request.urlopen", return_value=self._make_urlopen_mock(long_html)):
            result = rt.fetch_url("https://example.com/large")

        assert result["ok"] is True
        assert result["truncated"] is True
        assert len(result["content"]) <= 8000 + 100  # truncation suffix is small

    def test_fetch_url_returns_error_on_failure(self):
        """fetch_url returns ok=false on failure."""
        with patch("urllib.request.urlopen", side_effect=Exception("Connection error")):
            result = rt.fetch_url("https://example.com")

        assert result["ok"] is False
        assert "error" in result
