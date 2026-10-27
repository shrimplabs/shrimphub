from pathlib import Path

from tools.plan_probe import PlanProbeContext, build_probe
from tools.task_probe import Result


class FakePlanProbeContext(PlanProbeContext):
    def __init__(self, project_name="demo"):
        super().__init__(project_name, Path("/tmp/demo"), {})
        self.tasks = {}
        self.deleted = []

    def request(self, method: str, path: str, payload: dict | None = None, timeout: int = 10) -> dict:
        clean_path = path.split("?", 1)[0]
        if method == "GET" and clean_path == "/api/tasks":
            return {"tasks": list(self.tasks.values())}
        if method == "POST" and clean_path == "/api/tasks":
            task = {
                "id": payload["id"],
                "project": payload.get("project", ""),
                "type": payload.get("type", "feature"),
                "dependencies": payload.get("dependencies", []),
            }
            self.tasks[task["id"]] = task
            return {"task": task}
        if method == "GET" and clean_path.startswith("/api/tasks/") and clean_path.endswith("/dependencies"):
            task_id = clean_path.split("/")[3]
            task = self.tasks.get(task_id)
            if not task:
                return {"error": "Task not found", "status": 404}
            return {"task_id": task_id, "dependencies": task.get("dependencies", [])}
        if method == "GET" and clean_path.startswith("/api/tasks/"):
            task_id = clean_path.split("/")[3]
            task = self.tasks.get(task_id)
            if not task:
                return {"error": "Task not found", "status": 404}
            return {"task": task}
        if method == "DELETE" and clean_path.startswith("/api/tasks/"):
            task_id = clean_path.split("/")[3]
            self.deleted.append(task_id)
            if task_id in self.tasks:
                del self.tasks[task_id]
                return {"success": True}
            return {"error": "Task not found", "status": 404}
        return {"error": f"unhandled {method} {path}"}


def test_plan_probe_task_steps_create_wire_and_cleanup():
    ctx = FakePlanProbeContext()

    assert ctx.step_list_tasks().status == Result.PASS
    create = ctx.step_create_task()
    assert create.status == Result.PASS
    assert ctx.parent_id in ctx.tasks

    dep = ctx.step_dep_wiring()
    assert dep.status == Result.PASS
    assert ctx.tasks[ctx.child_id]["dependencies"] == [ctx.parent_id]

    cleanup = ctx.step_cleanup()
    assert cleanup.status == Result.PASS
    assert ctx.tasks == {}
    assert ctx.created_ids == []


def test_plan_probe_write_file_is_blocked_for_plan_tasks(tmp_path):
    ctx = PlanProbeContext("demo", tmp_path, {})

    outcome = ctx.step_write_blocked()

    assert outcome.status == Result.PASS
    assert not (tmp_path / ".plan_probe_should_not_exist").exists()


def test_build_probe_uses_expected_steps(tmp_path):
    runner = build_probe("demo", tmp_path, {})

    assert [step.name for step in runner.steps] == [
        "1-list-tasks",
        "2-create-task",
        "3-dep-wiring",
        "4-agent-view",
        "5-write-blocked",
        "6-cleanup",
    ]
