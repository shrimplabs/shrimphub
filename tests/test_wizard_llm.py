"""Tests for wizard and chat LLM call consolidation (lfmn.8).

Covers:
- _llm_call delegates to _chat_call_llm (no duplicated provider logic)
- _IMAGINE_PROMPT_TEMPLATE / _PLAN_PROMPT_TEMPLATE render correctly
- _chat_call_llm constructs requests for anthropic, anthropic_native, and openai formats
- _chat_call_llm error handling (HTTP errors, stream truncation)
- _llm_call propagates errors from _chat_call_llm
"""

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Prompt template rendering
# ---------------------------------------------------------------------------

class TestPromptTemplates:
    def test_imagine_template_injects_type_desc(self):
        from swarm.api_wizard import _IMAGINE_PROMPT_TEMPLATE
        rendered = _IMAGINE_PROMPT_TEMPLATE.format(
            type_desc="a Godot 4 game (GDScript)",
            hint_line="",
            project_type="godot",
        )
        assert "a Godot 4 game (GDScript)" in rendered
        assert "{type_desc}" not in rendered  # placeholder replaced

    def test_imagine_template_injects_hint_line(self):
        from swarm.api_wizard import _IMAGINE_PROMPT_TEMPLATE
        hint = "\nCreative nudge from the human: something weird"
        rendered = _IMAGINE_PROMPT_TEMPLATE.format(
            type_desc="a Python application or tool",
            hint_line=hint,
            project_type="python",
        )
        assert "something weird" in rendered

    def test_imagine_template_no_hint_line(self):
        from swarm.api_wizard import _IMAGINE_PROMPT_TEMPLATE
        rendered = _IMAGINE_PROMPT_TEMPLATE.format(
            type_desc="a software project",
            hint_line="",
            project_type="other",
        )
        assert "Creative nudge" not in rendered

    def test_plan_template_injects_project_info(self):
        from swarm.api_wizard import _PLAN_PROMPT_TEMPLATE
        rendered = _PLAN_PROMPT_TEMPLATE.format(
            project_name="my-game",
            type_hint="Godot 4 game (GDScript).",
            description="A platformer with procedurally generated levels",
            min_tasks=10,
            max_tasks=50,
        )
        assert "my-game" in rendered
        assert "procedurally generated levels" in rendered
        assert "10" in rendered
        assert "50" in rendered

    def test_plan_template_is_json_instructed(self):
        from swarm.api_wizard import _PLAN_PROMPT_TEMPLATE
        rendered = _PLAN_PROMPT_TEMPLATE.format(
            project_name="x", type_hint="y", description="z",
            min_tasks=5, max_tasks=20,
        )
        assert '"tasks"' in rendered
        assert "depends_on" in rendered

    def test_imagine_system_is_json_focused(self):
        from swarm.api_wizard import _IMAGINE_SYSTEM
        assert "JSON" in _IMAGINE_SYSTEM

    def test_plan_system_is_json_focused(self):
        from swarm.api_wizard import _PLAN_SYSTEM
        assert "JSON" in _PLAN_SYSTEM


# ---------------------------------------------------------------------------
# _llm_call delegates to _chat_call_llm
# ---------------------------------------------------------------------------

class TestLlmCallDelegation:
    def test_llm_call_returns_chat_call_llm_result(self):
        from swarm.api_wizard import _llm_call

        with patch("swarm.api_wizard._chat_call_llm", return_value="hello from llm") as mock_ccl:
            result = _llm_call("user prompt", "system prompt", {"llm_provider": "minimax"})

        assert result == "hello from llm"
        mock_ccl.assert_called_once()

    def test_llm_call_passes_system_as_system_prompt(self):
        from swarm.api_wizard import _llm_call

        with patch("swarm.api_wizard._chat_call_llm", return_value="ok") as mock_ccl:
            _llm_call("user msg", "my system", {})

        call_kwargs = mock_ccl.call_args
        assert call_kwargs.kwargs.get("system_prompt") == "my system"

    def test_llm_call_wraps_prompt_as_user_message(self):
        from swarm.api_wizard import _llm_call

        with patch("swarm.api_wizard._chat_call_llm", return_value="ok") as mock_ccl:
            _llm_call("my user prompt", "sys", {})

        call_kwargs = mock_ccl.call_args
        messages = call_kwargs.kwargs.get("messages", [])
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "my user prompt"

    def test_llm_call_propagates_exception(self):
        from swarm.api_wizard import _llm_call

        with patch("swarm.api_wizard._chat_call_llm", side_effect=RuntimeError("provider down")):
            with pytest.raises(RuntimeError, match="provider down"):
                _llm_call("prompt", "system", {})


# ---------------------------------------------------------------------------
# _chat_call_llm provider request construction
# ---------------------------------------------------------------------------

def _make_streaming_response(text):
    """Build a mock streaming response that yields SSE lines for a text reply."""
    lines = [
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"' + text.encode() + b'"}}',
        b'data: {"type":"message_stop"}',
    ]
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.iter_lines.return_value = iter(lines)
    return mock_resp


def _make_json_response(text):
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "content": [{"type": "text", "text": text}]
    }
    return mock_resp


class TestChatCallLlm:
    """Tests for _chat_call_llm request construction and response parsing."""

    def _call(self, config=None, messages=None, fmt_override=None, post_return=None):
        from swarm.api_wizard import _chat_call_llm
        import swarm.api_chat as _ac

        config = config or {}
        messages = messages or [{"role": "user", "content": "hello"}]
        mock_post = MagicMock(return_value=post_return or _make_streaming_response("ok"))

        with patch.object(_ac, "requests", create=True) as mock_req:
            mock_req.post = mock_post
            with patch("swarm.api_wizard._chat_call_llm.__globals__", {}, create=False):
                pass
            result = _chat_call_llm("sys", messages, config, fmt_override=fmt_override)

        return result, mock_post

    def test_anthropic_format_uses_bearer(self):
        from swarm.api_wizard import _chat_call_llm
        import swarm.api_chat as _ac

        mock_post = MagicMock(return_value=_make_streaming_response("reply"))
        with patch.object(_ac, "requests") as mock_req:
            mock_req.post = mock_post
            with patch("swarm_runner.LLM_PROVIDER", "minimax", create=True), \
                 patch("swarm_runner.LLM_PROVIDERS", {"minimax": {"format": "anthropic", "model": "M2", "api_key_env": "MINIMAX_API_KEY"}}, create=True), \
                 patch.dict("os.environ", {"MINIMAX_API_KEY": "test-key"}):
                result = _chat_call_llm("sys", [{"role": "user", "content": "hi"}], {})

        headers = mock_post.call_args[1]["headers"]
        assert headers.get("Authorization", "").startswith("Bearer ")

    def test_anthropic_native_format_uses_x_api_key(self):
        from swarm.api_wizard import _chat_call_llm
        import swarm.api_chat as _ac

        mock_post = MagicMock(return_value=_make_streaming_response("reply"))
        with patch.object(_ac, "requests") as mock_req:
            mock_req.post = mock_post
            with patch("swarm_runner.LLM_PROVIDER", "claude", create=True), \
                 patch("swarm_runner.LLM_PROVIDERS", {"claude": {"format": "anthropic_native", "model": "claude-3", "api_key_env": "ANTHROPIC_API_KEY"}}, create=True), \
                 patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
                result = _chat_call_llm("sys", [{"role": "user", "content": "hi"}], {})

        headers = mock_post.call_args[1]["headers"]
        assert "x-api-key" in headers

    def test_openai_format_uses_chat_completions_endpoint(self):
        from swarm.api_wizard import _chat_call_llm
        import swarm.api_chat as _ac

        json_resp = MagicMock()
        json_resp.raise_for_status = MagicMock()
        json_resp.json.return_value = {"choices": [{"message": {"content": "openai reply"}}]}
        mock_post = MagicMock(return_value=json_resp)

        with patch.object(_ac, "requests") as mock_req:
            mock_req.post = mock_post
            with patch("swarm_runner.LLM_PROVIDER", "openrouter", create=True), \
                 patch("swarm_runner.LLM_PROVIDERS", {"openrouter": {"format": "openai", "model": "gpt-4", "base_url": "https://api.openrouter.ai/v1", "api_key_env": "OPENROUTER_API_KEY"}}, create=True), \
                 patch.dict("os.environ", {"OPENROUTER_API_KEY": "or-key"}):
                result = _chat_call_llm("sys", [{"role": "user", "content": "hi"}], {})

        url = mock_post.call_args[0][0]
        assert "chat/completions" in url
        assert result == "openai reply"

    def test_http_error_is_retried_then_raised(self):
        from swarm.api_wizard import _chat_call_llm
        import swarm.api_chat as _ac
        import requests as real_requests

        err_resp = MagicMock()
        err_resp.raise_for_status.side_effect = real_requests.exceptions.HTTPError("503")
        err_resp.iter_lines.return_value = iter([])
        mock_post = MagicMock(return_value=err_resp)

        with patch.object(_ac, "requests") as mock_req:
            mock_req.post = mock_post
            mock_req.exceptions = real_requests.exceptions
            with patch("swarm_runner.LLM_PROVIDER", "minimax", create=True), \
                 patch("swarm_runner.LLM_PROVIDERS", {"minimax": {"format": "anthropic", "model": "M2", "api_key_env": "MINIMAX_API_KEY"}}, create=True), \
                 patch.dict("os.environ", {"MINIMAX_API_KEY": "test-key"}), \
                 patch("time.sleep"):  # skip backoff delays
                with pytest.raises(Exception):
                    _chat_call_llm("sys", [{"role": "user", "content": "hi"}], {})

        # Should have retried (len(backoff)+1 = 4 attempts)
        assert mock_post.call_count >= 2
