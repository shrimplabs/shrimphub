"""
swarm.tools.playthrough_kit -- genre-agnostic scaffold for per-project completion bots.

This module deliberately contains ZERO game-specific logic (no genre names,
no "shooter"/"puzzle" branches, no assumptions about what get_game_state()
returns). A single shared decision policy would inevitably get tuned against
whichever game exercises it first and silently overfit -- passing on that
genre, useless elsewhere. Instead, each project writes its own small bot
(typically `tests/playthrough_bot.py`) that imports this kit for the parts
that are mechanically identical across every game -- StateServer plumbing,
real coordinate-based click injection, stuck detection, trace output, and
the CLI harness -- and supplies only two callables: `decide()` and
`is_terminal()`.

Zero LLM calls, zero vision calls, in the hot loop. This both runs at native
game speed (no inference-latency bottleneck) and catches a class of bug that
vision/LLM-driven QA can miss: StateServer's `press_button` command fires
`button.emit_signal("pressed")` directly, bypassing Godot's real input
pipeline the same way a human's mouse click doesn't. This kit's `click_at`
and `click_label` use `{"command":"input","type":"click","x":N,"y":N}` --
real coordinate injection through `Input.parse_input_event`, which only
succeeds if the click actually lands on a live, hit-testable control. A
button whose signal connection is silently dead (as found in the
void-patrol-adaptive-flat-art-run11 investigation) produces zero state
change on a real click, which is exactly the "no progress" signal that
StuckDetector reports.

CLI usage from a project's own bot script:

    from swarm.tools.playthrough_kit import run_bot_cli, Action

    def decide(state, a11y, history):
        # project-specific: look at a11y["a11y_tree"] labels and
        # state["game_state"] fields, return an Action
        ...

    def is_terminal(state):
        # project-specific: what does "done" look like for this game?
        gs = state.get("game_state", {})
        return gs.get("status") in ("victory", "game_over")

    if __name__ == "__main__":
        run_bot_cli(decide, is_terminal)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# Reuse the existing StateServer TCP client rather than duplicating it --
# it's already a clean, dependency-free implementation.
from swarm.tools.scenario_qa import StateServerClient


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------

@dataclass
class Action:
    """One decision returned by a project's decide() callable."""
    kind: str  # click | click_label | key | key_combo | hold | wait | noop
    x: float = 0.0
    y: float = 0.0
    label: str = ""
    seconds: float = 0.1
    key: str = ""
    keys: list[str] = field(default_factory=list)
    action: str = ""
    duration: float = 0.1


# ---------------------------------------------------------------------------
# Real coordinate-based input (NOT StateServer's press_button bypass)
# ---------------------------------------------------------------------------

def click_at(client: StateServerClient, x: float, y: float) -> dict:
    """Inject a real mouse click at game-viewport coordinates (x, y).

    Goes through Godot's actual Control hit-testing pipeline
    (Input.parse_input_event), unlike StateServer's press_button command
    which calls emit_signal("pressed") directly on a node found by name.
    A click here only succeeds if it geometrically lands on a live,
    enabled, visible control -- which is exactly the fidelity a completion
    bot needs to catch dead UI wiring.
    """
    return client.send({"command": "input", "type": "click", "x": x, "y": y})


def click_label(client: StateServerClient, a11y: dict, label_substring: str) -> bool:
    """Find a visible a11y_tree entry whose label contains label_substring
    (case-insensitive) and click its bounds center. Returns False if no
    match was found (callers decide how to handle a missing expected button)."""
    entries = a11y.get("a11y_tree", []) if isinstance(a11y, dict) else []
    needle = label_substring.lower()
    for entry in entries:
        if not entry.get("visible", True):
            continue
        label = str(entry.get("label", ""))
        if needle in label.lower():
            bounds = entry.get("bounds")
            if not bounds or len(bounds) != 4:
                continue
            cx = bounds[0] + bounds[2] / 2.0
            cy = bounds[1] + bounds[3] / 2.0
            click_at(client, cx, cy)
            return True
    return False


# ---------------------------------------------------------------------------
# Stuck detection -- purely structural, no knowledge of field meaning
# ---------------------------------------------------------------------------

class StuckDetector:
    """Tracks a rolling window of state snapshots and reports True once
    nothing has changed for `window` consecutive ticks. Ignores the
    top-level "timestamp" field (which always changes) -- compares
    everything else structurally, with no knowledge of what any field means.
    """

    def __init__(self, window: int = 30):
        self.window = window
        self._history: list[str] = []

    @staticmethod
    def _fingerprint(state: dict) -> str:
        comparable = {k: v for k, v in state.items() if k != "timestamp"}
        try:
            return json.dumps(comparable, sort_keys=True, default=str)
        except Exception:
            return str(comparable)

    def tick(self, state: dict) -> bool:
        """Record a new state snapshot; return True if stuck."""
        self._history.append(self._fingerprint(state))
        if len(self._history) > self.window:
            self._history.pop(0)
        if len(self._history) < self.window:
            return False
        return len(set(self._history)) == 1


# ---------------------------------------------------------------------------
# Trace output -- same scenario_trace.jsonl convention as scenario_qa.py
# ---------------------------------------------------------------------------

def write_trace(path: Path, ticks: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for tick in ticks:
            f.write(json.dumps(tick, default=str) + "\n")


# ---------------------------------------------------------------------------
# Game process launch -- same pattern as scenario_qa.py's main()
# ---------------------------------------------------------------------------

def _launch(project_path: str, port: int) -> tuple[StateServerClient, Optional[subprocess.Popen]]:
    """Launch the game and return a connected client + process handle.

    Prefers swarm.qa_tools.launch_game (dynamic port allocation, avoids
    collisions with other concurrent agents); falls back to a manual
    subprocess launch on the requested port if that import fails (e.g. when
    running standalone outside the swarm's Python environment).
    """
    try:
        swarm_root = Path(__file__).parent.parent.parent
        sys.path.insert(0, str(swarm_root))
        from swarm.qa_tools import launch_game
        from swarm import qa_tools as _qt
        # qa_tools.DATA_DIR defaults to the relative string "data", which only
        # resolves correctly when CWD is the swarm-controller repo. A
        # project's own playthrough_bot.py runs from within ITS OWN directory,
        # so without this, launch_game's PID-tracking write fails with
        # "No such file or directory: 'data/godot_pids.json'" (non-fatal to
        # the actual game launch, but worth fixing rather than tolerating).
        _qt.DATA_DIR = str(swarm_root / "data")
        result = launch_game(project_path)
        if not result.get("ok", True) and "error" in result:
            print(f"[playthrough_kit] WARNING: launch_game reported: {result.get('error')}")
        client = StateServerClient(port=_qt._state_port)
        process = _qt._qa_game_process
        return client, process
    except Exception as e:
        print(f"[playthrough_kit] launch_game import failed ({e}), launching manually")
        godot_bin = os.environ.get("GODOT_PATH", "godot")
        process = subprocess.Popen(
            [godot_bin, "--path", project_path, "--", "--state-port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client = StateServerClient(port=port)
        return client, process


# ---------------------------------------------------------------------------
# CLI harness -- the poll loop. Project bots supply only decide + is_terminal.
# ---------------------------------------------------------------------------

def run_bot_cli(
    decide: Callable[[dict, dict, list], Action],
    is_terminal: Callable[[dict], bool],
    default_out_dir: str = "/tmp/playthrough_bot",
    classify_failure: Optional[Callable[[dict], Optional[str]]] = None,
    progress: Optional[Callable[[dict], dict]] = None,
) -> None:
    """Parse standard CLI args, run the poll loop, exit 0 on terminal state
    reached or 1 on stuck/timeout. Writes a trace file for post-mortem.

    `decide(state, a11y, history)` -- project-specific action selection.
        `history` is the list of prior tick dicts (state + a11y + action),
        oldest first, so decide() can react to recent trends if it wants to.
    `is_terminal(state)` -- project-specific full-completion check. Early loss
        states must return False and should be described by `classify_failure`.
    `classify_failure(state)` -- optional project-specific terminal failure
        classifier (for example, return "game_over" on an early loss).
    `progress(state)` -- optional project-specific structured progress evidence
        included in every trace tick and in the final result receipt.
    """
    parser = argparse.ArgumentParser(description="playthrough_kit: deterministic completion bot")
    parser.add_argument("--project-path", help="Path to Godot project (required unless --no-launch)")
    parser.add_argument("--port", type=int, default=11009, help="StateServer port (default 11009)")
    parser.add_argument("--max-ticks", type=int, default=2000, help="Max decision ticks before giving up")
    parser.add_argument("--timeout", type=float, default=300.0, help="Wall-clock seconds before giving up")
    parser.add_argument("--stuck-window", type=int, default=30, help="Ticks with no state change before declaring stuck")
    parser.add_argument("--out-dir", default=default_out_dir, help="Output directory for trace file")
    parser.add_argument("--no-launch", action="store_true", help="Skip launching the game (assume already running)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / "playthrough_trace.jsonl"

    process = None
    if args.no_launch:
        client = StateServerClient(port=args.port)
        if not client.wait_ready(timeout=10):
            print("[playthrough_kit] ERROR: StateServer not available", file=sys.stderr)
            sys.exit(1)
    else:
        if not args.project_path:
            print("[playthrough_kit] ERROR: --project-path required unless --no-launch", file=sys.stderr)
            sys.exit(1)
        client, process = _launch(args.project_path, args.port)
        if not client.wait_ready(timeout=15):
            print("[playthrough_kit] ERROR: StateServer did not come up in 15s", file=sys.stderr)
            if process:
                process.terminate()
            sys.exit(1)

    stuck = StuckDetector(window=args.stuck_window)
    history: list[dict] = []
    deadline = time.monotonic() + args.timeout
    result_ok = False
    result_reason = ""
    result_outcome = "unknown"
    final_progress: dict = {}

    try:
        for tick_num in range(args.max_ticks):
            if time.monotonic() > deadline:
                result_outcome = "timeout"
                result_reason = f"timeout after {args.timeout}s (tick {tick_num})"
                break

            state = client.get_state()
            a11y = client.a11y_tree()
            progress_snapshot = progress(state) if progress else {}
            if isinstance(progress_snapshot, dict):
                final_progress = progress_snapshot

            if isinstance(state, dict) and state.get("error"):
                result_outcome = "transport_error"
                result_reason = str(state.get("error"))
                history.append({"tick": tick_num, "state": state, "a11y": a11y,
                                "action": None, "progress": progress_snapshot,
                                "transport_error": True})
                break

            failure = classify_failure(state) if classify_failure else None
            if failure:
                result_outcome = str(failure)
                result_reason = f"terminal failure at tick {tick_num}: {failure}"
                history.append({"tick": tick_num, "state": state, "a11y": a11y,
                                "action": None, "progress": progress_snapshot,
                                "failed_terminal": failure})
                break

            if is_terminal(state):
                result_ok = True
                result_outcome = "complete"
                result_reason = f"terminal state reached at tick {tick_num}"
                history.append({"tick": tick_num, "state": state, "a11y": a11y,
                                "action": None, "progress": progress_snapshot,
                                "terminal": True})
                break

            if stuck.tick(state):
                result_reason = f"stuck: no state change in last {args.stuck_window} ticks (tick {tick_num})"
                result_outcome = "stuck"
                history.append({"tick": tick_num, "state": state, "a11y": a11y,
                                "action": None, "progress": progress_snapshot,
                                "stuck": True})
                break

            action = decide(state, a11y, history)
            action_record = {"kind": action.kind, "x": action.x, "y": action.y,
                             "label": action.label, "key": action.key,
                             "keys": action.keys, "action": action.action,
                             "duration": action.duration}

            if action.kind == "click":
                client.send({"command": "input", "type": "click", "x": action.x, "y": action.y})
            elif action.kind == "click_label":
                found = click_label(client, a11y, action.label)
                action_record["found"] = found
            elif action.kind == "key":
                client.send({"command": "input", "type": "key", "key": action.key,
                             "duration": action.duration})
            elif action.kind == "key_combo":
                client.send({"command": "input", "type": "key_combo", "keys": action.keys})
            elif action.kind == "hold":
                client.send({"command": "input", "type": "hold", "action": action.action,
                             "duration": action.duration})
            elif action.kind == "wait":
                time.sleep(action.seconds)
            # "noop" falls through -- no side effect this tick

            history.append({"tick": tick_num, "state": state, "a11y": a11y,
                            "progress": progress_snapshot, "action": action_record})
        else:
            result_reason = f"exhausted max_ticks ({args.max_ticks}) without reaching terminal state"
            result_outcome = "max_ticks"
    finally:
        write_trace(trace_path, history)
        if process is not None:
            try:
                process.terminate()
            except Exception:
                pass

    print()
    if result_ok:
        print(f"✓ PASSED: {result_reason}")
        print(f"  Trace: {trace_path}")
        print("PLAYTHROUGH_RESULT: " + json.dumps({
            "status": "success", "outcome": result_outcome,
            "reason": result_reason, "trace": str(trace_path),
            "progress": final_progress,
        }, sort_keys=True))
        sys.exit(0)
    else:
        print(f"✗ FAILED: {result_reason}")
        print(f"  Trace: {trace_path}")
        print("PLAYTHROUGH_RESULT: " + json.dumps({
            "status": "failure", "outcome": result_outcome,
            "reason": result_reason, "trace": str(trace_path),
            "progress": final_progress,
        }, sort_keys=True))
        sys.exit(1)
