"""Probe for art_pass task critical path."""

from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from tools.task_probe import ProbeRunner, ProbeStep, warn, fail, pass_


_PNG_1X1_RED = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class ArtPassProbeContext:
    def __init__(self, project_name: str, project_path: Path, config: dict):
        self.requested_project = project_name
        self.requested_project_path = project_path
        self.config = config
        self.tmp_root: Path | None = None
        self.project_name = f"art-probe-{int(time.time() * 1000)}"
        self.project_path: Path | None = None
        self.asset_library: Path | None = None
        self.source_asset: Path | None = None
        self.dest_asset: Path | None = None
        self.screenshot_path: str | None = None

    def setup(self) -> None:
        if self.project_path is not None:
            return
        self.tmp_root = Path(tempfile.mkdtemp(prefix="swarm-art-probe-"))
        self.project_path = self.tmp_root / self.project_name
        (self.project_path / "autoload").mkdir(parents=True)
        (self.project_path / "assets").mkdir()
        (self.project_path / "scripts").mkdir()
        shutil.copyfile(
            Path(__file__).resolve().parent.parent / "templates/godot/autoload/state_server.gd",
            self.project_path / "autoload/state_server.gd",
        )
        (self.project_path / "project.godot").write_text(
            "\n".join([
                "config_version=5",
                "",
                "[application]",
                f'config/name="{self.project_name}"',
                'run/main_scene="res://main.tscn"',
                "",
                "[autoload]",
                'StateServer="*res://autoload/state_server.gd"',
                "",
                "[display]",
                "window/size/viewport_width=320",
                "window/size/viewport_height=180",
                "",
            ])
        )
        (self.project_path / "GAME_DESIGN.md").write_text(
            "# Art Probe\n\nA minimal neon menu used to validate art pass tools.\n"
        )
        (self.project_path / "main.tscn").write_text(
            "\n".join([
                "[gd_scene load_steps=2 format=3]",
                "",
                '[ext_resource type="Script" path="res://scripts/main.gd" id="1_main"]',
                "",
                '[node name="ArtProbeMain" type="Node2D"]',
                'script = ExtResource("1_main")',
                "",
            ])
        )
        (self.project_path / "scripts/main.gd").write_text(
            "\n".join([
                "extends Node2D",
                "",
                "func _ready() -> void:",
                "\tpass",
                "",
                "func _draw() -> void:",
                "\tdraw_rect(Rect2(0, 0, 320, 180), Color(0.05, 0.06, 0.08))",
                "\tdraw_rect(Rect2(36, 42, 248, 96), Color(0.0, 0.75, 0.95))",
                "\tdraw_rect(Rect2(48, 54, 224, 72), Color(0.98, 0.15, 0.45))",
                "",
                "func get_game_state() -> Dictionary:",
                '\treturn {"screen": "art_probe", "palette": "cyan_magenta"}',
                "",
            ])
        )
        _run(["git", "init"], self.project_path)
        _run(["git", "config", "user.email", "probe@example.invalid"], self.project_path)
        _run(["git", "config", "user.name", "Art Pass Probe"], self.project_path)
        _run(["git", "add", "-A"], self.project_path)
        _run(["git", "commit", "-m", "Initial art pass probe fixture"], self.project_path)

        configured = _configured_asset_library(self.config)
        if configured and configured.exists():
            self.asset_library = configured
            candidates = [p for p in configured.rglob("*") if p.is_file()]
            self.source_asset = candidates[0] if candidates else None
        else:
            self.asset_library = self.tmp_root / "asset-library"
            self.asset_library.mkdir()
            self.source_asset = self.asset_library / "probe_red_pixel.png"
            self.source_asset.write_bytes(base64.b64decode(_PNG_1X1_RED))

    def cleanup(self) -> None:
        try:
            import swarm.qa_tools as qa
            qa.kill_game()
        finally:
            if self.tmp_root and self.tmp_root.exists():
                shutil.rmtree(self.tmp_root, ignore_errors=True)

    def step_launch(self):
        self.setup()
        result = self._execute_as_art_agent({
            "tool": "launch_game",
            "args": {"project_path": str(self.project_path)},
        })
        if not result.get("ok"):
            return fail(result.get("error", str(result)))
        return pass_(f"pid={result.get('pid')}")

    def step_screenshot(self):
        result = self._execute_as_art_agent({
            "tool": "take_screenshot",
            "args": {"filename": "art_probe"},
        })
        if not result.get("ok"):
            return fail(result.get("error", str(result)))
        path = result.get("path", "")
        if not path or not Path(path).exists() or Path(path).stat().st_size <= 0:
            return fail(f"screenshot missing or empty: {path}")
        self.screenshot_path = path
        return pass_(f"source={result.get('source')} path={path}")

    def step_vision(self):
        if not self.screenshot_path:
            return fail("screenshot was not captured")
        result = self._execute_as_art_agent({
            "tool": "vision_query",
            "args": {
                "image_path": self.screenshot_path,
                "question": "Describe the visible colors and simple UI shapes in one sentence.",
                "model": "fast",
                "timeout": 45,
            },
        })
        if not result.get("ok"):
            return fail(result.get("error", str(result)))
        answer = str(result.get("answer", "")).strip()
        if not answer:
            return fail("vision returned an empty answer")
        return pass_(f"provider={result.get('provider')} answer={answer[:80]}")

    def step_asset_list(self):
        self.setup()
        configured = _configured_asset_library(self.config)
        if not configured or not configured.exists():
            return warn(f"no configured asset library; using generated fallback {self.asset_library}")
        files = [p for p in configured.iterdir()]
        if not files:
            return fail(f"configured asset library is empty: {configured}")
        return pass_(f"{len(files)} entries in {configured}")

    def step_file_copy(self):
        if not self.source_asset or not self.source_asset.exists():
            return fail("source asset missing")
        self.dest_asset = self.project_path / "assets" / self.source_asset.name
        shutil.copyfile(self.source_asset, self.dest_asset)
        if not self.dest_asset.exists() or self.dest_asset.stat().st_size <= 0:
            return fail(f"copy failed: {self.dest_asset}")
        return pass_(f"{self.source_asset.name} -> assets/{self.dest_asset.name}")

    def step_commit(self):
        result = self._execute_as_art_agent({
            "tool": "git_commit",
            "args": {"message": "Art pass probe asset copy"},
        })
        if not result.get("ok"):
            return fail(result.get("error", str(result)))
        log = _run(["git", "log", "-1", "--pretty=%s"], self.project_path).stdout.strip()
        if log != "Art pass probe asset copy":
            return fail(f"unexpected git log -1: {log}")
        return pass_(log)

    def step_cleanup_commit(self):
        if self.dest_asset and self.dest_asset.exists():
            self.dest_asset.unlink()
        result = self._execute_as_art_agent({
            "tool": "git_commit",
            "args": {"message": "Art pass probe cleanup"},
        })
        if not result.get("ok"):
            return fail(result.get("error", str(result)))
        status = _run(["git", "status", "--short"], self.project_path).stdout.strip()
        if status:
            return fail(f"scratch repo not clean: {status}")
        return pass_("scratch project clean")

    def _execute_as_art_agent(self, tool_call: dict) -> dict:
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
            rt.WORKSPACE = self.project_path.parent
            rt.DATA_DIR = "data"
            rt.PROJECT = self.project_name
            rt.PROJECT_PATH_OVERRIDE = str(self.project_path)
            rt.TASK_ID = "art-pass-probe-agent-view"
            rt.TASK_TYPE = "art_pass"
            rt.TASK_PRIORITY = 50
            rt.API_PORT = int(self.config.get("api_port") or self.config.get("port") or 5001)
            rt.READONLY = False
            rt.TASK_METADATA = {}
            rt.RUN_BROADCAST_WRITE_COUNT = 1
            rt.CLAIMED_FILE_PATHS = {"assets/"}
            rt.QA_CONFIG = self.config
            rt._sync_all_tool_globals()
            validation_error = validate_tool_call(tool_call)
            if validation_error:
                return {"ok": False, "error": validation_error}
            result = execute_tool(tool_call)
            return result if isinstance(result, dict) else {"ok": False, "error": repr(result)}
        finally:
            for key, value in old_values.items():
                setattr(rt, key, value)
            rt._sync_all_tool_globals()


def build_probe(project_name: str, project_path: Path, config: dict) -> ProbeRunner:
    ctx = ArtPassProbeContext(project_name, project_path, config)
    steps = [
        ProbeStep("1-launch", ctx.step_launch, title="Launch"),
        ProbeStep("2-screenshot", ctx.step_screenshot, title="Screenshot"),
        ProbeStep("3-vision", ctx.step_vision, title="Vision"),
        ProbeStep("4-asset-list", ctx.step_asset_list, title="Asset list"),
        ProbeStep("5-file-copy", ctx.step_file_copy, title="File copy"),
        ProbeStep("6-commit", ctx.step_commit, title="Commit"),
        ProbeStep("7-cleanup", ctx.step_cleanup_commit, title="Cleanup"),
    ]
    return ProbeRunner(steps, project_name, config, cleanup=ctx.cleanup)


def _configured_asset_library(config: dict) -> Path | None:
    for key in ("asset_library", "asset_library_path", "assets_path", "asset_path"):
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser()
    return None


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {result.stderr or result.stdout}")
    return result
