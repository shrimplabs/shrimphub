"""
Provider Utilities

LLM provider configuration and resolution helpers extracted from constants.py.
"""

# Default LLM provider configurations
LLM_PROVIDERS: dict = {
    "minimax": {
        "base_url": "https://api.minimax.io/anthropic/v1",
        "model": "MiniMax-M3",
        "api_key_env": "MINIMAX_API_KEY",
        "format": "anthropic",
        "context_window": 200000,
        "max_tokens": 32768,
        "thinking_budget": 0,
    },
    "claude": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-6",
        "api_key_env": "ANTHROPIC_API_KEY",
        "format": "anthropic_native",
        "max_tokens": 8096,
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "anthropic/claude-3.5-sonnet",
        "api_key_env": "OPENROUTER_API_KEY",
        "format": "openai",
        "max_tokens": 8096,
    },
    "kimi": {
        "base_url": "https://api.kimi.com/coding/v1",
        "model": "k2p5",
        "api_key_env": "KIMI_API_KEY",
        "format": "anthropic",
        "max_tokens": 32768,
    },
    # Example local provider — replace base_url and model with your own server
    "local": {
        "base_url": "http://localhost:10098/v1",
        "model": "your-local-model",
        "format": "openai",
        "max_tokens": 32768,
    },
}


# Token pricing per million tokens (USD).
# cache_read is what providers charge for a cache hit (cheaper than input).
# cache_write is what providers charge to create a cache entry (often same as input or slightly more).
# If a provider doesn't support caching, cache_read == input and cache_write == input.
# Sources: provider pricing pages as of 2026-06.
TOKEN_PRICING: dict[str, dict[str, float]] = {
    # MiniMax M3 — subscription model, treat as $0 marginal cost but track usage
    "MiniMax-M3":                       {"input": 0.0,    "output": 0.0,    "cache_read": 0.0,   "cache_write": 0.0},
    "MiniMax-M2.7":                     {"input": 0.0,    "output": 0.0,    "cache_read": 0.0,   "cache_write": 0.0},
    # Kimi K2 — subscription, treat as $0
    "k2p5":                             {"input": 0.0,    "output": 0.0,    "cache_read": 0.0,   "cache_write": 0.0},
    "kimi-k2":                          {"input": 0.0,    "output": 0.0,    "cache_read": 0.0,   "cache_write": 0.0},
    # Claude (Anthropic native) — pay per token
    "claude-sonnet-4-6":                {"input": 3.0,    "output": 15.0,   "cache_read": 0.30,  "cache_write": 3.75},
    "claude-opus-4-6":                  {"input": 15.0,   "output": 75.0,   "cache_read": 1.50,  "cache_write": 18.75},
    "claude-haiku-4-5-20251001":        {"input": 0.80,   "output": 4.0,    "cache_read": 0.08,  "cache_write": 1.0},
    "claude-3-5-sonnet-20241022":       {"input": 3.0,    "output": 15.0,   "cache_read": 0.30,  "cache_write": 3.75},
    # OpenRouter — pay per token (deepseek-chat, qwen, claude)
    "deepseek/deepseek-chat":           {"input": 0.27,   "output": 1.10,   "cache_read": 0.07,  "cache_write": 0.27},
    "qwen/qwen-2.5-72b-instruct":       {"input": 0.35,   "output": 0.40,   "cache_read": 0.35,  "cache_write": 0.35},
    "anthropic/claude-3.5-sonnet":      {"input": 3.0,    "output": 15.0,   "cache_read": 0.30,  "cache_write": 3.75},
    "anthropic/claude-3-haiku":         {"input": 0.25,   "output": 1.25,   "cache_read": 0.03,  "cache_write": 0.30},
}

_DEFAULT_PRICING = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int,
                      cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float:
    """Return estimated cost in USD for a completed agent run."""
    pricing = TOKEN_PRICING.get(model, _DEFAULT_PRICING)
    per_m = 1_000_000
    return (
        (input_tokens       * pricing["input"])        / per_m
        + (output_tokens    * pricing["output"])       / per_m
        + (cache_read_tokens  * pricing["cache_read"]) / per_m
        + (cache_write_tokens * pricing["cache_write"])/ per_m
    )


def get_provider_config(provider_name: str, providers: dict = None) -> dict:
    """
    Get provider configuration, falling back to minimax defaults if unknown.

    Args:
        provider_name: Name of the provider (e.g. "minimax", "claude")
        providers: Optional dict of provider configs to use (defaults to LLM_PROVIDERS)

    Returns:
        Provider configuration dict
    """
    if providers is None:
        providers = LLM_PROVIDERS
    return dict(providers.get(provider_name, providers.get("minimax", {})))


def resolve_provider(provider_name: str, providers: dict = None) -> dict:
    """
    Resolve provider configuration with fallback to minimax defaults.

    Args:
        provider_name: Name of the provider
        providers: Optional dict of provider configs to use

    Returns:
        Resolved provider configuration dict
    """
    return get_provider_config(provider_name, providers)
