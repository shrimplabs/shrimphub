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
import re
import subprocess
import sys
import time
import uuid
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from swarm.branch_intent import branch_intent_metadata, format_branch_intent
from swarm.integrity import active_agent_matches_task, can_task_accept_agent, is_continuity_eligible_task
from swarm.platform import popen_session_kwargs

# Lazy imports to avoid circular dependencies
db = None
worktree = None
_validation = None
_learnings = None
_plan_cleanup = None
_task_chains = None
_task_mutations = None
_project_registry = None


@dataclass(frozen=True)
class _AgentLogSnapshot:
    full_output: str
    tail_output: str


@dataclass(frozen=True)
class _WorktreeFinishResult:
    success: bool
    validation_failed: bool = False
    validation_error: str = ""


def _lazy_imports():
    """Lazy import helper to avoid circular deps."""
    global db, worktree, _validation, _learnings, _plan_cleanup, _task_chains, _task_mutations
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


# ---------------------------------------------------------------------------
# Module-level config (set by orchestrator.py)
# ---------------------------------------------------------------------------

WORKSPACE: Path = Path(".")

# DATA_DIR - dynamic lookup from orchestrator
def _get_data_dir() -> Path:
    try:
        import swarm.orchestrator as _orc
        return _orc.DATA_DIR
    except Exception:
        return Path("data")

class _DataDirProxy:
    """Proxy that forwards to orchestrator.DATA_DIR."""
    def __str__(self):
        return str(_get_data_dir())
    def __truediv__(self, other):
        return _get_data_dir() / other
    def __fspath__(self):
        return str(_get_data_dir())
    
DATA_DIR = _DataDirProxy()
USE_WORKTREES: bool = True
WEBHOOK_URL: str = ""
AUTO_REPLAN_PROJECTS: list = []
PAUSED_PROJECTS: list = []
# LOCK_PROJECT - dynamic lookup from orchestrator
def _get_lock_project() -> bool:
    try:
        import swarm.orchestrator as _orc
        return getattr(_orc, 'LOCK_PROJECT', False)
    except Exception:
        return False

class _LockProjectProxy:
    def __bool__(self):
        return _get_lock_project()
    def __eq__(self, other):
        return _get_lock_project() == other

LOCK_PROJECT = _LockProjectProxy()

# State: agent_id -> handle dict
_active_handles: Dict[str, Dict] = {}
_handle_lock = threading.Lock()

# Agent IDs for which _finish_agent is running in a daemon thread.
# Used to prevent reconcile_agent_runtime_state from double-processing them.
_finishing_agents: Set[str] = set()
_finishing_lock = threading.Lock()

# Auto-QA/Audit counters
_qa_completion_counter: Dict[str, int] = {}
_projects_sprint_qa_done: Set[str] = set()
_audit_completion_counter: Dict[str, int] = {}

# Config constants (set from constants module)
MAX_ACTIVE_AGENTS: int = 5
AGENT_TIMEOUT: float = 0
QA_AUTO_THRESHOLD: int = 10
AUDIT_AUTO_THRESHOLD: int = 15


def configure(
    workspace: Path = Path("."),
    data_dir: Path = Path("data"),
    use_worktrees: bool = True,
    webhook_url: str = "",
    auto_replan_projects: list = [],
    paused_projects: list = [],
    max_active_agents: int = 5,
    agent_timeout: float = 0,
    qa_auto_threshold: int = 10,
    audit_auto_threshold: int = 15,
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
        max_active_agents: Maximum number of concurrent agents.
        agent_timeout: Timeout in seconds for agent execution (0 = no timeout).
        qa_auto_threshold: Number of completed tasks before auto-spawning QA.
        audit_auto_threshold: Number of completed tasks before auto-spawning audit.
        project_registry: Project registry instance for releasing file locks on agent exit.
    """
    global WORKSPACE, DATA_DIR, USE_WORKTREES
    global WEBHOOK_URL, AUTO_REPLAN_PROJECTS, PAUSED_PROJECTS
    global MAX_ACTIVE_AGENTS, AGENT_TIMEOUT, QA_AUTO_THRESHOLD, AUDIT_AUTO_THRESHOLD
    global _project_registry

    WORKSPACE = workspace
    DATA_DIR = data_dir
    USE_WORKTREES = use_worktrees
    WEBHOOK_URL = webhook_url
    AUTO_REPLAN_PROJECTS = auto_replan_projects
    PAUSED_PROJECTS = paused_projects
    MAX_ACTIVE_AGENTS = max_active_agents
    AGENT_TIMEOUT = agent_timeout
    QA_AUTO_THRESHOLD = qa_auto_threshold
    AUDIT_AUTO_THRESHOLD = audit_auto_threshold
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
# Agent completion
# ---------------------------------------------------------------------------

def _resolve_agent_exit_code(script_path: Optional[str], exit_code: int) -> int:
    """Prefer the explicit exit file written by the agent runtime when present."""
    if not script_path:
        return exit_code
    exit_file = Path(script_path).with_suffix(".exit")
    if not exit_file.exists():
        return exit_code
    try:
        resolved = int(exit_file.read_text().strip())
        exit_file.unlink()
        return resolved
    except Exception:
        return exit_code


def _read_agent_log(log_path: Optional[str]) -> _AgentLogSnapshot:
    if not log_path or not Path(log_path).exists():
        return _AgentLogSnapshot(full_output="", tail_output="")
    try:
        full_output = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return _AgentLogSnapshot(full_output="", tail_output="")
    return _AgentLogSnapshot(full_output=full_output, tail_output=full_output[-2000:])


def _classify_agent_success(agent_id: str, exit_code: int, full_output: str) -> bool:
    if exit_code == 0:
        return True
    # TASK_COMPLETE may appear before the final tail in long-running agents,
    # but negative runtime messages also mention it, e.g.
    # "Task ended without TASK_COMPLETE". Require a standalone completion
    # marker or the explicit positive runtime completion log.
    has_standalone_complete = bool(re.search(r"(?m)^\s*TASK_COMPLETE\s*$", full_output or ""))
    has_runtime_complete = "[Agent] Task complete!" in (full_output or "")
    if has_standalone_complete or has_runtime_complete:
        print(f"[Swarm] Agent {agent_id[:8]} exited {exit_code} but success marker found -- treating as success")
        return True
    return False


def _active_worktree_handle(agent_id: str) -> tuple[Optional[str], Optional[str]]:
    with _handle_lock:
        handle = _active_handles.get(agent_id, {})
        return handle.get("worktree_path"), handle.get("worktree_branch")


def _finish_worktree_phase(agent_id: str, success: bool, project: Optional[str],
                           task_id: Optional[str], wt_path_str: Optional[str],
                           wt_branch: Optional[str]) -> _WorktreeFinishResult:
    """Validate/merge/cleanup the agent worktree and return the adjusted result."""
    if not (wt_path_str and wt_branch and project):
        return _WorktreeFinishResult(success=success)

    worktree_path = Path(wt_path_str)
    project_path = WORKSPACE / project
    if success:
        _skip_types = {"manager", "project_create", "qa", "research", "harness_qa", "hybrid_qa",
                       "project_plan", "audit", "triage", "art_pass"}
        _task_for_val_early = db.task_get(task_id) if task_id else None
        _task_type_early = _task_for_val_early.get("type", "") if _task_for_val_early else ""
        if _task_for_val_early and _task_type_early not in _skip_types:
            _val_failed, _val_err = _validation._post_task_validation_in_worktree(project, task_id, worktree_path)
            if _val_failed:
                print(f"[Swarm] Pre-merge validation FAILED for agent {agent_id[:8]} -- skipping merge, preserving worktree")
                return _WorktreeFinishResult(
                    success=False,
                    validation_failed=True,
                    validation_error=_val_err,
                )

        merged = worktree._merge_worktree_branch(
            project_path,
            wt_branch,
            agent_id,
            task_id,
            worktree_path=worktree_path,
        )
        if not merged:
            return _WorktreeFinishResult(success=False)
        worktree._cleanup_worktree(project_path, worktree_path, wt_branch)
        return _WorktreeFinishResult(success=True)

    # Agent failed before declaring completion; discard its isolated worktree.
    worktree._cleanup_worktree(project_path, worktree_path, wt_branch)
    return _WorktreeFinishResult(success=False)


def _capture_project_diff_stat(project: Optional[str]) -> str:
    if not project:
        return ""
    project_path = WORKSPACE / project
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD~1"],
            cwd=str(project_path), capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _read_agent_token_usage(task_id: Optional[str], agent_id: str) -> tuple[int, int]:
    input_tokens = 0
    output_tokens = 0
    token_key = task_id or agent_id
    token_file = Path(_get_data_dir()) / f"agent_{token_key}_tokens.json"
    if token_file.exists():
        try:
            tok = json.loads(token_file.read_text())
            input_tokens = tok.get("input", 0)
            output_tokens = tok.get("output", 0)
            token_file.unlink()
        except Exception:
            pass
    return input_tokens, output_tokens


def _mark_agent_finished(agent_id: str, success: bool, exit_code: int,
                         output: str, diff_stat: str, input_tokens: int,
                         output_tokens: int):
    agent_meta = {"diff_stat": diff_stat} if diff_stat else {}
    db.agent_update_status(
        agent_id,
        "completed" if success else "failed",
        completed_at=datetime.now().isoformat(),
        exit_code=exit_code,
        output=output[-1000:],
        metadata=json.dumps(agent_meta),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _cleanup_agent_script(script_path: Optional[str]):
    if script_path and Path(script_path).exists():
        try:
            Path(script_path).unlink()
        except Exception:
            pass


def _finish_agent(agent_id: str, exit_code: int, project: Optional[str],
                  task_id: Optional[str], script_path: Optional[str],
                  log_path: Optional[str]):
    """Teardown for a finished agent."""
    _lazy_imports()

    # Release any file locks held by this agent so other agents aren't blocked.
    # This is a safety net -- the agent should unlock files itself, but if it was
    # killed, timed out, or crashed the locks would otherwise linger until restart.
    if project and _project_registry is not None:
        try:
            freed = _project_registry.unlock_all_for_agent(project, agent_id)
            if freed:
                print(f"[Swarm] Released {len(freed)} file lock(s) for agent {agent_id[:8]}: {freed}")
        except Exception as _ul_err:
            print(f"[Swarm] WARNING: could not release file locks for agent {agent_id[:8]}: {_ul_err}")

    exit_code = _resolve_agent_exit_code(script_path, exit_code)
    log_snapshot = _read_agent_log(log_path)
    full_output = log_snapshot.full_output
    output = log_snapshot.tail_output
    success = _classify_agent_success(agent_id, exit_code, full_output)

    wt_path_str, wt_branch = _active_worktree_handle(agent_id)
    worktree_result = _finish_worktree_phase(
        agent_id,
        success,
        project,
        task_id,
        wt_path_str,
        wt_branch,
    )
    success = worktree_result.success
    _validation_failed_in_worktree = worktree_result.validation_failed
    _validation_error_output = worktree_result.validation_error

    diff_stat = _capture_project_diff_stat(project)
    input_tokens, output_tokens = _read_agent_token_usage(task_id, agent_id)
    _mark_agent_finished(agent_id, success, exit_code, output, diff_stat, input_tokens, output_tokens)
    _task_snapshot_pre_complete: Optional[dict] = None

    if task_id:
        # Snapshot the task now, before any status mutations or prune_history()
        # races can delete it from the DB.
        _task_snapshot_early = db.task_get(task_id)
        _spawned_continuation = False

        if success:
            # Reparent dependents if a continuation task was spawned.
            # The agent logs "Continuation task created: <id>" -- parse it and swap
            # the original task ID out of all downstream dependencies so the chain
            # doesn't unblock prematurely before the continuation finishes.
            _cont_id_match = re.search(r"Continuation task created: ([^\s(]+)", full_output)
            _spawned_continuation = bool(_cont_id_match)
            if _cont_id_match:
                _cont_id = _cont_id_match.group(1)
                _reparented = 0
                for _t in db.task_get_all():
                    _deps = _t.get("dependencies") or []
                    if task_id in _deps:
                        _new_deps = [_cont_id if d == task_id else d for d in _deps]
                        db.task_update(_t["id"], {"dependencies": _new_deps})
                        _reparented += 1
                if _reparented:
                    print(f"[Swarm] Reparented {_reparented} dependent(s) from {task_id} \u2192 continuation {_cont_id}")

            _task_snapshot_pre_complete = db.task_get(task_id)
            db.task_update_status(
                task_id, "completed",
                completed=datetime.now().isoformat(),
            )
            db.task_record_completed(task_id, project or "")
            # Cancel any stale recovery tasks that were spawned for this task
            # while it was retrying -- now that it succeeded they're redundant.
            _all = db.task_get_all()
            for _rt in _all:
                _meta = _rt.get("metadata") or {}
                if (_meta.get("is_recovery_task") and
                        _meta.get("failed_task_id") == task_id and
                        _rt.get("status") == "pending"):
                    db.task_update_status(_rt["id"], "failed")
                    print(f"[Swarm] Cancelled stale recovery task {_rt['id'][:8]} "
                          f"(original {task_id[:8]} completed successfully)")
            task = db.task_get(task_id)
            if task:
                # Create plan record when a project_plan task completes successfully
                if task.get("type") == "project_plan":
                    plan_id = f"plan-{project}-{int(time.time())}"
                    now_iso = datetime.now().isoformat()
                    tasks = _project_plan_subtasks(project, task_id)
                    _plan_cleanup.delete_plans_for_planner(db, project, task_id)
                    _plan_cleanup.delete_matching_plans(db, project, [t["id"] for t in tasks])
                    db.plan_upsert({
                        "id": plan_id,
                        "project": project,
                        "planner_task_id": task_id,
                        "created_at": now_iso,
                        "task_ids": [t["id"] for t in tasks],
                        "task_graph": tasks,
                    })
                    for t in tasks:
                        db.task_update(t["id"], {"plan_id": plan_id})
                    continuity_tasks = [t for t in tasks if is_continuity_eligible_task(t)]
                    if continuity_tasks:
                        continuity_tasks.sort(
                            key=lambda t: (
                                t.get("completed") or t.get("created") or t.get("started") or "",
                                t.get("id") or "",
                            )
                        )
                        try:
                            _task_chains.set_project_head(db, project, continuity_tasks[-1]["id"])
                        except Exception as _head_err:
                            print(f"[Swarm] WARNING: could not advance project head after planner {task_id}: {_head_err}")
                    print(f"[Swarm] Created plan {plan_id} for {project} "
                          f"({len(tasks)} task(s) assigned)")

                _fire_task_webhook(
                    "task_completed",
                    project=project or "",
                    task_id=task_id,
                    task_type=task.get("type", ""),
                    description=task.get("description", "").split("\n")[0][:100],
                    diff_stat=diff_stat,
                )
        else:
            # If validation failed in-worktree, spawn bug task with worktree metadata
            # before calling _handle_task_failure (which handles retry/recovery logic)
            if _validation_failed_in_worktree and project and wt_path_str and wt_branch:
                # Cross-check main before spawning a worktree bug task.
                # If main is already clean the worktree error is stale (another agent
                # already fixed it in main). Spawning a bug task in the diverged
                # worktree would create an infinite chain chasing a ghost -- skip it.
                _main_failed, _main_err = _validation._post_task_validation_in_worktree(
                    project, task_id, worktree_path=None
                )
                if _validation.is_controller_config_blocker(_main_err):
                    print(f"[Swarm] Validation blocked by controller configuration for {project}: {_main_err}")
                    _handle_task_failure(task_id, project, _main_err, _task_snapshot=_task_snapshot_early)
                    return
                if not _main_failed:
                    print(f"[Swarm] Worktree validation failed for {task_id} but main is clean "
                          f"-- skipping bug task (stale worktree error)")
                    db.task_update_status(task_id, "completed", completed=datetime.now().isoformat())
                    db.task_record_completed(task_id, project or "")
                else:
                    # Main also has errors -- this is a real regression, spawn the bug task.
                    _current_task = db.task_get(task_id)
                    _task_meta = (_current_task.get("metadata") or {}) if _current_task else {}
                    _existing_notes = _task_meta.get("fix_notes", [])
                    _error_before = _task_meta.get("error_log", "")
                    _summary = _validation._llm_summarise_fix_attempt(
                        _error_before, _validation_error_output, Path(wt_path_str)
                    )
                    _fix_notes = _existing_notes + ([_summary] if _summary else [])
                    _validation._spawn_validation_bug_task(
                        project, task_id, _validation_error_output,
                        worktree_path=Path(wt_path_str),
                        worktree_branch=wt_branch,
                        fix_notes=_fix_notes,
                        original_task=_current_task,
                    )
                    # The bug task now owns the remaining work -- mark the original as
                    # completed so it doesn't retry and race against the bug task.
                    db.task_update_status(task_id, "completed", completed=datetime.now().isoformat())
                    db.task_record_completed(task_id, project or "")
                    print(f"[Swarm] Marked {task_id} completed (validation bug task takes over)")
            else:
                _handle_task_failure(task_id, project, full_output or output,
                                     _task_snapshot=_task_snapshot_early)

    if project and LOCK_PROJECT:
        db.project_set_locked(project, False)

    if script_path and Path(script_path).exists():
        try:
            Path(script_path).unlink()
        except Exception:
            pass

    print(f"[Swarm] Agent {agent_id[:8]} finished (exit {exit_code})"
          + (f" diff: {diff_stat.splitlines()[-1]}" if diff_stat else ""))

    # Extract learnings async (fire and forget -- doesn't block reaping)
    _task_for_learnings = db.task_get(task_id) if task_id else None
    _task_type_for_learnings = _task_for_learnings.get("type", "") if _task_for_learnings else ""
    if log_path and project and _task_type_for_learnings:
        _learnings.extract_learnings_async(
            task_id, _task_type_for_learnings, project, log_path, exit_code, str(DATA_DIR)
        )

    if success and task_id and project:
        # Sanity-check project_plan: it must create tagged subtasks and all dependencies
        # must resolve to real task IDs (never file paths).
        _task_for_san = db.task_get(task_id)
        if _task_for_san and _task_for_san.get("type") == "project_plan":
            _plan_errors = _validate_project_plan_subtasks(project, task_id)
            if _plan_errors:
                print(f"[Swarm] project_plan {task_id} invalid \u2014 treating as failure")
                for _err in _plan_errors[:10]:
                    print(f"[Swarm]   plan error: {_err}")
                _subs = _project_plan_subtasks(project, task_id)
                for _sub in _subs:
                    if _sub.get("status") == "pending":
                        db.task_update_status(_sub["id"], "cancelled")
                _deleted_for_planner = _plan_cleanup.delete_plans_for_planner(db, project, task_id)
                _deleted = _plan_cleanup.delete_matching_plans(db, project, [t["id"] for t in _subs])
                _all_deleted = list(dict.fromkeys(_deleted_for_planner + _deleted))
                if _all_deleted:
                    print(f"[Swarm] Deleted stale plan snapshot(s): {_all_deleted}")
                attempts = (_task_for_san.get("attempts") or 0) + 1
                max_att = _task_for_san.get("max_attempts", 3)
                db.task_update_status(task_id, "pending" if attempts < max_att else "failed", attempts=attempts)
                success = False

        # Skip post-validation for task types that don't produce code artifacts.
        # Note: for worktree-based tasks, validation was already run pre-merge above.
        # For non-worktree tasks (e.g. USE_WORKTREES=False), run it synchronously
        # here so follow-on integration/QA tasks cannot be scheduled from broken work.
        _skip_types = {"manager", "project_create", "qa", "research", "harness_qa", "hybrid_qa",
                       "project_plan", "audit", "art_pass"}
        # Re-fetch the task, but fall back to the pre-completion snapshot if the
        # monitor thread has already pruned it from the DB (race condition).
        _task_for_val = db.task_get(task_id) or _task_snapshot_pre_complete
        task_type_finished = _task_for_val.get("type", "") if _task_for_val else ""
        validation_failed_after_completion = False
        if not (_task_for_val and task_type_finished in _skip_types) and not _spawned_continuation:
            if not wt_path_str:
                _task_snapshot_for_validation = dict(_task_for_val) if _task_for_val else None
                _main_failed, _main_err = _validation._post_task_validation_in_worktree(
                    project,
                    task_id,
                    worktree_path=None,
                )
                if _main_failed and _main_err:
                    validation_failed_after_completion = True
                    if _validation.is_controller_config_blocker(_main_err):
                        print(f"[Swarm] Post-validation blocked by controller configuration for {project}; not spawning validation bug")
                        _handle_task_failure(task_id, project, _main_err, _task_snapshot=_task_snapshot_early)
                    else:
                        print(f"[Swarm] Post-validation FAILED for {project} {task_id} -- spawning validation bug before follow-on work")
                        _validation._spawn_validation_bug_task(
                            project,
                            task_id,
                            _main_err,
                            original_task=_task_snapshot_for_validation,
                        )
                else:
                    try:
                        _validation.run_closure_verification(project, task_id)
                    except Exception as _closure_err:
                        print(f"[Swarm] Closure verification trigger failed for {project} {task_id}: {_closure_err}")

        # Auto-integration: after a feature/polish on a Godot project, spawn a lightweight
        # task to wire the new system into the existing game (signals, autoloads, scene tree).
        # Chained as a dependency of nothing -- runs immediately in parallel with other work.
        # Gated on is_integration_task metadata to prevent infinite chains.
        _integration_skip = {"qa", "audit", "manager", "project_create", "project_plan", "art_pass"}
        if (not validation_failed_after_completion
                and task_type_finished in ("feature", "polish")
                and _task_for_val
                and not _task_for_val.get("metadata", {}).get("is_integration_task")):
            project_path = WORKSPACE / project
            if (project_path / "project.godot").exists():
                existing_tasks = db.task_get_by_project(project)
                has_integration = any(
                    t.get("metadata", {}).get("is_integration_task")
                    and t.get("status") in ("pending", "in_progress")
                    for t in existing_tasks
                )
                if not has_integration:
                    orig_desc = _task_for_val.get("description", "")[:300]
                    integ_id = f"integration-{project}-{int(time.time())}"
                    integ_desc = (
                        f"Integration: wire up recently completed features into the game.\n\n"
                        f"Triggering task: {orig_desc}\n\n"
                        f"Files changed by triggering task:\n{diff_stat[:400]}\n\n"
                        f"IMPORTANT: Other features may have completed while a previous integration "
                        f"task was running. Before doing anything, run:\n"
                        f"  git log --oneline -15\n"
                        f"  git diff HEAD~10..HEAD --stat\n"
                        f"to find ALL recently committed work, not just the triggering task. "
                        f"Read every changed file. Identify any new systems, signals, nodes, or "
                        f"autoloads that were added across all recent commits. Ensure they are "
                        f"properly connected to existing game systems (SignalBus, GameManager, "
                        f"scene tree, UI). Fix any disconnections. "
                        f"Do NOT rewrite existing working systems \u2014 only add the missing wiring."
                    )
                    db.task_upsert({
                        "id": integ_id,
                        "project": project,
                        "type": "bug",
                        "description": integ_desc,
                        "priority": 85,
                        "status": "pending",
                        "dependencies": [task_id],
                        "metadata": {"is_integration_task": True},
                        "attempts": 0,
                        "max_attempts": 2,
                    })
                    print(f"[Swarm] Auto-spawned integration task {integ_id} for {project} after feature completion")

        # Sprint QA: when a QA task completes for an auto_replan project, mark it ready for planner
        if (not validation_failed_after_completion
                and task_type_finished in ("qa", "harness_qa", "hybrid_qa")
                and project in AUTO_REPLAN_PROJECTS):
            _projects_sprint_qa_done.add(project)
            print(f"[Swarm] Sprint QA complete for {project} \u2014 planner fires on next empty queue")

        # Auto-QA: increment counter for Godot projects NOT on the sprint cycle
        global _qa_completion_counter
        _task_metadata_for_qa = (_task_for_val or {}).get("metadata") or {}
        _is_recovery_task = _task_metadata_for_qa.get("is_recovery_task", False)
        if (not validation_failed_after_completion
                and not _spawned_continuation
                and not _is_recovery_task
                and task_type_finished not in ("qa", "harness_qa", "hybrid_qa", "audit", "manager", "project_create", "project_plan")):
            project_path = WORKSPACE / project
            if (project_path / "project.godot").exists() and project not in AUTO_REPLAN_PROJECTS:
                _qa_completion_counter[project] = _qa_completion_counter.get(project, 0) + 1
                existing = db.task_get_by_project(project)
                has_qa = any(
                    t.get("type") in ("qa", "harness_qa", "hybrid_qa") and t.get("status") in ("pending", "in_progress")
                    for t in existing
                )
                open_nonqa = [
                    t for t in existing
                    if t.get("status") in ("pending", "in_progress")
                    and t.get("id") != task_id
                    and t.get("type") not in ("qa", "harness_qa", "hybrid_qa", "audit", "manager")
                ]
                threshold_due = _qa_completion_counter[project] >= QA_AUTO_THRESHOLD
                empty_queue_due = not open_nonqa
                if threshold_due or empty_queue_due:
                    _qa_completion_counter[project] = 0
                    if not has_qa:
                        # Use harness_qa if the project has TestHarness wired up,
                        # otherwise fall back to vision-based qa
                        has_harness = (project_path / "autoload" / "test_harness.gd").exists()
                        qa_type = "harness_qa" if has_harness else "qa"
                        qa_id = f"qa-auto-{project}-{int(time.time())}"
                        qa_reason = (
                            f"after {QA_AUTO_THRESHOLD} completions"
                            if threshold_due else
                            "because the Godot project queue is empty"
                        )
                        qa_desc = (
                            f"Auto harness QA: run synchronous checkpoint tests against the game logic ({qa_reason})"
                            if has_harness else
                            f"Auto QA: playtest and verify core game loop is functional after recent changes ({qa_reason})"
                        )
                        # Depend on all pending/in_progress sprint tasks so QA runs after
                        # the sprint finishes and appears connected in the dep graph chain.
                        # Also chain to project HEAD so the QA task never floats free from
                        # the project's historical dependency chain.
                        sprint_task_ids = [
                            t["id"] for t in existing
                            if t.get("status") in ("pending", "in_progress")
                            and t.get("type") not in ("qa", "harness_qa", "hybrid_qa", "audit", "manager")
                        ]
                        # Always chain to the canonical project HEAD as the base dep;
                        # sprint tasks are additional blockers.
                        qa_deps = _task_chains.append_project_head(
                            db, project, sprint_task_ids, task_id=qa_id, ensure_head=True
                        )
                        db.task_upsert({
                            "id": qa_id,
                            "project": project,
                            "type": qa_type,
                            "description": qa_desc,
                            "priority": 75,
                            "status": "pending",
                            "dependencies": qa_deps,
                            "metadata": {},
                            "attempts": 0,
                            "max_attempts": 2,
                        })
                        print(f"[Swarm] Auto-spawned {qa_type} task {qa_id} for {project} {qa_reason} (deps: {len(qa_deps)} task(s))")

        # Auto-audit: fire for any project (not just Godot) to catch false completions
        global _audit_completion_counter
        if (not validation_failed_after_completion
                and not _spawned_continuation
                and not _is_recovery_task
                and task_type_finished not in ("qa", "harness_qa", "hybrid_qa", "audit", "manager", "project_create", "project_plan")):
            _audit_completion_counter[project] = _audit_completion_counter.get(project, 0) + 1
            if _audit_completion_counter[project] >= AUDIT_AUTO_THRESHOLD:
                _audit_completion_counter[project] = 0
                existing = db.task_get_by_project(project)
                has_audit = any(
                    t.get("type") == "audit" and t.get("status") in ("pending", "in_progress")
                    for t in existing
                )
                if not has_audit:
                    audit_id = f"audit-auto-{project}-{int(time.time())}"
                    # Chain to project HEAD so the audit task always connects to the
                    # project's dependency chain rather than floating from just one task.
                    audit_deps = _task_chains.append_project_head(
                        db, project, [task_id], task_id=audit_id, ensure_head=True
                    )
                    db.task_upsert({
                        "id": audit_id,
                        "project": project,
                        "type": "audit",
                        "description": "Auto audit: verify recently completed tasks are genuinely implemented in the repo",
                        "priority": 70,
                        "status": "pending",
                        "dependencies": audit_deps,
                        "metadata": {},
                        "attempts": 0,
                        "max_attempts": 2,
                    })
                    print(f"[Swarm] Auto-spawned audit task {audit_id} for {project} after {AUDIT_AUTO_THRESHOLD} completions (deps: {len(audit_deps)})")


def _handle_task_failure(task_id: str, project: Optional[str], agent_output: str,
                         _task_snapshot: Optional[dict] = None):
    """On task failure, retry if attempts remain, else mark failed. (#6)"""
    _lazy_imports()

    # Prefer the caller-supplied snapshot: prune_history() may have already
    # deleted the row from the DB by the time we reach here.
    task = db.task_get(task_id) or _task_snapshot
    if not task:
        return

    attempts = task.get("attempts", 0) + 1
    max_attempts = task.get("max_attempts", 3)

    if attempts < max_attempts:
        # Extract last meaningful error lines from agent output
        failure_snippet = ""
        for line in reversed(agent_output.splitlines()):
            line = line.strip()
            if line and not line.startswith("["):
                failure_snippet = line[:300]
                break

        # Preserve failure context in metadata for the retry prompt
        meta = task.get("metadata", {})
        meta["last_failure"] = failure_snippet or agent_output[-300:]
        meta["failure_attempt"] = attempts

        db.task_upsert({
            **task,
            "status": "pending",
            "attempts": attempts,
            "started": None,
            "completed": None,
            "agent_id": None,
            "metadata": meta,
        })
        print(f"[Swarm] Task {task_id} failed (attempt {attempts}/{max_attempts}) \u2014 retrying")
    else:
        db.task_update_status(
            task_id, "failed",
            completed=datetime.now().isoformat(),
        )
        print(f"[Swarm] Task {task_id} failed after {attempts} attempts \u2014 giving up")
        _fire_task_webhook(
            "task_failed",
            project=project or "",
            task_id=task_id,
            task_type=task.get("type", ""),
            description=task.get("description", "").split("\n")[0][:100],
            attempts=attempts,
            max_attempts=max_attempts,
        )
        # Spawn a recovery task to attempt the work again with failure context.
        # Don't spawn recovery tasks for recovery tasks -- avoids infinite chains
        if project and (task.get("metadata") or {}).get("is_recovery_task"):
            _spawn_terminal_recovery_continuation(task, attempts, agent_output)
        elif project:
            _spawn_review_task(task, attempts, agent_output)


def _looks_like_file_path(value: str) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if "/" in text or "\\" in text:
        return True
    return text.lower().endswith((".gd", ".tscn", ".tres", ".py", ".json", ".md", ".png", ".wav"))


def _bounded_failure_excerpt(last_output: str) -> tuple[str, int]:
    _SIGNAL_WORDS = ("ERROR", "Error", "Exception", "Traceback", "FAIL", "assert", "WARNING: response truncated")
    signal_lines = [
        ln for ln in (last_output or "").splitlines()
        if any(sw in ln for sw in _SIGNAL_WORDS)
    ]
    signal_excerpt = "\n".join(signal_lines[-60:])
    tail_excerpt = (last_output or "")[-3000:]
    if signal_excerpt and signal_excerpt not in tail_excerpt:
        failure_excerpt = f"[Key error lines]\n{signal_excerpt}\n\n[Last ~3000 chars of log]\n{tail_excerpt}"
    else:
        failure_excerpt = tail_excerpt
    max_excerpt = 6000
    if len(failure_excerpt) > max_excerpt:
        failure_excerpt = failure_excerpt[-max_excerpt:]
    return failure_excerpt, len(last_output or "")


def _spawn_terminal_recovery_continuation(failed_task: dict, attempts: int, last_output: str) -> str | None:
    failed_id = failed_task.get("id", "unknown")
    project = failed_task.get("project", "")
    if not project:
        return None

    failed_meta = failed_task.get("metadata") or {}
    branch_root_id = failed_meta.get("recovery_root_task_id") or failed_meta.get("failed_task_id") or failed_id
    continuation_id = f"bug-{failed_id}"
    existing = db.task_get(continuation_id)
    excerpt, output_chars = _bounded_failure_excerpt(last_output)
    dependents = [
        t for t in db.task_get_all()
        if failed_id in (t.get("dependencies") or [])
    ]
    orig_desc = (failed_task.get("description") or "")[:2000]
    continuation_desc = (
        "TERMINAL RECOVERY CONTINUATION:\n\n"
        f"Recovery task {failed_id} exhausted its allowed attempts after {attempts} failure(s).\n"
        "Continue the branch as a normal bug-fix task so downstream dependencies stay valid.\n\n"
        f"{format_branch_intent(failed_task)}\n\n"
        f"FAILED RECOVERY TASK:\n{orig_desc}\n\n"
        f"LATEST FAILURE SUMMARY:\n{excerpt}\n\n"
        "YOUR JOB:\n"
        "1. Diagnose the remaining blocker.\n"
        "2. Complete the missing work for the original task objective above.\n"
        "3. Keep downstream tasks unblocked by preserving branch continuity.\n"
        "4. Validate and close the branch cleanly.\n"
    )
    metadata = {
        "continuation_for_failed_task": failed_id,
        "continuation_reason": "terminal_recovery_failure",
        "branch_continuation": True,
        "recovery_root_task_id": branch_root_id,
        "dropped_terminal_dependency": failed_id,
        "failure_attempts": attempts,
        "error_log_excerpt": excerpt,
        "error_log_chars": output_chars,
        **branch_intent_metadata(failed_task),
    }
    continuation_deps = _replacement_task_dependencies(
        failed_task,
        new_task_id=continuation_id,
    )
    if existing:
        db.task_update(continuation_id, {
            "description": continuation_desc,
            "metadata": metadata,
            "status": "pending",
            "priority": 100,
            "dependencies": continuation_deps,
        })
    else:
        db.task_upsert({
            "id": continuation_id,
            "project": project,
            "type": "bug",
            "priority": 100,
            "description": continuation_desc,
            "status": "pending",
            "dependencies": continuation_deps,
            "metadata": metadata,
            "attempts": 0,
            "max_attempts": 3,
            "created": datetime.now().isoformat(),
        })

    # Advance head to the new continuation task so new auto-chained tasks inherit a
    # valid dep. The dep graph already connects dependents via reparenting above.
    proj = db.project_get(project)
    if proj and proj.get("head_task_id") == failed_id:
        db.project_update(project, {"head_task_id": continuation_id})
        print(f"[Swarm] Advanced head to continuation {continuation_id} for {project}")

    for dep_task in dependents:
        new_deps = _task_mutations.replace_task_dependencies(
            db,
            dep_task["id"],
            {failed_id: continuation_id},
        )
        if new_deps is not None:
            print(f"[Swarm] Reparented {dep_task['id']}: dependency {failed_id} → continuation {continuation_id}")
    return continuation_id


def _project_plan_subtasks(project: str, planner_task_id: str) -> list[dict]:
    _lazy_imports()
    return _plan_cleanup.project_plan_subtasks(db, project, planner_task_id)


def _normalize_plan_hint(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _extract_dependency_hint_names(description: str) -> list[str]:
    desc = description or ""
    match = re.search(r"\[SEQUENTIAL:\s*after\s+([^\]]+)\]", desc, re.IGNORECASE)
    if not match:
        return []
    raw_hint = match.group(1)
    pieces = re.split(r"\s*\+\s*|\s*,\s*|\s+\band\b\s+", raw_hint, flags=re.IGNORECASE)
    return [piece.strip() for piece in pieces if piece and piece.strip()]


def _match_plan_hint_to_task_ids(
    hint_names: list[str],
    subtasks: list[dict],
    current_task_id: str,
) -> tuple[dict[str, str], list[str]]:
    matched: dict[str, str] = {}
    missing: list[str] = []
    candidates = [
        task for task in subtasks
        if task.get("id") != current_task_id
    ]
    normalized_candidates = [
        (
            task.get("id", ""),
            _normalize_plan_hint(task.get("description", "")),
        )
        for task in candidates
    ]
    for hint in hint_names:
        normalized_hint = _normalize_plan_hint(hint)
        if not normalized_hint:
            continue
        found_id = None
        for candidate_id, candidate_desc in normalized_candidates:
            if normalized_hint and normalized_hint in candidate_desc:
                found_id = candidate_id
                break
        if found_id:
            matched[hint] = found_id
        else:
            missing.append(hint)
    return matched, missing


def _validate_project_plan_subtasks(project: str, planner_task_id: str) -> list[str]:
    subtasks = _project_plan_subtasks(project, planner_task_id)
    if not subtasks:
        return ["project_plan created no tagged subtasks"]

    all_task_ids = {t["id"] for t in db.task_get_all()}
    completed_ids = db.task_get_completed_ids()
    errors: list[str] = []
    for task in subtasks:
        tid = task.get("id", "")
        deps = task.get("dependencies") or []
        for dep in deps:
            if dep == tid:
                errors.append(f"{tid}: self dependency")
            elif _looks_like_file_path(dep):
                errors.append(f"{tid}: file path used as dependency ({dep})")
            elif dep not in all_task_ids and dep not in completed_ids:
                errors.append(f"{tid}: unknown dependency ({dep})")

        hint_names = _extract_dependency_hint_names(task.get("description", ""))
        if hint_names:
            matched_hints, missing_hints = _match_plan_hint_to_task_ids(hint_names, subtasks, tid)
            for hint_name, expected_dep_id in matched_hints.items():
                if expected_dep_id not in deps:
                    errors.append(
                        f"{tid}: sequential hint '{hint_name}' missing dependency on {expected_dep_id}"
                    )
            if missing_hints:
                errors.append(
                    f"{tid}: sequential hint references unknown sibling task(s): {', '.join(missing_hints)}"
                )

        desc_prefix = (task.get("description", "") or "").strip().split("]", 1)[0].upper()
        sibling_subtask_ids = {subtask.get("id", "") for subtask in subtasks}
        sibling_deps = [dep for dep in deps if dep in sibling_subtask_ids]
        file_aware_auto_dep_indices = (task.get("metadata") or {}).get("file_aware_auto_dep_indices") or []
        if "[PARALLEL" in desc_prefix and sibling_deps and not file_aware_auto_dep_indices:
            errors.append(
                f"{tid}: marked parallel but depends on sibling task(s): {', '.join(sibling_deps)}"
            )
    return errors


def _spawn_review_task(failed_task: dict, attempts: int, last_output: str):
    """Create a recovery task that diagnoses failures and completes the original work.

    The recovery task MUST actually do the work -- not just document it -- because
    other tasks may depend on the output. If the recovery task also fails, a
    replacement task is created and all dependents are reparented to it.
    """
    _lazy_imports()

    import uuid as _uuid
    failed_id = failed_task.get("id", "unknown")
    project = failed_task.get("project", "")
    if project in PAUSED_PROJECTS:
        print(f"[Swarm] Skipping recovery task \u2014 {project} is paused")
        return
    orig_type = failed_task.get("type", "bug")
    orig_desc = failed_task.get("description", "")
    orig_priority = failed_task.get("priority", 70)

    # Find tasks that depend on the failed task so we can reparent them if needed
    all_tasks = db.task_get_all()
    dependents = [t for t in all_tasks if failed_id in (t.get("dependencies") or [])]
    failed_meta = failed_task.get("metadata") or {}
    branch_root_id = failed_meta.get("recovery_root_task_id") or failed_meta.get("failed_task_id") or failed_id
    # Decay priority with recovery chain depth so nested recoveries don't crowd out feature work.
    # Each recovery level drops priority by 5, floor at 70.
    _recovery_depth = int(failed_meta.get("recovery_depth") or 0)
    # Recovery tasks inherit the original task's priority so the project stays
    # ordered correctly in the scheduler queue.
    orig_priority = max(50, orig_priority - 5 * _recovery_depth)

    live_branch_recoveries = [
        t for t in all_tasks
        if (t.get("metadata") or {}).get("is_recovery_task")
        and (t.get("metadata") or {}).get("recovery_root_task_id") == branch_root_id
        and t.get("status") in ("pending", "in_progress")
    ]
    canonical_recovery = None
    if live_branch_recoveries:
        live_branch_recoveries.sort(
            key=lambda t: (
                0 if t.get("status") == "in_progress" else 1,
                t.get("created") or "",
                t.get("id") or "",
            )
        )
        canonical_recovery = live_branch_recoveries[0]
        for stale in live_branch_recoveries[1:]:
            if stale.get("status") == "pending":
                db.task_update_status(stale["id"], "cancelled")
                print(f"[Swarm] Cancelled stale recovery task {stale['id'][:8]} for branch {branch_root_id[:8]}")

    recovery_id = f"recovery-{_uuid.uuid4().hex[:8]}"

    if canonical_recovery:
        canonical_id = canonical_recovery["id"]
        proj = db.project_get(project) if project else None
        if proj and proj.get("head_task_id") == failed_id:
            # Advance head to the live recovery task so new auto-chained tasks inherit
            # a valid dep and don't stall on the old failed task.
            db.project_update(project, {"head_task_id": canonical_id})
            print(f"[Swarm] Advanced head to reused recovery {canonical_id} for {project}")
        for dep_task in dependents:
            new_deps = _task_mutations.replace_task_dependencies(
                db,
                dep_task["id"],
                {failed_id: canonical_id},
            )
            if new_deps is not None:
                print(f"[Swarm] Reparented {dep_task['id']}: dependency {failed_id} \u2192 recovery {canonical_id}")
        print(f"[Swarm] Reusing existing recovery task {canonical_id} for branch {branch_root_id[:8]}")
        return

    note = (
        f"NOTE: {len(dependents)} other task(s) are waiting on this work to complete."
        if dependents
        else "NOTE: No tasks depend on this one, but the feature is still missing from the project."
    )

    failure_excerpt, output_chars = _bounded_failure_excerpt(last_output)

    # Cap original description -- deep chains can recursively embed previous recovery
    # descriptions, causing exponential prompt growth.
    orig_desc_capped = orig_desc[:2000] + ("\n[... description truncated ...]" if len(orig_desc) > 2000 else "")

    recovery_desc = (
        f"RECOVERY TASK: Complete the work that failed {attempts} times.\n\n"
        f"ORIGINAL TASK ({failed_id}):\n{orig_desc_capped}\n\n"
        f"FAILURE HISTORY (excerpt -- full log in metadata.error_log):\n{failure_excerpt}\n\n"
        f"YOUR JOB:\n"
        f"1. Read the failure history above and understand what went wrong.\n"
        f"2. Inspect the codebase to understand the current state.\n"
        f"3. ACTUALLY COMPLETE the original task \u2014 implement the feature/fix.\n"
        f"   Do NOT just document the failure. The project is incomplete without this work.\n"
        f"4. Avoid the mistakes from previous attempts (described in failure history above).\n"
        f"5. Validate, commit, and push when done.\n\n"
        f"{note}"
    )

    # Inherit worktree from failed task if it was preserved (chained worktree flow)
    recovery_meta: dict = {
        "is_review_task": True,
        "is_recovery_task": True,
        "recovery_depth": _recovery_depth + 1,
        "error_log_excerpt": failure_excerpt,
        "error_log_chars": output_chars,
        "failed_task_id": failed_id,
        "recovery_root_task_id": branch_root_id,
        "dependent_count": len(dependents),
        **branch_intent_metadata(failed_task),
    }
    if failed_meta.get("worktree_path") and failed_meta.get("worktree_branch"):
        wt_p = Path(failed_meta["worktree_path"])
        if wt_p.exists():
            recovery_meta["worktree_path"] = str(wt_p)
            recovery_meta["worktree_branch"] = failed_meta["worktree_branch"]
            recovery_meta["worktree_inherited"] = True
            print(f"[Swarm] Recovery task will inherit worktree {wt_p.name} from {failed_id}")

    recovery_task = {
        "id": recovery_id,
        "project": project,
        "type": orig_type,
        "description": recovery_desc,
        "priority": orig_priority,
        "status": "pending",
        "attempts": 0,
        "max_attempts": 3,
        "dependencies": _replacement_task_dependencies(
            failed_task,
            new_task_id=recovery_id,
        ),
        "metadata": recovery_meta,
        "created": datetime.now().isoformat(),
    }
    db.task_upsert(recovery_task)
    print(f"[Swarm] Created recovery task {recovery_id} for failed task {failed_id}")

    # Advance head to the new recovery task so new auto-chained tasks inherit a
    # valid dep and don't stall on the old failed task. Dependents are reparented
    # to recovery_id below; the head advances cleanly.
    proj = db.project_get(project) if project else None
    if proj and proj.get("head_task_id") == failed_id:
        db.project_update(project, {"head_task_id": recovery_id})
        print(f"[Swarm] Advanced head to recovery {recovery_id} for {project}")

    # Reparent dependents: replace failed_id with recovery_id in their dependencies
    for dep_task in dependents:
        new_deps = _task_mutations.replace_task_dependencies(
            db,
            dep_task["id"],
            {failed_id: recovery_id},
        )
        if new_deps is not None:
            print(f"[Swarm] Reparented {dep_task['id']}: dependency {failed_id} \u2192 {recovery_id}")


def _task_history_lookup(task_id: str) -> Optional[dict]:
    if not task_id:
        return None
    history_file = _get_data_dir() / "task-history.jsonl"
    if not history_file.exists():
        return None
    latest_match: Optional[dict] = None
    try:
        with history_file.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if record.get("id") != task_id:
                    continue
                latest_match = record
    except Exception:
        return None
    return latest_match


def _replacement_task_dependencies(
    failed_task: dict,
    *,
    new_task_id: str,
) -> list[str]:
    """Preserve branch continuity for recovery/continuation tasks.

    Prefer the failed task's own upstream deps. If those were already lost,
    fall back to the branch root's historical deps. As a last resort, attach
    the replacement to the current project head/genesis chain.
    """
    _lazy_imports()

    project = failed_task.get("project", "")
    failed_id = failed_task.get("id", "")
    failed_meta = failed_task.get("metadata") or {}
    # is_recovery_task: True means this is a recovery/continuation of a recovery.
    # Such tasks must chain to genesis even when they have no own deps, because
    # they are new work that shouldn't float unconnected in the dep graph.
    is_recovery = bool(failed_meta.get("is_recovery_task"))
    is_review = bool(failed_meta.get("is_review_task"))

    def _normalized_deps(task: Optional[dict]) -> list[str]:
        deps = []
        seen: Set[str] = set()
        for dep in (task or {}).get("dependencies") or []:
            if not isinstance(dep, str):
                continue
            dep_id = dep.strip()
            if not dep_id or dep_id in {failed_id, new_task_id} or dep_id in seen:
                continue
            seen.add(dep_id)
            deps.append(dep_id)
        return deps

    completed_ids = db.task_get_completed_ids()

    def _filter_completed(deps: list[str]) -> list[str]:
        """Drop deps that are already completed/archived -- they can't block anything."""
        active_ids = {t["id"] for t in db.task_get_all()}
        return [
            d for d in deps
            if d not in completed_ids and d in active_ids
        ]

    direct_deps = _filter_completed(_normalized_deps(failed_task))
    if direct_deps:
        return direct_deps
    # If all direct deps were already completed (i.e. filtered list is empty but
    # original list wasn't), the continuation is unblocked -- return empty.
    # Only fall through to candidate_ids/genesis when the failed task genuinely
    # had no dependencies to begin with (original list was already empty).
    # NOTE: is_review tasks that are NOT is_recovery (i.e. spawned by _spawn_review_task
    # for the original failed task) have is_review=True but should not block here --
    # they represent continuation of original work that had no deps.
    # is_recovery tasks (continuations of recoveries) always chain to genesis.
    if _normalized_deps(failed_task) and not is_recovery:
        return []

    candidate_ids = [
        failed_meta.get("recovery_root_task_id"),
        failed_meta.get("failed_task_id"),
        failed_meta.get("branch_intent_root_task_id"),
    ]
    for candidate_id in candidate_ids:
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            continue
        if candidate_id in {failed_id, new_task_id}:
            continue
        candidate_task = db.task_get(candidate_id) or db.task_get_completed_record(candidate_id) or _task_history_lookup(candidate_id)
        candidate_deps = _filter_completed(_normalized_deps(candidate_task))
        if candidate_deps:
            return candidate_deps

    if not project and not is_recovery and not is_review:
        return []

    genesis_id = f"{project}-genesis"
    genesis_task = db.task_get(genesis_id) or db.task_get_completed_record(genesis_id)
    if genesis_task is None:
        now = datetime.now().isoformat()
        db.task_upsert({
            "id": genesis_id,
            "project": project,
            "type": "feature",
            "description": "Project registered - genesis anchor",
            "status": "completed",
            "created": now,
            "completed": now,
            "priority": 50,
        })
        try:
            db.task_record_completed(genesis_id, project=project)
        except Exception:
            pass
    return [genesis_id]


# ---------------------------------------------------------------------------
# Agent status checking
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
                process.kill()
                process.wait(timeout=5)
            except Exception as _ke:
                print(f"[Swarm] Dep violation kill error: {_ke}")
        elif pid:
            try:
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
    """Count agents that are genuinely still running."""
    _lazy_imports()

    with _handle_lock:
        in_process = {
            aid for aid, d in _active_handles.items()
            if d["process"].poll() is None
        }

    persisted = set()
    for a in db.agent_get_active():
        aid = a["id"]
        if aid in in_process:
            continue
        pid = a.get("pid")
        if pid and _is_pid_running(pid):
            persisted.add(aid)

    return len(in_process) + len(persisted)


def prune_history():
    """Archive finished agents and tasks to JSONL history, then remove from DB."""
    _lazy_imports()

    # Use orchestrator's HISTORY_FILE if available, otherwise fall back
    try:
        import swarm.orchestrator as _orc
        HISTORY_FILE = getattr(_orc, 'HISTORY_FILE', _get_data_dir() / "agent-history.jsonl")
    except Exception:
        HISTORY_FILE = _get_data_dir() / "agent-history.jsonl"
    finished = [a for a in db.agent_get_all() if a.get("status") not in ("active", "spawning")]
    if not finished:
        return

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a") as f:
        for agent in finished:
            f.write(json.dumps(agent) + "\n")

    # Archive completed/failed/cancelled tasks before deleting them
    finished_tasks = [t for t in db.task_get_all() if t.get("status") in ("completed", "failed", "cancelled")]
    if finished_tasks:
        task_history_file = HISTORY_FILE.parent / "task-history.jsonl"
        with task_history_file.open("a") as f:
            for task in finished_tasks:
                f.write(json.dumps(task) + "\n")
        # Prune task-history.jsonl: keep only the last 20000 entries
        _TASK_HISTORY_MAX = 20000
        try:
            existing = task_history_file.read_text().splitlines()
            if len(existing) > _TASK_HISTORY_MAX:
                task_history_file.write_text("\n".join(existing[-_TASK_HISTORY_MAX:]) + "\n")
        except Exception as _prune_err:
            print(f"[Swarm] task-history prune failed: {_prune_err}")

    # Update each project's head_task_id to the most recent continuity-eligible
    # task, but do not overwrite a live continuation with a failed/cancelled tail.
    if finished_tasks:
        latest_by_project: Dict[str, tuple[str, str]] = {}
        for task in finished_tasks:
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

    conn = db._connect()
    conn.execute(
        "DELETE FROM agents WHERE status NOT IN ('active', 'spawning')"
    )
    conn.commit()
    conn.execute(
        "DELETE FROM tasks WHERE status IN ('completed', 'failed', 'cancelled')"
    )
    conn.commit()
