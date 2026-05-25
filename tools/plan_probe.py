"""Probe for plan task critical path.

Validates that planning agents can inspect and create tasks while write tools
remain blocked for the read-only plan task type.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from tools.task_probe import ProbeRunner, ProbeStep, fail, pass_


class PlanProbeContext:
    def __init__(self, project_name: str, project_path: Path, config: dict):
        self.project_name = project_name
        self.project_path = project_path
        self.config = config
        self.api_port = int(config.get("api_port") or config.get("port") or 5001)
        self.base_url = f"http://localhost:{self.api_port}"
        self.created_ids: list[str] = []
        self.parent_id: str | None = None
        self.child_id: str | None = None
        self.agent_task_id: str | None = None

    def request(self, method: str, path: str, payload: dict | None = None, timeout: int = 10) -> dict:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"error": raw}
            data.setdefault("status", exc.code)
            return data
        except Exception as exc:
            return {"error": str(exc), "exception": type(exc).__name__}

    def cleanup_created_tasks(self) -> None:
        for task_id in reversed(self.created_ids):
            self.request("DELETE", f"/api/tasks/{task_id}", timeout=5)
        self.created_ids.clear()

    def step_list_tasks(self):
        data = self.request("GET", f"/api/tasks?project={self.project_name}")
        if "error" in data:
            return fail(data["error"])
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            return fail(f"expected tasks list, got keys={list(data.keys())}")
        return pass_(f"{len(tasks)} tasks returned")

    def step_create_task(self):
        task_id = _probe_task_id("plan-probe-parent")
        payload = {
            "id": task_id,
            "project": self.project_name,
            "type": "feature",
            "description": "plan_probe parent task; safe to delete",
            "priority": 5,
            "metadata": {"probe": "plan_probe", "role": "parent"},
        }
        data = self.request("POST", "/api/tasks", payload)
        if "error" in data:
            return fail(data["error"])
        task = data.get("task") or {}
        if task.get("id") != task_id:
            return fail(f"unexpected task id: {task.get('id')}")
        self.parent_id = task_id
        self.created_ids.append(task_id)

        fetched = self.request("GET", f"/api/tasks/{task_id}")
        if fetched.get("task", {}).get("id") != task_id:
            return fail("created task did not appear in DB")
        return pass_(f"id={task_id}")

    def step_dep_wiring(self):
        if not self.parent_id:
            return fail("parent task was not created")

        task_id = _probe_task_id("plan-probe-child")
        payload = {
            "id": task_id,
            "project": self.project_name,
            "type": "feature",
            "description": "plan_probe child task; safe to delete",
            "priority": 5,
            "dependencies": [self.parent_id],
            "metadata": {"probe": "plan_probe", "role": "child"},
        }
        data = self.request("POST", "/api/tasks", payload)
        if "error" in data:
            return fail(data["error"])
        self.child_id = task_id
        self.created_ids.append(task_id)

        deps = self.request("GET", f"/api/tasks/{task_id}/dependencies")
        dep_ids = deps.get("dependencies")
        if not isinstance(dep_ids, list):
            return fail(f"dependency endpoint returned keys={list(deps.keys())}")
        if self.parent_id not in dep_ids:
            return fail(f"parent {self.parent_id} missing from dependencies={dep_ids}")
        return pass_(f"{task_id} depends_on={self.parent_id}")

    def step_agent_runtime_view(self):
        if not self.parent_id:
            return fail("parent task was not created")

        import swarm.agent_runtime as rt
        from swarm.tool_dispatch import execute_tool, validate_tool_call

        old_values = {
            "WORKSPACE": rt.WORKSPACE,
            "DATA_DIR": rt.DATA_DIR,
            "PROJECT": rt.PROJECT,
            "PROJECT_PATH_OVERRIDE": rt.PROJECT_PATH_OVERRIDE,
            "TASK_ID": rt.TASK_ID,
            "TASK_TYPE": rt.TASK_TYPE,
            "TASK_PRIORITY": rt.TASK_PRIORITY,
            "API_PORT": rt.API_PORT,
            "READONLY": rt.READONLY,
            "TASK_METADATA": rt.TASK_METADATA,
            "RUN_BROADCAST_WRITE_COUNT": rt.RUN_BROADCAST_WRITE_COUNT,
            "CLAIMED_FILE_PATHS": rt.CLAIMED_FILE_PATHS,
        }
        try:
            rt.WORKSPACE = self.project_path.parent
            rt.DATA_DIR = "data"
            rt.PROJECT = self.project_name
            rt.PROJECT_PATH_OVERRIDE = str(self.project_path)
            rt.TASK_ID = "plan-probe-agent-view"
            rt.TASK_TYPE = "plan"
            rt.TASK_PRIORITY = 50
            rt.API_PORT = self.api_port
            rt.READONLY = False
            rt.TASK_METADATA = {}
            rt.RUN_BROADCAST_WRITE_COUNT = 0
            rt.CLAIMED_FILE_PATHS = set()
            rt._sync_all_tool_globals()

            list_call = {"tool": "list_tasks", "args": {"project": self.project_name}}
            validation_error = validate_tool_call(list_call)
            if validation_error:
                return fail(f"list_tasks validation failed: {validation_error}")
            listed = execute_tool(list_call)
            if not listed.get("ok") or not isinstance(listed.get("tasks"), list):
                return fail(f"list_tasks dispatch failed: {listed}")

            create_call = {
                "tool": "create_task",
                "args": {
                    "description": "plan_probe agent-view task; safe to delete",
                    "type": "feature",
                    "priority": 5,
                    "dependencies": [self.parent_id],
                    "project": self.project_name,
                    "metadata": {"probe": "plan_probe", "role": "agent-view"},
                },
            }
            validation_error = validate_tool_call(create_call)
            if validation_error:
                return fail(f"create_task validation failed: {validation_error}")
            created = execute_tool(create_call)
            if not created.get("ok") or not created.get("task_id"):
                return fail(f"create_task dispatch failed: {created}")
            self.agent_task_id = created["task_id"]
            self.created_ids.append(self.agent_task_id)

            deps = self.request("GET", f"/api/tasks/{self.agent_task_id}/dependencies")
            if self.parent_id not in deps.get("dependencies", []):
                return fail(f"agent-created task missing dependency: {deps}")

            write_call = {
                "tool": "write_file",
                "args": {"path": ".plan_probe_should_not_exist", "content": "blocked\n"},
            }
            validation_error = validate_tool_call(write_call)
            if validation_error:
                return fail(f"write_file validation failed unexpectedly: {validation_error}")
            denied = execute_tool(write_call)
            if denied.get("ok"):
                return fail(f"write_file dispatch unexpectedly succeeded: {denied}")
            error = denied.get("error", "")
            if "plan task cannot use write_file" not in error and "planning tasks are read-only" not in error:
                return fail(f"write_file dispatch failed for unexpected reason: {error}")

            return pass_(
                f"dispatch list_tasks={len(listed['tasks'])}; "
                f"create_task={self.agent_task_id}; write_file denied"
            )
        finally:
            for key, value in old_values.items():
                setattr(rt, key, value)
            rt._sync_all_tool_globals()

    def step_write_blocked(self):
        import swarm.tools.core as core

        old_values = {
            "WORKSPACE": core.WORKSPACE,
            "PROJECT": core.PROJECT,
            "PROJECT_PATH_OVERRIDE": core.PROJECT_PATH_OVERRIDE,
            "TASK_TYPE": core.TASK_TYPE,
            "READONLY": core.READONLY,
        }
        try:
            core.WORKSPACE = self.project_path.parent
            core.PROJECT = self.project_name
            core.PROJECT_PATH_OVERRIDE = str(self.project_path)
            core.TASK_TYPE = "plan"
            core.READONLY = False
            result = core.write_file(".plan_probe_should_not_exist", "blocked\n")
        finally:
            for key, value in old_values.items():
                setattr(core, key, value)

        if result.get("ok"):
            return fail(f"write_file unexpectedly succeeded: {result}")

        error = result.get("error", "")
        if "read-only" not in error and "planning tasks" not in error and "plan tasks" not in error:
            return fail(f"write_file failed for unexpected reason: {error}")
        return pass_(error)

    def step_cleanup(self):
        ids = list(self.created_ids)
        self.cleanup_created_tasks()
        remaining = []
        for task_id in ids:
            data = self.request("GET", f"/api/tasks/{task_id}")
            if "task" in data:
                remaining.append(task_id)
        if remaining:
            return fail(f"tasks still present: {remaining}")
        return pass_(f"deleted {len(ids)} probe tasks")


def build_probe(project_name: str, project_path: Path, config: dict) -> ProbeRunner:
    ctx = PlanProbeContext(project_name, project_path, config)
    steps = [
        ProbeStep("1-list-tasks", ctx.step_list_tasks, title="List tasks"),
        ProbeStep("2-create-task", ctx.step_create_task, title="Create task"),
        ProbeStep("3-dep-wiring", ctx.step_dep_wiring, title="Dependency wiring"),
        ProbeStep("4-agent-view", ctx.step_agent_runtime_view, title="Agent runtime view"),
        ProbeStep("5-write-blocked", ctx.step_write_blocked, title="Direct write block"),
        ProbeStep("6-cleanup", ctx.step_cleanup, title="Cleanup"),
    ]
    return ProbeRunner(steps, project_name, config, cleanup=ctx.cleanup_created_tasks)


def _probe_task_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}"
