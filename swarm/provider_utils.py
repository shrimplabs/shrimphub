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
        "context_window": 1048576,
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
}


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
