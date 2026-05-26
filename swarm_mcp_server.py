#!/usr/bin/env python3
"""
Swarm Controller MCP Server

Exposes the swarm controller API as MCP tools so any Claude Code instance
can create tasks, check status, and spawn agents without needing curl.

Register globally in ~/.claude/settings.json:
  {
    "mcpServers": {
      "swarm": {
        "command": "/path/to/.venv/bin/python",
        "args": ["/path/to/swarm_mcp_server.py"],
        "env": {"SWARM_API_URL": "http://localhost:5001"}
      }
    }
  }
"""

import json
import os
import sys
import urllib.request
import urllib.error
from mcp.server.fastmcp import FastMCP

SWARM_API_URL = os.environ.get("SWARM_API_URL", "http://localhost:5001")

mcp = FastMCP("swarm-controller")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(path: str) -> dict:
    url = f"{SWARM_API_URL}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def _post(path: str, body: dict) -> dict:
    url = f"{SWARM_API_URL}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def swarm_list_projects() -> str:
    """List all projects registered in the swarm controller, with their task counts and health."""
    result = _get("/api/projects")
    if "error" in result:
        return f"Error: {result['error']}"
    projects = result if isinstance(result, list) else result.get("projects", [])
    lines = []
    for p in projects:
        name = p.get("name", "?")
        pending = p.get("pending_tasks", 0)
        in_prog = p.get("in_progress_tasks", 0)
        completed = p.get("completed_tasks", 0)
        lines.append(f"  {name}: {pending} pending, {in_prog} in_progress, {completed} completed")
    return "Projects:\n" + "\n".join(lines) if lines else "No projects registered."


@mcp.tool()
def swarm_list_tasks(project: str = "", status: str = "") -> str:
    """
    List tasks in the swarm. Optionally filter by project name and/or status.
    status can be: pending, in_progress, completed, failed
    """
    result = _get("/api/tasks")
    if "error" in result:
        return f"Error: {result['error']}"
    tasks = result if isinstance(result, list) else result.get("tasks", [])
    if project:
        tasks = [t for t in tasks if t.get("project", "") == project]
    if status:
        tasks = [t for t in tasks if t.get("status", "") == status]
    if not tasks:
        return "No tasks match."
    lines = []
    for t in tasks[:50]:  # cap output
        tid = t.get("id", "?")[:12]
        proj = t.get("project", "?")
        ttype = t.get("type", "?")
        stat = t.get("status", "?")
        desc = t.get("description", "")[:60]
        lines.append(f"  [{stat}] {proj}/{ttype} {tid}… — {desc}")
    total = len(tasks)
    suffix = f"\n(showing {min(50, total)} of {total})" if total > 50 else ""
    return "\n".join(lines) + suffix


@mcp.tool()
def swarm_get_task(task_id: str) -> str:
    """Get full details for a specific task by ID."""
    result = _get(f"/api/tasks/{task_id}")
    if "error" in result:
        return f"Error: {result['error']}"
    return json.dumps(result, indent=2)


@mcp.tool()
def swarm_create_task(
    project: str,
    task_type: str,
    description: str,
    priority: int = 50,
    dependencies: list[str] | None = None,
) -> str:
    """
    Create a new task in the swarm controller.

    project: name of the managed project (e.g. "example-game")
    task_type: feature | bug | refactor | polish | qa | harness_qa | research | plan | project_plan
    description: what the agent should do — be specific
    priority: 100=critical bug/refactor, 80=bug, 75=qa, 50=feature/polish (default 50)
    dependencies: list of task IDs that must complete first (optional)
    """
    body = {
        "project": project,
        "type": task_type,
        "description": description,
        "priority": priority,
    }
    if dependencies:
        body["dependencies"] = dependencies
    result = _post("/api/tasks", body)
    if "error" in result:
        return f"Error: {result['error']}"
    tid = result.get("id") or result.get("task_id") or result.get("task", {}).get("id", "?")
    return f"Task created: {tid}\n{json.dumps(result, indent=2)}"


@mcp.tool()
def swarm_spawn(project: str = "") -> str:
    """
    Trigger the swarm to fill agent slots now (same as clicking Spawn in the dashboard).
    Optionally pass a project name to spawn only for that project.
    """
    body = {"project": project} if project else {}
    result = _post("/api/spawn-batch", body)
    if "error" in result:
        return f"Error: {result['error']}"
    return json.dumps(result, indent=2)


@mcp.tool()
def swarm_list_agents() -> str:
    """List currently running agents and their tasks."""
    result = _get("/api/agents")
    if "error" in result:
        return f"Error: {result['error']}"
    agents = result if isinstance(result, list) else result.get("agents", [])
    if not agents:
        return "No agents running."
    lines = []
    for a in agents:
        aid = a.get("id", "?")[:12]
        proj = a.get("project", "?")
        ttype = a.get("task_type", "?")
        stat = a.get("status", "?")
        task_id = str(a.get("task_id", "?"))[:12]
        lines.append(f"  [{stat}] {proj}/{ttype} agent={aid}… task={task_id}…")
    return "Running agents:\n" + "\n".join(lines)


@mcp.tool()
def swarm_project_health(project: str) -> str:
    """Get health metrics for a project: score, task counts, last commit age."""
    result = _get(f"/api/projects/{project}/health")
    if "error" in result:
        return f"Error: {result['error']}"
    return json.dumps(result, indent=2)


@mcp.tool()
def swarm_create_tasks(
    project: str,
    tasks: list[dict],
) -> str:
    """
    Create multiple tasks in one call with reliable DAG dependency wiring.

    Use this instead of calling swarm_create_task repeatedly. All task IDs are
    generated upfront so depends_on index references always resolve correctly —
    no chicken-and-egg problem with IDs you haven't created yet.

    Each task dict supports:
      - "type": "feature" | "bug" | "refactor" | "polish" | "qa" | "research" | "plan"
      - "description": str — what to do
      - "priority": int (optional, default 50)
      - "depends_on": list[int] — indices into THIS tasks list (resolved to IDs before creation)
      - "dependencies": list[str] — explicit task IDs from outside this batch (merged with depends_on)

    Root tasks (no deps) are automatically chained to the project HEAD so the
    history chain is never broken. This is always on — off-chain creation is not allowed.

    Example — three tasks where the third waits for both of the first two:
      tasks=[
        {"type": "bug",     "description": "Fix login crash",    "priority": 80},
        {"type": "bug",     "description": "Fix session expiry", "priority": 80},
        {"type": "feature", "description": "Add OAuth",          "priority": 50, "depends_on": [0, 1]},
      ]

    Returns created IDs and an id_map so you can reference generated IDs in follow-up calls.
    """
    if not tasks:
        return "Error: tasks list is empty."

    body = {
        "project": project,
        "tasks": tasks,
    }
    result = _post("/api/tasks/batch", body)
    if "error" in result:
        return f"Error: {result['error']}"

    ids = result.get("ids", [])
    id_map = result.get("id_map", {})
    lines = [f"Created {len(ids)} tasks for '{project}':"]
    for i, tid in enumerate(ids):
        lines.append(f"  [{i}] {tid}")
    if id_map:
        lines.append(f"\nid_map (use these to reference tasks in follow-up calls):")
        for k, v in id_map.items():
            lines.append(f"  {k} → {v}")
    return "\n".join(lines)


@mcp.tool()
def swarm_create_tasks_file_aware(
    project: str,
    tasks: list[dict],
) -> str:
    """
    Create multiple tasks with automatic file-aware dependency chaining.

    Before submitting, this tool analyses which files each task will touch and
    automatically adds dependencies between any two tasks that share a file —
    preventing merge conflicts from parallel agents working on the same file.

    Each task dict must have:
      - "type": "feature" | "bug" | "refactor" | "polish" | "research" | "plan"
      - "description": str — what to do (mention exact files to touch)
      - "files": list[str] — files this task will CREATE or MODIFY (e.g. ["scripts/game.gd", "scenes/hud.tscn"])
      - "priority": int (optional, default 50)
      - "dependencies": list[str] (optional) — explicit extra task IDs to wait for

    Chaining rules applied automatically:
      - Any two tasks sharing a file: the second (lower priority / later in list) depends on the first
      - If 3+ tasks touch the same file: only the first owns it; all others depend on that first task
      - .tscn scene files count as shared — multiple edits to the same scene are always chained
      - Tasks with no file conflicts run in parallel (no auto-deps added)

    Returns the created task IDs and the dependency graph that was built.
    """
    if not tasks:
        return "Error: tasks list is empty."

    # Build file → first-owner map and auto-chain deps
    file_owner: dict[str, int] = {}   # filename → index of first task that owns it
    auto_deps: list[set] = [set() for _ in tasks]

    for i, task in enumerate(tasks):
        files = task.get("files", [])
        for f in files:
            f = f.strip()
            if not f:
                continue
            if f in file_owner:
                # This task conflicts with the owner — add dependency
                auto_deps[i].add(file_owner[f])
            else:
                file_owner[f] = i

    # Create tasks in dependency order (roots first)
    # Topological sort: tasks with no auto_deps first
    order = []
    remaining = list(range(len(tasks)))
    created: list[int] = []

    while remaining:
        ready = [i for i in remaining if all(d in created for d in auto_deps[i])]
        if not ready:
            # Cycle or unresolvable — just take the first remaining
            ready = [remaining[0]]
        for i in ready:
            order.append(i)
            created.append(i)
            remaining.remove(i)

    # Create tasks in order, collecting IDs
    id_map: dict[int, str] = {}   # index → task_id
    results = []
    errors = []

    for i in order:
        task = tasks[i]
        # Merge auto_deps with any explicit deps the caller provided
        explicit_ids = [d for d in task.get("dependencies", []) if isinstance(d, str)]
        auto_ids = [id_map[d] for d in auto_deps[i] if d in id_map]
        all_deps = list(set(explicit_ids + auto_ids))

        body = {
            "project": project,
            "type": task.get("type", "feature"),
            "description": task.get("description", ""),
            "priority": int(task.get("priority", 50)),
            "dependencies": all_deps,
        }
        result = _post("/api/tasks", body)
        if "error" in result:
            errors.append(f"Task {i} ({task.get('type','?')}): {result['error']}")
            continue

        tid = result.get("id") or result.get("task_id") or result.get("task", {}).get("id", f"unknown-{i}")
        id_map[i] = tid
        conflict_files = [f for f in task.get("files", []) if file_owner.get(f.strip()) != i]
        results.append({
            "index": i,
            "id": tid,
            "type": task.get("type"),
            "files": task.get("files", []),
            "auto_deps": [id_map[d] for d in auto_deps[i] if d in id_map],
            "parallel": len(auto_deps[i]) == 0,
        })

    # Build summary
    lines = [f"Created {len(results)} tasks for '{project}' with file-aware dependency chaining:"]
    for r in results:
        dep_str = f" [waits: {', '.join(d[:12] for d in r['auto_deps'])}]" if r["auto_deps"] else " [PARALLEL]"
        files_str = ", ".join(r["files"]) if r["files"] else "no files specified"
        lines.append(f"  {'[SEQUENTIAL]' if r['auto_deps'] else '[PARALLEL]  '} {r['type']} {r['id'][:16]}…{dep_str}")
        lines.append(f"    files: {files_str}")
    if errors:
        lines.append(f"\nErrors ({len(errors)}):")
        lines.extend(f"  {e}" for e in errors)

    # Conflict summary
    shared = {f: [i for i in range(len(tasks)) if f in [x.strip() for x in tasks[i].get("files", [])]]
              for f in file_owner}
    conflicts = {f: idxs for f, idxs in shared.items() if len(idxs) > 1}
    if conflicts:
        lines.append(f"\nFile conflicts resolved ({len(conflicts)} files):")
        for f, idxs in conflicts.items():
            lines.append(f"  {f}: tasks {idxs} chained in order")

    return "\n".join(lines)


@mcp.tool()
def swarm_dependency_graph(project: str = "") -> str:
    """
    Show the dependency graph stats for all pending/in-progress tasks.
    Optionally filter to a single project.
    Also shows which tasks are currently ready to run (all deps met).
    """
    stats = _get("/api/dependencies")
    ready = _get("/api/dependencies/ready")
    if "error" in stats:
        return f"Error: {stats['error']}"

    ready_ids = set(t.get("id", "") for t in ready.get("ready", []))

    # Fetch tasks to annotate with project filter
    tasks = _get("/api/tasks")
    task_map = {}
    if isinstance(tasks, list):
        task_map = {t["id"]: t for t in tasks}
    elif "tasks" in tasks:
        task_map = {t["id"]: t for t in tasks["tasks"]}

    lines = [f"Dependency graph stats: {json.dumps(stats, indent=2)}", ""]
    lines.append(f"Ready to run ({len(ready_ids)} tasks):")
    for t in ready.get("ready", []):
        tid = t.get("id", "?")
        full = task_map.get(tid, t)
        if project and full.get("project", "") != project:
            continue
        proj = full.get("project", "?")
        ttype = full.get("type", "?")
        desc = full.get("description", "")[:60]
        lines.append(f"  {proj}/{ttype} {tid[:12]}… — {desc}")
    return "\n".join(lines)


@mcp.tool()
def swarm_set_dependencies(task_id: str, dependencies: list[str]) -> str:
    """
    Replace the dependency list for a task.
    Pass an empty list to clear all dependencies.
    dependencies: list of task IDs that must complete before this task runs.
    """
    result = _post(f"/api/tasks/{task_id}", {"dependencies": dependencies})
    if "error" in result:
        return f"Error: {result['error']}"
    task = result.get("task", result)
    deps = task.get("dependencies", [])
    warning = result.get("warning", "")
    out = f"Task {task_id} dependencies set to: {deps}"
    if warning:
        out += f"\nWarning: {warning}"
    return out


@mcp.tool()
def swarm_add_dependency(task_id: str, depends_on: str) -> str:
    """
    Add a single dependency to a task without replacing the existing list.
    task_id: the task that should wait
    depends_on: the task ID it should wait for
    """
    # Fetch current deps
    current = _get(f"/api/tasks/{task_id}")
    if "error" in current:
        return f"Error fetching task: {current['error']}"
    task = current.get("task", current)
    deps = list(task.get("dependencies", []))
    if depends_on in deps:
        return f"Task {task_id} already depends on {depends_on}."
    deps.append(depends_on)
    result = _post(f"/api/tasks/{task_id}", {"dependencies": deps})
    if "error" in result:
        return f"Error: {result['error']}"
    warning = result.get("warning", "")
    out = f"Added dependency: {task_id} now depends on {depends_on}. Full list: {deps}"
    if warning:
        out += f"\nWarning: {warning}"
    return out


@mcp.tool()
def swarm_remove_dependency(task_id: str, depends_on: str) -> str:
    """
    Remove a single dependency from a task.
    """
    current = _get(f"/api/tasks/{task_id}")
    if "error" in current:
        return f"Error fetching task: {current['error']}"
    task = current.get("task", current)
    deps = list(task.get("dependencies", []))
    if depends_on not in deps:
        return f"Task {task_id} does not depend on {depends_on}."
    deps.remove(depends_on)
    result = _post(f"/api/tasks/{task_id}", {"dependencies": deps})
    if "error" in result:
        return f"Error: {result['error']}"
    return f"Removed dependency: {task_id} no longer depends on {depends_on}. Remaining: {deps}"


@mcp.tool()
def swarm_execution_order(project: str = "") -> str:
    """
    Show the topological execution order of all pending tasks.
    Optionally filter to a single project.
    """
    result = _get("/api/dependencies/execution-order")
    if "error" in result:
        return f"Error: {result['error']}"
    order = result.get("order", [])
    if project:
        order = [t for t in order if t.get("project", "") == project]
    if not order:
        return "No tasks in execution order."
    lines = []
    for i, t in enumerate(order, 1):
        tid = t.get("id", "?")[:12]
        proj = t.get("project", "?")
        ttype = t.get("type", "?")
        deps = t.get("dependencies", [])
        desc = t.get("description", "")[:50]
        dep_str = f" [waits: {', '.join(d[:8] for d in deps)}]" if deps else ""
        lines.append(f"  {i:3}. {proj}/{ttype} {tid}…{dep_str} — {desc}")
    return f"Execution order ({len(order)} tasks):\n" + "\n".join(lines)


@mcp.tool()
def swarm_delete_task(task_id: str) -> str:
    """Delete a task from the queue entirely."""
    import urllib.request
    url = f"{SWARM_API_URL}/api/tasks/{task_id}"
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("success", True) and f"Deleted task {task_id}." or "Delete may have failed."
    except urllib.error.HTTPError as e:
        return f"Error: HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def swarm_create_project(
    name: str,
    project_type: str,
    overview: str,
    tasks: list[dict] | None = None,
) -> str:
    """
    Create a brand-new project: initialises a local git repo, creates a Gitea
    remote, pushes, registers it in the swarm, adds it to managed_projects, and
    optionally seeds the task queue.

    name: kebab-case project name (e.g. "job-bot", "example-game")
    project_type: "godot" or "python"
    overview: plain-text description written into GAME_DESIGN.md (or README for Python)
    tasks: optional list of task dicts to seed the queue immediately.
           Each task: {"type": "feature"|"bug"|..., "description": "...",
                       "priority": 50, "depends_on": [0, 1]}
           depends_on uses 0-based indices into the tasks list.

    Returns a summary of what was created, including task IDs.
    """
    if not name or not project_type:
        return "Error: name and project_type are required."
    body = {
        "project_name": name.strip().lower().replace(" ", "-"),
        "project_type": project_type,
        "overview": overview,
        "tasks": tasks or [],
    }
    result = _post("/api/create-project-tasks", body)
    if "error" in result:
        return f"Error: {result['error']}"
    task_ids = result.get("task_ids", [])
    git_log = result.get("git_log", [])
    out = [
        f"Project '{name}' created ({project_type}).",
        f"Tasks seeded: {result.get('tasks_created', 0)}",
    ]
    if task_ids:
        out.append("Task IDs: " + ", ".join(task_ids))
    if git_log:
        out.append("Git log:\n" + "\n".join(f"  {l}" for l in git_log))
    return "\n".join(out)


@mcp.tool()
def swarm_register_project(name: str, managed: bool = True) -> str:
    """
    Register an *existing* project directory with the swarm controller.
    Use this when the repo already exists on disk and you just want the swarm
    to start managing it.  For creating a new project from scratch, use
    swarm_create_project() instead.

    name: project directory name (must already exist under the swarm workspace)
    managed: if True (default), add to managed_projects so agents get assigned work
    """
    reg = _post("/api/projects", {"name": name})
    if "error" in reg:
        return f"Error registering project: {reg['error']}"

    if managed:
        current = _get("/api/managed-projects")
        if "error" in current:
            return f"Project registered but could not fetch managed list: {current['error']}"
        upd = _post("/api/managed-projects", {"managed_projects": [name], "merge": True})
        if "error" in upd:
            return f"Project registered but could not add to managed list: {upd['error']}"
        return f"Project '{name}' registered and added to managed_projects.\n{json.dumps(upd, indent=2)}"

    return f"Project '{name}' registered (not managed).\n{json.dumps(reg, indent=2)}"


@mcp.tool()
def swarm_reset_task(task_id: str) -> str:
    """Reset a failed task back to pending so it will be retried."""
    result = _post(f"/api/tasks/{task_id}/reset", {})
    if "error" in result:
        return f"Error: {result['error']}"
    return json.dumps(result, indent=2)


@mcp.tool()
def swarm_agent_log(agent_id: str, tail: int = 200) -> str:
    """
    Read the recent log output of an agent.

    agent_id: the agent ID (from swarm_list_agents())
    tail: approximate number of characters to return from the end of the log (default 200 lines worth)

    Shows what tool calls the agent has made, what it found, and where it's currently at.
    Works on active agents and recently-completed ones.
    """
    result = _get(f"/api/agents/{agent_id}/output")
    if "error" in result:
        return f"Error: {result['error']}"
    output = result.get("output", "")
    # Return last `tail` lines
    lines = output.splitlines()
    trimmed = "\n".join(lines[-tail:]) if len(lines) > tail else output
    return f"Agent {agent_id} log (last {tail} lines):\n{trimmed}"


@mcp.tool()
def swarm_agent_detail(agent_id: str) -> str:
    """
    Get full metadata for an agent: task details, status, pid, token usage, start time, log path.
    """
    agent = _get(f"/api/agents/{agent_id}")
    if "error" in agent:
        return f"Error: {agent['error']}"
    a = agent.get("agent", agent)
    # Also fetch the associated task for full description
    task_id = a.get("task_id", "")
    task_info = {}
    if task_id:
        t = _get(f"/api/tasks/{task_id}")
        task_info = t.get("task", t) if "error" not in t else {}

    out = {
        "agent": a,
        "task": task_info,
    }
    return json.dumps(out, indent=2)


@mcp.tool()
def swarm_agent_hint(agent_id: str, message: str) -> str:
    """
    Inject a hint message into a running agent's next LLM loop iteration.

    The message is written to a hint file that the agent reads before its next
    tool call — use this to nudge a stuck agent, correct its approach, or give
    it additional context without killing and restarting it.

    agent_id: the agent ID (from swarm_list_agents())
    message: plain text instruction to inject (e.g. "The file you need is scripts/game.gd, not scenes/game.gd")
    """
    result = _post(f"/api/agents/{agent_id}/hint", {"message": message})
    if "error" in result:
        return f"Error: {result['error']}"
    return f"Hint delivered to agent {agent_id} (task {result.get('task_id', '?')})."


@mcp.tool()
def swarm_agent_kill(agent_id: str) -> str:
    """
    Kill a running agent. The task will be reset to pending and retried.
    Use this if an agent is stuck, looping, or going in the wrong direction.
    """
    result = _post(f"/api/agents/{agent_id}/kill", {})
    if "error" in result:
        return f"Error: {result['error']}"
    success = result.get("success", False)
    return f"Agent {agent_id} {'killed.' if success else 'could not be killed: ' + result.get('error','unknown')}"


@mcp.tool()
def swarm_status() -> str:
    """Get overall swarm status: auto-mode, active agents, quota usage."""
    health = _get("/api/health")
    auto = _get("/api/auto-mode")
    agents = _get("/api/agents")
    agent_count = len(agents) if isinstance(agents, list) else len(agents.get("agents", []))
    return json.dumps({
        "health": health,
        "auto_mode": auto,
        "active_agent_count": agent_count,
    }, indent=2)


if __name__ == "__main__":
    mcp.run()
