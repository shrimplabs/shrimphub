"""Probe for harness_qa checkpoint handshake.

Creates a tiny Godot project with the canonical TestHarness autoload and a
scripted two-checkpoint scene, then exercises the same harness tools used by
harness_qa agents.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import time
from pathlib import Path

from tools.task_probe import ProbeRunner, ProbeStep, fail, pass_


class HarnessQAProbeContext:
    def __init__(self, project_name: str, project_path: Path, config: dict):
        self.requested_project = project_name
        self.requested_project_path = project_path
        self.config = config
        self.tmp_root: Path | None = None
        self.project_name = f"harness-probe-{int(time.time() * 1000)}"
        self.project_path: Path | None = None
        self.launch_result: dict | None = None
        self.first_checkpoint: dict | None = None
        self.second_checkpoint: dict | None = None

    def setup(self) -> None:
        if self.project_path is not None:
            return
        self.tmp_root = Path(tempfile.mkdtemp(prefix="swarm-harness-probe-"))
        self.project_path = self.tmp_root / self.project_name
        (self.project_path / "autoload").mkdir(parents=True)
        (self.project_path / "scripts").mkdir()
        shutil.copyfile(
            Path(__file__).resolve().parent.parent / "templates/godot/autoload/test_harness.gd",
            self.project_path / "autoload/test_harness.gd",
        )
        (self.project_path / "project.godot").write_text(
            "\n".join([
                'config_version=5',
                '',
                '[application]',
                f'config/name="{self.project_name}"',
                'run/main_scene="res://main.tscn"',
                '',
                '[autoload]',
                'TestHarness="*res://autoload/test_harness.gd"',
                '',
            ])
        )
        (self.project_path / "main.tscn").write_text(
            "\n".join([
                '[gd_scene load_steps=2 format=3]',
                '',
                '[ext_resource type="Script" path="res://scripts/main.gd" id="1_main"]',
                '',
                '[node name="HarnessProbeMain" type="Node"]',
                'script = ExtResource("1_main")',
                '',
            ])
        )
        (self.project_path / "scripts/main.gd").write_text(
            "\n".join([
                'extends Node',
                '',
                'var phase: int = 0',
                '',
                'func _ready() -> void:',
                '\tcall_deferred("_run_probe")',
                '',
                'func _run_probe() -> void:',
                '\tif not TestHarness.ENABLED:',
                '\t\treturn',
                '\tawait get_tree().create_timer(5.0).timeout',
                '\tvar first_action = await TestHarness.checkpoint({',
                '\t\t"event": "first_checkpoint",',
                '\t\t"phase": phase,',
                '\t\t"can_continue": true,',
                '\t})',
                '\tif str(first_action.get("type", "")) != "pass":',
                '\t\tpush_error("Expected pass action at first checkpoint")',
                '\t\tget_tree().quit(11)',
                '\t\treturn',
                '\tphase = 1',
                '\tvar second_action = await TestHarness.checkpoint({',
                '\t\t"event": "after_pass",',
                '\t\t"phase": phase,',
                '\t\t"can_fail": true,',
                '\t})',
                '\tif str(second_action.get("type", "")) == "fail":',
                '\t\tprint("HARNESS_PROBE_FAIL_RECEIVED")',
                '\t\tget_tree().quit(7)',
                '\telse:',
                '\t\tget_tree().quit(0)',
                '',
            ])
        )

    def cleanup(self) -> None:
        try:
            import swarm.qa_tools as qa
            qa.harness_kill_game()
        finally:
            if self.tmp_root and self.tmp_root.exists():
                shutil.rmtree(self.tmp_root, ignore_errors=True)

    def step_launch(self):
        self.setup()
        import swarm.qa_tools as qa

        result = self._execute_as_harness_agent({"tool": "harness_launch_game", "args": {}})
        self.launch_result = result
        if not result.get("ok"):
            return fail(result.get("error", "unknown launch error"))
        return pass_(f"pid={result.get('pid')} state_port={qa._state_port} harness_port={qa._harness_port}")

    def step_port(self):
        import swarm.qa_tools as qa

        deadline = time.time() + 10
        last_error = ""
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", qa._harness_port), timeout=1.0) as sock:
                    sock.sendall((json.dumps({"type": "wait"}) + "\n").encode())
                    sock.settimeout(2.0)
                    data = sock.recv(4096)
                if data:
                    return pass_(f"port={qa._harness_port}")
            except Exception as exc:
                last_error = str(exc)
                time.sleep(0.2)
        return fail(last_error or f"could not connect to harness port {qa._harness_port}")

    def step_checkpoint(self):
        state = self._checkpoint_exchange({"type": "pass"}, "first_checkpoint", timeout=12)
        if "error" in state:
            return fail(state["error"])
        if state.get("event") != "first_checkpoint":
            return fail(f"unexpected checkpoint state: {state}")
        self.first_checkpoint = state
        return pass_(f"event={state.get('event')} phase={state.get('phase')}")

    def step_pass(self):
        state = self._checkpoint_exchange({"type": "fail"}, "after_pass", timeout=12)
        if "error" in state:
            return fail(state["error"])
        if state.get("event") != "after_pass" or state.get("phase") != 1:
            return fail(f"game did not advance after pass: {state}")
        self.second_checkpoint = state
        return pass_(f"event={state.get('event')} phase={state.get('phase')}")

    def step_fail(self):
        import swarm.qa_tools as qa

        proc = qa._harness_game_process
        if proc is None:
            return fail("harness process missing")
        deadline = time.time() + 10
        while time.time() < deadline:
            code = proc.poll()
            if code is not None:
                if code == 7:
                    return pass_("game exited with fail code 7")
                return fail(f"game exited with unexpected code {code}")
            time.sleep(0.2)
        return fail("game did not halt after fail response")

    def _checkpoint_exchange(self, action: dict, event: str, timeout: int) -> dict:
        import swarm.qa_tools as qa

        deadline = time.time() + timeout
        last_state: dict = {}
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", qa._harness_port), timeout=1.0) as sock:
                    sock.settimeout(2.0)
                    data = b""
                    while b"\n" not in data:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    if not data:
                        last_state = {"error": "empty harness response"}
                    else:
                        state = json.loads(data.decode().strip())
                        last_state = state
                        if state.get("event") == event:
                            sock.sendall((json.dumps(action) + "\n").encode())
                            return state
            except Exception as exc:
                last_state = {"error": str(exc)}
            time.sleep(0.2)
        return {"error": f"timed out waiting for checkpoint {event}; last_state={last_state}"}

    def _execute_as_harness_agent(self, tool_call: dict) -> dict:
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
            "QA_CONFIG": rt.QA_CONFIG,
        }
        try:
            rt.WORKSPACE = self.tmp_root
            rt.DATA_DIR = "data"
            rt.PROJECT = self.project_name
            rt.PROJECT_PATH_OVERRIDE = ""
            rt.TASK_ID = "harness-qa-probe-agent-view"
            rt.TASK_TYPE = "harness_qa"
            rt.TASK_PRIORITY = 50
            rt.API_PORT = int(self.config.get("api_port") or self.config.get("port") or 5001)
            rt.READONLY = False
            rt.TASK_METADATA = {}
            rt.RUN_BROADCAST_WRITE_COUNT = 0
            rt.CLAIMED_FILE_PATHS = set()
            rt.QA_CONFIG = self.config
            rt._sync_all_tool_globals()

            validation_error = validate_tool_call(tool_call)
            if validation_error:
                return {"ok": False, "error": validation_error}
            result = execute_tool(tool_call)
            if isinstance(result, dict):
                return result
            return {"ok": False, "error": f"unexpected tool result: {result!r}"}
        finally:
            for key, value in old_values.items():
                setattr(rt, key, value)
            rt._sync_all_tool_globals()


def build_probe(project_name: str, project_path: Path, config: dict) -> ProbeRunner:
    ctx = HarnessQAProbeContext(project_name, project_path, config)
    steps = [
        ProbeStep("1-launch", ctx.step_launch, title="Harness launch"),
        ProbeStep("2-port", ctx.step_port, title="Harness port"),
        ProbeStep("3-checkpoint", ctx.step_checkpoint, title="Checkpoint"),
        ProbeStep("4-pass", ctx.step_pass, title="Pass response"),
        ProbeStep("5-fail", ctx.step_fail, title="Fail response"),
    ]
    return ProbeRunner(steps, project_name, config, cleanup=ctx.cleanup)
