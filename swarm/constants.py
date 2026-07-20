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
IGNORE_DIRS: set = {"addons", ".git", ".godot"}
MINIMAX_BASE_URL: str = "https://api.minimax.io/anthropic/v1"
LLM_PROVIDER: str = "minimax"
FALLBACK_PROVIDERS: list = []

# Agent runtime constants
# Default maximum tool-use iterations per agent task before forcing termination.
# playthrough_bot tasks get 500 (set in swarm_runner.py) — they legitimately
# need to fix game bugs AND write/iterate a bot in the same task.
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

MINIMAX_MODEL: str = "MiniMax-M3"

# Pipeline feature constants
# Marks the structured phase-pipeline (plan -> scout -> work -> validate) as shipped.
# Bump when the pipeline contract in swarm/pipeline.py changes in a non-backwards-compatible way.
PIPELINE_VERSION: str = "1.0"

# Pipeline phase loop limits — all phase files import from here
PLAN_MAX_LOOPS: int = 10
SCOUT_MAX_LOOPS: int = 24
WORK_MAX_LOOPS: int = 150
DIAGNOSE_MAX_LOOPS: int = 10
SYNTHESIZE_MAX_ATTEMPTS: int = 2
CREATE_TASKS_MAX_LOOPS: int = 15

# Escalation / retry limits — agent_recovery.py reads max_attempts from task rows,
# which are seeded from these defaults at task-creation time.
DEFAULT_MAX_ATTEMPTS: int = 3          # bug / feature / refactor / integration
DEFAULT_MAX_ATTEMPTS_QA: int = 2       # qa / harness_qa / art_pass / plan types
RESEARCH_FEEDER_MAX_ATTEMPTS: int = 2  # research feeders spawned by recovery
MAX_RESEARCH_FEEDER_CYCLES: int = 3    # cap on how many feeders one task can spawn

# Infrastructure freeze escalation — tasks that accumulate this many consecutive
# infrastructure failures without consuming a real attempt are force-escalated to
# consume one attempt, eventually triggering the research-feeder path.
INFRA_FREEZE_THRESHOLD: int = 5        # consecutive infra failures before force-escalation

# Task types expected to produce a git commit when they complete. A completed
# task of one of these types with zero new commits since the agent spawned is
# suspicious -- the completion truth layer (agent_finish._finish_agent) flags
# it via metadata.completion_evidence.unverified. Everything not in this set
# (qa, research, plan, audit, project_plan, manager, triage, ...) is
# report/orchestration output and legitimately produces no diff.
WRITE_TASK_TYPES: frozenset = frozenset({
    "feature", "bug", "refactor", "polish", "art_pass", "integration", "playthrough_bot",
})
