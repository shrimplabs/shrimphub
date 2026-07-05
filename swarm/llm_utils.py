"""LLM utilities and tool parsing extracted from agent_runtime.py."""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse
from swarm.provider_utils import LLM_PROVIDERS as _STATIC_LLM_PROVIDERS


def _is_loopback_base_url(base_url: str) -> bool:
    """Return true for localhost-style provider URLs."""
    try:
        host = urlparse(base_url).hostname or ""
    except Exception:
        return False
    return host.lower() in {"localhost", "127.0.0.1", "::1"}

class MCPClient:
    """MCP client for connecting to external MCP servers (godot-mcp, etc.)."""

    def __init__(self, servers: dict, cwd=None):
        self.servers = servers
        self.cwd = cwd
        self.processes: dict = {}
        self.request_id: int = 0
        self._initialized: set = set()

    def start_server(self, name: str) -> bool:
        if name not in self.servers:
            return False
        if name in self.processes:
            return True
        config = self.servers[name]
        cmd = [config.get("command", "")] + config.get("args", [])
        env = dict(os.environ)
        env.update(config.get("env", {}))
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=self.cwd,
            )
            self.processes[name] = proc
            self.request_id += 1
            req = {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "swarm-agent", "version": "1.0"},
                },
            }
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
            proc.stdout.readline()  # consume init response
            self._initialized.add(name)
            return True
        except Exception:
            return False

    def _send_request(self, server: str, method: str, params=None) -> dict:
        if server not in self.processes:
            return {"error": "Server not running"}
        proc = self.processes[server]
        self.request_id += 1
        req = {"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params or {}}
        try:
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
            resp_str = proc.stdout.readline()
            if not resp_str:
                return {"error": "No response"}
            resp = json.loads(resp_str)
            if "error" in resp:
                return {"error": resp["error"]}
            return resp.get("result", {})
        except Exception as e:
            return {"error": str(e)}

    def list_tools(self, server: str) -> list:
        if server not in self.processes and not self.start_server(server):
            return []
        result = self._send_request(server, "tools/list")
        return result.get("tools", []) if isinstance(result, dict) else []

    def call_tool(self, server: str, tool: str, args=None) -> dict:
        if server not in self.processes and not self.start_server(server):
            return {"error": f"Failed to start {server}"}
        result = self._send_request(server, "tools/call", {"name": tool, "arguments": args or {}})
        if isinstance(result, dict) and "content" in result:
            content = result["content"]
            if content and isinstance(content, list) and content[0].get("type") == "text":
                text = content[0].get("text", "")
                try:
                    return json.loads(text)
                except Exception:
                    return {"ok": True, "text": text}
        return result

    def stop_all(self):
        for name in list(self.processes.keys()):
            try:
                self.processes[name].terminate()
            except Exception:
                pass
            del self.processes[name]




def _try_parse_multi(block: str) -> list:
    """Parse a block containing multiple concatenated JSON objects.

    Handles the case where the model writes several tool call objects inside a
    single [TOOL_CALL]...[/TOOL_CALL] block, e.g.:
        {"tool": "read_file", "args": {...}}
        {"tool": "run_command", "args": {...}}

    Uses a simple brace-depth scanner to find each top-level object boundary.
    Returns a list of successfully parsed dicts (may be empty).
    """
    results = []
    depth = 0
    start = None
    for i, ch in enumerate(block):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                fragment = block[start:i + 1]
                try:
                    obj = json.loads(fragment)
                    if isinstance(obj, dict) and "tool" in obj:
                        results.append(obj)
                except json.JSONDecodeError:
                    pass
                start = None
    return results


def parse_tool_calls(text: str) -> list:
    # MiniMax-M3 occasionally returns a valid JSON body and the literal closing
    # tag ``[/TOOL_CALL`` while omitting only its final ``]``. Repair only this
    # exact end-of-response suffix; genuinely partial calls remain unparseable.
    if text.rstrip().endswith("[/TOOL_CALL"):
        text = text.rstrip() + "]"
    tool_calls = []

    def _try_parse(json_str):
        # Sanitize common MiniMax formatting quirks before parsing
        import re as _re2
        # Replace Ruby/PHP-style hash rockets: "key" => "value" → "key": "value"
        sanitized = _re2.sub(r'"(\w+)"\s*=>\s*', r'"\1": ', json_str)
        # Also handle unquoted key => value: key => "value" → "key": "value"
        sanitized = _re2.sub(r'(\w+)\s*=>\s*', r'"\1": ', sanitized)
        # Replace CLI-style flag args: --key "value" → "key": "value"
        # e.g. {"tool": "x", "args": {\n--command "echo hi"\n}}
        sanitized = _re2.sub(r'--(\w+)\s+"([^"]*)"', r'"\1": "\2"', sanitized)
        sanitized = _re2.sub(r"--(\w+)\s+'([^']*)'", r'"\1": "\2"', sanitized)
        # Handle --key [...] (JSON array value, keep as-is)
        sanitized = _re2.sub(r'--(\w+)\s+(\[.*?\])', r'"\1": \2', sanitized)
        # Handle --key {...} (JSON object value, keep as-is)
        sanitized = _re2.sub(r'--(\w+)\s+(\{.*?\})', r'"\1": \2', sanitized)
        # Handle --key number → "key": number
        sanitized = _re2.sub(r'--(\w+)\s+(\d+(?:\.\d+)?)\b', r'"\1": \2', sanitized)
        # Handle --key true/false/null
        sanitized = _re2.sub(r'--(\w+)\s+(true|false|null)\b', r'"\1": \2', sanitized)
        # Handle remaining --key value (unquoted string) → "key": "value"
        sanitized = _re2.sub(r'--(\w+)\s+(\S+)', r'"\1": "\2"', sanitized)
        # Fix missing commas between "key": value pairs on adjacent lines
        sanitized = _re2.sub(r'(":\s*(?:"[^"]*"|\d+(?:\.\d+)?|true|false|null|\]|\}))\s*\n(\s*")', r'\1,\n\2', sanitized)
        # Remove trailing commas before closing braces/brackets
        sanitized = _re2.sub(r',(\s*[}\]])', r'\1', sanitized)
        for candidate in ([sanitized, json_str] if sanitized != json_str else [json_str]):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                for extra in ["}", "}}", "}}}"]:
                    try:
                        return json.loads(candidate + extra)
                    except json.JSONDecodeError:
                        pass
        return None

    # [TOOL_CALL]...[/TOOL_CALL]
    pos = 0
    while True:
        start = text.find("[TOOL_CALL]", pos)
        if start == -1:
            break
        start += len("[TOOL_CALL]")
        end = text.find("[/TOOL_CALL]", start)
        if end == -1:
            # Fallback: model used [TOOL_CALL]...[TOOL_CALL] (wrong closing tag).
            # Treat the next opening tag as the boundary so it still parses.
            end = text.find("[TOOL_CALL]", start)
            if end == -1:
                break
            parsed = _try_parse(text[start:end].strip())
            if parsed:
                tool_calls.append(parsed)
            pos = end + len("[TOOL_CALL]")  # skip past the malformed closing tag
            continue
        block = text[start:end].strip()
        parsed = _try_parse(block)
        if parsed:
            tool_calls.append(parsed)
        else:
            # Model may have written multiple JSON objects in one block (NDJSON style).
            # Try splitting on object boundaries and parsing each one individually.
            multi = _try_parse_multi(block)
            tool_calls.extend(multi)
        pos = end + len("[/TOOL_CALL]")

    # <tool_call>...</tool_call>  OR  <minimax:tool_call>...</minimax:tool_call>
    pos = 0
    for open_tag, close_tag in [("<tool_call>", "</tool_call>"), ("<minimax:tool_call>", "</minimax:tool_call>")]:
        pos = 0
        while True:
            start = text.find(open_tag, pos)
            if start == -1:
                break
            start += len(open_tag)
            end = text.find(close_tag, start)
            if end == -1:
                break
            block = text[start:end].strip()
            # Try JSON parse first
            parsed = _try_parse(block)
            if parsed:
                tool_calls.append(parsed)
            else:
                # MiniMax native format: funcName(key="val", ...) lines separated by "- "
                import re as _re_mm
                for line in _re_mm.split(r'\n-\s+|\n', block):
                    line = line.strip().lstrip('- ').strip()
                    if not line:
                        continue
                    m = _re_mm.match(r'(\w+)\((.*)\)$', line, _re_mm.DOTALL)
                    if not m:
                        continue
                    tool_name = m.group(1)
                    args_str = m.group(2).strip()
                    args = {}
                    # Parse key=value or key="value" pairs
                    for kv in _re_mm.finditer(r'(\w+)\s*=\s*("(?:[^"\\]|\\.)*"|\S+)', args_str):
                        k, v = kv.group(1), kv.group(2)
                        try:
                            args[k] = json.loads(v)
                        except Exception:
                            args[k] = v.strip('"')
                    tool_calls.append({"tool": tool_name, "args": args})
            pos = end + len(close_tag)

    # <tool name="...">...</tool> (Minimax XML fallback)
    import re as _re
    for m in _re.finditer(r'<tool\s+name=["\']([^"\']+)["\'][^>]*>(.*?)</tool>', text, _re.DOTALL):
        tool_name = m.group(1).strip()
        inner = m.group(2).strip()
        # inner may be JSON args or empty
        if inner:
            parsed = _try_parse(inner)
            args = parsed if isinstance(parsed, dict) else {}
        else:
            args = {}
        tool_calls.append({"tool": tool_name, "args": args})

    # <function_calls><invoke name="..."><arg name="...">...</arg></invoke></function_calls> (Kimi format)
    for invoke_m in _re.finditer(r'<invoke\s+name=["\']([^"\']+)["\'][^>]*>(.*?)</invoke>', text, _re.DOTALL):
        tool_name = invoke_m.group(1).strip()
        inner = invoke_m.group(2).strip()
        args = {}
        # Match <parameter name="key">value</parameter> or <arg name="key">value</arg>
        for arg_m in _re.finditer(r'<(?:parameter|arg)\s+name=["\']([^"\']+)["\'][^>]*>(.*?)</(?:parameter|arg)>', inner, _re.DOTALL):
            args[arg_m.group(1).strip()] = arg_m.group(2).strip()
        # Also match bare tag names as argument names: <description>value</description>
        if not args:
            for arg_m in _re.finditer(r'<([a-zA-Z_][a-zA-Z0-9_]*)>(.*?)</\1>', inner, _re.DOTALL):
                args[arg_m.group(1).strip()] = arg_m.group(2).strip()
        # Also try JSON body inside invoke
        if not args and inner:
            parsed = _try_parse(inner)
            if isinstance(parsed, dict):
                args = parsed
        tool_calls.append({"tool": tool_name, "args": args})

    return tool_calls




def _record_rate_limit_event(provider_name: str):
    """Append a timestamp to data/rl_events.jsonl so the monitor can detect pressure."""
    try:
        flag_dir = Path(__file__).parent.parent / "data"
        flag_dir.mkdir(exist_ok=True)
        import json as _j
        with open(flag_dir / "rl_events.jsonl", "a") as _f:
            _f.write(_j.dumps({"t": time.time(), "provider": provider_name}) + "\n")
    except Exception:
        pass


def _quota_sleep_if_needed(log_fn=None):
    """Check MiniMax quota via the local API and poll-sleep until quota clears.

    Reads QUOTA_SLEEP_THRESHOLD env var (default 88) -- slightly below the
    swarm's quota_limit_percent (90) so agents pause before the orchestrator
    suspends spawning. Polls every 60s so agents wake up immediately when the
    interval resets rather than sleeping for the full remaining window.
    """
    threshold = float(os.environ.get("QUOTA_SLEEP_THRESHOLD", "88"))
    api_port = os.environ.get("API_PORT", "5001")
    poll_interval = 60  # re-check quota every 60s
    logged = False
    while True:
        try:
            import urllib.request as _ur
            with _ur.urlopen(f"http://localhost:{api_port}/api/quota", timeout=5) as resp:
                data = json.loads(resp.read())
            over = False
            for m in data.get("model_remains", []):
                if m.get("model_name") != "MiniMax-M*":
                    continue
                total = m.get("current_interval_total_count", 0)
                remaining = m.get("current_interval_usage_count", 0)
                used = total - remaining
                if total <= 0:
                    break
                pct = 100.0 * used / total
                if pct >= threshold:
                    over = True
                    if log_fn and not logged:
                        log_fn(f"[LLM] Quota at {pct:.1f}% (threshold {threshold}%) -- polling every {poll_interval}s until interval resets")
                        logged = True
                break
            if not over:
                return  # quota cleared, proceed with LLM call
            time.sleep(poll_interval)
        except Exception:
            return  # Don't block LLM calls if quota check fails


def call_llm(sys_prompt: str, messages: list, provider: str | None = None):
    """Call the configured LLM provider. Returns (text: str, tokens: dict).

    provider: override the active provider for this call (e.g. "claude" for
    the meta-investigator).  Falls back to LLM_PROVIDER if None or unknown.
    """
    try:
        import requests
    except ImportError:
        return "requests library not installed", {"input": 0, "output": 0}

    # Resolve provider config (fall back to minimax defaults if unknown provider)
    # Lazy import to avoid circular dep. Use agent_runtime.LLM_PROVIDERS (baked dict
    # from wrapper, contains custom providers like local-qwen27b) in preference to the
    # static LLM_PROVIDERS from provider_utils which only has built-in providers.
    from swarm.agent_runtime import LLM_PROVIDER, log
    try:
        from swarm.agent_runtime import LLM_PROVIDERS as _rt_providers
    except ImportError:
        _rt_providers = {}
    _providers = _rt_providers if _rt_providers else _STATIC_LLM_PROVIDERS
    provider_name = provider or LLM_PROVIDER or "minimax"
    cfg = dict(_providers.get(provider_name, _providers.get("minimax", _STATIC_LLM_PROVIDERS.get("minimax", {}))))

    # Allow per-provider overrides in the env
    api_key_env = cfg.get("api_key_env", "MINIMAX_API_KEY")
    api_key = os.environ.get(api_key_env, "")
    # Also try legacy MINIMAX-API alias
    if not api_key and provider_name == "minimax":
        api_key = os.environ.get("MINIMAX-API", "")
    # Direct api_key in provider config (for custom providers registered without env var)
    if not api_key:
        api_key = cfg.get("api_key", "")

    base_url = cfg.get("base_url", "https://api.minimax.io/anthropic/v1").rstrip("/")

    is_loopback_provider = _is_loopback_base_url(base_url)

    # Local providers don't need a real API key -- use a placeholder.
    if not api_key and is_loopback_provider:
        api_key = "local"
    elif not api_key:
        return f"{api_key_env} not set (provider: {provider_name})", {"input": 0, "output": 0}, []
    model     = cfg.get("model", "MiniMax-M3")
    fmt       = cfg.get("format", "anthropic")
    max_tok   = cfg.get("max_tokens", 8096)

    # Build request based on wire format
    if fmt == "anthropic_native":
        # Direct Anthropic API
        url = f"{base_url}/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body = {"model": model, "max_tokens": max_tok, "system": sys_prompt, "messages": messages}

    elif fmt == "openai":
        # OpenAI-compatible (OpenRouter, local models, etc.)
        url = f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        oai_msgs = [{"role": "system", "content": sys_prompt}] + [
            {"role": m["role"], "content": m["content"] if isinstance(m["content"], str)
             else "".join(p.get("text", "") for p in m["content"] if p.get("type") == "text")}
            for m in messages
        ]
        body = {"model": model, "max_tokens": max_tok, "messages": oai_msgs}

    else:
        # Anthropic-compatible Bearer (Minimax, OpenRouter anthropic mode, etc.)
        url = f"{base_url}/messages"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        # Use structured system prompt with cache_control so MiniMax caches the full
        # system prompt (not just the small auto-cached header). This dramatically
        # reduces input token cost on long-running agents that call the same system
        # prompt repeatedly across many tool loops.
        body = {
            "model": model,
            "max_tokens": max_tok,
            "system": [{"type": "text", "text": sys_prompt, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        }
        # Minimax group_id extra param
        group_id = os.environ.get("MINIMAX_GROUP_ID", "")
        if group_id and provider_name == "minimax":
            body["group_id"] = group_id
        # Extended thinking
        thinking_budget = cfg.get("thinking_budget", 0)
        if thinking_budget > 0:
            body["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}

    # Inject routing hint headers when pointing at shrimp-router.
    # Controlled by SHRIMP_ROUTER_HINTS env var (default "1", set "0" to disable).
    # The router uses these to escalate to stronger models on struggling agents.
    if os.environ.get("SHRIMP_ROUTER_HINTS", "1") != "0" and is_loopback_provider:
        try:
            import swarm.agent_runtime as _rt
            headers["X-Task-Type"] = getattr(_rt, "TASK_TYPE", "")
            headers["X-Loop-Count"] = str(getattr(_rt, "_ROUTING_LOOP", 0))
            headers["X-Has-Commits"] = "true" if getattr(_rt, "_ROUTING_COMMITS", 0) > 0 else "false"
            headers["X-Phase"] = getattr(_rt, "_ROUTING_PHASE", "")
            log.info(f"[routing] X-Phase={headers['X-Phase']} X-Task-Type={headers.get('X-Task-Type')} provider={provider_name} base_url={base_url}")
        except Exception:
            pass

    # Quota-aware pre-call sleep: if MiniMax quota is near the cap, sleep until
    # the interval resets rather than burning retries on 429s.
    if provider_name == "minimax":
        _quota_sleep_if_needed(log)

    # Jitter: spread requests across the minute window to avoid RPM burst spikes.
    import random as _random
    _jitter_base = float(os.environ.get("LLM_JITTER_MIN", "1.0"))
    _jitter_max  = float(os.environ.get("LLM_JITTER_MAX", "6.0"))
    time.sleep(_random.uniform(_jitter_base, _jitter_max))

    _routing_phase = ""
    if is_loopback_provider:
        try:
            import swarm.agent_runtime as _rt2
            _routing_phase = getattr(_rt2, "_ROUTING_PHASE", "")
        except Exception:
            pass
    if _routing_phase:
        log(f"[LLM] provider={provider_name} model={model} X-Phase={_routing_phase}")
    else:
        log(f"[LLM] provider={provider_name} model={model}")

    def _parse_sse_stream(resp):
        """Parse an Anthropic SSE stream, returning (text, tokens, thinking_blocks).

        thinking_blocks is a list of {"type": "thinking", "thinking": ..., "signature": ...}
        dicts -- one per thinking block in the response. Empty list if no thinking occurred.
        Callers that want to preserve reasoning across turns should include these in the
        next assistant message content alongside the text block.
        """
        import json as _json
        text_parts: list[str] = []
        # Track per-block thinking state: index → {parts, signature}
        _thinking_blocks: dict[int, dict] = {}
        _current_index: int = -1
        tokens = {"input": 0, "output": 0}
        cache_read = cache_write = 0
        stream_complete = False
        _routed_model_logged = False

        try:
          line_iter = iter(resp.iter_lines(chunk_size=8192))
        except Exception as _ie:
          return f"Error: stream init failed: {_ie}", {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}, []

        while True:
            try:
                raw_line = next(line_iter)
            except StopIteration:
                break
            except Exception as _se:
                # Connection dropped mid-stream — treat as truncation, will retry
                log(f"Stream read error: {_se}")
                return f"Error: stream interrupted: {_se}", {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}, []

            if not raw_line:
                continue
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode("utf-8", errors="replace")
            if not raw_line.startswith("data: "):
                continue
            data_str = raw_line[6:]
            if data_str == "[DONE]":
                stream_complete = True
                break
            try:
                ev = _json.loads(data_str)
            except Exception:
                continue
            ev_type = ev.get("type", "")

            # OpenAI streaming chunk (from shrimp-router translating opencode backends)
            if "choices" in ev and ev_type in ("", "chat.completion.chunk", None):
                _resp_model = ev.get("model", "")
                if _resp_model and _resp_model != model and not _routed_model_logged:
                    log(f"[LLM] routed to model={_resp_model}")
                    _routed_model_logged = True
                for choice in ev.get("choices", []):
                    delta = choice.get("delta", {})
                    chunk_text = delta.get("content") or delta.get("reasoning_content") or ""
                    if chunk_text:
                        text_parts.append(chunk_text)
                _usage = ev.get("usage") or {}
                if _usage:
                    tokens["input"] = _usage.get("prompt_tokens", tokens.get("input", 0))
                    tokens["output"] = _usage.get("completion_tokens", tokens.get("output", 0))
                    if ev.get("choices") and ev["choices"][-1].get("finish_reason"):
                        stream_complete = True
                continue

            if ev_type == "message_start":
                _msg_start = ev.get("message", {})
                usage = _msg_start.get("usage", {})
                tokens["input"] = usage.get("input_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)
                cache_write = usage.get("cache_creation_input_tokens", 0)
                _resp_model = _msg_start.get("model", "")
                if _resp_model and _resp_model != model and not _routed_model_logged:
                    log(f"[LLM] routed to model={_resp_model}")
                    _routed_model_logged = True

            elif ev_type == "content_block_start":
                idx = ev.get("index", -1)
                block = ev.get("content_block", {})
                if block.get("type") == "thinking":
                    _thinking_blocks[idx] = {"parts": [], "signature": ""}
                    _current_index = idx

            elif ev_type == "content_block_delta":
                delta = ev.get("delta", {})
                dtype = delta.get("type", "")
                idx = ev.get("index", _current_index)
                if dtype == "text_delta":
                    text_parts.append(delta.get("text", ""))
                elif dtype == "thinking_delta":
                    if idx in _thinking_blocks:
                        _thinking_blocks[idx]["parts"].append(delta.get("thinking", ""))
                elif dtype == "signature_delta":
                    if idx in _thinking_blocks:
                        _thinking_blocks[idx]["signature"] = delta.get("signature", "")

            elif ev_type == "message_delta":
                usage = ev.get("usage", {})
                tokens["output"] = usage.get("output_tokens", 0)

            elif ev_type == "message_stop":
                stream_complete = True

        # If we got no Anthropic events but have text parts, it was an OpenAI stream
        # that happened to use data: lines -- that's handled below.
        # But if we got nothing at all, try to detect OpenAI streaming format by
        # checking if text_parts came from OpenAI chunks (handled in loop above via
        # the choices[].delta.content path which we add here):
        # stream was cut off before message_stop arrived.  Returning partial content
        # in this case is dangerous -- the caller may believe the task is done when
        # in fact the model was mid-sentence.  We must report an error instead.
        if not stream_complete:
            return "Error: stream truncated before completion", {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}, []
        if cache_read or cache_write:
            log(f"[LLM] cache read={cache_read} write={cache_write}")
        tokens["cache_read"] = cache_read
        tokens["cache_write"] = cache_write

        thinking_blocks = []
        for idx in sorted(_thinking_blocks):
            tb = _thinking_blocks[idx]
            thinking = "".join(tb["parts"])
            if thinking:
                snippet = thinking[:200].replace("\n", " ")
                log(f"[LLM] thinking: {snippet}{'...' if len(thinking) > 200 else ''}")
                thinking_blocks.append({
                    "type": "thinking",
                    "thinking": thinking,
                    "signature": tb["signature"],
                })

        return "".join(text_parts), tokens, thinking_blocks

    def _parse_response(result: dict):
        """Parse a non-streaming response (OpenAI format fallback)."""
        usage = result.get("usage", {})
        cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
        tokens = {
            "input": usage.get("prompt_tokens", 0),
            "output": usage.get("completion_tokens", 0),
            "cache_read": cached,
            "cache_write": 0,
        }
        if cached:
            log(f"[LLM] cache read={cached}")
        resp_model = result.get("model", "")
        if resp_model and resp_model != model:
            log(f"[LLM] routed to model={resp_model}")
        _msg = result.get("choices", [{}])[0].get("message", {})
        text = _msg.get("content") or _msg.get("reasoning_content") or _msg.get("reasoning") or "No response"
        return text, tokens, []

    # Use streaming for anthropic formats; non-streaming for openai
    use_stream = fmt in ("anthropic", "anthropic_native")
    if use_stream:
        body["stream"] = True

    backoff = [10, 30, 60, 120, 240]
    for attempt in range(7):
        try:
            if use_stream:
                resp = requests.post(url, headers=headers, json=body, stream=True, timeout=(10, 300))
                if resp.status_code == 200:
                    text, tokens, thinking = _parse_sse_stream(resp)
                    if isinstance(text, str) and text == "Error: stream truncated before completion":
                        # Connection dropped mid-response; retrying won't help — the model
                        # already sent a partial response and won't resume it.
                        return text, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}, []
                    if isinstance(text, str) and text.startswith("Error: stream"):
                        wait = backoff[min(attempt, len(backoff) - 1)]
                        log(f"Stream error — retrying in {wait}s (attempt {attempt+1}/7): {text[:80]}")
                        time.sleep(wait)
                        continue
                    return text, tokens, thinking
                elif resp.status_code in (429, 529):
                    wait = backoff[min(attempt, len(backoff) - 1)]
                    log(f"Rate limited ({resp.status_code}) -- waiting {wait}s (attempt {attempt+1}/7)")
                    _record_rate_limit_event(provider_name)
                    time.sleep(wait)
                elif resp.status_code in (502, 503, 504):
                    # Proxy/gateway error — backends temporarily down, always retry
                    wait = backoff[min(attempt, len(backoff) - 1)]
                    try:
                        body_text = resp.text[:200]
                    except Exception:
                        body_text = "<unreadable stream>"
                    log(f"Gateway error {resp.status_code} (stream): {body_text} -- retrying in {wait}s (attempt {attempt+1}/7)")
                    time.sleep(wait)
                else:
                    try:
                        body_text = resp.text[:500]
                    except Exception:
                        body_text = "<unreadable stream>"
                    log(f"API error {resp.status_code} (stream): {body_text}")
                    if resp.status_code == 500 and ("999" in body_text or "unknown error" in body_text.lower()):
                        wait = backoff[min(attempt, len(backoff) - 1)]
                        log(f"MiniMax 999/1000 error -- retrying in {wait}s (attempt {attempt+1}/7)")
                        time.sleep(wait)
                        continue
                    return f"API error {resp.status_code}: {body_text}", {"input": 0, "output": 0}, []
            else:
                resp = requests.post(url, headers=headers, json=body, timeout=300)
                if resp.status_code == 200:
                    return _parse_response(resp.json())
                elif resp.status_code in (429, 529):
                    wait = backoff[min(attempt, len(backoff) - 1)]
                    log(f"Rate limited ({resp.status_code}) -- waiting {wait}s (attempt {attempt+1}/7)")
                    _record_rate_limit_event(provider_name)
                    time.sleep(wait)
                elif resp.status_code in (502, 503, 504):
                    wait = backoff[min(attempt, len(backoff) - 1)]
                    log(f"Gateway error {resp.status_code}: {resp.text[:200]} -- retrying in {wait}s (attempt {attempt+1}/7)")
                    time.sleep(wait)
                else:
                    body_text = resp.text[:500]
                    log(f"API error {resp.status_code}: {body_text}")
                    if resp.status_code == 500 and ("999" in body_text or "unknown error" in body_text.lower()):
                        wait = backoff[min(attempt, len(backoff) - 1)]
                        log(f"MiniMax 999/1000 error -- retrying in {wait}s (attempt {attempt+1}/7)")
                        time.sleep(wait)
                        continue
                    return f"API error {resp.status_code}: {body_text}", {"input": 0, "output": 0}, []
        except Exception as e:
            wait = backoff[min(attempt, len(backoff) - 1)]
            if attempt < 6:
                log(f"API error ({e}) -- retrying in {wait}s (attempt {attempt+1}/7)")
                time.sleep(wait)
            else:
                return f"Error: {e}", {"input": 0, "output": 0}, []

    # All retries exhausted on 429 -- signal orchestrator to rotate provider / cooldown spawn
    try:
        flag_dir = Path(__file__).parent.parent / "data"
        flag_dir.mkdir(exist_ok=True)
        (flag_dir / f"rate_limited_{provider_name}.flag").touch()
        log(f"Rate limit exhausted for {provider_name} -- wrote flag for provider rotation")
    except Exception:
        pass
    return f"Error: {provider_name} rate limited after 7 attempts", {"input": 0, "output": 0}, []


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------
