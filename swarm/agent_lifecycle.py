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
import subprocess
import sys
import time
import uuid
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from swarm.integrity import active_agent_matches_task, can_task_accept_agent, is_continuity_eligible_task
from swarm.platform import popen_session_kwargs, kill_godot_children
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
    _task_history_lookup,
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
    global MAX_ACTIVE_AGENTS, AGENT_TIMEOUT, QA_AUTO_THRESHOLD
    global _project_registry

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
    """Fire a task-level webhook event if a URL is configured."""
    if not WEBHOOK_URL:
        return
    import urllib.request
    import json as _json
    try:
        url = WEBHOOK_URL
        project = kwargs.get("project", "")
        desc = kwargs.get("description", "")
        ttype = kwargs.get("task_type", "")
        if event == "task_completed":
            diff = kwargs.get("diff_stat", "")
            title = f"\u2705 Task completed \u2014 {project}"
            body_s = f"{ttype}: {desc}" + (f"\n`{diff.splitlines()[-1]}`" if diff else "")
            color = 0x3fb950
            ntfy_tag = "white_check_mark"
        else:  # task_failed
            attempts = kwargs.get("attempts", 0)
            max_att = kwargs.get("max_attempts", 3)
            title = f"\u274c Task failed \u2014 {project}"
            body_s = f"{ttype}: {desc} (attempt {attempts}/{max_att})"
            color = 0xf85149
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
    _BASELINE_SKIP_TYPES = {"manager", "project_create", "qa", "research",
                            "harness_qa", "hybrid_qa", "project_plan", "audit",
                            "triage", "art_pass", "scenario_qa"}
    _baseline_path = worktree_path if worktree_path is not None else (WORKSPACE / project)
    if task.get("id") and task.get("type") not in _BASELINE_SKIP_TYPES and _baseline_path.exists():
        try:
            _validation.capture_validation_baseline(project, task["id"], _baseline_path)
        except Exception as _blerr:
            print(f"[Swarm] WARNING: pre-flight baseline failed for {task['id'][:8]}: {_blerr}")

    script_content = generate_script_fn(task)
    script_path = _get_data_dir() / f"agent_{agent_id}.py"
    script_path.write_text(script_content, encoding="utf-8")
    os.chmod(script_path, 0o755)

    log_path = _get_data_dir() / f"agent_{agent_id}.log"

    try:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        log_file = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(script_path)],
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
        if worktree_path and worktree_branch:
            worktree._cleanup_worktree(WORKSPACE / project, worktree_path, worktree_branch)
        return None


# ---------------------------------------------------------------------------
# Agent status checking
# (Agent completion pipeline is in swarm/agent_finish.py)
# ---------------------------------------------------------------------------

def check_dep_violations():
    """Kill any running agent whose task dependencies are not yet satisfied.

    This catches the race where deps are patched after an agent was already
    spawned, or where fill_slots incorrectly picked a task before its deps
    completed.
    """
    _lazy_imports()

    # Backfill first so tasks that just completed are in the permanent record
    # before we decide to kill anything.  Prevents a race where _finish_agent()
    # writes "completed" to the tasks table but task_record_completed() hasn't
    # been called yet when this check runs.
    db.backfill_completed_task_ids()

    completed_ids = db.task_get_completed_ids()
    all_task_ids = {t["id"] for t in db.task_get_all()}

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
                kill_godot_children(process.pid)
                process.kill()
                process.wait(timeout=5)
            except Exception as _ke:
                print(f"[Swarm] Dep violation kill error: {_ke}")
        elif pid:
            try:
                kill_godot_children(pid)
                os.kill(pid, 9)
            except Exception as _ke:
                print(f"[Swarm] Dep violation PID kill error: {_ke}")
        try:
            db.task_update_status(task_id, "pending", agent_id=None)
            db.agent_update_status(agent_id, "failed",
                                   completed_at=datetime.now().isoformat(), exit_code=-1)
        except Exception as _de:
            print(f"[Swarm] Dep violation DB error: {_de}")
        with _handle_lock:
            _active_handles.pop(agent_id, None)

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


def check_agent_status() -> List[threading.Thread]:
    """Check all active agents; clean up finished and timed-out ones.

    Returns a list of daemon threads started for finishing work (validation,
    DB updates, etc.).  Callers that need synchronous completion (e.g. tests)
    can join these threads; the monitor loop discards them so it is never
    blocked by long-running Godot validation subprocesses.
    """
    _lazy_imports()

    now = time.time()
    with _handle_lock:
        finished = []
        timed_out = []
        for agent_id, data in list(_active_handles.items()):
            exit_code = data["process"].poll()
            if exit_code is not None:
                finished.append((agent_id, exit_code, data))
            elif AGENT_TIMEOUT > 0 and now - data["started"] > AGENT_TIMEOUT:
                timed_out.append((agent_id, data))

    finish_threads: List[threading.Thread] = []

    for agent_id, data in timed_out:
        print(f"[Swarm] Agent {agent_id[:8]} timed out after {AGENT_TIMEOUT}s \u2014 killing")
        try:
            kill_godot_children(data["process"].pid)
            data["process"].kill()
            data["process"].wait(timeout=5)
        except Exception:
            pass
        with _finishing_lock:
            _finishing_agents.add(agent_id)
        with _handle_lock:
            _active_handles.pop(agent_id, None)

        def _run_finish_timeout(aid=agent_id, d=data):
            try:
                _finish_agent(
                    aid, -1,
                    d.get("project"), d.get("task_id"),
                    d.get("script_path"), d.get("log_path"),
                )
            except Exception as e:
                print(f"[Swarm] Error finishing timed-out agent {aid[:8]}: {e}")
            finally:
                with _finishing_lock:
                    _finishing_agents.discard(aid)

        t = threading.Thread(target=_run_finish_timeout, daemon=True)
        t.start()
        finish_threads.append(t)

    for agent_id, exit_code, data in finished:
        # Kill any Godot game processes the agent may have launched but not cleaned
        # up (e.g. when the agent crashed before calling kill_game()).
        try:
            kill_godot_children(data["process"].pid)
        except Exception:
            pass
        # Remove from active handles immediately -- the process has exited, so the
        # concurrency slot is free.  _finish_agent() (which runs Godot validation
        # and can block for up to ~5 minutes) is offloaded to a daemon thread so
        # the monitor loop is never stalled waiting for subprocess completion.
        with _finishing_lock:
            _finishing_agents.add(agent_id)
        with _handle_lock:
            _active_handles.pop(agent_id, None)

        def _run_finish(aid=agent_id, ec=exit_code, d=data):
            try:
                _finish_agent(
                    aid, ec,
                    d.get("project"), d.get("task_id"),
                    d.get("script_path"), d.get("log_path"),
                )
            except Exception as e:
                print(f"[Swarm] Error finishing agent {aid[:8]}: {e}")
            finally:
                with _finishing_lock:
                    _finishing_agents.discard(aid)

        t = threading.Thread(target=_run_finish, daemon=True)
        t.start()
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

    persisted = set()
    for a in db.agent_get_active():
        aid = a["id"]
        if aid in in_process or aid in finishing:
            continue
        pid = a.get("pid")
        if pid and _is_pid_running(pid):
            persisted.add(aid)

    return len(in_process) + len(finishing) + len(persisted)


def prune_history():
    """Archive finished agents to JSONL and remove from DB.

    Completed/failed/cancelled tasks are kept in the tasks table permanently
    (immutable history).  They are still written to task-history.jsonl as a
    write-only export log, but are never deleted from the DB.  A
    ``metadata.archived`` flag prevents double-writing on repeated prune cycles.
    """
    _lazy_imports()

    # Use orchestrator's HISTORY_FILE if available, otherwise fall back
    try:
        import swarm.orchestrator as _orc
        HISTORY_FILE = getattr(_orc, 'HISTORY_FILE', _get_data_dir() / "agent-history.jsonl")
    except Exception:
        HISTORY_FILE = _get_data_dir() / "agent-history.jsonl"
    # --- Agent archival ---
    finished = [a for a in db.agent_get_all() if a.get("status") not in ("active", "spawning")]
    if finished:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_FILE.open("a") as f:
            for agent in finished:
                f.write(json.dumps(agent) + "\n")

    # --- Task archival (decoupled from agent archival) ---
    # Only archive tasks that haven't been written to JSONL yet.
    finished_tasks = [
        t for t in db.task_get_all()
        if t.get("status") in ("completed", "failed", "cancelled")
        and not (t.get("metadata") or {}).get("archived")
    ]
    if finished_tasks:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        task_history_file = HISTORY_FILE.parent / "task-history.jsonl"
        with task_history_file.open("a") as f:
            for task in finished_tasks:
                f.write(json.dumps(task) + "\n")
        # Mark tasks as archived so they aren't re-written next cycle.
        for task in finished_tasks:
            meta = dict(task.get("metadata") or {})
            meta["archived"] = True
            db.task_update(task["id"], {"metadata": meta})

    # Update each project's head_task_id to the most recent continuity-eligible
    # task, but do not overwrite a live continuation with a failed/cancelled tail.
    # Consider all terminal tasks (including previously-archived ones).
    all_terminal = [
        t for t in db.task_get_all()
        if t.get("status") in ("completed", "failed", "cancelled")
    ]
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
