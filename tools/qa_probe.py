#!/usr/bin/env python3
"""qa_probe.py — diagnostic tool for the QA pipeline.

Exercises the full QA stack against a live Godot game with no LLM involved.
Each probe prints PASS / WARN / FAIL with evidence so you can see exactly
what the agent sees before you run one.

Usage:
    .venv/bin/python tools/qa_probe.py <project_name>
    .venv/bin/python tools/qa_probe.py anti-grav-rush
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: add swarm-controller root to sys.path so `swarm.*` imports work
# ---------------------------------------------------------------------------

SWARM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SWARM_ROOT))


def load_config() -> dict:
    cfg_path = SWARM_ROOT / "config.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text())
        except Exception as e:
            print(f"[warn] Could not read config.json: {e}")
    return {}


def resolve_project_path(project_name: str, config: dict) -> Path:
    workspace = config.get("workspace", "~/workspace")
    return Path(os.path.expanduser(workspace)) / project_name


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

class Result:
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class ProbeResults:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []  # (probe, status, note)

    def record(self, probe: str, status: str, note: str = ""):
        self.rows.append((probe, status, note))
        icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}.get(status, "?")
        colour = {"PASS": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31m"}.get(status, "")
        reset = "\033[0m"
        print(f"  {colour}{icon} {status}{reset}  {note}")

    def summary(self):
        print()
        print("=" * 60)
        print("SUMMARY")
        print("=" * 60)
        for probe, status, note in self.rows:
            icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}.get(status, "?")
            print(f"  {icon} {probe:<30}  {status}  {note[:60]}")
        total = len(self.rows)
        passed = sum(1 for _, s, _ in self.rows if s == Result.PASS)
        warned = sum(1 for _, s, _ in self.rows if s == Result.WARN)
        failed = sum(1 for _, s, _ in self.rows if s == Result.FAIL)
        print(f"\n  {passed}/{total} passed, {warned} warnings, {failed} failures")


# ---------------------------------------------------------------------------
# Probe runner
# ---------------------------------------------------------------------------

def run_probes(project_name: str, project_path: Path, config: dict):
    import swarm.qa_tools as qa

    # Patch module globals so qa_tools knows where the project lives
    qa.WORKSPACE = project_path.parent
    qa.PROJECT = project_name
    qa.PROJECT_PATH_OVERRIDE = str(project_path)
    qa.QA_CONFIG = config
    qa.DATA_DIR = str(SWARM_ROOT / "data")

    # Also set godot_path in config if specified
    godot_path = config.get("godot_path", "")
    if godot_path:
        os.environ.setdefault("GODOT_PATH", godot_path)

    results = ProbeResults()

    # ------------------------------------------------------------------
    # Probe 1: Launch
    # ------------------------------------------------------------------
    print()
    print("Probe 1 — Launch")
    launch_result = qa.launch_game(str(project_path))
    time.sleep(3)

    if not launch_result.get("ok"):
        results.record("1-launch", Result.FAIL, launch_result.get("error", "unknown"))
        print("  Cannot continue without a running game — aborting.")
        results.summary()
        return

    pid = launch_result.get("pid")
    state_port = qa._state_port
    harness_port = qa._harness_port
    results.record(
        "1-launch", Result.PASS,
        f"pid={pid}  state_port={state_port}  harness_port={harness_port}",
    )

    # ------------------------------------------------------------------
    # Probe 2: StateServer connection
    # ------------------------------------------------------------------
    print()
    print("Probe 2 — StateServer connection")
    state = qa.get_game_state()
    if "error" in state:
        results.record("2-state-server", Result.FAIL, state["error"])
    elif "scene_tree" not in state:
        results.record(
            "2-state-server", Result.WARN,
            f"Response lacks scene_tree key. Keys: {list(state.keys())}",
        )
    else:
        node_type = state["scene_tree"].get("type", "?")
        results.record("2-state-server", Result.PASS, f"root node type={node_type}")

    # ------------------------------------------------------------------
    # Probe 3: Screenshot
    # ------------------------------------------------------------------
    print()
    print("Probe 3 — Screenshot")
    ss = qa.take_screenshot("probe_test")
    if not ss.get("ok"):
        results.record("3-screenshot", Result.FAIL, ss.get("error", "unknown"))
    else:
        source = ss.get("source", "unknown")
        path = ss.get("path", "")
        note = f"source={source}  path={path}"
        if source == "state_server":
            results.record("3-screenshot", Result.PASS, note)
        else:
            results.record("3-screenshot", Result.WARN, f"screencapture fallback — {note}")

        if path and os.path.exists(path):
            try:
                subprocess.Popen(["open", path])
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Probe 4: Game state (game_state dict)
    # ------------------------------------------------------------------
    print()
    print("Probe 4 — Game state (get_game_state)")
    state2 = qa.get_game_state()
    game_state = state2.get("game_state", {})
    print(f"  game_state = {json.dumps(game_state, indent=2)[:400]}")
    if "error" in state2:
        results.record("4-game-state", Result.FAIL, state2["error"])
    elif game_state:
        results.record("4-game-state", Result.PASS, f"{len(game_state)} keys")
    else:
        results.record(
            "4-game-state", Result.WARN,
            "game_state is empty — root scene may not implement get_game_state()",
        )

    # ------------------------------------------------------------------
    # Probe 5: key_hold
    # ------------------------------------------------------------------
    print()
    print("Probe 5 — key_hold")

    def _extract_position(state_dict: dict) -> list | None:
        """Try common position fields from game_state, including nested ship_state."""
        gs = state_dict.get("game_state", {})
        # Top-level position fields
        for key in ("position", "player_position", "ship_position", "pos"):
            if key in gs:
                return gs[key]
        # Nested ship_state / player / ship objects
        for key in ("ship_state", "player", "ship", "character", "player_state"):
            obj = gs.get(key, {})
            if isinstance(obj, dict):
                for pk in ("position", "pos"):
                    if pk in obj:
                        return obj[pk]
        return None

    pos_before = _extract_position(state2)
    print(f"  position before = {pos_before}")

    # Try ship_thrust first (anti-grav / space games), fall back to move_right
    hold_key = "ship_thrust"
    hold_result = qa.key_hold(hold_key, 2.0)
    if not hold_result.get("ok"):
        hold_key = "move_right"
        hold_result = qa.key_hold(hold_key, 2.0)

    if not hold_result.get("ok"):
        results.record("5-key-hold", Result.FAIL, hold_result.get("error", "key_hold returned not ok"))
    else:
        time.sleep(0.5)
        state3 = qa.get_game_state()
        pos_after = _extract_position(state3)
        print(f"  position after  = {pos_after}")

        # Take burst and open last frame
        burst = qa.screenshot_burst("probe_burst", 4, 0.5)
        if burst.get("paths"):
            last_frame = burst["paths"][-1]
            try:
                subprocess.Popen(["open", last_frame])
            except Exception:
                pass
            print(f"  burst: {len(burst['paths'])} frames, last={last_frame}")

        if pos_before is None or pos_after is None:
            results.record(
                "5-key-hold", Result.WARN,
                f"key={hold_key} ok  source={hold_result.get('source')}  "
                "cannot verify movement (game_state has no position field)",
            )
        elif pos_before != pos_after:
            results.record(
                "5-key-hold", Result.PASS,
                f"key={hold_key}  position changed {pos_before} → {pos_after}",
            )
        else:
            results.record(
                "5-key-hold", Result.WARN,
                f"key={hold_key} ok but position unchanged — check input map",
            )

    # ------------------------------------------------------------------
    # Probe 6: play_macro
    # ------------------------------------------------------------------
    print()
    print("Probe 6 — play_macro")
    time.sleep(1.0)  # let StateServer finish any pending hold before issuing macro
    macro = [
        {"type": "hold",   "action": "ship_thrust",  "duration": 1.0},
        {"type": "wait",   "seconds": 0.5},
        {"type": "hold",   "action": "turn_right",    "duration": 1.0},
    ]
    macro_result = qa.play_macro(macro)
    if macro_result.get("ok"):
        results.record(
            "6-play-macro", Result.PASS,
            f"source={macro_result.get('source')}  actions={macro_result.get('actions')}",
        )
    else:
        # StateServer may not support play_macro; that's still informative
        results.record(
            "6-play-macro", Result.WARN,
            macro_result.get("error", "play_macro not supported by this StateServer version"),
        )

    # ------------------------------------------------------------------
    # Kill the game
    # ------------------------------------------------------------------
    print()
    print("Killing game…")
    kill_result = qa.kill_game()
    print(f"  kill_game → {kill_result}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    results.summary()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <project_name>")
        print(f"  e.g. {sys.argv[0]} anti-grav-rush")
        sys.exit(1)

    project_name = sys.argv[1]
    config = load_config()
    project_path = resolve_project_path(project_name, config)

    if not project_path.exists():
        print(f"Error: project path does not exist: {project_path}")
        sys.exit(1)

    if not (project_path / "project.godot").exists():
        print(f"Warning: no project.godot found at {project_path} — is this a Godot project?")

    print(f"QA Probe — {project_name}")
    print(f"  path: {project_path}")
    print(f"  config: {SWARM_ROOT / 'config.json'}")

    run_probes(project_name, project_path, config)


if __name__ == "__main__":
    main()
