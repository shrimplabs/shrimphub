#!/usr/bin/env python3
"""Scheduler diagnostic check."""
import json
import urllib.request

BASE = "http://localhost:5001"

def get(path):
    with urllib.request.urlopen(f"{BASE}{path}") as r:
        return json.loads(r.read())

def patch(task_id, data):
    req = urllib.request.Request(
        f"{BASE}/api/tasks/{task_id}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="PATCH"
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

# Gather all tasks
all_tasks = get("/api/tasks?limit=2000").get("tasks", [])
valid_ids = {t["id"] for t in all_tasks}

pending = [t for t in all_tasks if t["status"] == "pending"]
inprogress = [t for t in all_tasks if t["status"] == "in_progress"]
failed = [t for t in all_tasks if t["status"] == "failed"]
completed = [t for t in all_tasks if t["status"] == "completed"]

# Phantom deps
phantom_blocked = []
for t in pending:
    for dep in t.get("dependencies", []):
        dep_id = dep if isinstance(dep, str) else dep.get("task_id", "")
        if dep_id and dep_id not in valid_ids:
            phantom_blocked.append(t)
            break

# Agents
agents_data = get("/api/agents")
agents = agents_data.get("agents", agents_data) if isinstance(agents_data, dict) else agents_data
active_agents = [a for a in agents if a.get("status") == "active"]

# Quota
quota = get("/api/quota-limit")

print(f"=== Scheduler Check ===")
print(f"Total tasks: {len(all_tasks)}")
print(f"  Completed: {len(completed)}")
print(f"  In-Progress: {len(inprogress)}")
print(f"  Pending: {len(pending)}")
print(f"  Failed (zombie): {len(failed)}")
print(f"  Phantom-blocked: {len(phantom_blocked)}")
print(f"Agents: {len(active_agents)} active / {len(agents)} total")
print(f"Quota: {quota.get('used_percent', '?')}% used, {quota.get('remaining_percent', '?')}% remaining")
print(f"Over limit: {quota.get('over_limit', False)}")

if phantom_blocked:
    print(f"\nPHANTOM: Clearing {len(phantom_blocked)} phantom-blocked tasks...")
    for t in phantom_blocked:
        tid = t["id"]
        print(f"  Clearing deps for {tid[:40]}")
        try:
            result = patch(tid, {"dependencies": []})
            print(f"    -> OK")
        except Exception as e:
            print(f"    -> ERROR: {e}")
else:
    print("\nNo phantom deps found.")
