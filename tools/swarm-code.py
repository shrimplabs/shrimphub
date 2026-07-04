#!/usr/bin/env python3
"""
swarm-code — CLI harness for the swarm controller.

Usage:
  swarm-code <project> "<description>" [--type=feature] [--priority=50] [--wait]
  swarm-code --chat [<project>]
  swarm-code --watch <agent_id>
  swarm-code --status

Environment:
  SWARM_URL   Base URL of the swarm API (default: http://localhost:5001)
  SWARM_TOKEN Bearer token if login_required is enabled
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Optional

SWARM_URL = os.environ.get("SWARM_URL", "http://localhost:5001").rstrip("/")
SWARM_TOKEN = os.environ.get("SWARM_TOKEN", "")

# ── colour helpers ────────────────────────────────────────────────────────────

_NO_COLOR = not sys.stdout.isatty() or os.environ.get("NO_COLOR")

def _c(code: str, text: str) -> str:
    if _NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def green(t):  return _c("32", t)
def yellow(t): return _c("33", t)
def red(t):    return _c("31", t)
def bold(t):   return _c("1",  t)
def dim(t):    return _c("2",  t)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _headers(extra: dict | None = None) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if SWARM_TOKEN:
        h["Authorization"] = f"Bearer {SWARM_TOKEN}"
    if extra:
        h.update(extra)
    return h


def _get(path: str) -> dict | list:
    req = urllib.request.Request(f"{SWARM_URL}{path}", headers=_headers())
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _get_list(path: str, key: str) -> list:
    """GET an endpoint that wraps its list under a key."""
    data = _get(path)
    if isinstance(data, list):
        return data
    return data.get(key, [])


def _post(path: str, body: dict, timeout: int = 60) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{SWARM_URL}{path}", data=data, headers=_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _stream_sse(path: str):
    """Yield lines from an SSE endpoint until the connection closes."""
    req = urllib.request.Request(
        f"{SWARM_URL}{path}",
        headers=_headers({"Accept": "text/event-stream"}),
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            for raw in r:
                line = raw.decode("utf-8", errors="replace").rstrip("\n\r")
                if line.startswith("data: "):
                    yield line[6:]
    except (urllib.error.URLError, TimeoutError):
        return


# ── status command ────────────────────────────────────────────────────────────

def cmd_status():
    try:
        h      = _get("/api/health")
        agents = _get_list("/api/agents", "agents")
        tasks  = _get_list("/api/tasks",  "tasks")
    except Exception as e:
        print(red(f"Cannot reach swarm at {SWARM_URL}: {e}"))
        sys.exit(1)

    lag = h.get("monitor_lag_seconds", "?")
    active = len([a for a in agents if a.get("status") == "active"])
    max_ag = h.get("max_agents", "?")
    pending = len([t for t in tasks if t.get("status") == "pending"])
    in_prog = len([t for t in tasks if t.get("status") == "in_progress"])

    print(bold("Swarm status"))
    print(f"  URL:        {SWARM_URL}")
    print(f"  Monitor:    lag={lag}s  auto={'on' if h.get('auto_mode') else 'off'}")
    print(f"  Agents:     {active}/{max_ag} active")
    print(f"  Tasks:      {pending} pending  {in_prog} in_progress")

    active_agents = [a for a in agents if a.get("status") == "active"]
    if active_agents:
        print()
        print(bold("Active agents:"))
        for a in active_agents:
            print(f"  {dim(a['id'][:8])}  {a.get('project','?'):20s}  {a.get('task_type','?')}")


# ── watch command ─────────────────────────────────────────────────────────────

_INTERESTING = (
    "[Pipeline]", "PHASE:", "[PostValidation]", "TASK_COMPLETE",
    "Gateway error", "Done.", "diff:", "[Swarm]", "ERROR",
    "Warning:", "loop limit", "context limit",
)

def _is_interesting(line: str) -> bool:
    low = line.lower()
    return any(k.lower() in low for k in _INTERESTING)


def cmd_watch(agent_id: str):
    print(dim(f"Streaming agent {agent_id} …  (Ctrl-C to stop)"))
    try:
        for line in _stream_sse(f"/api/agents/{agent_id}/stream"):
            if not line.strip():
                continue
            if "TASK_COMPLETE" in line or "Done. OK" in line:
                print(green(f"  {line}"))
            elif "ERROR" in line or "Done. FAILED" in line or "failed" in line.lower():
                print(red(f"  {line}"))
            elif _is_interesting(line):
                print(yellow(f"  {line}"))
            else:
                print(dim(f"  {line}"))
    except KeyboardInterrupt:
        print()
        print(dim("Stream interrupted."))


# ── fire-and-wait command ─────────────────────────────────────────────────────

def cmd_run(project: str, description: str, task_type: str, priority: int, wait: bool):
    # 1. Health check
    try:
        _get("/api/health")
    except Exception as e:
        print(red(f"Cannot reach swarm at {SWARM_URL}: {e}"))
        sys.exit(1)

    # 2. Verify project is managed
    try:
        resp = _get("/api/projects")
        # /api/projects returns {"projects": {"name": {...}, ...}}
        proj_map = resp.get("projects", resp) if isinstance(resp, dict) else {}
        names = list(proj_map.keys()) if isinstance(proj_map, dict) else [p.get("name") for p in proj_map]
        if project not in names:
            print(red(f"Project '{project}' not found. Managed projects:"))
            for n in sorted(names):
                print(f"  {n}")
            sys.exit(1)
    except Exception as e:
        print(red(f"Failed to list projects: {e}"))
        sys.exit(1)

    # 3. Create task via batch endpoint (auto-chains to HEAD)
    print(bold(f"Creating {task_type} task for {project} …"))
    try:
        resp = _post("/api/tasks/batch", {
            "project": project,
            "tasks": [{
                "type": task_type,
                "description": description,
                "priority": priority,
            }],
        })
        task_ids = resp.get("ids") or list((resp.get("id_map") or {}).values())
        if not task_ids:
            print(red(f"Unexpected response: {resp}"))
            sys.exit(1)
        task_id = task_ids[0]
    except Exception as e:
        print(red(f"Failed to create task: {e}"))
        sys.exit(1)

    print(green(f"  Task created: {task_id}"))

    if not wait:
        print(dim(f"  Run with --wait to block until complete, or:"))
        print(dim(f"    swarm-code --watch <agent_id>  (once an agent picks it up)"))
        print(dim(f"    curl {SWARM_URL}/api/tasks/{task_id}"))
        return

    # 4. Poll for agent pickup
    print(dim("  Waiting for agent to pick up task …"))
    agent_id: Optional[str] = None
    for _ in range(120):  # up to 10 min
        time.sleep(5)
        try:
            t = _get(f"/api/tasks/{task_id}")
            status = t.get("status")
            if status == "in_progress":
                agent_id = t.get("metadata", {}).get("agent_id") or _find_agent_for_task(task_id)
                print(green(f"  Agent picked up task (status=in_progress)"))
                if agent_id:
                    print(dim(f"  Agent: {agent_id}"))
                break
            elif status in ("completed", "failed", "cancelled"):
                _print_final(t)
                return
            else:
                sys.stdout.write(".")
                sys.stdout.flush()
        except Exception:
            pass
    else:
        print(yellow("\n  Timed out waiting for agent pickup. Task is still pending."))
        print(dim(f"  Check: curl {SWARM_URL}/api/tasks/{task_id}"))
        return

    # 5. Stream log
    if agent_id:
        print(dim(f"\n  Streaming agent log (Ctrl-C to detach) …\n"))
        try:
            for line in _stream_sse(f"/api/agents/{agent_id}/stream"):
                if not line.strip():
                    continue
                if "TASK_COMPLETE" in line or "Done. OK" in line:
                    print(green(f"  {line}"))
                    break
                elif "Done. FAILED" in line or ("failed" in line.lower() and "ERROR" in line):
                    print(red(f"  {line}"))
                    break
                elif _is_interesting(line):
                    print(yellow(f"  {line}"))
        except KeyboardInterrupt:
            print(dim("\n  Detached from stream. Task continues in background."))
            print(dim(f"  Poll: curl {SWARM_URL}/api/tasks/{task_id}"))
            return

    # 6. Final result
    try:
        t = _get(f"/api/tasks/{task_id}")
        _print_final(t)
    except Exception as e:
        print(yellow(f"  Could not fetch final status: {e}"))


def _find_agent_for_task(task_id: str) -> Optional[str]:
    try:
        agents = _get_list("/api/agents", "agents")
        for a in agents:
            if a.get("task_id") == task_id:
                return a.get("id")
    except Exception:
        pass
    return None


def _print_final(task: dict):
    status = task.get("status", "unknown")
    meta   = task.get("metadata") or {}
    diff   = meta.get("diff_stat", "")
    err    = meta.get("last_failure", "")

    print()
    if status == "completed":
        print(bold(green("✓ Task completed")))
        if diff:
            print(f"  Changes: {diff}")
    elif status == "failed":
        print(bold(red("✗ Task failed")))
        if err:
            print(red(f"  Error: {err[:300]}"))
    elif status == "cancelled":
        print(yellow("  Task cancelled"))
    else:
        print(f"  Status: {status}")

    attempts = task.get("attempts", 0)
    if attempts:
        print(dim(f"  Attempts: {attempts}"))


# ── chat command ──────────────────────────────────────────────────────────────

def cmd_chat(project: Optional[str]):
    scope = f"project={project}" if project else "global"
    print(bold(f"Swarm chat  [{scope}]"))
    print(dim("Type your message and press Enter. Ctrl-C or 'exit' to quit.\n"))

    session_id: Optional[str] = None

    while True:
        try:
            user_input = input(bold("You: ")).strip()
        except (KeyboardInterrupt, EOFError):
            print()
            break

        if not user_input or user_input.lower() in ("exit", "quit", "q"):
            break

        body: dict = {"message": user_input}
        if session_id:
            body["session_id"] = session_id
        if project:
            body["project"] = project

        try:
            req = urllib.request.Request(
                f"{SWARM_URL}/api/unified-chat",
                data=json.dumps(body).encode(),
                headers=_headers(),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            print(red(f"  HTTP {e.code}: {e.read().decode()[:200]}"))
            continue
        except Exception as e:
            print(red(f"  Error: {e}"))
            continue

        session_id = resp.get("session_id", session_id)
        reply = resp.get("reply", "").strip()
        tool_calls = resp.get("tool_calls", [])

        print()
        print(bold("Swarm: "), end="")
        print(reply)

        if tool_calls:
            print(dim(f"  [{len(tool_calls)} tool call(s): {', '.join(tc.get('name','?') for tc in tool_calls[:3])}]"))

        print()


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="swarm-code",
        description="CLI harness for the swarm controller.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  swarm-code raccoon-city "add a leaderboard system" --wait
  swarm-code raccoon-city "fix the save bug" --type=bug --wait
  swarm-code --chat
  swarm-code --chat raccoon-city
  swarm-code --watch abc123def
  swarm-code --status
        """,
    )

    parser.add_argument("--status",  action="store_true", help="Show swarm health and active agents")
    parser.add_argument("--watch",   metavar="AGENT_ID",  help="Stream a running agent's log")
    parser.add_argument("--chat",    action="store_true", help="Interactive chat with the swarm manager")
    parser.add_argument("--type",    default="feature",   help="Task type (feature/bug/refactor/polish/qa/research)")
    parser.add_argument("--priority",type=int, default=None, help="Task priority (default: 80 for bug, 50 otherwise)")
    parser.add_argument("--wait",    action="store_true", help="Block until the task completes")
    parser.add_argument("project",   nargs="?",           help="Project name")
    parser.add_argument("description", nargs="?",         help="Task description")

    args = parser.parse_args()

    if args.status:
        cmd_status()
        return

    if args.watch:
        cmd_watch(args.watch)
        return

    if args.chat:
        cmd_chat(args.project)
        return

    # Fire-and-wait mode
    if not args.project:
        parser.error("project is required")
    if not args.description:
        parser.error("description is required")

    priority = args.priority
    if priority is None:
        priority = 80 if args.type == "bug" else 50

    cmd_run(
        project=args.project,
        description=args.description,
        task_type=args.type,
        priority=priority,
        wait=args.wait,
    )


if __name__ == "__main__":
    main()
