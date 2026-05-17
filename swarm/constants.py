"""
Swarm Constants

Module-level constant values extracted from orchestrator.py and agent_runtime.py.
"""

from swarm.provider_utils import LLM_PROVIDERS

# Orchestrator constants
MAX_ACTIVE_AGENTS: int = 3
MAX_LINES: int = 5000
AGENT_TIMEOUT: int = 7200
QUOTA_LIMIT_PERCENT: float = 90.0
QA_AUTO_THRESHOLD: int = 8
AUDIT_AUTO_THRESHOLD: int = 20
IGNORE_DIRS: set = {"addons", ".git", ".godot"}
MINIMAX_BASE_URL: str = "https://api.minimax.io/anthropic/v1"
LLM_PROVIDER: str = "minimax"
FALLBACK_PROVIDERS: list = []

# Agent runtime constants
MAX_TOOL_LOOPS: int = 200
API_PORT: int = 5001
QA_MAX_CYCLES: int = 3
IGNORE_EXTENSIONS: set = {
    ".fbx", ".obj", ".glb", ".gltf", ".blend",
    ".png", ".jpg", ".jpeg", ".webp", ".svg",
    ".wav", ".mp3", ".ogg", ".opus",
    ".ttf", ".otf", ".woff",
    ".zip", ".tar", ".gz",
    ".import", ".uid",
}

MINIMAX_MODEL: str = "MiniMax-M2.7"
