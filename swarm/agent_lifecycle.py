"""
Agent Lifecycle Management

Extracted from orchestrator.py - handles agent spawning, completion,
status checking, and recovery logic.

Functions:
- spawn_agent: spawn agent subprocesses
- _finish_agent, _handle_task_failure, _spawn_review_task: handle completion/recovery
- check_agent_status: monitor running agents and clean up finished ones
- check_dep_violations: kill agents whose deps are unmet
"""

import json
import os
import secrets
import subprocess
import sys
import time
import uuid
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from swarm.constants import EVENT_BUS_ENABLED_DEFAULT
from swarm.integrity import active_agent_matches_task, can_task_accept_agent, is_continuity_eligible_task
from swarm.platform import popen_session_kwargs, kill_godot_children
from swarm import sandbox as _sandbox
from swarm.agent_recovery import (  # noqa: F401
    _handle_task_failure,
    _looks_like_file_path,
    _bounded_failure_excerpt,
    _spawn_terminal_recovery_continuation,
    _project_plan_subtasks,
    _normalize_plan_hint,
    _extract_dependency_hint_names,
    _match_plan_hint_to_task_ids,
    _validate_project_plan_subtasks,
    _spawn_review_task,
    _replacement_task_dependencies,
)
from swarm.agent_auto_tasks import (  # noqa: F401
    auto_spawn_integration_task,
    auto_handle_sprint_qa,
    auto_spawn_qa_task,
)
from swarm.agent_finish import (  # noqa: F401
    _AgentLogSnapshot,
    _WorktreeFinishResult,
    _resolve_agent_exit_code,
    _read_agent_log,
    _classify_agent_success,
    _active_worktree_handle,
    _finish_worktree_phase,
    _capture_project_diff_stat,
    _read_agent_token_usage,
    _mark_agent_finished,
    _cleanup_agent_script,
    _finish_agent,
)

# Lazy imports to avoid circular dependencies
db = None
worktree = None
_validation = None
_learnings = None
_plan_cleanup = None
_task_chains = None
_task_mutations = None
_project_registry = None
_regressions = None


def _lazy_imports():
    """Lazy import helper to avoid circular deps."""
    global db, worktree, _validation, _learnings, _plan_cleanup, _task_chains, _task_mutations, _regressions
    if db is None:
        from swarm import db as _db
        db = _db
    if worktree is None:
        from swarm import worktree as _wt
        worktree = _wt
    if _validation is None:
        from swarm import validation as _val
        _validation = _val
    if _learnings is None:
        from swarm import learnings as _l
        _learnings = _l
    if _plan_cleanup is None:
        from swarm import plan_cleanup as _pc
        _plan_cleanup = _pc
    if _task_chains is None:
        from swarm import task_chains as _tc
        _task_chains = _tc
    if _task_mutations is None:
        from swarm import task_mutations as _tm
        _task_mutations = _tm
    if _regressions is None:
        try:
            from swarm.closure import regressions as _reg
            _regressions = _reg
        except Exception:
            pass  # closure module not present in all deployments


# ---------------------------------------------------------------------------
# Module-level config (set by orchestrator.py)
# ---------------------------------------------------------------------------

WORKSPACE: Path = Path(".")

# DATA_DIR: set explicitly by configure(). _get_data_dir() falls back to
# orchestrator.DATA_DIR so that code paths that configure orchestrator but not
# agent_lifecycle (tests, early startup) resolve the correct directory.
def _get_data_dir() -> Path:
    """Return DATA_DIR, falling back to orchestrator if it's the unconfigured default."""
    if _configured:
        return DATA_DIR
    try:
        import swarm.orchestrator as _orc
        return _orc.DATA_DIR
    except Exception:
        return DATA_DIR

DATA_DIR: Path = Path("data")
_configured: bool = False  # set True by configure(); guards _get_data_dir fallback
USE_WORKTREES: bool = True
WEBHOOK_URL: str = ""
AUTO_REPLAN_PROJECTS: list = []
PAUSED_PROJECTS: list = []
LOCK_PROJECT: bool = False
HUMAN_REVIEW_FLAG_ENABLED: bool = False
PLAYTHROUGH_AUTO_ENABLED: bool = False
EVENT_BUS_ENABLED: bool = False

# State: agent_id -> handle dict
_active_handles: Dict[str, Dict] = {}
_handle_lock = threading.Lock()

# Agent IDs for which _finish_agent is running in a daemon thread.
# Used to prevent reconcile_agent_runtime_state from double-processing them.
_finishing_agents: Set[str] = set()
_finishing_lock = threading.Lock()

# Auto-QA counters
_qa_completion_counter: Dict[str, int] = {}
_projects_sprint_qa_done: Set[str] = set()

# Config constants (set from constants module)
MAX_ACTIVE_AGENTS: int = 5
AGENT_TIMEOUT: float = 0
QA_AUTO_THRESHOLD: int = 10

# Full config snapshot (set by configure(config=...)); consumed by build_agent_env().
_CONFIG: dict = {}


# ---------------------------------------------------------------------------
# Agent subprocess environment allowlist
# ---------------------------------------------------------------------------
#
# Agents used to inherit ``os.environ.copy()`` -- every API key the controller
# holds was readable with a single ``env`` call.  build_agent_env() instead
# constructs an explicit allowlist: a fixed base of process/runtime vars, plus
# ONLY the ``api_key_env`` variables of the LLM/vision providers this task can
# actually route to.  Web-search keys never leave the controller (web_search is
# proxied through POST /api/tools/web_search).

# Exact variable names always forwarded when present in the controller env.
AGENT_ENV_BASE_VARS: tuple = (
    # Process basics
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER", "LOGNAME", "SHELL",
    # Python
    "PYTHONPATH", "PYTHONIOENCODING", "PYTHONUNBUFFERED", "VIRTUAL_ENV",
    # Swarm runtime (explicit names; SWARM_*/PIPELINE_* prefixes handled below)
    "GODOT_PATH", "SWARM_CONTROLLER_PATH",
    # Read by the agent side (swarm/llm_utils.py, swarm/tools/gpu_scheduler.py)
    "API_PORT", "QUOTA_SLEEP_THRESHOLD", "LLM_JITTER_MIN", "LLM_JITTER_MAX",
    "SHRIMP_ROUTER_HINTS", "ATHENA_SCHEDULER_URL",
    # NOTE: SSH_AUTH_SOCK / GIT_SSH_COMMAND / GIT_ASKPASS are deliberately NOT
    # forwarded -- agents push only to the LAN Gitea over HTTP and must never
    # reach the operator's SSH agent or credential helpers.
    # Godot GUI / screenshots on Linux (macOS needs nothing extra)
    "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
    # Windows (swarm/platform.py Godot discovery + basic process needs)
    "SYSTEMROOT", "SystemRoot", "COMSPEC", "ComSpec", "PATHEXT", "TEMP", "TMP",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "HOMEDRIVE", "HOMEPATH",
)

# Any variable starting with one of these prefixes is forwarded.
AGENT_ENV_BASE_PREFIXES: tuple = ("SWARM_", "PIPELINE_")

# Never forwarded, regardless of anything else (search keys live server-side).
AGENT_ENV_DENY_ALWAYS: frozenset = frozenset({
    "BRAVE_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY",
})

# Never *inherited* from the controller environment even though they match
# the SWARM_ prefix: the agent token is minted per spawn (see spawn_agent) and
# the operator token must never reach an agent.
AGENT_ENV_NEVER_INHERIT: frozenset = frozenset({
    "SWARM_AGENT_TOKEN", "SWARM_OPERATOR_TOKEN",
})

# Extra vars that ride along with MINIMAX_API_KEY (llm_utils reads them).
_MINIMAX_COMPANION_VARS: tuple = ("MINIMAX_GROUP_ID", "MINIMAX-API")

# Task types whose tool set includes vision (mirrors tool_dispatch._QA_TYPES,
# widened to the harness/playthrough variants).  Only these get vision keys.
_VISION_TASK_TYPES: frozenset = frozenset({
    "qa", "harness_qa", "hybrid_qa", "scenario_qa", "art_pass", "polish", "playthrough_bot",
})

# Global config keys whose (string) value names an LLM provider.
_GLOBAL_PROVIDER_KEYS: tuple = (
    "llm_provider", "fast_provider", "strong_provider", "plan_provider",
    "scout_provider", "work_provider", "compaction_provider", "synthesize_provider",
    "diagnose_provider", "validate_provider", "meta_investigation_provider",
)


def _collect_provider_names(obj, out: set, depth: int = 0) -> None:
    """Recursively harvest provider names from a config/metadata subtree.

    Any key ending in ``provider`` with a non-empty string value counts, as do
    string items of lists under keys ending in ``providers`` (e.g.
    ``fallback_providers``).  Covers ``project_pipelines.<p>._work_provider``,
    ``_flat_provider``, ``_loop_model_routing.fast_provider``,
    ``_experiment.flat_provider`` and task ``metadata.flat_provider``.
    """
    if depth > 4 or not isinstance(obj, dict):
        return
    for key, val in obj.items():
        k = str(key)
        if k.endswith("provider") and isinstance(val, str) and val.strip():
            out.add(val.strip())
        elif k.endswith("providers") and isinstance(val, list):
            out.update(v.strip() for v in val if isinstance(v, str) and v.strip())
        elif isinstance(val, dict):
            _collect_provider_names(val, out, depth + 1)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    _collect_provider_names(item, out, depth + 1)


def routed_provider_names(task: Dict, config: dict) -> Set[str]:
    """Return every LLM provider name a task could route to under ``config``."""
    names: Set[str] = set()
    for key in _GLOBAL_PROVIDER_KEYS:
        val = config.get(key)
        if isinstance(val, str) and val.strip():
            names.add(val.strip())
    if not (config.get("llm_provider") or "").strip():
        names.add("minimax")  # swarm_runner default
    _collect_provider_names(config.get("loop_model_routing") or {}, names)
    for fb in config.get("fallback_providers") or []:
        if isinstance(fb, str) and fb.strip():
            names.add(fb.strip())
    project = str(task.get("project") or "")
    proj_cfg = (config.get("project_pipelines") or {}).get(project) or {}
    _collect_provider_names(proj_cfg, names)
    _collect_provider_names(task.get("metadata") or {}, names)
    return names


def _llm_key_env_names(provider_names: Set[str], config: dict) -> Set[str]:
    """Map provider names -> api_key_env variable names (built-ins + config overrides)."""
    from swarm.provider_utils import LLM_PROVIDERS as _BUILTIN
    providers = {k: dict(v) for k, v in _BUILTIN.items()}
    for name, cfg in (config.get("llm_providers") or {}).items():
        if isinstance(cfg, dict):
            providers.setdefault(name, {}).update(cfg)
    envs: Set[str] = set()
    for name in provider_names:
        cfg = providers.get(name)
        if cfg is None:
            # get_provider_config() falls back to minimax defaults for unknown names
            cfg = providers.get("minimax", {})
        # llm_utils.call_llm: cfg.get("api_key_env", "MINIMAX_API_KEY"); "" means keyless
        key_env = cfg.get("api_key_env", "MINIMAX_API_KEY")
        if isinstance(key_env, str) and key_env.strip():
            envs.add(key_env.strip())
    return envs


def _vision_key_env_names(task: Dict, config: dict) -> Set[str]:
    """api_key_env names for the vision providers a vision-capable task can use."""
    if str(task.get("type") or "") not in _VISION_TASK_TYPES:
        return set()
    vproviders = config.get("vision_providers") or {}
    names = set()
    for key in ("vision_provider", "vision_provider_fast"):
        val = config.get(key)
        if isinstance(val, str) and val.strip():
            names.add(val.strip())
    if "minimax-rest" in vproviders:  # qa_tools.click_element prefers this REST alias
        names.add("minimax-rest")
    envs: Set[str] = set()
    for name in names:
        cfg = vproviders.get(name)
        if not isinstance(cfg, dict):
            # swarm/vision.py falls back to the Anthropic default for unknown names
            envs.add("ANTHROPIC_API_KEY")
            continue
        key_env = cfg.get("api_key_env", "")
        if isinstance(key_env, str) and key_env.strip():
            envs.add(key_env.strip())
    return envs


def build_agent_env(task: Dict, config: dict, base_environ, extra: Optional[dict] = None) -> dict:
    """Build the allowlisted environment for an agent subprocess.

    Pure function: reads only ``task``, ``config``, ``base_environ``
    (a mapping such as ``os.environ``) and ``extra``.  Returns a new dict
    containing:

    * the fixed base vars (``AGENT_ENV_BASE_VARS`` + ``SWARM_*``/``PIPELINE_*``)
      that exist in ``base_environ``;
    * ONLY the ``api_key_env`` variables of the LLM providers this task can
      route to (active/fast/strong/plan/scout/work/compaction/synthesize/
      meta-investigation providers, ``fallback_providers``, per-project
      ``project_pipelines`` overrides, task-metadata overrides), plus the
      vision providers for vision-capable task types;
    * ``SWARM_FETCH_ALLOWED_HOSTS`` when ``fetch_url_allowed_hosts`` is set;
    * every key of ``extra`` -- values *generated* for this spawn rather than
      inherited (the per-spawn ``SWARM_AGENT_TOKEN``).

    Web-search keys (``AGENT_ENV_DENY_ALWAYS``) are never included, and
    ``AGENT_ENV_NEVER_INHERIT`` names are never copied from ``base_environ``.
    """
    config = config or {}
    env: dict = {}

    for name, val in base_environ.items():
        if name in AGENT_ENV_DENY_ALWAYS or name in AGENT_ENV_NEVER_INHERIT:
            continue
        if name in AGENT_ENV_BASE_VARS or name.startswith(AGENT_ENV_BASE_PREFIXES):
            env[name] = val

    wanted = _llm_key_env_names(routed_provider_names(task, config), config)
    wanted |= _vision_key_env_names(task, config)
    if "MINIMAX_API_KEY" in wanted:
        wanted.update(_MINIMAX_COMPANION_VARS)
    for name in sorted(wanted):
        if name in AGENT_ENV_DENY_ALWAYS:
            continue
        if name in base_environ:
            env[name] = base_environ[name]

    hosts = config.get("fetch_url_allowed_hosts")
    if isinstance(hosts, (list, tuple)):
        env["SWARM_FETCH_ALLOWED_HOSTS"] = ",".join(
            str(h).strip().lower() for h in hosts if str(h).strip()
        )

    assets = config.get("asset_library_paths")
    if isinstance(assets, (list, tuple)):
        env["SWARM_ASSET_LIBRARY_PATHS"] = os.pathsep.join(
            str(Path(str(a)).expanduser()) for a in assets if str(a).strip()
        )

    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("SWARM_CONTROLLER_PATH", str(Path(__file__).resolve().parents[1]))

    for name, val in (extra or {}).items():
        if name in AGENT_ENV_DENY_ALWAYS or val is None:
            continue
        env[str(name)] = str(val)
    return env


def configure(
    workspace: Path = Path("."),
    data_dir: Path = Path("data"),
    use_worktrees: bool = True,
    webhook_url: str = "",
    auto_replan_projects: list = [],
    paused_projects: list = [],
    lock_project: bool = False,
    max_active_agents: int = 5,
    agent_timeout: float = 0,
    qa_auto_threshold: int = 10,
    project_registry=None,
    human_review_flag_enabled: bool = False,
    playthrough_auto_enabled: bool = False,
    # Sourced from swarm.constants.EVENT_BUS_ENABLED_DEFAULT so the default
    event_bus_enabled: bool = EVENT_BUS_ENABLED_DEFAULT,
    config: Optional[dict] = None,
    **_kwargs,
):
    """Configure module-level settings for the agent lifecycle system.

    Args:
        workspace: Root directory for all projects.
        data_dir: Directory for agent data (scripts, logs, etc.).
        use_worktrees: Whether to use git worktrees for agent isolation.
        webhook_url: URL for task completion/failure webhook notifications.
        auto_replan_projects: List of projects that should auto-replan after QA.
        paused_projects: List of projects that are paused and won't spawn agents.
        lock_project: When True, only one agent per project runs at a time.
        max_active_agents: Maximum number of concurrent agents.
        agent_timeout: Timeout in seconds for agent execution (0 = no timeout).
        qa_auto_threshold: Number of completed tasks before auto-spawning QA.
        project_registry: Project registry instance for releasing file locks on agent exit.
    """
    global WORKSPACE, DATA_DIR, USE_WORKTREES, _configured
    global WEBHOOK_URL, AUTO_REPLAN_PROJECTS, PAUSED_PROJECTS, LOCK_PROJECT
    global MAX_ACTIVE_AGENTS, AGENT_TIMEOUT, QA_AUTO_THRESHOLD, HUMAN_REVIEW_FLAG_ENABLED
    global PLAYTHROUGH_AUTO_ENABLED
    global _project_registry, _CONFIG

    if config is not None:
        _CONFIG = dict(config)
    WORKSPACE = workspace
    DATA_DIR = data_dir
    _configured = True
    USE_WORKTREES = use_worktrees
    WEBHOOK_URL = webhook_url
    AUTO_REPLAN_PROJECTS = auto_replan_projects
    PAUSED_PROJECTS = paused_projects
    LOCK_PROJECT = lock_project
    MAX_ACTIVE_AGENTS = max_active_agents
    AGENT_TIMEOUT = agent_timeout
    QA_AUTO_THRESHOLD = qa_auto_threshold
    HUMAN_REVIEW_FLAG_ENABLED = human_review_flag_enabled
    PLAYTHROUGH_AUTO_ENABLED = playthrough_auto_enabled

    # Event bus -- wire up before enabling so the handler is registered first.
    _env_flag = os.environ.get("SWARM_EVENT_BUS", "")
    _bus_on = event_bus_enabled or _env_flag in ("1", "true", "yes")
    EVENT_BUS_ENABLED = _bus_on
    from swarm.events import bus as _event_bus
    _event_bus.subscribe("AGENT_EXITED", _on_agent_exited)
    _event_bus.set_enabled(_bus_on)
    if project_registry is not None:
        _project_registry = project_registry



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _fire_task_webhook(event: str, **kwargs):
    """Fire a task-level webhook event if a URL is configured.

    Thin wrapper around the canonical implementation in
    ``swarm.task_mutations._fire_task_webhook`` that injects this module's
    ``WEBHOOK_URL``. Tests may patch ``swarm.agent_lifecycle._fire_task_webhook``
    directly to intercept the call.
    """
    from swarm.task_mutations import _fire_task_webhook as _impl
    return _impl(event, webhook_url=WEBHOOK_URL, **kwargs)


# ---------------------------------------------------------------------------
# Agent spawning
# ---------------------------------------------------------------------------

def spawn_agent(task: Dict, generate_script_fn) -> Optional[str]:
    """
    Spawn a subprocess agent for the given task.

    Args:
        task: task dict with 'id', 'project', 'type', 'description'
        generate_script_fn: callable(task) -> script_source_str

    Returns:
        agent_id on success, None on failure
    """
    _lazy_imports()

    current_task = db.task_get(task["id"]) if task.get("id") else None
    if current_task and not can_task_accept_agent(current_task):
        print(
            f"[Swarm] Refusing to spawn agent for task {task.get('id', '')[:8]} "
            f"with status={current_task.get('status')} agent_id={current_task.get('agent_id')}"
        )
        return None

    existing_agent = None
    if task.get("id"):
        for agent in db.agent_get_active():
            if agent.get("task_id") == task["id"]:
                existing_agent = agent
                break
    if existing_agent:
        print(
            f"[Swarm] Refusing to spawn duplicate agent for task {task['id'][:8]} "
            f"because agent {existing_agent['id'][:8]} is already active"
        )
        return None

    agent_id = str(uuid.uuid4())
    project = task["project"]

    # Create an isolated git worktree so parallel agents don't stomp each other
    worktree_path: Optional[Path] = None
    worktree_branch: Optional[str] = None
    if USE_WORKTREES and task.get("type") not in ("manager", "project_create") and worktree._is_git_repo(WORKSPACE / project):
        meta = dict(task.get("metadata") or {})
        existing_wt_path = meta.get("worktree_path")
        existing_wt_branch = meta.get("worktree_branch")

        if existing_wt_path and Path(existing_wt_path).exists():
            # Reuse the existing worktree from a previous failed attempt (chained worktree)
            worktree_path = Path(existing_wt_path)
            worktree_branch = existing_wt_branch
            print(f"[Swarm] Reusing existing worktree {worktree_path.name} for {task.get('id', '')[:8]}")
        else:
            wt = worktree._create_worktree(WORKSPACE / project, agent_id)
            if wt:
                worktree_path, worktree_branch = wt
                # Inject worktree path into task so generate_task_script bakes it into prompts
                task = dict(task)
                meta = dict(task.get("metadata") or {})
                meta["worktree_path"] = str(worktree_path)
                meta["worktree_branch"] = worktree_branch
                task["metadata"] = meta

    # Pre-flight baseline: capture which validation errors exist BEFORE the agent
    # touches anything.  Post-task validation diffs against this so only NEW errors
    # count as failures.  Only runs for task types that go through post-validation.
    # Runs asynchronously in a background thread so it never blocks the monitor loop --
    # capture_validation_baseline can take up to 60s (Godot headless validation).
    # The baseline is stored in task metadata before the agent does meaningful work;
    # agents spend their first several tool-loops reading files, so there's ample
    # window before any writes occur.
    _BASELINE_SKIP_TYPES = {"manager", "project_create", "qa", "research",
                            "harness_qa", "hybrid_qa", "project_plan", "audit",
                            "triage", "art_pass", "scenario_qa"}
    _baseline_path = worktree_path if worktree_path is not None else (WORKSPACE / project)
    _do_baseline = task.get("id") and task.get("type") not in _BASELINE_SKIP_TYPES and _baseline_path.exists()
    if _do_baseline:
        def _run_baseline(proj=project, tid=task["id"], bpath=_baseline_path):
            try:
                _validation.capture_validation_baseline(proj, tid, bpath)
            except Exception as _blerr:
                print(f"[Swarm] WARNING: pre-flight baseline failed for {tid[:8]}: {_blerr}")
        threading.Thread(target=_run_baseline, daemon=True, name=f"preflight-{task['id'][:8]}").start()

    # Record the project HEAD at spawn so the completion truth layer can attribute
    # a diff/commit to THIS agent (agent_finish._finish_agent). Without this, diff
    # evidence is "git diff HEAD~1" -- whatever the last commit was, regardless of
    # author -- so a no-op agent inherits the previous commit as false evidence.
    if task.get("id"):
        try:
            _head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
                cwd=str(WORKSPACE / project),
            ).stdout.strip()
            if _head:
                _t = db.task_get(task["id"])
                _meta = dict((_t or {}).get("metadata") or {})
                _meta["head_at_spawn"] = _head
                db.task_update(task["id"], {"metadata": _meta})
        except Exception as _hserr:
            print(f"[Swarm] WARNING: head_at_spawn capture failed for {task['id'][:8]}: {_hserr}")

    script_content = generate_script_fn(task)
    script_path = _get_data_dir() / f"agent_{agent_id}.py"
    script_path.write_text(script_content, encoding="utf-8")
    os.chmod(script_path, 0o755)

    log_path = _get_data_dir() / f"agent_{agent_id}.log"

    # Per-spawn API token (swarm.api_authz): minted here, persisted BEFORE the
    # process starts (so its first API call already validates), handed to the
    # subprocess as SWARM_AGENT_TOKEN, and revoked when the agent leaves the
    # 'active' state (claim_finish / db.agent_update_status).
    agent_token = secrets.token_urlsafe(32)
    try:
        db.agent_token_create(agent_token, agent_id, task.get("id"), project)
    except Exception as _tok_err:
        print(f"[Swarm] WARNING: could not persist agent token for {agent_id[:8]}: {_tok_err}")

    try:
        # Allowlisted environment: base runtime vars + only the provider keys
        # this task can route to.  Never a blanket os.environ.copy().
        env = build_agent_env(task, _CONFIG, os.environ, extra={"SWARM_AGENT_TOKEN": agent_token})

        # Layer 2 containment (swarm.sandbox): wrap the agent in a seatbelt
        # profile so the kernel -- not just the in-process guards -- limits
        # what it and its children can write and reach.  Opt-in via
        # config sandbox_agents; never fails the spawn, but always logs
        # which way it went so an unsandboxed fleet is visible rather than
        # silent.
        agent_argv, _sb_note = _sandbox.maybe_wrap(
            [sys.executable, str(script_path)],
            config=_CONFIG,
            workspace=WORKSPACE,
            data_dir=_get_data_dir(),
            project=project,
            worktree=worktree_path,
        )
        if _sb_note:
            print(f"[Swarm] {agent_id[:8]}: {_sb_note}")
        if agent_argv[0] == _sandbox.SANDBOX_EXEC:
            # Tell the sandboxed child it is already under seatbelt containment,
            # so its own test suite (tests/test_sandbox.py) knows a nested
            # sandbox-exec invocation will be kernel-denied (returncode 71)
            # regardless of is_supported(), and skips those cases instead of
            # reporting them as validation failures.
            env["SWARM_SANDBOXED"] = "1"

        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            agent_argv,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            **popen_session_kwargs(),
        )
        log_file.close()

        with _handle_lock:
            _active_handles[agent_id] = {
                "process": proc,
                "project": project,
                "task_id": task.get("id"),
                "started": time.time(),
                "script_path": str(script_path),
                "log_path": str(log_path),
                "worktree_path": str(worktree_path) if worktree_path else None,
                "worktree_branch": worktree_branch,
            }

        # Waiter thread: blocks on proc.wait() and fires AGENT_EXITED the
        # instant the process exits.  The flag gates the publish -- if the bus
        # is disabled the thread exits immediately after proc.wait() with no
        # effect.  Sweep still runs normally as the fallback.
        def _waiter(aid=agent_id, p=proc, tid=task.get("id"), proj=project):
            _ec = p.wait()
            from swarm.events import bus as _eb
            if not _eb.enabled:
                return
            _eb.publish("AGENT_EXITED", agent_id=aid, exit_code=_ec,
                        task_id=tid, project=proj)

        threading.Thread(
            target=_waiter, daemon=True, name=f"waiter-{agent_id[:8]}"
        ).start()

        db.agent_upsert({
            "id": agent_id,
            "project": project,
            "task_type": task["type"],
            "status": "active",
            "spawned_at": datetime.now().isoformat(),
            "pid": proc.pid,
            "log_path": str(log_path),
            "script_path": str(script_path),
            "task_id": task.get("id"),
        })

        db.task_update_status(
            task["id"], "in_progress",
            agent_id=agent_id,
            started=datetime.now().isoformat(),
        )

        print(f"[Swarm] Spawned agent {agent_id[:8]} for {project} ({task['type']})")
        return agent_id

    except Exception as e:
        print(f"[Swarm] Failed to spawn agent: {e}")
        try:
            db.agent_token_revoke(agent_id)
        except Exception:
            pass
        if worktree_path and worktree_branch:
            worktree._cleanup_worktree(WORKSPACE / project, worktree_path, worktree_branch)
        return None


# ---------------------------------------------------------------------------
# Agent status checking
# (Agent completion pipeline is in swarm/agent_finish.py)
# ---------------------------------------------------------------------------

def _on_agent_exited(ev) -> None:
    """Event bus handler: an agent process exited -- claim teardown and finish it.

    Fires on the dispatcher thread.  Must not block (starts a finish thread and
    returns immediately).  If the sweep already claimed teardown, this is a
    no-op.
    """
    agent_id = ev.payload.get("agent_id")
    exit_code = ev.payload.get("exit_code", -1)
    if not agent_id:
        return
    data = claim_finish(agent_id)
    if data is None:
        return  # sweep or dep-violator kill beat us to it
    try:
        kill_godot_children(data["process"].pid)
    except Exception:
        pass
    print(f"[EventBus] agent {agent_id[:8]} exited → finish "
          f"(latency {time.time() - ev.ts:.3f}s)")
    start_finish_thread(agent_id, exit_code, data, name_suffix="waiter")


def check_dep_violations(completed_ids=None, all_task_ids=None):
    """Kill any running agent whose task dependencies are not yet satisfied.

    This catches the race where deps are patched after an agent was already
    spawned, or where fill_slots incorrectly picked a task before its deps
    completed.

    *completed_ids* and *all_task_ids* may be pre-fetched by the caller (the
    monitor loop) to avoid redundant full-table scans when several checks run
    in the same cycle.
    """
    _lazy_imports()

    # Backfill first so tasks that just completed are in the permanent record
    # before we decide to kill anything.  Prevents a race where _finish_agent()
    # writes "completed" to the tasks table but task_record_completed() hasn't
    # been called yet when this check runs.
    db.backfill_completed_task_ids()

    if completed_ids is None:
        completed_ids = db.task_get_completed_ids()
    if all_task_ids is None:
        all_task_ids = db.task_get_all_ids()

    with _handle_lock:
        handles_snapshot = list(_active_handles.items())
    handle_task_ids = {data.get("task_id") for _, data in handles_snapshot}

    # Also check DB-tracked active agents (survive server restarts, not in _active_handles)
    db_active_agents = db.agent_get_active()

    def _kill_dep_violator(task_id: str, agent_id: str, unmet: list, pid: Optional[int] = None, process=None):
        print(f"[Swarm] Dep violation: agent {agent_id[:8]} task {task_id} "
              f"deps not met: {unmet} \u2014 killing")
        if process:
            try:
                print(f"[SwarmKill] reason=dep_violation agent={agent_id[:8]} pid={process.pid}")
                kill_godot_children(process.pid)
                process.kill()
                process.wait(timeout=5)
            except Exception as _ke:
                print(f"[Swarm] Dep violation kill error: {_ke}")
        elif pid:
            try:
                print(f"[SwarmKill] reason=dep_violation agent={agent_id[:8]} pid={pid}")
                kill_godot_children(pid)
                os.kill(pid, 9)
            except Exception as _ke:
                print(f"[Swarm] Dep violation PID kill error: {_ke}")
        # claim_finish removes from _active_handles and blocks any concurrent
        # waiter thread from also running _finish_agent for this agent.
        claim_finish(agent_id)
        try:
            db.task_update_status(task_id, "pending", agent_id=None)
            db.agent_update_status(agent_id, "failed",
                                   completed_at=datetime.now().isoformat(), exit_code=-1)
        except Exception as _de:
            print(f"[Swarm] Dep violation DB error: {_de}")
        with _finishing_lock:
            _finishing_agents.discard(agent_id)

    # Check in-memory handles
    for agent_id, data in handles_snapshot:
        task_id = data.get("task_id")
        if not task_id:
            continue
        task = db.task_get(task_id)
        if not task:
            continue
        deps = task.get("dependencies") or []
        unmet = [d for d in deps if d not in completed_ids and d in all_task_ids]
        if unmet:
            _kill_dep_violator(task_id, agent_id, unmet, process=data["process"])

    # Check DB-tracked agents (PIDs from previous server runs)
    for agent in db_active_agents:
        agent_id = agent["id"]
        task_id = agent.get("task_id")
        if not task_id or task_id in handle_task_ids:
            continue  # already checked above
        task = db.task_get(task_id)
        if not task:
            continue
        deps = task.get("dependencies") or []
        unmet = [d for d in deps if d not in completed_ids and d in all_task_ids]
        if unmet:
            _kill_dep_violator(task_id, agent_id, unmet, pid=agent.get("pid"))


def wait_for_all_finishes(timeout: float = 10.0) -> bool:
    """Block until _finishing_agents is empty or *timeout* seconds pass.

    Use in tests after check_agent_status() to ensure _finish_agent has
    completed regardless of whether the sweep or a waiter thread claimed
    teardown.  Not needed in production (monitor doesn't care).
    """
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        with _finishing_lock:
            if not _finishing_agents:
                return True
        _time.sleep(0.05)
    with _finishing_lock:
        return not _finishing_agents


def claim_finish(agent_id: str) -> Optional[Dict]:
    """Atomically claim teardown ownership for an agent.

    Moves the agent from ``_active_handles`` into ``_finishing_agents`` in one
    operation so that exactly one caller (sweep, waiter thread, or dep-violator
    kill) wins teardown rights.

    Returns the handle dict if this caller won, ``None`` if another caller
    already claimed it (double-claim guard).
    """
    with _finishing_lock:
        if agent_id in _finishing_agents:
            return None  # another path already owns teardown
        _finishing_agents.add(agent_id)
    # Teardown ownership claimed: the agent's API token is dead from here on,
    # regardless of how the rest of the finish pipeline goes.
    try:
        _lazy_imports()
        db.agent_token_revoke(agent_id)
    except Exception as _tok_err:
        print(f"[Swarm] WARNING: token revoke failed for {agent_id[:8]}: {_tok_err}")
    with _handle_lock:
        return _active_handles.pop(agent_id, None)


def start_finish_thread(
    agent_id: str,
    exit_code: int,
    data: Dict,
    *,
    name_suffix: str = "",
) -> threading.Thread:
    """Start a daemon thread that runs ``_finish_agent`` for *agent_id*.

    ``claim_finish()`` must have been called (and returned non-None) before
    calling this.  The thread removes the agent from ``_finishing_agents`` when
    done regardless of success or failure.

    Returns the started thread so callers that need synchronous completion
    (e.g. tests) can join it.
    """
    _lazy_imports()

    def _run(aid=agent_id, ec=exit_code, d=data):
        final_status = "failed"
        try:
            _finish_agent(
                aid, ec,
                d.get("project"), d.get("task_id"),
                d.get("script_path"), d.get("log_path"),
            )
            final_status = "completed"
        except Exception as e:
            print(f"[Swarm] Error finishing agent {aid[:8]}: {e}")
        finally:
            with _finishing_lock:
                _finishing_agents.discard(aid)
            from swarm.events import bus as _eb
            _eb.publish(
                "AGENT_FINISHED",
                agent_id=aid,
                task_id=d.get("task_id"),
                project=d.get("project"),
                final_status=final_status,
            )

    label = f"finish-{agent_id[:8]}{('-' + name_suffix) if name_suffix else ''}"
    t = threading.Thread(target=_run, daemon=True, name=label)
    t.start()
    return t


def check_agent_status() -> List[threading.Thread]:
    """Check all active agents; clean up finished and timed-out ones.

    Returns a list of daemon threads started for finishing work (validation,
    DB updates, etc.).  Callers that need synchronous completion (e.g. tests)
    can join these threads; the monitor loop discards them so it is never
    blocked by long-running Godot validation subprocesses.
    """
    _lazy_imports()

    now = time.time()
    # Absolute deadline: 4× AGENT_TIMEOUT (default 8 h). A handle older than
    # this is a zombie regardless of freeze_started — quota freeze can't last
    # days. Protects against freeze_started leaking and exempting a handle from
    # the normal watchdog forever (root cause of the 13-day idle incident).
    _ZOMBIE_DEADLINE = max(AGENT_TIMEOUT * 4, 28800)  # min 8 h
    with _handle_lock:
        finished = []
        timed_out = []
        for agent_id, data in list(_active_handles.items()):
            exit_code = data["process"].poll()
            age = now - data["started"]
            if exit_code is not None:
                finished.append((agent_id, exit_code, data))
            elif data.get("containment_frozen"):
                # Deliberately frozen by the containment breaker; only an
                # operator release thaws it.  Never time it out or reap it.
                continue
            elif age > _ZOMBIE_DEADLINE:
                # Hard absolute deadline — kills even freeze-exempt handles.
                print(f"[Swarm] Agent {agent_id[:8]} zombie: age={age:.0f}s exceeds absolute deadline {_ZOMBIE_DEADLINE}s — force-killing")
                timed_out.append((agent_id, data))
            elif data.get("freeze_started") and (now - data["freeze_started"]) > 1800:
                # Frozen handle that's been stuck for >30 min — likely a leaked
                # quota-freeze (SIGCONT never sent). Log so the cause is visible
                # in monitor-errors.jsonl and stdout for post-incident diagnosis.
                frozen_age = now - data["freeze_started"]
                print(f"[Swarm] Agent {agent_id[:8]} frozen {frozen_age:.0f}s — possible quota-freeze leak (freeze_started set but SIGCONT never fired)")
            elif AGENT_TIMEOUT > 0 and not data.get("freeze_started") and age > AGENT_TIMEOUT:
                timed_out.append((agent_id, data))

    finish_threads: List[threading.Thread] = []

    for agent_id, data in timed_out:
        print(f"[Swarm] Agent {agent_id[:8]} timed out after {AGENT_TIMEOUT}s \u2014 killing")
        try:
            print(f"[SwarmKill] reason=agent_timeout agent={agent_id[:8]} pid={data['process'].pid} timeout={AGENT_TIMEOUT}")
            kill_godot_children(data["process"].pid)
            data["process"].kill()
            data["process"].wait(timeout=5)
        except Exception:
            pass
        # claim_finish pops from _active_handles; if it returns None another path
        # already owns teardown (shouldn't happen for timeout, but guard anyway).
        claimed = claim_finish(agent_id)
        if claimed is None:
            continue
        t = start_finish_thread(agent_id, -1, data, name_suffix="timeout")
        finish_threads.append(t)

    for agent_id, exit_code, data in finished:
        # Kill any Godot game processes the agent may have launched but not cleaned
        # up (e.g. when the agent crashed before calling kill_game()).
        try:
            kill_godot_children(data["process"].pid)
        except Exception:
            pass
        # claim_finish atomically moves agent to _finishing_agents and pops the
        # handle.  Returns None if a waiter thread already claimed teardown.
        claimed = claim_finish(agent_id)
        if claimed is None:
            continue
        t = start_finish_thread(agent_id, exit_code, data)
        finish_threads.append(t)

    reconcile_agent_runtime_state(prune=False)

    prune_history()

    return finish_threads


def reconcile_agent_runtime_state(*, prune: bool = True) -> dict:
    """Repair drift between DB-tracked agents and live task ownership."""
    _lazy_imports()
    from swarm.maintenance import agents as _maintenance_agents

    with _finishing_lock:
        _currently_finishing = frozenset(_finishing_agents)
    return _maintenance_agents.reconcile_agent_runtime_state(
        db=db,
        active_handles=_active_handles,
        finish_agent=_finish_agent,
        is_pid_running=_is_pid_running,
        active_agent_matches_task=active_agent_matches_task,
        task_mutations=_task_mutations,
        prune_history=prune_history,
        logger=print,
        prune=prune,
        finishing_agents=_currently_finishing,
    )


def cleanup_recovery_branches(project: str) -> dict:
    """Collapse stale recovery trees for a project to one sane live path per branch."""
    _lazy_imports()
    from swarm.maintenance import recovery as _maintenance_recovery

    return _maintenance_recovery.cleanup_recovery_branches(
        db=db,
        project=project,
        task_mutations=_task_mutations,
        task_chains=_task_chains,
        spawn_terminal_recovery_continuation=_spawn_terminal_recovery_continuation,
    )


# ---------------------------------------------------------------------------
# Accessors for orchestrator
# ---------------------------------------------------------------------------

def get_active_handles() -> Dict[str, Dict]:
    """Return a copy of active handles for external inspection."""
    with _handle_lock:
        return dict(_active_handles)


def get_active_count() -> int:
    """Count agents that are genuinely still running.

    Includes agents currently finishing (process exited but _finish_agent thread
    not yet done) so that fill_slots doesn't see a false dip and over-spawn.
    """
    _lazy_imports()

    with _handle_lock:
        in_process = {
            aid for aid, d in _active_handles.items()
            if d["process"].poll() is None
        }

    with _finishing_lock:
        finishing = frozenset(_finishing_agents)

    # Count all DB-active agents not already tracked in-process or finishing.
    # We do NOT filter by PID liveness here -- a just-died pre-restart agent
    # still holds a slot until the reconciler updates its DB status next cycle.
    # Checking PID liveness here creates a race window where a dying agent
    # is counted as neither in-process, finishing, nor persisted, causing
    # fill_slots to over-spawn.
    persisted = {
        a["id"] for a in db.agent_get_active()
        if a["id"] not in in_process and a["id"] not in finishing
    }

    return len(in_process) + len(finishing) + len(persisted)


def prune_history():
    """Archive finished agents to JSONL and remove from DB.

    Tasks (completed/failed/cancelled) stay in the DB permanently -- they are
    the authoritative record and are never deleted.  task-history.jsonl is no
    longer written; use the DB directly.

    Agents are still written to agent-history.jsonl before being deleted from
    the DB (metrics endpoint reads the JSONL as a fallback for old archived rows).
    """
    _lazy_imports()

    # Use orchestrator's HISTORY_FILE if available, otherwise fall back
    try:
        import swarm.orchestrator as _orc
        HISTORY_FILE = getattr(_orc, "HISTORY_FILE", _get_data_dir() / "agent-history.jsonl")
    except Exception:
        HISTORY_FILE = _get_data_dir() / "agent-history.jsonl"

    # --- Agent archival to JSONL then delete from DB ---
    finished = [a for a in db.agent_get_all() if a.get("status") not in ("active", "spawning")]
    if finished:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_FILE.open("a") as f:
            for agent in finished:
                f.write(json.dumps(agent) + "\n")

    # Resolve managed-projects filter once for all task queries below.
    try:
        from swarm import orchestrator as _orc
        _managed = list(_orc.MANAGED_PROJECTS) if _orc.MANAGED_PROJECTS else None
    except Exception:
        _managed = None

    # Update each project's head_task_id to the most recent continuity-eligible
    # task, but do not overwrite a live continuation with a failed/cancelled tail.
    # Only run this scan when agents actually finished -- it fetches 15k+ rows and
    # would block the monitor for seconds on every cycle if run unconditionally.
    all_terminal = db.task_get_all(
        exclude_statuses=("pending", "in_progress"),
        projects=_managed,
    ) if finished else []
    if all_terminal:
        latest_by_project: Dict[str, tuple[str, str]] = {}
        for task in all_terminal:
            proj = task.get("project", "")
            completed = task.get("completed", "")
            if proj and completed and is_continuity_eligible_task(task):
                existing = latest_by_project.get(proj)
                if not existing or completed > existing[1]:
                    latest_by_project[proj] = (task["id"], completed)
        for proj_name, (task_id, _) in latest_by_project.items():
            existing_proj = db.project_get(proj_name)
            if not existing_proj:
                continue
            current_head = _task_chains.get_project_head(db, proj_name)
            if current_head:
                current_head_task = db.task_get(current_head)
                if current_head_task and current_head_task.get("status") in ("pending", "in_progress"):
                    continue
                current_completed = (current_head_task or {}).get("completed") or ""
                if current_completed and current_completed >= latest_by_project[proj_name][1]:
                    continue
            if existing_proj.get("head_task_id") != task_id:
                db.project_upsert({**existing_proj, "head_task_id": task_id})

    # --- Delete finished agents from DB (agents still archived; tasks are NOT deleted) ---
    if finished:
        conn = db._connect()
        conn.execute(
            "DELETE FROM agents WHERE status NOT IN ('active', 'spawning')"
        )
        conn.commit()
