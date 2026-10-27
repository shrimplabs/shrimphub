"""Probe for feature task critical path.

Runs against a temporary Godot-shaped git repo so feature tool behavior and
post-task validation can be exercised without mutating a managed project.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from tools.task_probe import ProbeRunner, ProbeStep, fail, pass_


class FeatureProbeContext:
    def __init__(self, project_name: str, project_path: Path, config: dict):
        self.requested_project = project_name
        self.requested_project_path = project_path
        self.config = config
        self.api_port = int(config.get("api_port") or config.get("port") or 5001)
        self.base_url = f"http://localhost:{self.api_port}"
        self.tmp_root: Path | None = None
        self.project_name = f"feature-probe-{int(time.time() * 1000)}"
        self.project_path: Path | None = None
        self.created_task_ids: list[str] = []
        self.target_file = "feature_probe_target.txt"
        self.feature_task_id = f"{self.project_name}-feature"
        self.pass_task_id = f"{self.project_name}-pass"
        self.fail_task_id = f"{self.project_name}-fail"
        self.fail_error = "SCRIPT ERROR: feature probe forced validation failure"

    def setup(self) -> None:
        self.tmp_root = Path(tempfile.mkdtemp(prefix="swarm-feature-probe-"))
        self.project_path = self.tmp_root / self.project_name
        self.project_path.mkdir(parents=True)
        (self.project_path / "project.godot").write_text("[application]\nconfig/name=\"Feature Probe\"\n")
        (self.project_path / "check_scripts.gd").write_text("extends SceneTree\nfunc _init(): quit()\n")
        (self.project_path / self.target_file).write_text("before\n")
        (self.project_path / ".swarm_validate").write_text("test -f feature_probe_target.txt\n")
        _run(["git", "init"], self.project_path)
        _run(["git", "config", "user.email", "probe@example.invalid"], self.project_path)
        _run(["git", "config", "user.name", "Feature Probe"], self.project_path)
        _run(["git", "add", "-A"], self.project_path)
        _run(["git", "commit", "-m", "Initial feature probe fixture"], self.project_path)

        import swarm.orchestrator as orchestrator
        from swarm import db

        db.init(Path("data") / "swarm.db")
        orchestrator.WORKSPACE = self.tmp_root
        orchestrator.DATA_DIR = Path("data")

        self._create_task(self.feature_task_id, "Feature probe embodied task")
        self._create_task(self.pass_task_id, "Feature probe validation pass task")
        self._create_task(self.fail_task_id, "Feature probe validation fail task")

    def cleanup(self) -> None:
        for task_id in [
            f"bug-{self.fail_task_id}",
            f"bug-{self.pass_task_id}",
            f"bug-{self.feature_task_id}",
            *reversed(self.created_task_ids),
        ]:
            self.request("DELETE", f"/api/tasks/{task_id}", timeout=5)
        self.created_task_ids.clear()
        if self.tmp_root and self.tmp_root.exists():
            shutil.rmtree(self.tmp_root, ignore_errors=True)

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

    def step_read_file(self):
        self._ensure_setup()
        result = self._execute_as_feature_agent({
            "tool": "read_file",
            "args": {"path": self.target_file},
        })
        if not result.get("ok"):
            return fail(str(result))
        if "before" not in result.get("content", ""):
            return fail(f"unexpected content: {result.get('content')!r}")
        return pass_(f"{self.target_file} read")

    def step_write_file(self):
        self._ensure_setup()
        result = self._execute_as_feature_agent({
            "tool": "write_file",
            "args": {"path": self.target_file, "content": "after\n"},
        })
        if not result.get("ok"):
            return fail(str(result))
        actual = (self.project_path / self.target_file).read_text()
        if actual != "after\n":
            return fail(f"file content mismatch: {actual!r}")
        return pass_(f"{self.target_file} updated")

    def step_git_commit(self):
        self._ensure_setup()
        result = self._execute_as_feature_agent({
            "tool": "git_commit",
            "args": {"message": "Feature probe commit"},
        })
        if not result.get("ok"):
            return fail(str(result))
        log = _run(["git", "log", "-1", "--pretty=%s"], self.project_path).stdout.strip()
        if log != "Feature probe commit":
            return fail(f"unexpected git log -1: {log}")
        return pass_(log)

    def step_validation_fires(self):
        self._ensure_setup()
        from swarm import validation

        failed, error = validation._post_task_validation_in_worktree(
            self.project_name,
            self.pass_task_id,
            self.project_path,
            timeout=15,
        )
        if failed:
            return fail(error)
        if self.request("GET", f"/api/tasks/bug-{self.pass_task_id}").get("task"):
            return fail("validation pass spawned a bug task")
        return pass_(".swarm_validate passed")

    def step_pass_no_bug(self):
        self._ensure_setup()
        if self.request("GET", f"/api/tasks/bug-{self.pass_task_id}").get("task"):
            return fail("bug task exists after clean validation")
        return pass_("no validation bug task spawned")

    def step_fail_spawns_bug(self):
        self._ensure_setup()
        from swarm import validation

        (self.project_path / ".swarm_validate").write_text(
            f"echo '{self.fail_error}' >&2\nexit 1\n"
        )
        failed, error = validation._post_task_validation_in_worktree(
            self.project_name,
            self.fail_task_id,
            self.project_path,
            timeout=15,
        )
        if not failed:
            return fail("bad validation unexpectedly passed")
        validation._spawn_validation_bug_task(
            self.project_name,
            self.fail_task_id,
            error,
            original_task=self._get_task(self.fail_task_id),
        )
        bug = self._get_task(f"bug-{self.fail_task_id}")
        if not bug:
            return fail("validation bug task was not created")
        problems = []
        if bug.get("type") != "bug":
            problems.append(f"type={bug.get('type')}")
        if bug.get("priority") != 100:
            problems.append(f"priority={bug.get('priority')} expected=100")
        metadata = bug.get("metadata") or {}
        if not metadata.get("last_failure"):
            problems.append("metadata.last_failure missing")
        if self.fail_error not in metadata.get("error_log", ""):
            problems.append("metadata.error_log missing validation output")
        if problems:
            return fail("; ".join(problems))
        return pass_(f"bug-{self.fail_task_id}")

    def _ensure_setup(self) -> None:
        if self.project_path is None:
            self.setup()

    def _create_task(self, task_id: str, description: str) -> None:
        data = self.request("POST", "/api/tasks", {
            "id": task_id,
            "project": self.project_name,
            "type": "feature",
            "description": description,
            "priority": 50,
            "metadata": {"probe": "feature_probe"},
        })
        if "error" in data:
            raise RuntimeError(data["error"])
        self.created_task_ids.append(task_id)

    def _get_task(self, task_id: str) -> dict | None:
        data = self.request("GET", f"/api/tasks/{task_id}")
        return data.get("task")

    def _execute_as_feature_agent(self, tool_call: dict) -> dict:
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
            rt.TASK_ID = self.feature_task_id
            rt.TASK_TYPE = "feature"
            rt.TASK_PRIORITY = 50
            rt.API_PORT = self.api_port
            rt.READONLY = False
            rt.TASK_METADATA = {}
            rt.RUN_BROADCAST_WRITE_COUNT = 1
            rt.CLAIMED_FILE_PATHS = {self.target_file}
            rt._sync_all_tool_globals()
            validation_error = validate_tool_call(tool_call)
            if validation_error:
                return {"ok": False, "error": validation_error}
            return execute_tool(tool_call)
        finally:
            for key, value in old_values.items():
                setattr(rt, key, value)
            rt._sync_all_tool_globals()


def build_probe(project_name: str, project_path: Path, config: dict) -> ProbeRunner:
    ctx = FeatureProbeContext(project_name, project_path, config)
    steps = [
        ProbeStep("1-read", ctx.step_read_file, title="Read file"),
        ProbeStep("2-write", ctx.step_write_file, title="Write file"),
        ProbeStep("3-commit", ctx.step_git_commit, title="Git commit"),
        ProbeStep("4-validation-fires", ctx.step_validation_fires, title="Validation fires"),
        ProbeStep("5-pass-no-bug", ctx.step_pass_no_bug, title="Pass creates no bug"),
        ProbeStep("6-fail-spawns-bug", ctx.step_fail_spawns_bug, title="Fail spawns bug"),
    ]
    return ProbeRunner(steps, project_name, config, cleanup=ctx.cleanup)


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {result.stderr or result.stdout}")
    return result
