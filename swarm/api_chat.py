"""Chat and project-creation route handlers for the Swarm API.

Routes: POST /api/chat, POST /api/project-chat, POST /api/create-project-tasks
"""

import json
import os
import requests
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
import re as _re

from flask import jsonify, request

from swarm import project_graph_policy as _graph_policy
from swarm.closure.documents import write_project_closure_doc
from swarm.closure.proposals import propose_project_closure
from swarm.godot_bootstrap import install_gut_into_project
from swarm.task_chains import anchor_project_batch_roots, chain_to_project_head, ensure_project_head, set_project_head



from swarm.llm_utils import parse_tool_calls as _parse_tool_calls

from swarm.api_wizard import (
    _append_project_design_phase,
    _bootstrap_godot_project_support,
    _chat_call_llm,
    _cleanup_failed_project_creation,
    _copy_tree,
    _ensure_project_godot_file,
    _extract_first_json_array,
    _extract_prd_content,
    _extract_prd_quality_gates,
    _graph_diagnostics_payload,
    _normalize_project_type,
    _parse_prd_tasks_preview,
    _prd_dependency_annotation_stats,
    _project_brief_filename,
    _project_chat_user_confirmed_generation,
    _project_creation_validation_errors,
    _project_file_extensions,
    _task_graph_quality_errors,
    _task_graph_semantic_warnings,
    _python_project_chat_prompt,
    _repair_task_graph_with_llm,
    _render_graph_repair_context,
    _render_project_design_doc,
    _render_project_preview_from_tasks,
    _write_project_design_doc,
    _scaffold_project_repo,
    _task_graph_shape_summary,
    _task_title,
)
def _build_state_snapshot(db, all_tasks=None, all_agents=None, projects=None):
    """Build a current swarm state string for chat system prompts."""
    if all_tasks is None:
        all_tasks = db.task_get_all()
    if all_agents is None:
        all_agents = db.agent_get_all()
    if projects is None:
        projects = db.project_get_all()

    pending_tasks = [t for t in all_tasks if t["status"] == "pending"]
    in_prog_tasks = [t for t in all_tasks if t["status"] == "in_progress"]
    failed_tasks  = [t for t in all_tasks if t["status"] == "failed"]
    completed_count = sum(1 for t in all_tasks if t["status"] in ("completed", "cancelled"))
    active_agents = [a for a in all_agents if a["status"] == "active"]

    proj_summary = {}
    for t in all_tasks:
        p = t["project"]
        if p not in proj_summary:
            proj_summary[p] = {"pending": 0, "in_progress": 0, "failed": 0, "completed": 0}
        proj_summary[p][t["status"]] = proj_summary[p].get(t["status"], 0) + 1

    recent_failed = [a for a in all_agents if a.get("exit_code") not in (0, None)][-10:]
    failure_snippets = [
        f"  agent {a.get('id','')[:8]} ({a.get('project','')}/{a.get('task_id','')[:20]}): {(a.get('output') or '')[-200:].strip()}"
        for a in recent_failed
    ]

    # projects may be a dict {name: data} or a list of dicts
    if isinstance(projects, dict):
        known_project_names = sorted(n for n in projects if n and n != "_swarm")
    else:
        known_project_names = sorted(p["name"] for p in projects if p.get("name") and p["name"] != "_swarm")

    return f"""CURRENT SWARM STATE ({datetime.now().strftime('%H:%M:%S')}):

KNOWN PROJECTS (exact names — use these verbatim when creating tasks):
{', '.join(known_project_names) or '(none registered)'}

Active agents ({len(active_agents)}):
{chr(10).join(f'  id={a.get("id","")[:12]} project={a.get("project","")} task={a.get("task_id","")[:30]}' for a in active_agents) or '  (none)'}

Pending tasks: {len(pending_tasks)}  In-progress: {len(in_prog_tasks)}  Failed: {len(failed_tasks)}  Completed (history): {completed_count}

Per-project summary (projects with tasks):
{chr(10).join(f'  {p}: pending={v["pending"]} in_progress={v["in_progress"]} failed={v["failed"]} completed={v["completed"]}' for p, v in sorted(proj_summary.items()) if p != '_swarm') or '  (none)'}

Failed tasks (up to 10):
{chr(10).join(f'  {t["id"]} ({t["project"]}) att:{t.get("attempts",0)}/{t.get("max_attempts",3)}: {t["description"][:80]}' for t in failed_tasks[-10:]) or '  (none)'}

Recent agent failures:
{chr(10).join(failure_snippets) or '  (none)'}
"""


def _execute_chat_actions(action_blocks, db, config, config_file, _config_write_lock,
                          orchestrator, auto_mode_state):
    """Execute [ACTION] blocks and return a results summary string."""
    import swarm.orchestrator as _orch
    results = []
    for raw in action_blocks:
        try:
            act = json.loads(raw)
        except Exception as e:
            results.append(f"[ACTION parse error: {e}]")
            continue

        atype = act.get("type", "")

        if atype == "kill_agent":
            agent_id = act.get("agent_id", "")
            agent = db.agent_get(agent_id)
            if not agent:
                results.append(f"kill_agent: agent {agent_id!r} not found")
                continue
            killed = False
            handle = _orch._active_handles.get(agent_id)
            if handle and handle.get("process"):
                try:
                    handle["process"].kill()
                    killed = True
                except Exception:
                    pass
            if not killed:
                pid = agent.get("pid")
                if pid:
                    try:
                        os.kill(int(pid), 9)
                        killed = True
                    except Exception:
                        pass
            if killed:
                db.agent_update_status(agent_id, "killed")
                task_id = agent.get("task_id")
                if task_id:
                    task = db.task_get(task_id)
                    if task and task.get("status") == "in_progress":
                        db.task_update(task_id, {"status": "pending"})
                results.append(f"kill_agent: killed {agent_id[:12]} (task reset to pending)")
            else:
                results.append(f"kill_agent: could not kill {agent_id[:12]}")

        elif atype == "kill_all_agents":
            all_agents = db.agent_get_all()
            active = [a for a in all_agents if a.get("status") == "active"]
            killed_count = 0
            for agent in active:
                agent_id = agent["id"]
                killed = False
                handle = _orch._active_handles.get(agent_id)
                if handle and handle.get("process"):
                    try:
                        handle["process"].kill()
                        killed = True
                    except Exception:
                        pass
                if not killed:
                    pid = agent.get("pid")
                    if pid:
                        try:
                            os.kill(int(pid), 9)
                            killed = True
                        except Exception:
                            pass
                if killed:
                    db.agent_update_status(agent_id, "killed")
                    task_id = agent.get("task_id")
                    if task_id:
                        task = db.task_get(task_id)
                        if task and task.get("status") == "in_progress":
                            db.task_update(task_id, {"status": "pending"})
                    killed_count += 1
            results.append(f"kill_all_agents: killed {killed_count}/{len(active)} agents")

        elif atype == "create_task":
            project  = act.get("project", "")
            ttype    = act.get("task_type", "bug")
            desc     = act.get("description", "")
            priority = int(act.get("priority", 80 if ttype == "bug" else 50))
            deps     = act.get("dependencies", [])
            if not project or not desc:
                results.append("create_task: project and description required")
                continue
            task_id = f"{ttype}-{project}-chat-{uuid.uuid4().hex[:12]}"
            try:
                db.task_upsert({
                    "id": task_id, "project": project, "type": ttype,
                    "description": desc, "priority": priority,
                    "status": "pending", "attempts": 0, "max_attempts": 3,
                    "dependencies": chain_to_project_head(db, project, deps, task_id=task_id, ensure_head=True),
                    "metadata": {"created_by": "chat"},
                })
            except ValueError as e:
                results.append(f"create_task: invalid task payload ({e})")
                continue
            existing_project = db.project_get(project)
            if existing_project:
                db.project_upsert({**existing_project, "managed": True})
            # Auto-add project to managed_projects so it will be picked up
            if project not in orchestrator.MANAGED_PROJECTS:
                orchestrator.MANAGED_PROJECTS.append(project)
                config["managed_projects"] = orchestrator.MANAGED_PROJECTS
                try:
                    with _config_write_lock:
                        cfg = json.loads(config_file.read_text()) if config_file.exists() else {}
                        cfg["managed_projects"] = orchestrator.MANAGED_PROJECTS
                        config_file.write_text(json.dumps(cfg, indent=2) + "\n")
                except Exception as e:
                    print(f"[Chat] Warning: could not persist managed_projects: {e}")
            results.append(f"create_task: created {task_id}")

        elif atype == "set_auto_mode":
            enabled = bool(act.get("enabled", True))
            _orch.AUTO_MODE = enabled  # kept for backward compat (some tests check this)
            with auto_mode_state["lock"]:
                auto_mode_state["enabled"] = enabled
                if not enabled:
                    auto_mode_state["suspended_for_quota"] = False
            results.append(f"set_auto_mode: auto mode {'enabled' if enabled else 'disabled'}")

        elif atype == "restart_server":
            results.append("restart_server: restarting in 1s...")
            import threading as _t
            import sys as _sys
            def _do_restart():
                import time as _time
                _time.sleep(1)
                os.execv(_sys.executable, [_sys.executable] + _sys.argv)
            _t.Thread(target=_do_restart, daemon=True).start()

        else:
            results.append(f"unknown action type: {atype!r}")

    return "\n".join(results) if results else ""


_SESSION_TTL_DAYS = 7


def _session_dir(data_dir: Path, project: str) -> Path:
    d = data_dir / "chat_sessions" / project
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_path(data_dir: Path, project: str, session_id: str) -> Path:
    return _session_dir(data_dir, project) / f"{session_id}.jsonl"


def _load_session(data_dir: Path, project: str, session_id: str) -> list:
    """Load session history from disk. Returns [] if missing or expired."""
    p = _session_path(data_dir, project, session_id)
    if not p.exists():
        return []
    age_days = (time.time() - p.stat().st_mtime) / 86400
    if age_days > _SESSION_TTL_DAYS:
        p.unlink(missing_ok=True)
        return []
    messages = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return messages


def _save_session(data_dir: Path, project: str, session_id: str, messages: list) -> None:
    """Persist session history to disk."""
    p = _session_path(data_dir, project, session_id)
    p.write_text(
        "\n".join(json.dumps(m, ensure_ascii=False) for m in messages) + "\n",
        encoding="utf-8",
    )


def _delete_session(data_dir: Path, project: str, session_id: str) -> bool:
    p = _session_path(data_dir, project, session_id)
    if p.exists():
        p.unlink()
        return True
    return False


_DEBUG_BLOCKED_COMMANDS = frozenset([
    "rm -rf /", "git push --force", "git push -f", "sudo rm", "mkfs",
    "dd if=", ":(){:|:&};:", "chmod -R 777 /",
])

_DEBUG_TOOLS_DESCRIPTION = """\
You have access to the following tools. Call them using JSON blocks:
[TOOL_CALL]{"tool": "...", "args": {...}}[/TOOL_CALL]
IMPORTANT: The closing tag MUST include the forward slash: [/TOOL_CALL]. Never use [TOOL_CALL] as a closing tag.

File/shell tools (paths relative to project root):
- read_file(path) — read a file
- write_file(path, content) — write/overwrite a file
- list_dir(path="") — list directory contents
- run_command(command, timeout_seconds=30) — run a shell command in the project root
- git_commit(message) — stage all changes and commit

Task graph tools:
- list_tasks(status="") — list tasks for this project; filter by status (pending/in_progress/completed/failed)
- create_task(type, description, priority=50, dependencies=[]) — create a new task; type MUST be one of: feature, bug, refactor, polish, qa, harness_qa, art_pass, audit, research, plan, project_plan
- update_task(task_id, fields) — update task fields (e.g. priority, description)
- get_critical_path() — find the longest pending dependency chain (the bottleneck)
- get_subgraph(root_id, direction="downstream", depth=3) — BFS from a task to see its dependency cluster
- bulk_deps(ops) — apply multiple add/remove dependency ops atomically; ops=[{action,task_id,dep_id}]

When asked about priorities or what to work on next, call get_critical_path() first.
When asked about project state, read AGENT_KNOWLEDGE.md if present.
"""

_DEBUG_SYSTEM_PROMPT = """\
You are the Swarm, focused on project '{project}'.
Your role is to help the user understand what is happening, diagnose issues, and decide what to work on next.

BEHAVIOUR GUIDELINES:
- Investigate before answering: use read_file or run_command to look at actual code, logs, or test output rather than guessing
- When asked about priorities or what to work on, call get_critical_path() first, then explain the bottleneck
- Read AGENT_KNOWLEDGE.md before answering questions about project architecture or recent changes
- Be concise and direct — reference specific file paths, task IDs, and log line numbers
- You have full write access: use write_file and git_commit when the user asks you to make changes
- When you find a bug or issue worth tracking, offer to create a task for it

TOOL USAGE:
{tools}
{context}"""


def _validate_project_path(project_root: Path, rel_path: str) -> Path:
    """Resolve rel_path relative to project_root and verify it stays within."""
    resolved = (project_root / rel_path).resolve()
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError:
        raise ValueError(f"Path '{rel_path}' is outside the project directory")
    return resolved


_GRAPH_TOOLS = frozenset([
    "list_tasks", "create_task", "update_task",
    "get_critical_path", "get_subgraph", "bulk_deps",
])


def _execute_debug_tool(tool: str, args: dict, project_root: Path,
                        project: str = "", db=None) -> str:
    """Execute a single debug tool call. Returns string result (never raises)."""
    try:
        if tool in _GRAPH_TOOLS:
            return _execute_graph_tool(tool, args, project, db)
        return _execute_debug_tool_inner(tool, args, project_root)
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def _execute_debug_tool_inner(tool: str, args: dict, project_root: Path) -> str:
    if tool == "read_file":
        path = _validate_project_path(project_root, args.get("path", ""))
        if not path.exists():
            return f"Error: file not found: {args.get('path')}"
        return path.read_text(encoding="utf-8", errors="replace")[:20000]

    elif tool == "write_file":
        path = _validate_project_path(project_root, args.get("path", ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args.get("content", ""), encoding="utf-8")
        return f"Written: {args.get('path')}"

    elif tool == "list_dir":
        path = _validate_project_path(project_root, args.get("path", ""))
        if not path.exists():
            return f"Error: directory not found: {args.get('path', '.')}"
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        return "\n".join(
            ("/" if e.is_dir() else " ") + e.name for e in entries
        )

    elif tool == "run_command":
        command = args.get("command", "")
        for blocked in _DEBUG_BLOCKED_COMMANDS:
            if blocked in command:
                return f"Error: command blocked for safety: {blocked!r}"
        timeout = min(int(args.get("timeout_seconds", 30)), 60)
        try:
            result = subprocess.run(
                command, shell=True, cwd=str(project_root),
                capture_output=True, text=True, timeout=timeout,
            )
            out = (result.stdout or "") + (result.stderr or "")
            return out[:10000] or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout}s"
        except Exception as exc:
            return f"Error: {exc}"

    elif tool == "git_commit":
        message = args.get("message", "chore: update from project-debug chat")
        try:
            subprocess.run(["git", "add", "-A"], cwd=str(project_root),
                           capture_output=True, timeout=15)
            result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(project_root), capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                return result.stdout.strip() or "Committed"
            return f"Error: {result.stderr.strip()}"
        except Exception as exc:
            return f"Error: {exc}"

    else:
        return f"Error: unknown tool '{tool}'"


def _execute_graph_tool(tool: str, args: dict, project: str, db) -> str:
    """Execute a task-graph tool for the project-debug chat."""
    if tool == "list_tasks":
        status_filter = args.get("status", "")
        all_tasks = db.task_get_all() if hasattr(db, "task_get_all") else {}
        tasks = [
            t for t in (all_tasks.values() if isinstance(all_tasks, dict) else all_tasks)
            if t.get("project") == project
            and (not status_filter or t.get("status") == status_filter)
        ]
        if not tasks:
            return f"No tasks found for project '{project}'" + (f" with status '{status_filter}'" if status_filter else "")
        lines = [
            f"[{t['status']}] {t['id']}: {t.get('type','')} — {t.get('description','')[:100]}"
            for t in tasks[:50]
        ]
        return "\n".join(lines)

    elif tool == "create_task":
        _valid_types = {"feature","bug","refactor","polish","qa","harness_qa",
                        "art_pass","audit","research","plan","project_plan","scenario_qa"}
        task_type = args.get("type", "")
        if not task_type:
            return "Error: type is required (e.g. feature, bug, art_pass, qa)"
        if task_type not in _valid_types:
            return f"Error: invalid type '{task_type}'. Must be one of: {', '.join(sorted(_valid_types))}"
        task_id = f"{task_type}-{uuid.uuid4().hex[:8]}"
        task = {
            "id": task_id,
            "project": project,
            "type": task_type,
            "description": args.get("description", ""),
            "priority": int(args.get("priority", 50)),
            "status": "pending",
            "attempts": 0,
            "max_attempts": 3,
            "metadata": {},
            "dependencies": args.get("dependencies", []),
        }
        db.task_upsert(task)
        return f"Created task {task_id}"

    elif tool == "update_task":
        task_id = args.get("task_id", "")
        fields = {k: v for k, v in args.items() if k != "task_id"}
        if not task_id:
            return "Error: task_id required"
        if not db.task_get(task_id):
            return f"Error: task '{task_id}' not found"
        db.task_update(task_id, fields)
        return f"Updated task {task_id}"

    elif tool == "get_critical_path":
        from swarm.dependencies import build_graph_from_tasks
        all_tasks = db.task_get_all()
        graph = build_graph_from_tasks(all_tasks)
        chain = graph.get_critical_path(project=project)
        if not chain:
            return "No pending tasks or no critical path found."
        task_map = {t["id"]: t for t in all_tasks}
        lines = []
        for tid in chain:
            t = task_map.get(tid, {})
            desc = (t.get("description") or "").split("\n")[0][:100]
            lines.append(f"  {tid}: {desc}")
        return f"Critical path ({len(chain)} tasks):\n" + "\n".join(lines)

    elif tool == "get_subgraph":
        from swarm.dependencies import build_graph_from_tasks
        root_id = args.get("root_id", "")
        direction = args.get("direction", "downstream")
        depth = int(args.get("depth", 3))
        if not root_id:
            return "Error: root_id required"
        all_tasks = db.task_get_all()
        graph = build_graph_from_tasks(all_tasks)
        task_ids = graph.get_subgraph(root_id, direction=direction, depth=depth)
        if not task_ids:
            return f"No tasks found in {direction} subgraph of '{root_id}'"
        task_map = {t["id"]: t for t in all_tasks}
        lines = []
        for tid in task_ids:
            t = task_map.get(tid, {})
            desc = (t.get("description") or "").split("\n")[0][:80]
            lines.append(f"  [{t.get('status','')}] {tid}: {desc}")
        return f"Subgraph ({direction}, depth={depth}) from '{root_id}':\n" + "\n".join(lines)

    elif tool == "bulk_deps":
        from swarm.dependencies import build_graph_from_tasks
        ops = args.get("ops", [])
        applied, skipped, errors = [], [], []
        all_tasks = db.task_get_all()
        graph = build_graph_from_tasks(all_tasks)
        for op in ops:
            action = op.get("action", "")
            tid = op.get("task_id", "")
            dep_id = op.get("dep_id", "")
            if action == "add":
                # Cycle check: adding dep_id as dep of tid creates a cycle if
                # tid is already a transitive dependency of dep_id
                if tid in graph.get_all_dependencies(dep_id) or tid == dep_id:
                    skipped.append(f"{tid}←{dep_id} (would create cycle)")
                else:
                    existing = db.task_get(tid) or {}
                    deps = list(existing.get("dependencies") or [])
                    if dep_id not in deps:
                        deps.append(dep_id)
                        db.task_update(tid, {"dependencies": deps})
                    applied.append(f"add {tid}←{dep_id}")
            elif action == "remove":
                existing = db.task_get(tid) or {}
                deps = [d for d in (existing.get("dependencies") or []) if d != dep_id]
                db.task_update(tid, {"dependencies": deps})
                applied.append(f"remove {tid}←{dep_id}")
            else:
                errors.append(f"unknown action '{action}'")
        return json.dumps({"applied": applied, "skipped": skipped, "errors": errors})

    else:
        return f"Error: unknown graph tool '{tool}'"


def _run_debug_tool_loop(
    system_prompt: str,
    history: list,
    config: dict,
    project_root: Path,
    project: str = "",
    db=None,
    max_rounds: int = 5,
) -> tuple[str, list]:
    """Run a tool loop for the project-debug chat.

    Calls the LLM, parses [TOOL_CALL] blocks, executes them, feeds results
    back as a user message, and repeats until no tool calls remain or max_rounds
    is hit. Returns (final_response_text, list_of_tool_call_dicts).
    """
    tool_calls_log = []
    messages = list(history)

    for _ in range(max_rounds):
        response_text = _chat_call_llm(system_prompt, messages, config)
        parsed_calls = _parse_tool_calls(response_text)
        if not parsed_calls:
            return response_text, tool_calls_log

        # Execute each tool call and collect results
        tool_results = []
        for call in parsed_calls:
            tool = call.get("tool", "unknown")
            args = call.get("args", {})

            result = _execute_debug_tool(tool, args, project_root, project=project, db=db)
            tool_calls_log.append({"tool": tool, "args": args, "result": result})
            tool_results.append(f"[TOOL_RESULT: {tool}]\n{result}\n[/TOOL_RESULT]")

        # Add the assistant turn and tool results to history for next round
        messages.append({"role": "assistant", "content": response_text})
        messages.append({"role": "user", "content": "\n\n".join(tool_results)})

    # Final LLM call to summarise after max rounds
    final = _chat_call_llm(system_prompt, messages, config)
    return final, tool_calls_log


def _build_project_context(workspace: Path, project: str, data_dir: Path, db) -> str:
    """Build a project context block for the system prompt on session start.

    Loads: recent git commits, pending/in-progress tasks, last 3 agent log tails,
    AGENT_KNOWLEDGE.md. Each section is capped to keep the prompt reasonable.
    Returns empty string if the project workspace does not exist.
    """
    project_path = workspace / project
    if not project_path.exists():
        return ""

    sections = []

    # --- Git log ---
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            cwd=str(project_path),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            sections.append("## Recent commits (git log)\n" + result.stdout.strip())
    except Exception:
        pass

    # --- Task queue ---
    try:
        all_tasks = db.task_get_all() if hasattr(db, "task_get_all") else {}
        relevant = [
            t for t in (all_tasks.values() if isinstance(all_tasks, dict) else all_tasks)
            if t.get("project") == project and t.get("status") in ("pending", "in_progress")
        ][:50]
        if relevant:
            lines = [
                f"- [{t['status']}] {t['id'][:12]}: {t.get('description','')[:120]}"
                for t in relevant
            ]
            sections.append("## Current task queue\n" + "\n".join(lines))
    except Exception:
        pass

    # --- Recent agent logs ---
    try:
        log_files = sorted(
            data_dir.glob("agent_*.log"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        # Find logs for this project by scanning the first few lines
        project_logs = []
        for lf in log_files:
            if len(project_logs) >= 3:
                break
            try:
                head = lf.read_text(encoding="utf-8", errors="replace")[:2000]
                if project in head:
                    tail_lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()[-100:]
                    project_logs.append((lf.name, "\n".join(tail_lines)))
            except Exception:
                pass
        if project_logs:
            log_block = "\n\n".join(
                f"### {name} (last 100 lines)\n{tail}" for name, tail in project_logs
            )
            sections.append("## Recent agent logs\n" + log_block)
    except Exception:
        pass

    # --- AGENT_KNOWLEDGE.md ---
    knowledge_path = project_path / "AGENT_KNOWLEDGE.md"
    if knowledge_path.exists():
        try:
            content = knowledge_path.read_text(encoding="utf-8", errors="replace")[:8000]
            sections.append("## AGENT_KNOWLEDGE.md\n" + content)
        except Exception:
            pass

    if not sections:
        return ""
    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# Unified chat — module-level state (stop signals, confirm tokens)
# ---------------------------------------------------------------------------

_UNIFIED_STOP_EVENTS: dict[str, threading.Event] = {}
_UNIFIED_CONFIRM_TOKENS: dict[str, dict] = {}   # token -> {action, args, expires_at}
_UNIFIED_CONFIRM_LOCK = threading.Lock()
_UNIFIED_CONFIRM_TTL_SECONDS = 60

# Hard-blocked shell patterns — rejected regardless of any confirmation
_UNIFIED_HARD_BLOCKED = frozenset([
    "rm -rf /", "rm -rf ~", "git push --force", "git push -f",
    "sudo rm", "mkfs", "dd if=", ":(){:|:&};:", "chmod -R 777 /",
    "drop table", "DROP TABLE", "truncate table", "TRUNCATE TABLE",
])


def _issue_confirm_challenge(action: str, args: dict) -> str:
    """Issue a confirmation challenge for a destructive action.

    Stores the pending action and returns a JSON challenge string that the
    client must acknowledge by re-sending with confirm_token.
    """
    token = uuid.uuid4().hex
    expires_at = time.time() + _UNIFIED_CONFIRM_TTL_SECONDS
    with _UNIFIED_CONFIRM_LOCK:
        # Prune expired tokens
        expired = [t for t, v in _UNIFIED_CONFIRM_TOKENS.items() if v["expires_at"] < time.time()]
        for t in expired:
            _UNIFIED_CONFIRM_TOKENS.pop(t, None)
        _UNIFIED_CONFIRM_TOKENS[token] = {"action": action, "args": args, "expires_at": expires_at}
    return json.dumps({
        "requires_confirmation": True,
        "action": action,
        "confirm_token": token,
        "expires_in_seconds": _UNIFIED_CONFIRM_TTL_SECONDS,
    })


def _validate_confirm_token(token: str) -> tuple[bool, str]:
    """Validate a confirm token. Returns (valid, error_message)."""
    with _UNIFIED_CONFIRM_LOCK:
        entry = _UNIFIED_CONFIRM_TOKENS.get(token)
        if not entry:
            return False, "confirm_token not found or already used"
        if entry["expires_at"] < time.time():
            _UNIFIED_CONFIRM_TOKENS.pop(token, None)
            return False, f"confirm_token expired (TTL {_UNIFIED_CONFIRM_TTL_SECONDS}s)"
        # Single-use: consume it
        _UNIFIED_CONFIRM_TOKENS.pop(token, None)
    return True, ""

_UNIFIED_GLOBAL_SCOPE = "_global"

_UNIFIED_GLOBAL_SYSTEM_PROMPT = """\
You are the Swarm — the brain behind the agent orchestration system.
You can observe and control tasks, agents, and projects across all managed projects.

BEHAVIOUR GUIDELINES:
- Investigate before answering: use list_tasks or the state snapshot to look at actual data
- Be concise and direct — reference specific task IDs, project names, and agent IDs
- When asked what to work on, use get_critical_path for the most relevant project
- Destructive actions (delete_task, kill_agent, reset_all_tasks) will require confirmation
- CRITICAL: project names must exactly match the KNOWN PROJECTS list in the state snapshot. If the user gives a name that doesn't match exactly, pick the closest match and confirm before creating anything — never silently create a task on a misspelled or invented project name
- Before executing a cluster of actions (2+ tool calls), briefly state what you're about to do in plain English — one sentence is enough. After completing a complex set of actions, give a short summary of what was done. For single simple actions, just do it.

TOOL USAGE:
{tools}

{memory}

{state}"""

_UNIFIED_PROJECT_SYSTEM_PROMPT = """\
You are the Swarm, focused on project '{project}'.
You can read code, inspect tasks and agents, make changes, and commit — ask anything or give orders.
Your role is to help the user understand what is happening, diagnose issues, and decide what to work on next.

BEHAVIOUR GUIDELINES:
- Investigate before answering: use read_file or run_command to look at actual code, logs, or test output rather than guessing
- When asked about priorities or what to work on, call get_critical_path() first, then explain the bottleneck
- Read AGENT_KNOWLEDGE.md before answering questions about project architecture or recent changes
- Be concise and direct — reference specific file paths, task IDs, and log line numbers
- You have full write access: use write_file and git_commit when the user asks you to make changes
- When you find a bug or issue worth tracking, offer to create a task for it
- Destructive actions (delete_task, kill_agent, reset_all_tasks) will require confirmation
- Before executing a cluster of actions (2+ tool calls), briefly state what you're about to do in plain English — one sentence is enough. After completing a complex set of actions, give a short summary of what was done. For single simple actions, just do it.

TOOL USAGE:
{tools}

{memory}

{context}"""

_UNIFIED_TOOLS_DESCRIPTION = """\
You have access to the following tools. Call them using strict JSON blocks — args MUST be a JSON object with quoted keys and JSON values, never CLI flags:
[TOOL_CALL]{"tool": "tool_name", "args": {"key": "value", "num": 50, "list": []}}[/TOOL_CALL]

Example — create a QA task:
[TOOL_CALL]{"tool": "create_task", "args": {"project": "my-project", "type": "qa", "description": "Run QA checks", "priority": 75, "dependencies": []}}[/TOOL_CALL]

IMPORTANT: args must be a JSON object. Never use --flag style. The closing tag must be [/TOOL_CALL] with a forward slash.

File/shell tools (paths relative to project root — only available in project scope):
- read_file(path) — read a file
- write_file(path, content) — write/overwrite a file
- list_dir(path="") — list directory contents
- run_command(command, timeout_seconds=30) — run a shell command in the project root
- git_commit(message) — stage all changes and commit

Task graph tools:
- list_tasks(project="", status="") — list tasks; in global scope pass project name; in project scope defaults to current project
- create_task(project, type, description, priority=50, dependencies=[]) — create a new task; type MUST be one of: feature, bug, refactor, polish, qa, harness_qa, art_pass, audit, research, plan, project_plan
- update_task(task_id, fields) — update task fields (e.g. priority, description)
- delete_task(task_id) — delete a task (requires confirmation)
- get_critical_path(project) — find the longest pending dependency chain
- get_subgraph(root_id, direction="downstream", depth=3) — BFS from a task to see its dependency cluster
- bulk_deps(ops) — apply multiple add/remove dependency ops atomically; ops=[{"action":"add"|"remove","task_id":"...","dep_id":"..."}]

Agent tools:
- kill_agent(agent_id) — kill a running agent (requires confirmation)
- list_agents(project="") — list active agents

Memory tools:
- write_swarm_memory(content) — overwrite the swarm-level knowledge file (data/SWARM_KNOWLEDGE.md)
- write_project_memory(project, content) — overwrite a project knowledge file (data/project_knowledge/<project>.md)

When asked about priorities or what to work on next, call get_critical_path() first.
When asked about project state, read AGENT_KNOWLEDGE.md if present."""


def _load_unified_memory(data_dir: Path, project: str = "") -> str:
    """Load swarm-level and optionally project-level memory files."""
    sections = []
    swarm_mem = data_dir / "SWARM_KNOWLEDGE.md"
    if swarm_mem.exists():
        content = swarm_mem.read_text(encoding="utf-8", errors="replace").strip()
        if content:
            sections.append(f"## Swarm Knowledge\n{content}")

    if project and project != _UNIFIED_GLOBAL_SCOPE:
        proj_mem = data_dir / "project_knowledge" / f"{project}.md"
        if proj_mem.exists():
            content = proj_mem.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                sections.append(f"## Project Knowledge: {project}\n{content}")

    if not sections:
        return ""
    return "--- MEMORY ---\n" + "\n\n".join(sections) + "\n--- END MEMORY ---"


def _execute_unified_tool(tool: str, args: dict, scope: str, workspace: Path,
                          data_dir: Path, db, config: dict) -> tuple[str, bool]:
    """Execute a unified chat tool. Returns (result_string, requires_confirmation).

    Destructive tools (delete_task, kill_agent) require a confirm_token in args.
    If confirm_token is absent, a challenge is returned instead of executing.
    Hard-blocked commands are rejected regardless of any token.
    """
    # --- Hard-blocked command check for run_command ---
    if tool == "run_command":
        command = args.get("command", "")
        for blocked in _UNIFIED_HARD_BLOCKED:
            if blocked.lower() in command.lower():
                return f"Error: command hard-blocked for safety: {blocked!r}", False
    # --- Memory write tools ---
    if tool == "write_swarm_memory":
        content = args.get("content", "")
        swarm_mem = data_dir / "SWARM_KNOWLEDGE.md"
        swarm_mem.write_text(content, encoding="utf-8")
        return "Swarm knowledge file updated.", False

    if tool == "write_project_memory":
        project = args.get("project", "").strip()
        content = args.get("content", "")
        if not project:
            return "Error: project required", False
        proj_dir = data_dir / "project_knowledge"
        proj_dir.mkdir(parents=True, exist_ok=True)
        proj_mem = proj_dir / f"{project}.md"
        proj_mem.write_text(content, encoding="utf-8")
        return f"Project knowledge file updated for '{project}'.", False

    # --- Agent tools ---
    if tool == "list_agents":
        project_filter = args.get("project", "")
        all_agents = db.agent_get_all()
        agents = [a for a in all_agents if a.get("status") == "active"]
        if project_filter:
            agents = [a for a in agents if a.get("project") == project_filter]
        if not agents:
            return "No active agents.", False
        lines = [
            f"  [{a.get('id','')[:12]}] {a.get('project','')} — {a.get('task_id','')[:40]}"
            for a in agents[:30]
        ]
        return f"Active agents ({len(agents)}):\n" + "\n".join(lines), False

    if tool == "kill_agent":
        agent_id = args.get("agent_id", "")
        if not agent_id:
            return "Error: agent_id required", False
        agent = db.agent_get(agent_id)
        if not agent:
            return f"Error: agent '{agent_id}' not found", False
        # Require confirmation unless token already provided
        confirm_token = args.get("confirm_token", "")
        if not confirm_token:
            return _issue_confirm_challenge(
                f"kill_agent {agent_id[:12]} (project: {agent.get('project','')})",
                args,
            ), True
        valid, err = _validate_confirm_token(confirm_token)
        if not valid:
            return f"Error: {err}", False
        import swarm.orchestrator as _orch
        killed = False
        handle = _orch._active_handles.get(agent_id)
        if handle and handle.get("process"):
            try:
                handle["process"].kill()
                killed = True
            except Exception:
                pass
        if not killed:
            pid = agent.get("pid")
            if pid:
                try:
                    os.kill(int(pid), 9)
                    killed = True
                except Exception:
                    pass
        if killed:
            db.agent_update_status(agent_id, "killed")
            task_id = agent.get("task_id")
            if task_id:
                task = db.task_get(task_id)
                if task and task.get("status") == "in_progress":
                    db.task_update(task_id, {"status": "pending"})
            return f"Killed agent {agent_id[:12]} (task reset to pending).", False
        return f"Error: could not kill agent {agent_id[:12]}", False

    # --- Task graph tools ---
    if tool == "list_tasks":
        project_filter = args.get("project", scope if scope != _UNIFIED_GLOBAL_SCOPE else "")
        status_filter = args.get("status", "")
        all_tasks = db.task_get_all()
        tasks = [
            t for t in all_tasks
            if (not project_filter or t.get("project") == project_filter)
            and (not status_filter or t.get("status") == status_filter)
        ]
        if not tasks:
            label = f" for project '{project_filter}'" if project_filter else ""
            return f"No tasks found{label}" + (f" with status '{status_filter}'" if status_filter else ""), False
        lines = [
            f"[{t['status']}] {t['id']}: {t.get('type','')} — {t.get('description','')[:100]}"
            for t in tasks[:50]
        ]
        return "\n".join(lines), False

    if tool == "create_task":
        _valid_types = {"feature","bug","refactor","polish","qa","harness_qa",
                        "art_pass","audit","research","plan","project_plan","scenario_qa"}
        project = args.get("project", scope if scope != _UNIFIED_GLOBAL_SCOPE else "")
        task_type = args.get("type", "")
        if not project:
            return "Error: project required", False
        if not task_type:
            return "Error: type is required (e.g. feature, bug, art_pass, qa)", False
        if task_type not in _valid_types:
            return f"Error: invalid type '{task_type}'. Must be one of: {', '.join(sorted(_valid_types))}", False
        # Validate project exists (project_get_all returns dict {name: data})
        all_projects = db.project_get_all()
        known_projects = set(all_projects.keys()) if isinstance(all_projects, dict) else {p["name"] for p in all_projects}
        if known_projects and project not in known_projects:
            close = [p for p in known_projects if project.lower().replace('-','') in p.lower().replace('-','') or p.lower().replace('-','') in project.lower().replace('-','')]
            suggestion = f" Did you mean: {', '.join(close[:3])}?" if close else f" Known projects: {', '.join(sorted(known_projects)[:10])}..."
            return f"Error: project '{project}' not found.{suggestion}", False
        task_payload = {
            "project": project,
            "type": task_type,
            "description": args.get("description", ""),
            "priority": int(args.get("priority", 50)),
            "max_attempts": 3,
            "metadata": {"created_by": "unified-chat"},
            "dependencies": args.get("dependencies", []),
        }
        try:
            resp = requests.post(
                "http://localhost:5001/api/tasks",
                json=task_payload,
                timeout=10,
            )
            result = resp.json()
            if "error" in result:
                return f"Error creating task: {result['error']}", False
            task_id = result.get("task", {}).get("id", "unknown")
            warning = result.get("warning", "")
            msg = f"Created task {task_id}"
            if warning:
                msg += f" ({warning})"
            return msg, False
        except Exception as e:
            return f"Error creating task: {e}", False

    if tool == "update_task":
        task_id = args.get("task_id", "")
        fields = {k: v for k, v in args.items() if k != "task_id"}
        if not task_id:
            return "Error: task_id required", False
        if not db.task_get(task_id):
            return f"Error: task '{task_id}' not found", False
        db.task_update(task_id, fields)
        return f"Updated task {task_id}", False

    if tool == "delete_task":
        task_id = args.get("task_id", "")
        if not task_id:
            return "Error: task_id required", False
        task = db.task_get(task_id)
        if not task:
            return f"Error: task '{task_id}' not found", False
        # Require confirmation unless token already provided
        confirm_token = args.get("confirm_token", "")
        if not confirm_token:
            desc = (task.get("description") or "")[:60]
            return _issue_confirm_challenge(
                f"delete_task {task_id} ({task.get('project','')}): {desc}",
                args,
            ), True
        valid, err = _validate_confirm_token(confirm_token)
        if not valid:
            return f"Error: {err}", False
        db.task_delete(task_id)
        return f"Deleted task {task_id}", False

    if tool == "get_critical_path":
        from swarm.dependencies import build_graph_from_tasks
        project = args.get("project", scope if scope != _UNIFIED_GLOBAL_SCOPE else "")
        all_tasks = db.task_get_all()
        graph = build_graph_from_tasks(all_tasks)
        chain = graph.get_critical_path(project=project if project else None)
        if not chain:
            return "No pending tasks or no critical path found.", False
        task_map = {t["id"]: t for t in all_tasks}
        lines = []
        for tid in chain:
            t = task_map.get(tid, {})
            desc = (t.get("description") or "").split("\n")[0][:100]
            lines.append(f"  {tid}: {desc}")
        return f"Critical path ({len(chain)} tasks):\n" + "\n".join(lines), False

    if tool == "get_subgraph":
        from swarm.dependencies import build_graph_from_tasks
        root_id = args.get("root_id", "")
        direction = args.get("direction", "downstream")
        depth = int(args.get("depth", 3))
        if not root_id:
            return "Error: root_id required", False
        all_tasks = db.task_get_all()
        graph = build_graph_from_tasks(all_tasks)
        task_ids = graph.get_subgraph(root_id, direction=direction, depth=depth)
        if not task_ids:
            return f"No tasks found in {direction} subgraph of '{root_id}'", False
        task_map = {t["id"]: t for t in all_tasks}
        lines = []
        for tid in task_ids:
            t = task_map.get(tid, {})
            desc = (t.get("description") or "").split("\n")[0][:80]
            lines.append(f"  [{t.get('status','')}] {tid}: {desc}")
        return f"Subgraph ({direction}, depth={depth}) from '{root_id}':\n" + "\n".join(lines), False

    if tool == "bulk_deps":
        from swarm.dependencies import build_graph_from_tasks
        ops = args.get("ops", [])
        applied, skipped, errors = [], [], []
        all_tasks = db.task_get_all()
        graph = build_graph_from_tasks(all_tasks)
        for op in ops:
            action = op.get("action", "")
            tid = op.get("task_id", "")
            dep_id = op.get("dep_id", "")
            if action == "add":
                if tid in graph.get_all_dependencies(dep_id) or tid == dep_id:
                    skipped.append(f"{tid}←{dep_id} (would create cycle)")
                else:
                    existing = db.task_get(tid) or {}
                    deps = list(existing.get("dependencies") or [])
                    if dep_id not in deps:
                        deps.append(dep_id)
                        db.task_update(tid, {"dependencies": deps})
                    applied.append(f"add {tid}←{dep_id}")
            elif action == "remove":
                existing = db.task_get(tid) or {}
                deps = [d for d in (existing.get("dependencies") or []) if d != dep_id]
                db.task_update(tid, {"dependencies": deps})
                applied.append(f"remove {tid}←{dep_id}")
            else:
                errors.append(f"unknown action '{action}'")
        return json.dumps({"applied": applied, "skipped": skipped, "errors": errors}), False

    # --- File/shell tools (project scope only) ---
    if scope == _UNIFIED_GLOBAL_SCOPE:
        return f"Error: tool '{tool}' is only available in project scope", False

    project_root = workspace / scope
    return _execute_debug_tool(tool, args, project_root, project=scope, db=db), False


_COMPACT_TOKEN_THRESHOLD = 800_000
_COMPACT_KEEP_TAIL = 4  # messages to keep verbatim at the end
_COMPACT_MARKER = "[COMPACTED]"


def _compact_unified_session(messages: list, config: dict) -> list:
    """Summarize the middle of a long session to keep it under the token threshold.

    Preserves: first 2 messages + last _COMPACT_KEEP_TAIL messages verbatim.
    Replaces middle with a single summary message.
    Returns the compacted message list (or original if no compaction needed).
    """
    estimated_tokens = sum(len(m.get("content", "")) for m in messages) // 2
    if estimated_tokens <= _COMPACT_TOKEN_THRESHOLD:
        return messages

    # Need at least keep_tail + 3 messages to compact meaningfully
    min_messages = _COMPACT_KEEP_TAIL + 3
    if len(messages) < min_messages:
        return messages

    head = messages[:2]
    tail = messages[-_COMPACT_KEEP_TAIL:]
    middle = messages[2:-_COMPACT_KEEP_TAIL]

    if not middle:
        return messages

    middle_text = "\n\n".join(
        f"{m['role'].upper()}: {m.get('content','')[:500]}" for m in middle
    )
    summary_prompt = (
        "Summarize the following conversation excerpt concisely. "
        "Focus on decisions made, actions taken, and key findings. "
        "Omit filler exchanges.\n\n"
        + middle_text
    )
    try:
        summary = _chat_call_llm(
            "You are a conversation summarizer. Output only the summary, no preamble.",
            [{"role": "user", "content": summary_prompt}],
            config,
        )
    except Exception:
        return messages  # If compaction fails, keep original

    summary_message = {
        "role": "user",
        "content": f"{_COMPACT_MARKER}\n[Earlier conversation summarized]\n{summary}",
    }
    return head + [summary_message] + tail


def _run_unified_tool_loop(
    system_prompt: str,
    history: list,
    config: dict,
    scope: str,
    workspace: Path,
    data_dir: Path,
    db,
    session_id: str,
    max_rounds: int = 8,
) -> tuple[str, list]:
    """Run the unified chat tool loop with stop-signal and compaction support."""
    stop_event = _UNIFIED_STOP_EVENTS.get(session_id)
    tool_calls_log = []
    # Compact if history is long before starting
    messages = _compact_unified_session(list(history), config)

    for _ in range(max_rounds):
        if stop_event and stop_event.is_set():
            break

        response_text = _chat_call_llm(system_prompt, messages, config)
        parsed_calls = _parse_tool_calls(response_text)
        if not parsed_calls:
            return response_text, tool_calls_log

        tool_results = []
        for call in parsed_calls:
            if stop_event and stop_event.is_set():
                break
            tool = call.get("tool", "unknown")
            args = call.get("args", {})

            result, _ = _execute_unified_tool(
                tool, args, scope, workspace, data_dir, db, config
            )
            tool_calls_log.append({"tool": tool, "args": args, "result": result})
            tool_results.append(f"[TOOL_RESULT: {tool}]\n{result}\n[/TOOL_RESULT]")

        messages.append({"role": "assistant", "content": response_text})
        messages.append({"role": "user", "content": "\n\n".join(tool_results)})

    if stop_event and stop_event.is_set():
        return (
            "⚠️ Stopped by user. Tell me what you'd like to do instead.",
            tool_calls_log,
        )

    final = _chat_call_llm(system_prompt, messages, config)
    return final, tool_calls_log


def register_routes(app, config, config_file, _config_write_lock, orchestrator, workspace,
                    db, auto_mode_state, generate_task_script):
    """Register chat and project-creation routes on the Flask app."""

    import re as _re

    # ------------------------------------------------------------------ #
    #  Unified chat  (POST /api/unified-chat)                              #
    # ------------------------------------------------------------------ #

    @app.route("/api/unified-chat", methods=["POST"])
    def unified_chat():
        """Unified swarm + project chat endpoint.

        Request JSON:
          { "message": "...", "session_id": "<uuid>", "project": "my-project" }
          project is optional; omit for global (swarm-wide) scope.

        Response JSON:
          { "reply": "...", "session_id": "<uuid>", "tool_calls": [...], "scope": "global"|"<project>" }
        """
        data = request.get_json(force=True) or {}
        user_message = (data.get("message") or "").strip()
        session_id = (data.get("session_id") or "").strip() or str(uuid.uuid4())
        project = (data.get("project") or "").strip()
        scope = project if project else _UNIFIED_GLOBAL_SCOPE

        if not user_message:
            return jsonify({"error": "message required"}), 400

        data_dir = Path(app.config.get("DATA_DIR", "data"))

        # Register stop event for this session
        if session_id not in _UNIFIED_STOP_EVENTS:
            _UNIFIED_STOP_EVENTS[session_id] = threading.Event()
        _UNIFIED_STOP_EVENTS[session_id].clear()

        history = _load_session(data_dir, scope, session_id)
        is_new_session = len(history) == 0

        history.append({"role": "user", "content": user_message})
        _save_session(data_dir, scope, session_id, history)

        memory_block = _load_unified_memory(data_dir, project=project)

        if scope == _UNIFIED_GLOBAL_SCOPE:
            try:
                state_snapshot = _build_state_snapshot(db)
            except Exception as e:
                state_snapshot = f"(Could not load swarm state: {e})"
            system_prompt = _UNIFIED_GLOBAL_SYSTEM_PROMPT.format(
                tools=_UNIFIED_TOOLS_DESCRIPTION,
                memory=memory_block,
                state=state_snapshot,
            )
        else:
            project_context = ""
            if is_new_session:
                project_context = _build_project_context(
                    Path(workspace), project, data_dir, db
                )
            context_block = (
                f"\n\n--- PROJECT CONTEXT (loaded at session start) ---\n{project_context}"
                if project_context else ""
            )
            system_prompt = _UNIFIED_PROJECT_SYSTEM_PROMPT.format(
                project=project,
                tools=_UNIFIED_TOOLS_DESCRIPTION,
                memory=memory_block,
                context=context_block,
            )

        try:
            response_text, tool_calls = _run_unified_tool_loop(
                system_prompt, history, config,
                scope=scope,
                workspace=Path(workspace),
                data_dir=data_dir,
                db=db,
                session_id=session_id,
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            # Clear stop event after response
            if session_id in _UNIFIED_STOP_EVENTS:
                _UNIFIED_STOP_EVENTS[session_id].clear()

        history.append({"role": "assistant", "content": response_text})
        _save_session(data_dir, scope, session_id, history)

        return jsonify({
            "reply": response_text,
            "session_id": session_id,
            "tool_calls": tool_calls,
            "scope": "global" if scope == _UNIFIED_GLOBAL_SCOPE else scope,
        })

    @app.route("/api/unified-chat/<session_id>/stop", methods=["POST"])
    def stop_unified_chat(session_id):
        """Emergency stop for unified chat tool loop.

        Request JSON: { "project": "..." }  (optional — for logging only)
        Response JSON: { "stopped": true, "session_id": "..." }
        """
        event = _UNIFIED_STOP_EVENTS.get(session_id)
        if event:
            event.set()
        return jsonify({"stopped": True, "session_id": session_id})

    @app.route("/api/unified-chat/<session_id>", methods=["DELETE"])
    def delete_unified_chat(session_id):
        """Delete a unified chat session.

        Request JSON: { "project": "..." }  (optional; omit for global)
        """
        data = request.get_json(force=True) or {}
        project = (data.get("project") or "").strip()
        scope = project if project else _UNIFIED_GLOBAL_SCOPE
        data_dir = Path(app.config.get("DATA_DIR", "data"))
        deleted = _delete_session(data_dir, scope, session_id)
        _UNIFIED_STOP_EVENTS.pop(session_id, None)
        return jsonify({"deleted": deleted, "session_id": session_id})

    @app.route("/api/unified-chat/<session_id>/last", methods=["DELETE"])
    def rollback_unified_chat(session_id):
        """Roll back the last user+assistant exchange.

        Request JSON: { "project": "..." }  (optional; omit for global)
        """
        data = request.get_json(force=True) or {}
        project = (data.get("project") or "").strip()
        scope = project if project else _UNIFIED_GLOBAL_SCOPE
        data_dir = Path(app.config.get("DATA_DIR", "data"))
        history = _load_session(data_dir, scope, session_id)

        turns_removed = 0
        while history and history[-1]["role"] == "assistant":
            history.pop()
            turns_removed += 1
        while history and history[-1]["role"] == "user":
            history.pop()
            turns_removed += 1

        _save_session(data_dir, scope, session_id, history)
        return jsonify({
            "rolled_back": turns_removed > 0,
            "turns_removed": turns_removed,
            "session_id": session_id,
        })

    @app.route("/api/chat", methods=["POST"])
    def chat():
        """Conversational swarm manager — can observe state and execute actions.

        Request JSON:
          { "message": "...", "history": [{"role": "user"|"assistant", "content": "..."}] }

        Response JSON:
          { "response": "...", "history": [...], "actions_taken": [...] }

        The LLM may emit [ACTION]{...}[/ACTION] blocks to take actions.
        Supported action types:
          kill_agent        {"type":"kill_agent","agent_id":"<full-id>"}
          kill_all_agents   {"type":"kill_all_agents"}
          create_task       {"type":"create_task","project":"...","task_type":"bug","description":"...","priority":80,"dependencies":[]}
          set_auto_mode     {"type":"set_auto_mode","enabled":true|false}
          restart_server    {"type":"restart_server"}
        """
        data = request.get_json(force=True) or {}
        user_message = (data.get("message") or "").strip()
        history = data.get("history") or []
        if not user_message:
            return jsonify({"error": "message required"}), 400

        # Build live state snapshot
        try:
            state_snapshot = _build_state_snapshot(db)
        except Exception as e:
            state_snapshot = f"(Could not load swarm state: {e})"

        system_prompt = f"""You are the Swarm Controller manager — you can both observe the swarm and take actions on it.

CAPABILITIES:
- Answer questions about projects, tasks, agents, failures
- Kill specific agents or all agents
- Create bug tasks or feature tasks for any project
- Enable or disable auto mode
- Restart the server

HOW TO TAKE ACTIONS:
When you want to take an action, emit one or more [ACTION] blocks in your response (anywhere in the text). The server will execute them and inject results back.

Examples:
  [ACTION]{{"type":"kill_agent","agent_id":"full-agent-uuid-here"}}[/ACTION]
  [ACTION]{{"type":"kill_all_agents"}}[/ACTION]
  [ACTION]{{"type":"create_task","project":"chrome-rebellion","task_type":"bug","description":"Fix parse error in lab.gd","priority":80}}[/ACTION]
  [ACTION]{{"type":"set_auto_mode","enabled":false}}[/ACTION]
  [ACTION]{{"type":"restart_server"}}[/ACTION]

IMPORTANT RULES:
- Always confirm with the user before taking destructive actions (kill, restart). Ask "Shall I go ahead?" and only emit [ACTION] blocks after they confirm.
- Use the full agent ID from the state snapshot (not truncated).
- Be concise. Use plain text, minimal bullet points.
- For non-destructive actions (creating a bug task) you can act immediately without asking.
- After emitting actions, describe what you did in plain text after the [ACTION] block.

{state_snapshot}"""

        messages = list(history) + [{"role": "user", "content": user_message}]

        try:
            reply = _chat_call_llm(system_prompt, messages, config)
        except Exception as e:
            reply = f"(LLM error: {e})"

        # Parse and execute [ACTION] blocks
        action_pattern = _re.compile(r'\[ACTION\](.*?)\[/ACTION\]', _re.DOTALL)
        action_blocks = action_pattern.findall(reply)
        actions_taken = []
        if action_blocks:
            try:
                action_results = _execute_chat_actions(
                    action_blocks, db, config, config_file, _config_write_lock,
                    orchestrator, auto_mode_state,
                )
                actions_taken = action_blocks
                # If any task was created and auto mode is on, fill slots immediately
                has_create = any(
                    '"create_task"' in b or "'create_task'" in b for b in action_blocks
                )
                if has_create and auto_mode_state["enabled"]:
                    try:
                        orchestrator.fill_slots(generate_task_script)
                    except Exception:
                        pass
                # Strip the [ACTION] tags from the visible reply, append results
                clean_reply = action_pattern.sub('', reply).strip()
                if action_results:
                    reply = clean_reply + f"\n\n**Actions executed:**\n{action_results}"
                else:
                    reply = clean_reply
            except Exception as e:
                reply = reply + f"\n\n(Action execution error: {e})"

        updated_history = messages + [{"role": "assistant", "content": reply}]
        if len(updated_history) > 20:
            updated_history = updated_history[-20:]

        return jsonify({"response": reply, "history": updated_history, "actions_taken": actions_taken})

    @app.route("/api/project-chat", methods=["POST"])
    def project_chat():
        """Conversational project creation assistant.

        The LLM asks clarifying questions, builds the concept, then emits
        [CREATE_PROJECT]{"name": "...", "description": "..."}[/CREATE_PROJECT]
        when it has enough information. The server intercepts the tool call,
        queues the project_create task, and returns confirmation.
        """
        data = request.get_json(force=True) or {}
        user_message = (data.get("message") or "").strip()
        history = data.get("history") or []
        project_type = _normalize_project_type(data.get("project_type", "godot"))
        if not user_message:
            return jsonify({"error": "message required"}), 400

        existing_projects = list(db.project_get_all().keys())
        force_prd_generation = _project_chat_user_confirmed_generation(history, user_message)

        system_prompt = f"""You are a Godot game project designer. Your job is to gather requirements through iterative questions, then produce a structured PRD that the swarm agents will use to build the game.

EXISTING PROJECTS (avoid name conflicts): {', '.join(existing_projects) or 'none yet'}

YOUR PROCESS — THREE PHASES:

PHASE 1 — QUESTIONS: Ask 3-5 clarifying questions with lettered options, one set at a time.
PHASE 2 — PLAN PREVIEW: Once you have enough context, write a short plan preview with:
- project name and concept
- planned tasks
- an explicit dependency map per task
End with: "Want to change anything, or shall I generate the tasks?"
PHASE 3 — PRD GENERATION: Only when the user explicitly says to proceed (e.g. "yes", "go ahead", "create", "looks good"), emit the full [PRD] block.

FORMAT QUESTIONS LIKE THIS (always use lettered options so users can reply "1A, 2C"):
1. What is the core mechanic?
   A. Arcade action (fast reflexes, score attack)
   B. Puzzle / strategy (think before you act)
   C. Exploration / adventure
   D. Other: [specify]

ALWAYS ASK ABOUT QUALITY GATES (in Phase 1):
What quality checks should pass for each user story in the NEW project?
   A. No script errors on load (simplest — no test framework needed)
   B. GUT tests pass (headless Godot run) — the project bootstrap installs GUT automatically
   C. Other: [specify]

PHASE 2 PLAN SUMMARY EXAMPLE:
Here's the plan for **snake-game**:
- Project: snake-game (Godot 4, pixel art, top-down)
- US-001: Player movement (grid-based, 4 directions)
- US-002: Food spawning and snake growth
- US-003: Collision detection and game over
- US-004: Score display and high score
- US-005: Main menu and restart

Dependency map:
- US-001 Player movement -> []
- US-002 Food spawning and snake growth -> [US-001]
- US-003 Collision detection and game over -> [US-001, US-002]
- US-004 Score display and high score -> [US-002, US-003]
- US-005 Main menu and restart -> [US-003, US-004]

Quality gate: GUT tests pass.

Want to change anything, or shall I generate the tasks?

DEPENDENCY RULES (CRITICAL — apply inside the PRD):
- Use a DAG, NOT a chain. Most stories should run in parallel.
- Foundation stories (core scene, data model, foundational gameplay systems) come first; everything that builds on them fans out in parallel.
- Do NOT create stories for generic project bootstrap, GUT installation, harness installation, StateServer installation, or placeholder tests. New Godot projects get those automatically during project creation.
- WRONG: US-001 → US-002 → US-003 → US-004 (pure chain)
- RIGHT: US-001 → [US-002, US-003, US-004 in parallel] → US-005
- Add a `depends-on: US-NNN` line inside each story block that needs a predecessor. Use `depends-on: US-NNN, US-MMM` for multiple.
- Stories with no dependency get no depends-on line.
- The dependency map in Phase 2 and the `depends-on` lines in Phase 3 must describe the same graph.

PHASE 3 — ONLY when user confirms, emit this EXACT format (dependencies go INSIDE each story block):

[PRD]
# PRD: [Game Title]
project-name: kebab-case-slug

## Overview
[2-3 sentence description]

## Quality Gates
These must pass for every user story:
- [quality gate command]

## User Stories

### US-001: [Foundation title — no deps]
**Description:** As a player, I want [feature] so that [benefit].
**Acceptance Criteria:**
- [ ] [verifiable criterion]

### US-002: [Title that needs US-001]
depends-on: US-001
**Description:** As a player, I want [feature] so that [benefit].
**Acceptance Criteria:**
- [ ] [verifiable criterion]

### US-003: [Title that also needs US-001, parallel with US-002]
depends-on: US-001
**Description:** As a player, I want [feature] so that [benefit].
**Acceptance Criteria:**
- [ ] [verifiable criterion]

### US-004: [Title that needs US-002 and US-003]
depends-on: US-002, US-003
**Description:** As a player, I want [feature] so that [benefit].
**Acceptance Criteria:**
- [ ] [verifiable criterion]

## Non-Goals
- [what this will NOT include]

## Technical Notes
- Godot 4, GDScript
- GUT for testing (addons/gut/) is already installed for new Godot projects when applicable
[/PRD]

OTHER RULES:
- Never emit [PRD] before the user explicitly confirms in Phase 3
- In Phase 2, write plain text only — no [PRD] tags
- Stories should be small (one agent session each), 5-10 total
- [PRD] block must be the last thing in your message when emitted
- ONLY create tasks for the new project being designed. NEVER create tasks for other existing projects."""
        if project_type == "python":
            system_prompt = _python_project_chat_prompt(existing_projects)

        if force_prd_generation:
            system_prompt += """

PHASE OVERRIDE:
- The user has explicitly approved the current plan preview.
- Do NOT ask more questions.
- Do NOT restate the preview.
- Emit the final [PRD] block now as the last thing in your response.
"""

        messages = list(history) + [{"role": "user", "content": user_message}]

        # Call LLM
        try:
            reply = _chat_call_llm(system_prompt, messages, config)
        except Exception as e:
            return jsonify({"error": f"LLM error: {e}"}), 500

        # Check for PRD output — parse into task preview instead of treating it as plain chat.
        tasks_preview = None
        project_name = None
        overview = ""
        quality_gates: list[str] = []
        prd_content = _extract_prd_content(reply)
        if prd_content:
            try:
                preview_note = ""
                prd_stats = _prd_dependency_annotation_stats(prd_content)
                # Extract project-name
                name_match = _re.search(r'^project-name:\s*([a-z0-9-]+)', prd_content, _re.MULTILINE)
                project_name = name_match.group(1).strip() if name_match else f"project-{int(time.time())}"
                # Extract overview section
                ov_match = _re.search(r'##\s+Overview\s*\n(.*?)(?=\n##|\Z)', prd_content, _re.DOTALL)
                if ov_match:
                    overview = ov_match.group(1).strip()
                quality_gates = _extract_prd_quality_gates(prd_content)
                tasks_preview = _parse_prd_tasks_preview(prd_content, project_name)

                quality_errors = _task_graph_quality_errors(tasks_preview, project_type=project_type)
                if quality_errors:
                    repaired_preview, repair_rounds, repair_errors = _repair_task_graph_with_llm(
                        project_name,
                        overview,
                        tasks_preview,
                        quality_errors,
                        config,
                        project_type=project_type,
                    )
                    if repaired_preview:
                        tasks_preview = repaired_preview
                        preview_note = f"Refined the dependency graph automatically after {repair_rounds} validation round(s)."
                    else:
                        reply += (
                            "\n\nThe draft task graph is not valid enough to create yet:\n- "
                            + "\n- ".join(repair_errors[:6])
                            + "\n\nPlease refine the plan and regenerate dependencies."
                        )
                        tasks_preview = None
                        project_name = None

                if tasks_preview and project_name and prd_stats["stories"] >= 8 and prd_stats["stories_with_dep_lines"] < max(2, prd_stats["stories"] // 4):
                    sparse_note = (
                        f"Warning: the PRD only included explicit depends-on lines for "
                        f"{prd_stats['stories_with_dep_lines']} of {prd_stats['stories']} stories."
                    )
                    preview_note = f"{preview_note}\n{sparse_note}".strip()

                # Replace the model-authored PRD display with the authoritative
                # parsed preview so the visible graph matches tasks_preview exactly.
                if tasks_preview and project_name:
                    reply = _render_project_preview_from_tasks(project_name, overview, tasks_preview, note=preview_note)
                else:
                    reply = reply.replace('[PRD]', '').replace('[/PRD]', '').strip()
            except Exception as e:
                reply += f"\n\n(Error parsing PRD: {e})"

        updated_history = messages + [{"role": "assistant", "content": reply}]
        if len(updated_history) > 30:
            updated_history = updated_history[-30:]

        # Build overview string to pass through to create-project-tasks
        resp_overview = ""
        preview_diagnostics = None
        if tasks_preview and project_name:
            try:
                task_lines = "\n".join(
                    f"- {t['description'].split(chr(10))[0]}" for t in tasks_preview
                )
                resp_overview = (overview or "") + ("\n\n## Features\n" + task_lines if task_lines else "")
            except Exception:
                resp_overview = overview or ""
            preview_diagnostics = _graph_diagnostics_payload(tasks_preview)

        return jsonify({
            "response": reply,
            "history": updated_history,
            "tasks_preview": tasks_preview,
            "project_name": project_name,
            "project_type": project_type,
            "overview": resp_overview,
            "quality_gates": quality_gates,
            "design_doc": prd_content or "",
            "preview_diagnostics": preview_diagnostics,
        })

    @app.route("/api/project-ideas", methods=["POST"])
    def project_ideas():
        """Generate lightweight project ideas for the new-project chat."""
        data = request.get_json(force=True) or {}
        raw_kind = (data.get("kind") or "mixed").strip().lower()
        kind = raw_kind if raw_kind in {"mixed", "game", "programming"} else "mixed"
        history = data.get("history") or []
        existing_projects = list(db.project_get_all().keys())
        kind_instruction = {
            "game": "Generate game ideas only. Every idea must be a game concept.",
            "programming": (
                "Generate programming/software project ideas only. Every idea must be a software project, "
                "developer tool, automation, data tool, web/API service, or library. Do not include games, "
                "Godot projects, game mechanics, levels, enemies, or game vertical slices."
            ),
            "mixed": "Generate a mix: mostly games, plus a few programming/software project ideas.",
        }[kind]
        type_label = "Programming" if kind == "programming" else ("Game" if kind == "game" else "Game/Programming")
        system_prompt = f"""You generate concise project ideas for a human using a swarm project builder.

EXISTING PROJECTS (avoid name conflicts): {', '.join(existing_projects) or 'none yet'}

RULES:
- {kind_instruction}
- Return 8 ideas.
- Do not ask clarifying questions.
- Do not generate a PRD.
- Do not emit [PRD] tags or task lists.
- Keep each idea small enough for a first vertical slice.
- Include project name, type, one-sentence concept, and a tiny first slice.
- Prefer ideas that can later become a healthy dependency DAG with parallel branches.

FORMAT:
## Project Ideas
1. **kebab-case-name** — {type_label}
   Concept: ...
   First slice: ...
"""
        # Do not pass stale project-chat history here: a previous game greeting can
        # overpower the selected "programming" modality and make the idea list drift.
        messages = [{
            "role": "user",
            "content": f"Generate {kind} project ideas I can choose from.",
        }]
        try:
            reply = _chat_call_llm(system_prompt, messages, config)
        except Exception as e:
            return jsonify({"error": f"LLM error: {e}"}), 500

        updated_history = list(history) + [
            {"role": "user", "content": f"Generate {kind} project ideas."},
            {"role": "assistant", "content": reply},
        ]
        if len(updated_history) > 30:
            updated_history = updated_history[-30:]
        return jsonify({"ideas": reply, "history": updated_history})

    @app.route("/api/create-project-tasks", methods=["POST"])
    def create_project_tasks():
        """Create a project and its tasks from a confirmed PRD task list."""
        data = request.get_json(force=True) or {}
        project_name = data.get("project_name", "").strip()
        project_type = _normalize_project_type(data.get("project_type", "godot"))
        tasks = data.get("tasks", [])
        if not project_name:
            return jsonify({"error": "project_name required"}), 400
        if not tasks:
            return jsonify({"error": "tasks list is empty"}), 400

        project_path = Path(workspace) / project_name
        existing = db.project_get(project_name)
        if existing:
            return jsonify({
                "error": f"Project '{project_name}' already exists. New project chat cannot append a fresh graph onto an existing project.",
                "retryable": True,
                "chat_recovery_assistant": (
                    f"The project name `{project_name}` is already in use. "
                    "Choose a new project name or start a fresh new-project chat if this draft resumed an older project by mistake."
                ),
            }), 409
        overview_text = data.get("overview", "").strip()
        source_design_doc = (
            data.get("design_doc")
            or data.get("prd")
            or data.get("prd_text")
            or ""
        )
        quality_gates = data.get("quality_gates")
        created_tasks = []

        try:
            git_log, gitea_url = _scaffold_project_repo(
                project_name,
                project_path,
                project_type,
                overview_text,
                config,
            )
        except Exception as e:
            return jsonify({"error": f"Project scaffold failed: {e}"}), 400

        closure_proposal = propose_project_closure(
            project_name,
            project_path,
            project_type,
            context={
                "overview": overview_text,
                "quality_gates": quality_gates,
                "tasks": tasks,
                "design_doc": source_design_doc,
            },
        )

        # 4. Register project in DB and add to managed_projects
        cfg_path = Path(__file__).parent.parent / "config.json"
        if not existing:
            db.project_upsert({
                "name": project_name,
                "path": str(project_path),
                "file_extensions": _project_file_extensions(project_type),
                "files": {},
                "managed": True,
                "profile": closure_proposal["profile"],
                "closure_mode": closure_proposal["closure_spec"].get("mode", "build"),
                "closure_spec": closure_proposal["closure_spec"],
            })
        else:
            db.project_upsert({
                **existing,
                "managed": True,
                "file_extensions": _project_file_extensions(project_type),
                "profile": closure_proposal["profile"],
                "closure_mode": closure_proposal["closure_spec"].get("mode", "build"),
                "closure_spec": closure_proposal["closure_spec"],
            })
        head_id = ensure_project_head(db, project_name)
        if project_name not in config.get("managed_projects", []):
            config.setdefault("managed_projects", []).append(project_name)
            import swarm.orchestrator as _orch
            _orch.MANAGED_PROJECTS = config["managed_projects"]
            # Persist to config.json
            try:
                with _config_write_lock:
                    existing_cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
                    existing_cfg["managed_projects"] = config["managed_projects"]
                    cfg_path.write_text(json.dumps(existing_cfg, indent=2))
            except Exception as e:
                print(f"[Warning] Could not persist managed_projects: {e}")

        # Auto-scan so file sizes are populated immediately
        try:
            exts = tuple(_project_file_extensions(project_type))
            orchestrator.update_project_registry(exts)
        except Exception as e:
            print(f"[Warning] Auto-scan after project creation failed: {e}")

        def _delete_attempt_tasks(task_ids):
            for tid in task_ids:
                try:
                    db.task_delete(tid)
                except Exception:
                    pass

        current_tasks = [dict(t) for t in tasks]
        correction_rounds = 0
        creation_validation_errors = []
        max_correction_rounds = int(
            app.config.get(
                "PROJECT_CREATION_RETRY_ROUNDS_OVERRIDE",
                config.get("project_creation_retry_rounds", 2),
            )
        )

        for attempt in range(max_correction_rounds + 1):
            created_tasks = []
            pending_batch = []
            for t in current_tasks:
                task_id = t.get("id", f"{project_name}-{uuid.uuid4().hex[:12]}")
                pending_batch.append({
                    "id": task_id,
                    "project": project_name,
                    "type": t.get("type", "feature"),
                    "priority": t.get("priority", 50),
                    "max_attempts": t.get("max_attempts", 3),
                    "description": t.get("description", ""),
                    "dependencies": list(t.get("dependencies", []) or []),
                    "metadata": {"prd_generated": True, "project_head_id": head_id},
                })
            for task in anchor_project_batch_roots(pending_batch, head_id):
                try:
                    db.task_upsert(task)
                except ValueError as e:
                    cycle_errors = [f"Invalid dependency graph in task list: {e}"]
                    repair_context = _render_graph_repair_context(
                        current_tasks,
                        cycle_errors,
                        allowed_external_roots=[head_id],
                    )
                    _delete_attempt_tasks([x["id"] for x in created_tasks])
                    _cleanup_failed_project_creation(
                        db,
                        project_name,
                        project_path,
                        config,
                        config_file=cfg_path,
                        config_write_lock=_config_write_lock,
                    )
                    return jsonify({
                        "error": "Project creation validation failed",
                        "details": cycle_errors,
                        "retryable": True,
                        "graph_diagnostics": _graph_diagnostics_payload(
                            current_tasks,
                            cycle_errors,
                            allowed_external_roots=[head_id],
                        ),
                        "chat_recovery_assistant": (
                            "The generated dependency graph contains a cycle, so the project was not created. "
                            "Tell me which task should come first or which stories should run in parallel, and I will regenerate the plan."
                            "\n\nLatest validation issues:\n- "
                            + "\n- ".join(cycle_errors[:8])
                            + "\n\nGraph diagnostics:\n"
                            + repair_context
                        ),
                        "task_id": task["id"],
                    }), 400
                created_tasks.append(task)

            creation_validation_errors = _project_creation_validation_errors(
                project_path,
                created_tasks,
                allowed_external_roots=[head_id],
                project_type=project_type,
            )
            # Defensive check: re-read exactly what was persisted so star/fan
            # regressions cannot slip through if preview-time state diverges.
            persisted_tasks = [
                db.task_get(task["id"]) or task
                for task in created_tasks
            ]
            persisted_validation_errors = _project_creation_validation_errors(
                project_path,
                persisted_tasks,
                allowed_external_roots=[head_id],
                project_type=project_type,
            )
            if persisted_validation_errors:
                creation_validation_errors = list(dict.fromkeys(
                    list(creation_validation_errors or []) + list(persisted_validation_errors)
                ))
            if not creation_validation_errors:
                break

            _delete_attempt_tasks([t["id"] for t in created_tasks])
            created_tasks = []
            if attempt >= max_correction_rounds:
                break

            repaired_tasks, _, repair_errors = _repair_task_graph_with_llm(
                project_name,
                overview_text,
                current_tasks,
                creation_validation_errors,
                config,
                project_type=project_type,
            )
            if not repaired_tasks:
                creation_validation_errors = repair_errors or creation_validation_errors
                break
            current_tasks = repaired_tasks
            correction_rounds += 1

        if creation_validation_errors:
            repair_context = _render_graph_repair_context(
                current_tasks,
                creation_validation_errors,
                allowed_external_roots=[head_id],
            )
            retry_note = (
                "Automatic project-graph repair could not produce a valid dependency DAG. "
                "Tell me which systems should unlock others or which tasks should converge, and I will regenerate the plan."
            )
            _cleanup_failed_project_creation(
                db,
                project_name,
                project_path,
                config,
                config_file=cfg_path,
                config_write_lock=_config_write_lock,
            )
            return jsonify({
                "error": "Project creation validation failed",
                "details": creation_validation_errors,
                "retryable": True,
                "graph_diagnostics": _graph_diagnostics_payload(
                    current_tasks,
                    creation_validation_errors,
                    allowed_external_roots=[head_id],
                ),
                "chat_recovery_assistant": (
                    f"{retry_note}\n\nLatest validation issues:\n- "
                    + "\n- ".join(creation_validation_errors[:8])
                    + "\n\nGraph diagnostics:\n"
                    + repair_context
                ),
            }), 400

        try:
            _write_project_design_doc(
                project_path,
                project_name,
                overview_text,
                created_tasks,
                project_type,
                quality_gates=quality_gates,
                source_doc=source_design_doc,
            )
        except Exception as e:
            _cleanup_failed_project_creation(
                db,
                project_name,
                project_path,
                config,
                created_task_ids=[t["id"] for t in created_tasks],
                config_file=cfg_path,
                config_write_lock=_config_write_lock,
            )
            return jsonify({"error": f"Project design doc generation failed: {e}"}), 400

        try:
            write_project_closure_doc(project_path, project_name, closure_proposal)
        except Exception as e:
            _cleanup_failed_project_creation(
                db,
                project_name,
                project_path,
                config,
                created_task_ids=[t["id"] for t in created_tasks],
                config_file=cfg_path,
                config_write_lock=_config_write_lock,
            )
            return jsonify({"error": f"Project closure doc generation failed: {e}"}), 400

        return jsonify({
            "created": len(created_tasks),
            "project": project_name,
            "project_type": project_type,
            "closure_proposal": closure_proposal,
            "task_ids": [t["id"] for t in created_tasks],
            "git_log": git_log,
            "gitea_url": gitea_url,
            "correction_rounds": correction_rounds,
        })

    @app.route("/api/projects/<project_name>/append-phase", methods=["POST"])
    def append_project_phase(project_name):
        """Append a validated task DAG phase to an existing project."""
        data = request.get_json(force=True) or {}
        phase_name = (data.get("phase_name") or data.get("name") or "Appended Phase").strip()
        overview_text = (data.get("overview") or "").strip()
        project_type = _normalize_project_type(data.get("project_type") or "")
        tasks = data.get("tasks", [])

        project = db.project_get(project_name)
        if not project:
            return jsonify({"error": f"Project '{project_name}' not found"}), 404
        if not project_type:
            project_type = _normalize_project_type(project.get("profile") or "godot")

        project_path = Path(workspace) / project_name
        if not project_path.exists():
            return jsonify({"error": f"Project path not found: {project_path}"}), 404

        anchor_task_id = (data.get("anchor_task_id") or "").strip() or ensure_project_head(db, project_name)
        if not anchor_task_id:
            return jsonify({"error": "Could not determine project phase anchor"}), 400
        anchor_task = db.task_get(anchor_task_id) or db.task_get_completed_record(anchor_task_id)
        if not anchor_task or anchor_task.get("project") != project_name:
            return jsonify({"error": f"Invalid anchor_task_id '{anchor_task_id}' for project '{project_name}'"}), 400

        pending_batch = []
        batch_ids: set[str] = set()
        boundary_anchor_id = anchor_task_id
        use_phase_qa = bool(data.get("qa_before_phase") or data.get("phase_qa"))
        use_phase_gate = bool(data.get("phase_gate") or data.get("pause_before_phase"))
        if not tasks and not (use_phase_qa or use_phase_gate):
            return jsonify({"error": "tasks list is empty"}), 400
        phase_qa_id = (data.get("phase_qa_id") or f"{project_name}-{_re.sub(r'[^a-z0-9]+', '-', phase_name.lower()).strip('-')}-qa").strip()
        if use_phase_qa:
            if db.task_get(phase_qa_id) or db.task_get_completed_record(phase_qa_id):
                return jsonify({"error": f"Phase QA id already exists: {phase_qa_id}"}), 400
            qa_type = data.get("phase_qa_type") or data.get("qa_task_type") or "harness_qa"
            batch_ids.add(phase_qa_id)
            pending_batch.append({
                "id": phase_qa_id,
                "project": project_name,
                "type": qa_type,
                "priority": 90,
                "max_attempts": 3,
                "description": (
                    f"Phase QA checkpoint: {phase_name}\n\n"
                    "Run QA/signoff against the current project state before the next phase unlocks. "
                    "File bugs for failures and complete only when the phase boundary is acceptable."
                ),
                "dependencies": [anchor_task_id],
                "metadata": {
                    "phase_name": phase_name,
                    "phase_anchor_task_id": anchor_task_id,
                    "phase_boundary": True,
                },
            })
            boundary_anchor_id = phase_qa_id

        phase_gate_id = (data.get("phase_gate_id") or f"{project_name}-{_re.sub(r'[^a-z0-9]+', '-', phase_name.lower()).strip('-')}-gate").strip()
        if use_phase_gate:
            if db.task_get(phase_gate_id) or db.task_get_completed_record(phase_gate_id):
                return jsonify({"error": f"Phase gate id already exists: {phase_gate_id}"}), 400
            batch_ids.add(phase_gate_id)
            pending_batch.append({
                "id": phase_gate_id,
                "project": project_name,
                "type": "phase_gate",
                "priority": 100,
                "max_attempts": 1,
                "description": (
                    f"Phase gate: {phase_name}\n\n"
                    "This task intentionally pauses the project graph. "
                    "Release it explicitly when testing/signoff is complete."
                ),
                "dependencies": [boundary_anchor_id],
                "metadata": {
                    "phase_name": phase_name,
                    "phase_anchor_task_id": anchor_task_id,
                    "phase_boundary_dep": boundary_anchor_id,
                    "release_endpoint": f"/api/projects/{project_name}/phase-gates/{phase_gate_id}/release",
                },
            })
            boundary_anchor_id = phase_gate_id

        for t in tasks:
            if not isinstance(t, dict):
                return jsonify({"error": "Each task must be an object"}), 400
            task_id = t.get("id") or f"{project_name}-phase-{uuid.uuid4().hex[:12]}"
            if db.task_get(task_id) or db.task_get_completed_record(task_id) or task_id in batch_ids:
                return jsonify({"error": f"Task id already exists or is duplicated: {task_id}"}), 400
            batch_ids.add(task_id)
            pending_batch.append({
                "id": task_id,
                "project": project_name,
                "type": t.get("type", "feature"),
                "priority": t.get("priority", 50),
                "max_attempts": t.get("max_attempts", 3),
                "description": t.get("description", ""),
                "dependencies": list(t.get("dependencies", []) or []),
                "metadata": {
                    **(t.get("metadata") or {}),
                    "prd_generated": True,
                    "phase_name": phase_name,
                    "phase_anchor_task_id": anchor_task_id,
                },
            })

        for task in pending_batch:
            for dep in list(task.get("dependencies") or []):
                if dep in batch_ids or dep == anchor_task_id:
                    continue
                existing = db.task_get(dep) or db.task_get_completed_record(dep)
                if not existing:
                    return jsonify({"error": f"Unknown dependency '{dep}' for task '{task['id']}'"}), 400
                if existing.get("project") != project_name:
                    return jsonify({"error": f"Dependency '{dep}' belongs to another project"}), 400

        created_tasks = anchor_project_batch_roots(pending_batch, boundary_anchor_id)
        for task in created_tasks:
            if task["id"] == phase_qa_id and use_phase_qa:
                task["dependencies"] = [anchor_task_id]
            elif task["id"] == phase_gate_id and use_phase_gate:
                task["dependencies"] = [phase_qa_id if use_phase_qa else anchor_task_id]
        validation_errors = _project_creation_validation_errors(
            project_path,
            created_tasks,
            allowed_external_roots=[anchor_task_id],
            project_type=project_type,
        )
        if validation_errors:
            return jsonify({
                "error": "Project phase validation failed",
                "details": validation_errors,
                "graph_diagnostics": _graph_diagnostics_payload(
                    created_tasks,
                    validation_errors,
                    allowed_external_roots=[anchor_task_id],
                ),
            }), 400

        added: list[str] = []
        try:
            for task in created_tasks:
                db.task_upsert(task)
                added.append(task["id"])

            depended_on = {
                dep
                for task in created_tasks
                for dep in (task.get("dependencies") or [])
                if dep in batch_ids
            }
            leaves = [task for task in created_tasks if task["id"] not in depended_on]
            requested_head = (data.get("head_task_id") or "").strip()
            if requested_head:
                new_head = requested_head
            elif leaves:
                new_head = leaves[-1]["id"]
            else:
                new_head = created_tasks[-1]["id"]
            if new_head not in batch_ids:
                return jsonify({"error": "head_task_id must be one of the appended tasks"}), 400
            set_project_head(db, project_name, new_head)

            _append_project_design_phase(
                project_path,
                project_name,
                phase_name,
                overview_text,
                created_tasks,
                project_type,
                anchor_task_id,
            )
        except Exception as e:
            for task_id in reversed(added):
                try:
                    db.task_delete(task_id)
                except Exception:
                    pass
            return jsonify({"error": f"Append phase failed: {e}"}), 400

        return jsonify({
            "created": len(created_tasks),
            "project": project_name,
            "project_type": project_type,
            "phase_name": phase_name,
            "anchor_task_id": anchor_task_id,
            "phase_qa_id": phase_qa_id if use_phase_qa else None,
            "phase_gate_id": phase_gate_id if use_phase_gate else None,
            "head_task_id": new_head,
            "task_ids": [t["id"] for t in created_tasks],
        })

    @app.route("/api/projects/<project_name>/phase-gates/<gate_id>/release", methods=["POST"])
    def release_project_phase_gate(project_name, gate_id):
        """Release a phase gate so downstream appended-phase tasks can run."""
        task = db.task_get(gate_id)
        if not task:
            return jsonify({"error": f"Phase gate '{gate_id}' not found"}), 404
        if task.get("project") != project_name:
            return jsonify({"error": f"Phase gate '{gate_id}' does not belong to project '{project_name}'"}), 400
        if task.get("type") != "phase_gate":
            return jsonify({"error": f"Task '{gate_id}' is not a phase_gate"}), 400
        if task.get("status") == "completed":
            return jsonify({"released": True, "task": task})
        if task.get("status") != "pending":
            return jsonify({"error": f"Phase gate '{gate_id}' is not pending", "status": task.get("status")}), 409

        db.task_update_status(gate_id, "completed", completed=datetime.now().isoformat())
        released = db.task_get(gate_id) or {**task, "status": "completed"}
        return jsonify({"released": True, "task": released})

    # ------------------------------------------------------------------ #
    #  Project debug chat  (POST /api/project-debug)                       #
    # ------------------------------------------------------------------ #

    def _debug_data_dir() -> Path:
        from flask import current_app
        return Path(current_app.config.get("DATA_DIR", "data"))

    @app.route("/api/project-debug", methods=["POST"])
    def project_debug_chat():
        """Persistent project-scoped debugging chat.

        Request JSON:
          { "project": "my-project", "message": "...", "session_id": "<uuid>" }

        Response JSON:
          { "response": "...", "session_id": "<uuid>", "tool_calls": [...] }

        Sessions survive page refreshes; history stored in
        data/chat_sessions/<project>/<session_id>.jsonl.
        Expires after 7 days of inactivity.
        """
        data = request.get_json(force=True) or {}
        project = (data.get("project") or "").strip()
        user_message = (data.get("message") or "").strip()
        session_id = (data.get("session_id") or "").strip() or str(uuid.uuid4())

        if not project:
            return jsonify({"error": "project required"}), 400
        if not user_message:
            return jsonify({"error": "message required"}), 400

        data_dir = _debug_data_dir()
        history = _load_session(data_dir, project, session_id)
        is_new_session = len(history) == 0

        history.append({"role": "user", "content": user_message})
        _save_session(data_dir, project, session_id, history)

        project_context = ""
        if is_new_session:
            project_context = _build_project_context(
                Path(workspace), project, data_dir, db
            )

        context_block = (
            f"\n\n--- PROJECT CONTEXT (loaded at session start) ---\n{project_context}"
            if project_context else ""
        )
        project_root = Path(workspace) / project
        system_prompt = _DEBUG_SYSTEM_PROMPT.format(
            project=project,
            tools=_DEBUG_TOOLS_DESCRIPTION,
            context=context_block,
        )
        try:
            response_text, tool_calls = _run_debug_tool_loop(
                system_prompt, history, config, project_root,
                project=project, db=db,
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

        history.append({"role": "assistant", "content": response_text})
        _save_session(data_dir, project, session_id, history)  # data_dir bound above

        return jsonify({
            "response": response_text,
            "session_id": session_id,
            "tool_calls": tool_calls,
        })

    @app.route("/api/project-debug/<session_id>", methods=["DELETE"])
    def delete_project_debug_session(session_id):
        """Delete a project debug chat session."""
        data = request.get_json(force=True) or {}
        project = (data.get("project") or "").strip()
        if not project:
            return jsonify({"error": "project required"}), 400
        deleted = _delete_session(_debug_data_dir(), project, session_id)
        return jsonify({"deleted": deleted, "session_id": session_id})

    @app.route("/api/project-debug/<session_id>/stop", methods=["POST"])
    def stop_project_debug_session(session_id):
        """Emergency stop — inject a reset message, return immediately without LLM call.

        Request JSON: { "project": "..." }
        Response JSON: { "response": "...", "session_id": "...", "stopped": true }
        """
        data = request.get_json(force=True) or {}
        project = (data.get("project") or "").strip()
        if not project:
            return jsonify({"error": "project required"}), 400

        data_dir = _debug_data_dir()
        history = _load_session(data_dir, project, session_id)

        stop_message = (
            "⚠️ Stopped by user. I've been interrupted — please tell me what you'd "
            "like to do instead and I'll follow your direction."
        )
        history.append({"role": "assistant", "content": stop_message})
        _save_session(data_dir, project, session_id, history)

        return jsonify({
            "response": stop_message,
            "session_id": session_id,
            "tool_calls": [],
            "stopped": True,
        })

    @app.route("/api/project-debug/<session_id>/last", methods=["DELETE"])
    def rollback_project_debug_session(session_id):
        """Roll back the last user+assistant exchange from session history.

        Request JSON: { "project": "..." }
        Response JSON: { "rolled_back": true, "turns_removed": N, "session_id": "..." }
        """
        data = request.get_json(force=True) or {}
        project = (data.get("project") or "").strip()
        if not project:
            return jsonify({"error": "project required"}), 400

        data_dir = _debug_data_dir()
        history = _load_session(data_dir, project, session_id)

        # Remove trailing assistant message(s) then trailing user message
        turns_removed = 0
        while history and history[-1]["role"] == "assistant":
            history.pop()
            turns_removed += 1
        while history and history[-1]["role"] == "user":
            history.pop()
            turns_removed += 1

        _save_session(data_dir, project, session_id, history)
        return jsonify({
            "rolled_back": turns_removed > 0,
            "turns_removed": turns_removed,
            "session_id": session_id,
        })
