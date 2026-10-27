"""
Vision provider dispatcher for QA agents.

Supports:
  anthropic_native — Claude (base64 image in message content)
  openai           — OpenAI-compatible (ollama, mlx-lm, local models)
  mcp              — Via MCP tool call (e.g. Minimax vision MCP)

Config keys expected in the swarm config:
  vision_provider        — provider name for "powerful" model (default: "claude")
  vision_provider_fast   — provider name for "fast" model (default: same as vision_provider)
  vision_providers       — dict of provider configs

Example config:
  {
    "vision_provider": "claude",
    "vision_provider_fast": "local",
    "vision_providers": {
      "claude": {
        "format": "anthropic_native",
        "model": "claude-sonnet-4-6",
        "api_key_env": "ANTHROPIC_API_KEY",
        "base_url": "https://api.anthropic.com/v1"
      },
      "local": {
        "format": "openai",
        "model": "qwen2.5-vl:7b",
        "base_url": "http://localhost:11434/v1",
        "api_key_env": ""
      },
      "minimax-vision": {
        "format": "mcp",
        "mcp_server": "minimax-vision",
        "mcp_tool": "analyze_image"
      }
    }
  }
"""

import base64
import json
import os
import urllib.request
import urllib.error
from pathlib import Path


def _load_image_b64(image_path: str) -> tuple[str, str]:
    """Load an image file and return (base64_data, media_type).

    Raises OSError if the file is truncated or unreadable by PIL.
    """
    path = Path(image_path)
    data = path.read_bytes()

    # Verify the image is not truncated before sending to VLM
    try:
        from PIL import Image, ImageOps
        import io
        img = Image.open(io.BytesIO(data))
        img.load()  # forces full decode — raises OSError if truncated
    except ImportError:
        pass  # PIL not available, skip validation
    except OSError as e:
        raise OSError(f"Image file is truncated or corrupt: {image_path}: {e}") from e

    b64 = base64.standard_b64encode(data).decode()
    ext = path.suffix.lower()
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/png")
    return b64, media_type


def call_vision(image_path: str, prompt: str, provider_name: str, config: dict) -> str:
    """
    Send an image + prompt to a vision model and return the text response.

    Args:
        image_path: Path to the screenshot file
        prompt: Question or instruction for the vision model
        provider_name: Key into config["vision_providers"]
        config: Full swarm config dict

    Returns:
        Model response string
    """
    providers = config.get("vision_providers", {})
    provider = providers.get(provider_name, {})

    if not provider:
        # Fall back to claude if provider not configured
        provider = {
            "format": "anthropic_native",
            "model": "claude-sonnet-4-6",
            "api_key_env": "ANTHROPIC_API_KEY",
            "base_url": "https://api.anthropic.com/v1",
        }

    fmt = provider.get("format", "anthropic_native")

    if fmt in ("anthropic_native", "anthropic"):
        return _call_anthropic(image_path, prompt, provider)
    elif fmt == "openai":
        return _call_openai(image_path, prompt, provider)
    elif fmt == "mcp":
        return _call_mcp(image_path, prompt, provider)
    else:
        raise ValueError(f"Unknown vision provider format: {fmt!r}")


def _call_anthropic(image_path: str, prompt: str, provider: dict) -> str:
    """Call Claude via Anthropic native API with image."""
    b64, media_type = _load_image_b64(image_path)
    api_key = os.environ.get(provider.get("api_key_env", "ANTHROPIC_API_KEY"), "")
    if not api_key:
        raise RuntimeError(f"API key not set: {provider.get('api_key_env', 'ANTHROPIC_API_KEY')}")

    base_url = provider.get("base_url", "https://api.anthropic.com/v1").rstrip("/")
    model = provider.get("model", "claude-sonnet-4-6")
    max_tokens = provider.get("max_tokens", 1024)

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; SwarmController/1.0)",
    }
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    }).encode()

    req = urllib.request.Request(f"{base_url}/messages", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
        return result["content"][0]["text"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic vision error {e.code}: {detail}")


def _call_openai(image_path: str, prompt: str, provider: dict) -> str:
    """Call an OpenAI-compatible vision endpoint (ollama, mlx-lm, etc.)."""
    b64, media_type = _load_image_b64(image_path)
    api_key_env = provider.get("api_key_env", "")
    api_key = os.environ.get(api_key_env, "ollama") if api_key_env else "ollama"

    base_url = provider.get("base_url", "http://localhost:11434/v1").rstrip("/")
    model = provider.get("model", "qwen2.5-vl:7b")
    max_tokens = provider.get("max_tokens", 1024)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; SwarmController/1.0)",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "num_ctx": provider.get("num_ctx", 4096),  # cap context to avoid OOM/slowness
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{b64}"},
                },
                {"type": "text", "text": prompt},
            ],
        }],
    }
    # Forward thinking params if present (mlx-vlm thinking_budget support)
    for key in ("enable_thinking", "thinking_budget", "thinking_start_token", "thinking_end_token"):
        if key in provider:
            payload[key] = provider[key]
    body = json.dumps(payload).encode()

    req = urllib.request.Request(f"{base_url}/chat/completions", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read())
        text = result["choices"][0]["message"]["content"]
        # Strip <think>...</think> block if present (mlx-vlm thinking models)
        end_token = payload.get("thinking_end_token", "</think>")
        idx = text.find(end_token)
        if idx != -1:
            text = text[idx + len(end_token):].strip()
        return text
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI-compat vision error {e.code}: {detail}")


def _call_mcp(image_path: str, prompt: str, provider: dict) -> str:
    """Call a vision model via MCP tool (e.g. Minimax vision MCP)."""
    # MCP calls are handled via the MCPClient in agent_runtime.
    # This path is invoked from the QA tool handler which has access to the client.
    # Here we just format the args — the caller is responsible for executing.
    raise NotImplementedError(
        "MCP vision calls must be dispatched through the agent's MCPClient. "
        "Use the mcp_call_tool handler in agent_runtime instead."
    )


def call_vision_multi(image_paths: list, prompt: str, provider_name: str, config: dict) -> str:
    """Send multiple images + prompt to a vision model in a single call.

    Useful for before/after comparisons, burst frame analysis, or any query
    that needs the model to reason across several frames simultaneously.
    Falls back to single-image call_vision on the first image if multi not supported.
    """
    if not image_paths:
        raise ValueError("image_paths must be non-empty")
    if len(image_paths) == 1:
        return call_vision(image_paths[0], prompt, provider_name, config)

    providers = config.get("vision_providers", {})
    provider = providers.get(provider_name, {})
    if not provider:
        provider = {
            "format": "anthropic_native",
            "model": "claude-sonnet-4-6",
            "api_key_env": "ANTHROPIC_API_KEY",
            "base_url": "https://api.anthropic.com/v1",
        }

    fmt = provider.get("format", "anthropic_native")

    if fmt in ("anthropic_native", "anthropic"):
        return _call_anthropic_multi(image_paths, prompt, provider)
    elif fmt == "openai":
        return _call_openai_multi(image_paths, prompt, provider)
    else:
        # MCP/unknown: fall back to first image only
        return call_vision(image_paths[0], prompt, provider_name, config)


def _call_anthropic_multi(image_paths: list, prompt: str, provider: dict) -> str:
    """Call Claude with multiple images in one message."""
    api_key = os.environ.get(provider.get("api_key_env", "ANTHROPIC_API_KEY"), "")
    if not api_key:
        raise RuntimeError(f"API key not set: {provider.get('api_key_env', 'ANTHROPIC_API_KEY')}")
    base_url = provider.get("base_url", "https://api.anthropic.com/v1").rstrip("/")
    model = provider.get("model", "claude-sonnet-4-6")
    max_tokens = provider.get("max_tokens", 1024)

    content = []
    for i, path in enumerate(image_paths):
        b64, media_type = _load_image_b64(path)
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })
        content.append({"type": "text", "text": f"[Frame {i+1}/{len(image_paths)}]"})
    content.append({"type": "text", "text": prompt})

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; SwarmController/1.0)",
    }
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }).encode()
    req = urllib.request.Request(f"{base_url}/messages", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        result = json.loads(resp.read())
    return result["content"][0]["text"]


def _call_openai_multi(image_paths: list, prompt: str, provider: dict) -> str:
    """Call an OpenAI-compatible endpoint with multiple images."""
    api_key_env = provider.get("api_key_env", "")
    api_key = os.environ.get(api_key_env, "ollama") if api_key_env else "ollama"
    base_url = provider.get("base_url", "http://localhost:11434/v1").rstrip("/")
    model = provider.get("model", "qwen2.5-vl:7b")
    max_tokens = provider.get("max_tokens", 1024)

    content = []
    for i, path in enumerate(image_paths):
        b64, media_type = _load_image_b64(path)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{b64}"},
        })
        content.append({"type": "text", "text": f"[Frame {i+1}/{len(image_paths)}]"})
    content.append({"type": "text", "text": prompt})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; SwarmController/1.0)",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "num_ctx": provider.get("num_ctx", 4096),
        "messages": [{"role": "user", "content": content}],
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{base_url}/chat/completions", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=240) as resp:
        result = json.loads(resp.read())
    text = result["choices"][0]["message"]["content"]
    end_token = payload.get("thinking_end_token", "</think>")
    idx = text.find(end_token)
    if idx != -1:
        text = text[idx + len(end_token):].strip()
    return text


def get_provider_name(model_tier: str, config: dict) -> str:
    """
    Resolve the provider name for 'fast' or 'powerful' tier.

    Args:
        model_tier: "fast" or "powerful"
        config: Full swarm config dict

    Returns:
        Provider name string
    """
    if model_tier == "fast":
        return config.get("vision_provider_fast") or config.get("vision_provider", "claude")
    return config.get("vision_provider", "claude")
