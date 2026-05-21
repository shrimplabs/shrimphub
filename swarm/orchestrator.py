"""
Swarm Orchestrator

Centralises the coordination logic that was previously scattered across
swarm_runner.py:

- spawn_agent
- check_agent_status / _finish_agent
- fill_slots
- prune_history
- check_quota_limit
- get_active_count
- update_project_registry

All state is read/written via swarm.db (SQLite) so multiple threads are safe.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from swarm import db
from swarm import worktree
from swarm import constants
from swarm import validation as _validation
from swarm import learnings as _learnings
from swarm import agent_lifecycle
from swarm.godot_bootstrap import GUT_VERSION
from swarm.task_chains import chain_to_project_head
from swarm.constants import (
    MAX_ACTIVE_AGENTS, MAX_LINES, AGENT_TIMEOUT, QUOTA_LIMIT_PERCENT,
    QA_AUTO_THRESHOLD, AUDIT_AUTO_THRESHOLD, IGNORE_DIRS, IGNORE_EXTENSIONS,
    MAX_TOOL_LOOPS, API_PORT, QA_MAX_CYCLES,
)
from swarm.integrity import can_task_accept_agent

# ---------------------------------------------------------------------------
# Module-level config -- set by the caller (e.g. create_app or swarm_runner)
# ---------------------------------------------------------------------------

WORKSPACE: Path = Path(".")
DATA_DIR = Path("data")
HISTORY_FILE: Path = DATA_DIR / "agent-history.jsonl"

LOCK_PROJECT: bool = False
USE_WORKTREES: bool = True
MCP_SERVERS: dict = {}
IGNORE_EXTENSIONS: set = set()
MANAGED_PROJECTS: list = []
PAUSED_PROJECTS: list = []
AUTO_REPLAN_PROJECTS: list = []  # projects that auto-get a project_plan when they run out of tasks
TASK_SELECTION_STRATEGY: str = "priority"
WEBHOOK_URL: str = ""
MINIMAX_API_KEY: str = ""
MINIMAX_BASE_URL: str = constants.MINIMAX_BASE_URL

# Max agents to spawn per fill_slots call -- prevents burst spawning after a cooldown.
# Set low (e.g. 2-3) so slots fill gradually across monitor cycles rather than all at once.
SPAWN_PER_CYCLE: int = 3

# Auto-scaling: when enabled, the monitor adjusts MAX_ACTIVE_AGENTS dynamically based
# on observed 429 pressure, up to the configured ceiling (max_active_agents in config).
# MAX_ACTIVE_AGENTS becomes the live count; AUTO_SCALE_CEILING is the user's cap.
AUTO_SCALE: bool = False
AUTO_SCALE_CEILING: int = 60  # hard cap -- never exceed this regardless of 429 pressure
_auto_scale_floor: int = 1    # never drop below this

# Auto-scale state (managed by monitor thread)
_auto_scale_current: int = 3          # current dynamic max (starts at floor, ramps up)
_auto_scale_last_change: float = 0.0  # timestamp of last increment/decrement
_auto_scale_clean_cycles: int = 0     # consecutive cycles with zero 429s

_fill_slots_lock = threading.Lock()

# Auto-QA: track completed (non-QA) tasks per Godot project since last QA spawn
# Only used for projects NOT in AUTO_REPLAN_PROJECTS (sprint-based projects use sprint-boundary QA)
_qa_completion_counter: Dict[str, int] = {}

# Sprint cycle: tracks which auto_replan projects have completed QA since their last sprint.
# Sprint flow: queue empty → QA → bugs fixed → queue empty → planner → next sprint
_projects_sprint_qa_done: set = set()

# Auto-audit: track completed tasks per project since last audit spawn
_audit_completion_counter: Dict[str, int] = {}


# ---------------------------------------------------------------------------
# Webhook helper
# ---------------------------------------------------------------------------

def _fire_task_webhook(event: str, **kwargs):
    """Fire a task-level webhook event if a URL is configured."""
    if not WEBHOOK_URL:
        return
    import urllib.request, json as _json
    try:
        url = WEBHOOK_URL
        project = kwargs.get("project", "")
        desc    = kwargs.get("description", "")
        ttype   = kwargs.get("task_type", "")
        if event == "task_completed":
            diff   = kwargs.get("diff_stat", "")
            title  = f"✅ Task completed -- {project}"
            body_s = f"{ttype}: {desc}" + (f"\n`{diff.splitlines()[-1]}`" if diff else "")
            color  = 0x3fb950
            ntfy_tag = "white_check_mark"
        else:  # task_failed
            attempts = kwargs.get("attempts", 0)
            max_att  = kwargs.get("max_attempts", 3)
            title  = f"❌ Task failed -- {project}"
            body_s = f"{ttype}: {desc} (attempt {attempts}/{max_att})"
            color  = 0xf85149
            ntfy_tag = "x"

        if "discord.com/api/webhooks" in url:
            body = _json.dumps({"embeds": [{"title": title, "description": body_s, "color": color}]}).encode()
            headers = {"Content-Type": "application/json"}
        elif "hooks.slack.com" in url:
            body = _json.dumps({"text": f"*{title}*\n{body_s}"}).encode()
            headers = {"Content-Type": "application/json"}
        elif "ntfy.sh" in url:
            body = body_s.encode()
            headers = {"Content-Type": "text/plain", "Title": title, "Tags": ntfy_tag}
        else:
            body = _json.dumps({"event": event, "title": title, "summary": body_s, **kwargs}).encode()
            headers = {"Content-Type": "application/json"}

        headers["User-Agent"] = "Mozilla/5.0 (compatible; SwarmController/1.0)"
        req = urllib.request.Request(url, data=body, headers=headers)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[Webhook] Failed to send {event}: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Quota checking
# ---------------------------------------------------------------------------

def check_quota_limit() -> Tuple[bool, float, float, int, int]:
    """
    Returns (over_limit, pct_used, pct_remaining, used_count, total).
    Never raises; returns safe defaults on error.
    """
    if LLM_PROVIDER != "minimax":
        return False, 0.0, 100.0, 0, 4500
    if not MINIMAX_API_KEY:
        return False, 0.0, 100.0, 0, 4500
    try:
        import requests
        resp = requests.get(
            "https://www.minimax.io/v1/api/openplatform/coding_plan/remains",
            headers={"Authorization": f"Bearer {MINIMAX_API_KEY}"},
            timeout=10,
            verify=False,  # macOS SSL cert chain issue; this is a read-only monitoring call
        )
        if resp.status_code == 200:
            data = resp.json()
            model_remains = data.get("model_remains", [])
            if model_remains:
                total = model_remains[0].get("current_interval_total_count", 4500)
                remaining = model_remains[0].get("current_interval_usage_count", 0)
                used = total - remaining
                pct_used = (used / total * 100) if total > 0 else 0.0
                pct_remaining = 100.0 - pct_used
            else:
                pct_used, pct_remaining, used, total = 0.0, 100.0, 0, 4500
            return pct_used >= QUOTA_LIMIT_PERCENT, pct_used, pct_remaining, used, total
    except Exception as e:
        print(f"[Quota] check failed: {e}")
    return False, 0.0, 100.0, 0, 4500


def check_rate_limit_flags() -> Optional[str]:
    """
    Check for rate-limit flag files written by agent subprocesses.
    Returns the name of the provider that was rate-limited, or None.
    Deletes the flag after reading it.
    """
    try:
        for flag in DATA_DIR.glob("rate_limited_*.flag"):
            provider = flag.stem.replace("rate_limited_", "")
            flag.unlink(missing_ok=True)
            return provider
    except Exception:
        pass
    return None


def rotate_provider(rate_limited_provider: str, llm_providers: dict) -> Optional[str]:
    """
    Pick the next available provider from FALLBACK_PROVIDERS that isn't rate-limited.
    Returns the new provider name, or None if no fallback available.
    """
    if not FALLBACK_PROVIDERS:
        return None
    candidates = [p for p in FALLBACK_PROVIDERS if p != rate_limited_provider and p in llm_providers]
    if not candidates:
        return None
    # Prefer providers whose key is actually set
    for p in candidates:
        key_env = llm_providers[p].get("api_key_env", "")
        import os
        if key_env and os.environ.get(key_env):
            return p
    return candidates[0]


def auto_scale_step(recent_429_count: int) -> None:
    """
    Adjust MAX_ACTIVE_AGENTS dynamically based on observed 429 pressure.
    Called once per monitor cycle when AUTO_SCALE is enabled.

    Strategy:
    - Any 429s in the last 2 min → decrement by 1 (min cooldown: 60s between decrements)
    - Zero 429s for 3 consecutive cycles → increment by 1 (min cooldown: 120s between increments)
    - Never go below _auto_scale_floor or above AUTO_SCALE_CEILING
    """
    global MAX_ACTIVE_AGENTS, _auto_scale_current, _auto_scale_last_change, _auto_scale_clean_cycles

    if not AUTO_SCALE:
        return

    now = time.time()
    ceiling = min(AUTO_SCALE_CEILING, MAX_ACTIVE_AGENTS if not AUTO_SCALE else AUTO_SCALE_CEILING)

    if recent_429_count > 0:
        _auto_scale_clean_cycles = 0
        # Decrement: at most once per 60s to avoid thrashing
        if now - _auto_scale_last_change >= 60 and _auto_scale_current > _auto_scale_floor:
            _auto_scale_current -= 1
            _auto_scale_last_change = now
            MAX_ACTIVE_AGENTS = _auto_scale_current
            print(f"[AutoScale] {recent_429_count} 429s -- reducing to {_auto_scale_current} agents")
    else:
        _auto_scale_clean_cycles += 1
        # Increment: only after 3 clean cycles AND at least 120s since last change
        if (_auto_scale_clean_cycles >= 3
                and now - _auto_scale_last_change >= 120
                and _auto_scale_current < ceiling):
            _auto_scale_current += 1
            _auto_scale_last_change = now
            MAX_ACTIVE_AGENTS = _auto_scale_current
            _auto_scale_clean_cycles = 0
            print(f"[AutoScale] Clean -- increasing to {_auto_scale_current} agents (ceiling={ceiling})")


# ---------------------------------------------------------------------------
# Fill slots (core auto-mode loop)
# ---------------------------------------------------------------------------

def _check_llm_connectivity() -> bool:
    """TCP-level reachability check for the active LLM provider. No quota consumed."""
    try:
        import swarm_runner as _runner
        provider_name = getattr(_runner, "LLM_PROVIDER", "minimax")
        providers = getattr(_runner, "LLM_PROVIDERS", {})
        base_url = (providers.get(provider_name) or {}).get("base_url", "https://api.minimax.io")
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        host = parsed.hostname or "api.minimax.io"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=5):
            return True
    except Exception:
        return False



# Configure agent_lifecycle module with our settings
agent_lifecycle.configure(
    workspace=WORKSPACE,
    data_dir=DATA_DIR,
    use_worktrees=USE_WORKTREES,
    webhook_url=WEBHOOK_URL,
    auto_replan_projects=AUTO_REPLAN_PROJECTS,
    paused_projects=PAUSED_PROJECTS,
    max_active_agents=MAX_ACTIVE_AGENTS,
    agent_timeout=AGENT_TIMEOUT,
    qa_auto_threshold=QA_AUTO_THRESHOLD,
    audit_auto_threshold=AUDIT_AUTO_THRESHOLD,
)

def fill_slots(generate_script_fn, max_spawn: Optional[int] = None) -> Tuple[List[str], List[str]]:
    """
    Spawn agents until MAX_ACTIVE_AGENTS is reached or no tasks remain.
    Returns (spawned_ids, skipped_projects).
    A lock prevents concurrent fill_slots calls from picking the same task.
    """
    if not _check_llm_connectivity():
        print("[Swarm] LLM endpoint unreachable -- skipping fill_slots (will retry next cycle)")
        return [], []

    with _fill_slots_lock:
        spawned: List[str] = []
        skipped: List[str] = []
        limit = max_spawn if max_spawn is not None else 9999

        # Sprint cycle for auto_replan projects:
        #   queue empty → QA → bugs fixed → queue empty → planner → next sprint
        _plan_candidates = set(AUTO_REPLAN_PROJECTS) - set(PAUSED_PROJECTS)
        if _plan_candidates:
            all_tasks = db.task_get_all()
            projects_with_tasks = {t["project"] for t in all_tasks}
            for proj in _plan_candidates:
                if proj in projects_with_tasks:
                    continue  # still has work to do
                proj_path = WORKSPACE / proj
                if not (proj_path / "project.godot").exists():
                    continue
                if not (proj_path / "GAME_DESIGN.md").exists():
                    continue

                if proj not in _projects_sprint_qa_done:
                    # Queue empty, QA hasn't run yet -- spawn QA first
                    has_harness = (proj_path / "autoload" / "test_harness.gd").exists()
                    qa_type = "harness_qa" if has_harness else "qa"
                    qa_id = f"qa-sprint-{proj}-{int(time.time())}"
                    db.task_upsert({
                        "id": qa_id,
                        "project": proj,
                        "type": qa_type,
                        "description": (
                            "Sprint QA: run synchronous checkpoint tests against the game logic "
                            "to verify the completed sprint before planning the next one."
                            if has_harness else
                            "Sprint QA: playtest and verify the completed sprint is functional "
                            "before planning the next one."
                        ),
                        "priority": 90,
                        "status": "pending",
                        "dependencies": chain_to_project_head(db, proj, task_id=qa_id, ensure_head=True),
                        "metadata": {"sprint_qa": True},
                        "attempts": 0,
                        "max_attempts": 2,
                    })
                    print(f"[Swarm] Sprint complete for {proj} -- spawned QA before next sprint plan")
                else:
                    # QA done and any bugs fixed -- spawn the next sprint planner
                    _projects_sprint_qa_done.discard(proj)
                    plan_id = f"project-plan-{proj}-{int(time.time())}"
                    plan_deps = chain_to_project_head(db, proj, task_id=plan_id, ensure_head=True)
                    db.task_upsert({
                        "id": plan_id,
                        "project": proj,
                        "type": "project_plan",
                        "description": (
                            f"Plan the next sprint for {proj}. Read GAME_DESIGN.md and the "
                            f"current codebase state, identify the most valuable next sprint "
                            f"goal, and create 5-8 focused tasks with correct dependencies."
                        ),
                        "priority": 100,
                        "status": "pending",
                        "dependencies": plan_deps,
                        "metadata": {},
                        "attempts": 0,
                        "max_attempts": 2,
                    })
                    print(f"[Swarm] Sprint QA passed for {proj} -- spawned next sprint planner")

        # Cap per-cycle spawning to avoid burst after cooldown expiry.
        cycle_limit = min(limit, SPAWN_PER_CYCLE)
        while len(spawned) < cycle_limit:
            if get_active_count() >= MAX_ACTIVE_AGENTS:
                break

            task = _get_next_task()
            if not task:
                break

            project = task["project"]
            if LOCK_PROJECT:
                db.project_set_locked(project, True)

            agent_id = spawn_agent(task, generate_script_fn)
            if agent_id:
                spawned.append(agent_id)
            else:
                if LOCK_PROJECT:
                    db.project_set_locked(project, False)
                skipped.append(project)
                break

        if not spawned and get_active_count() == 0:
            _run_idle_closure_verification_cycle()
        return spawned, skipped


def _run_idle_closure_verification_cycle() -> list[str]:
    """Opportunistically verify projects when the controller is otherwise idle.

    This is intentionally conservative: it only touches managed, unpaused projects
    that either have never been verified or still have closure pressure
    (regressions / non-green status). The closure run guard layer is responsible
    for suppressing duplicate or over-frequent executions.
    """
    triggered: list[str] = []
    projects = db.project_get_all()
    for project_name, project_row in projects.items():
        if project_name in PAUSED_PROJECTS:
            continue
        if not project_row.get("managed", True):
            continue
        if project_row.get("locked"):
            continue
        if (
            project_row.get("last_verification_at")
            and int(project_row.get("open_regression_count") or 0) <= 0
            and project_row.get("closure_status") == "green"
        ):
            continue
        try:
            run = _validation.run_closure_verification(project_name, run_type="periodic")
        except Exception as exc:
            print(f"[Swarm] Idle closure verification failed for {project_name}: {exc}")
            continue
        if run is not None:
            triggered.append(project_name)
    return triggered


def _get_next_task() -> Optional[Dict]:
    """Select the next pending task with met dependencies, using TASK_SELECTION_STRATEGY."""
    db.backfill_completed_task_ids()
    all_tasks = db.task_get_all()
    pending = [t for t in all_tasks if t["status"] == "pending"]
    # Include permanently-recorded completed IDs so pruned tasks don't block deps
    completed_ids = db.task_get_completed_ids()
    completed_ids |= {t["id"] for t in all_tasks if t["status"] == "completed"}
    # Active task IDs -- deps not in this set are gone (failed+pruned) and should not block
    active_ids = {t["id"] for t in all_tasks}

    paused = set(PAUSED_PROJECTS)

    # Only one vision QA task may run globally at a time -- local mlx-vlm can't
    # handle concurrent vision inference. harness_qa has no vision dependency so
    # it is not restricted. Port collisions are handled by dynamic allocation.
    qa_active = any(
        t["status"] == "in_progress" and t.get("type") == "qa"
        for t in all_tasks
    )

    # Collect worktree paths currently in use by active agents -- tasks that inherit
    # the same worktree must wait until the current occupant finishes.
    active_worktrees = {
        d["worktree_path"]
        for d in _get_active_handles().values()
        if d.get("worktree_path")
    }

    now_iso = datetime.now().isoformat()
    ready = []
    project_rows: Dict[str, Dict[str, Any]] = {}
    for t in pending:
        if t.get("type") == "phase_gate":
            continue
        # Guard against malformed rows (for example pending tasks carrying a stale
        # completed timestamp or agent_id after a restart). These should be repaired
        # by startup reconciliation, but they should never poison ready-task
        # selection if one slips through.
        if not can_task_accept_agent(t):
            continue
        proj = t["project"]
        if proj in paused:
            continue
        project_row = project_rows.get(proj)
        if project_row is None and proj:
            project_row = db.project_get(proj)
            if project_row is not None:
                project_rows[proj] = project_row
        if project_row and not project_row.get("managed", True) and t.get("type") not in ("manager", "project_create"):
            continue
        # Block all QA picks while any QA task is running
        if t.get("type") == "qa" and qa_active:
            continue
        # Skip tasks scheduled for the future
        run_after = t.get("run_after")
        if run_after and run_after > now_iso:
            continue
        locked = db.project_get(proj)
        if locked and locked.get("locked"):
            continue
        # Skip tasks whose inherited worktree is already occupied by another active agent
        task_wt = (t.get("metadata") or {}).get("worktree_path")
        if task_wt and task_wt in active_worktrees:
            continue
        deps = t.get("dependencies", [])
        # A dep is met if completed, OR if it no longer exists (failed+pruned -- chain self-heals)
        if all(d in completed_ids or d not in active_ids for d in deps):
            ready.append(t)

    if not ready:
        return None

    _sort_by_strategy(ready, project_rows=project_rows)
    if not ready:
        return None

    # Block expansion tasks when project is frozen/stalled with open regressions.
    # The sort puts repair tasks first and expansion tasks last within each project.
    # If the top task is an expansion-blocked task but non-blocked alternatives exist,
    # pick the first non-blocked task. If ALL ready tasks are blocked, allow the
    # top task through to avoid deadlock (the closure policy "no deadlock" rule).
    if _is_expansion_blocked(ready[0], project_rows):
        non_blocked = [t for t in ready if not _is_expansion_blocked(t, project_rows)]
        if non_blocked:
            ready[:] = non_blocked
            return ready[0]
        return None  # All tasks are expansion-blocked -- no safe alternative

    return ready[0]


# Task type priority used by refactor_first strategy
_TYPE_PRIORITY = {"refactor": 0, "bug": 1, "feature": 2, "polish": 3}


def _is_expansion_blocked(t: Dict, project_rows: Dict[str, Dict[str, Any]]) -> bool:
    """Return True if task is a feature/polish/refactor expansion blocked by frozen/stalled closure.

    Frozen/stalled projects with open regressions block expansion tasks unless:
    - The task is itself a recovery or repair task
    - There are zero open regressions (nothing concrete to repair)
    """
    project_row = project_rows.get(t.get("project") or "")
    if not project_row:
        return False
    closure_status = (project_row.get("closure_status") or "").strip().lower()
    if closure_status not in {"frozen", "stalled"}:
        return False
    open_regressions = int(project_row.get("open_regression_count") or 0)
    if open_regressions == 0:
        return False
    metadata = t.get("metadata") or {}
    if metadata.get("is_recovery_task") or metadata.get("is_closure_repair_task"):
        return False
    task_type = (t.get("type") or "").strip().lower()
    return task_type in {"feature", "polish", "refactor"}


def _sort_by_strategy(tasks: List[Dict], *, project_rows: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    """Sort tasks in-place according to TASK_SELECTION_STRATEGY.
    Conflict-resolution tasks (priority >= 200) always sort first regardless of strategy."""
    project_rows = project_rows or {}

    # Conflict resolution tasks jump to the front in all strategies
    def _conflict_first(t: Dict) -> int:
        return 0 if t.get("priority", 50) >= 200 else 1

    def _complexity_adjusted_priority(t: Dict) -> int:
        """Return effective priority with complexity adjustment.
        complex tasks get +5 so they're picked slightly earlier (more buffer time).
        simple tasks get -5 so they're deferred in favour of more demanding peers."""
        base = t.get("priority", 50)
        complexity = (t.get("metadata") or {}).get("complexity", "")
        if complexity == "complex":
            return base + 5
        if complexity == "simple":
            return base - 5
        return base

    def _closure_policy_rank(t: Dict) -> tuple[int, int]:
        project_row = project_rows.get(t.get("project") or "")
        if not project_row:
            return (1, 1)

        closure_status = (project_row.get("closure_status") or "").strip().lower()
        open_regressions = int(project_row.get("open_regression_count") or 0)
        unhealthy = closure_status in {"yellow", "red", "frozen", "stalled"} and open_regressions > 0
        if not unhealthy:
            return (1, 1)

        metadata = t.get("metadata") or {}
        task_type = (t.get("type") or "").strip().lower()
        is_closure_repair = bool(metadata.get("is_closure_repair_task"))
        is_repair_task = is_closure_repair or task_type in {"bug", "triage"}
        is_expansion_task = task_type in {"feature", "polish", "refactor"}
        if is_repair_task:
            return (0, 0)
        if is_expansion_task:
            return (2, 2)
        return (1, 1)

    def _expansion_blocked(t: Dict) -> bool:
        return _is_expansion_blocked(t, project_rows)

    if TASK_SELECTION_STRATEGY == "refactor_first":
        tasks.sort(key=lambda t: (
            _conflict_first(t),
            _closure_policy_rank(t),
            _TYPE_PRIORITY.get(t.get("type", ""), 99),
            -_complexity_adjusted_priority(t),
            t.get("created", ""),
        ))
    elif TASK_SELECTION_STRATEGY == "round_robin":
        tasks.sort(key=lambda t: (_conflict_first(t), _closure_policy_rank(t), t.get("project", ""), -_complexity_adjusted_priority(t)))
    elif TASK_SELECTION_STRATEGY == "least_recently_worked":
        tasks.sort(key=lambda t: (_conflict_first(t), _closure_policy_rank(t), t.get("created", ""), -_complexity_adjusted_priority(t)))
    else:  # "priority" (default) and "dependency_aware"
        tasks.sort(key=lambda t: (_conflict_first(t), _closure_policy_rank(t), -_complexity_adjusted_priority(t), t.get("created", "")))
    # Only filter out expansion tasks when non-blocked alternatives exist.
    # If ALL ready tasks are expansion-blocked the closure policy would create a
    # deadlock (bug tasks depend on blocked feature tasks → nothing can run).
    # In that case allow expansion tasks through so the chain can make progress.
    non_blocked = [task for task in tasks if not _expansion_blocked(task)]
    if non_blocked:
        tasks[:] = non_blocked
    # else: leave tasks as-is to avoid deadlock


# ---------------------------------------------------------------------------
# Auto-setup helpers
# ---------------------------------------------------------------------------

def _maybe_spawn_gut_setup(project_name: str, project_path: Path):
    """If this is a Godot project without GUT installed, spawn a setup task (once)."""
    if not (project_path / "project.godot").exists():
        return  # not a Godot project
    if (project_path / "addons" / "gut").exists():
        return  # GUT already installed

    task_id = f"setup-gut-{project_name}"
    if db.task_get(task_id):
        return  # already queued or done

    controller_root = Path(__file__).resolve().parent.parent
    install_instruction = (
        f"Run the controller cache installer from the swarm-controller repo:\n"
        f"  run_command(\"cd {str(controller_root)!r} && python3 - <<'PY'\\n"
        f"from pathlib import Path\\n"
        f"from swarm.godot_bootstrap import install_gut_into_project\\n"
        f"install_gut_into_project(Path({str(project_path)!r}))\\n"
        f"print('GUT installed')\\n"
        f"PY\")\n"
        f"  # This will download GUT {GUT_VERSION} once into the local cache if needed,\n"
        f"  # then copy it into {project_path}/addons/gut.\n"
    )

    task = {
        "id": task_id,
        "project": project_name,
        "type": "feature",
        "priority": 90,
        "description": (
            f"Install GUT (Godot Unit Testing) addon into {project_name}.\n\n"
            f"STEPS:\n"
            f"1. Create the addons directory:\n"
            f"   run_command(\"mkdir -p {project_path}/addons\")\n"
            f"2. {install_instruction}"
            f"3. Enable the GUT plugin in project.godot -- add these lines if not present:\n"
            f"   [editor_plugins]\n"
            f"   enabled=PackedStringArray(\"res://addons/gut/plugin.cfg\")\n"
            f"   Write the updated project.godot using write_file.\n"
            f"4. Create the tests/ directory with a placeholder:\n"
            f"   write_file(\"{project_path}/tests/.gitkeep\", \"\")\n"
            f"5. Run the script parse check to confirm GUT loads cleanly:\n"
            f"   (write and run _swarm_check.gd as per standard validation)\n"
            f"6. Commit and push: git_commit(\"Add GUT testing addon and tests/ directory\")"
        ),
        "status": "pending",
        "metadata": {"auto_setup": "gut"},
    }
    db.task_upsert(task)
    print(f"[Swarm] Auto-queued GUT setup task for {project_name}")


# ---------------------------------------------------------------------------
# Project registry update (scan file sizes + git commits)
# ---------------------------------------------------------------------------

def update_project_registry(file_extensions=(".gd",)):
    """Scan managed projects and refresh db."""
    for project_name in MANAGED_PROJECTS:
        project_path = WORKSPACE / project_name
        if not project_path.exists():
            continue

        # Upsert project record if missing
        if db.project_get(project_name) is None:
            db.project_upsert({"name": project_name, "status": "active"})

        # Scan files
        files = {}
        for ext in file_extensions:
            for fp in project_path.rglob(f"*{ext}"):
                if any(ig in fp.parts for ig in IGNORE_DIRS):
                    continue
                try:
                    lines = len(fp.read_text().splitlines())
                    files[str(fp.relative_to(project_path))] = lines
                except Exception:
                    pass

        # Git commits
        try:
            result = subprocess.run(
                ["git", "log", "--pretty=format:%h|%s|%ar", "-5"],
                cwd=str(project_path), capture_output=True, text=True, timeout=5,
            )
            commits = []
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    parts = line.split("|", 2)
                    if len(parts) == 3:
                        commits.append({"hash": parts[0], "message": parts[1], "age": parts[2]})
        except Exception:
            commits = []

        conn = db._connect()
        conn.execute(
            """UPDATE projects SET files=?, last_update=?, recent_commits=?,
               last_commit=?, last_commit_msg=? WHERE name=?""",
            (
                json.dumps(files),
                datetime.now().isoformat(),
                json.dumps(commits),
                commits[0]["hash"] if commits else None,
                commits[0]["message"] if commits else None,
                project_name,
            ),
        )
        conn.commit()

        # Auto-setup: spawn GUT install task for Godot projects missing it
        _maybe_spawn_gut_setup(project_name, project_path)


# ---------------------------------------------------------------------------
# Re-export worktree functions for backward compatibility
# ---------------------------------------------------------------------------

cleanup_orphaned_worktrees = worktree.cleanup_orphaned_worktrees
check_ghost_merge_tasks = worktree.check_ghost_merge_tasks

# Re-export validation functions for backward compatibility
_post_task_validation = _validation._post_task_validation
_post_task_validation_in_worktree = _validation._post_task_validation_in_worktree
_spawn_validation_bug_task = _validation._spawn_validation_bug_task
_llm_summarise_fix_attempt = _validation._llm_summarise_fix_attempt


# ---------------------------------------------------------------------------
# Re-export agent lifecycle functions for backward compatibility
# ---------------------------------------------------------------------------

spawn_agent = agent_lifecycle.spawn_agent
check_agent_status = agent_lifecycle.check_agent_status
reconcile_agent_runtime_state = agent_lifecycle.reconcile_agent_runtime_state
cleanup_recovery_branches = agent_lifecycle.cleanup_recovery_branches
get_active_count = agent_lifecycle.get_active_count
prune_history = agent_lifecycle.prune_history
check_dep_violations = agent_lifecycle.check_dep_violations
_get_active_handles = agent_lifecycle.get_active_handles

# Also expose internal functions that tests may call
_finish_agent = agent_lifecycle._finish_agent
_spawn_review_task = agent_lifecycle._spawn_review_task
_handle_task_failure = agent_lifecycle._handle_task_failure

# Backward compatibility: expose _active_handles and _handle_lock for tests
_active_handles = agent_lifecycle._active_handles
_handle_lock = agent_lifecycle._handle_lock
