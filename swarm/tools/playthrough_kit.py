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
`is_complete()`, plus an optional `MILESTONES` ladder.

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
the milestone-aware stuck detector reports.

## Milestone ladder (recommended for all games beyond single-screen arcade)

Instead of a single `is_complete(state) -> bool`, define an ordered list of
milestone predicates. The kit tracks which milestones have fired (in order),
stamps every trace tick and the final receipt with the high-water mark, and
uses the *last* milestone as the completion predicate. This gives:

  - A monotone progress signal the stuck detector can use (no milestone
    advanced in N game-seconds = stuck, regardless of visual churn)
  - Richer failure receipts: "reached level 5, failed at boss_engaged"
  - A natural fit for tower-defense waves, platformer levels, RPG chapters

Example (in project's tests/playthrough_bot.py):

    from swarm.tools.playthrough_kit import run_bot_cli, Action, Milestone

    MILESTONES = [
        Milestone("menu_passed",    lambda s: s.get("game_state", {}).get("scene") == "game"),
        Milestone("reached_wave_3", lambda s: s.get("game_state", {}).get("wave", 0) >= 3),
        Milestone("boss_killed",    lambda s: s.get("game_state", {}).get("boss_hp", 1) == 0),
        Milestone("victory",        lambda s: s.get("game_state", {}).get("status") == "victory"),
    ]

    def decide(state, a11y, history): ...

    if __name__ == "__main__":
        run_bot_cli(decide, milestones=MILESTONES, ...)

The last milestone in the list is the completion gate. A run that reaches
all milestones emits `outcome=complete`; a run that reaches some but not
all emits `outcome=<failure>` with `furthest_milestone` set.

## Time scale (for games longer than ~5 minutes)

Pass `--time-scale N` (default 1.0) to run the game at N× speed.
`Action.seconds` values are always in *game seconds*; the kit divides by
time_scale before the wall-clock sleep, so waits stay calibrated regardless
of speed. The game's StateServer receives `{"command":"set_time_scale",
"scale":N}` once after the ready handshake; StateServer applies it via
`Engine.time_scale`. The value is recorded in the receipt so a completion
at time_scale=8 is distinguishable from one at 1.0.

CLI usage from a project's own bot script:

    from swarm.tools.playthrough_kit import run_bot_cli, Action, Milestone

    def decide(state, a11y, history):
        # project-specific: look at a11y["a11y_tree"] labels and
        # state["game_state"] fields, return an Action
        ...

    def is_complete(state):
        # simple form (no milestones): what does full victory look like?
        return state.get("game_state", {}).get("status") == "victory"

    if __name__ == "__main__":
        # Simple form (single is_complete):
        run_bot_cli(decide, is_complete=is_complete)

        # Milestone form (recommended):
        run_bot_cli(decide, milestones=MILESTONES)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

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
    seconds: float = 0.1   # game-seconds; divided by time_scale before sleep
    key: str = ""
    keys: list[str] = field(default_factory=list)
    action: str = ""
    duration: float = 0.1


# ---------------------------------------------------------------------------
# Milestone ladder
# ---------------------------------------------------------------------------

@dataclass
class Milestone:
    """One step in a project's ordered completion ladder.

    `name`      -- short identifier for receipts and traces ("reached_wave_3")
    `predicate` -- callable(state) -> bool; must stay True once it fires
                   (predicates are checked in order; once milestone N fires,
                   milestone N+1 is checked next tick)
    """
    name: str
    predicate: Callable[[dict], bool]


class MilestoneTracker:
    """Tracks progress through an ordered Milestone list.

    Milestones fire in order: once milestone N's predicate returns True, the
    tracker advances to N+1 and never re-checks N. This tolerates games where
    the predicate becomes transiently False after firing (e.g. a "boss_dead"
    flag that gets cleared on scene change) -- the high-water mark is sticky.

    The final milestone in the list is the completion gate.
    """

    def __init__(self, milestones: list[Milestone]):
        if not milestones:
            raise ValueError("Milestone list must not be empty")
        self.milestones = milestones
        self._next_idx = 0
        self._fired: list[dict] = []   # {name, tick, game_time}

    # ---- properties ----

    @property
    def complete(self) -> bool:
        return self._next_idx >= len(self.milestones)

    @property
    def furthest(self) -> Optional[str]:
        return self._fired[-1]["name"] if self._fired else None

    @property
    def fired_names(self) -> list[str]:
        return [e["name"] for e in self._fired]

    @property
    def pending_name(self) -> Optional[str]:
        if self.complete:
            return None
        return self.milestones[self._next_idx].name

    def timeline(self) -> list[dict]:
        return list(self._fired)

    # ---- update ----

    def tick(self, state: dict, tick_num: int, game_time: float) -> bool:
        """Check the next pending milestone; return True if it just fired."""
        if self.complete:
            return False
        ms = self.milestones[self._next_idx]
        try:
            fired = bool(ms.predicate(state))
        except Exception:
            fired = False
        if fired:
            self._fired.append({"name": ms.name, "tick": tick_num, "game_time": round(game_time, 2)})
            self._next_idx += 1
            return True
        return False


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


def drag_at(client: StateServerClient, x1: float, y1: float, x2: float, y2: float, duration: float = 0.3) -> dict:
    """Inject a click-drag from (x1,y1) to (x2,y2) over `duration` seconds.

    Useful for swipe gestures, letter-grid word tracing, sliders, and
    drag-and-drop. Goes through Godot's Input.parse_input_event pipeline."""
    return client.send({"command": "input", "type": "drag",
                        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                        "duration": duration})


def drag_path(client: StateServerClient, points: list, duration_per_step: float = 0.1) -> None:
    """Drag through an ordered list of (x, y) points — useful for tracing
    a word path across a letter grid. Each consecutive pair is one drag."""
    for i in range(len(points) - 1):
        x1, y1 = points[i]
        x2, y2 = points[i + 1]
        drag_at(client, x1, y1, x2, y2, duration_per_step)


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
# Stuck detection -- milestone-aware, falls back to fingerprint mode
# ---------------------------------------------------------------------------

class StuckDetector:
    """Reports True once no meaningful progress has been made for a window.

    When a MilestoneTracker is supplied, "progress" means a new milestone
    fired within the last `window_seconds` of *game time* -- this is robust
    to visual churn (animated HUDs, particle counts, ticking clocks) that
    would fool fingerprint-based detectors.

    Without a tracker (simple is_complete mode), falls back to comparing
    state fingerprints over a rolling tick window, as before. Note: the
    fingerprint approach breaks on any game with a continuously-changing
    field in game_state (timers, particle counts, scroll offsets). For those
    games, supply a MilestoneTracker even if only one milestone is defined.
    """

    def __init__(self, window: int = 30, tracker: Optional[MilestoneTracker] = None,
                 milestone_window_seconds: float = 60.0):
        self.window = window
        self._tracker = tracker
        self._milestone_window_s = milestone_window_seconds
        self._history: list[str] = []          # fingerprint mode
        self._last_milestone_time: float = -1.0  # -1 = not yet initialised

    @staticmethod
    def _fingerprint(state: dict) -> str:
        comparable = {k: v for k, v in state.items() if k != "timestamp"}
        try:
            return json.dumps(comparable, sort_keys=True, default=str)
        except Exception:
            return str(comparable)

    def reset_milestone_timer(self, game_time: float) -> None:
        """Call whenever a milestone fires to reset the stuck window."""
        self._last_milestone_time = max(0.0, game_time)

    def tick(self, state: dict, game_time: float = 0.0) -> bool:
        """Record a new state snapshot; return True if stuck."""
        if self._tracker is not None:
            # Milestone mode: stuck if no milestone fired in the window.
            # Initialise on first real tick so the window starts from game start.
            if self._last_milestone_time < 0:
                self._last_milestone_time = game_time
            elapsed = game_time - self._last_milestone_time
            return elapsed >= self._milestone_window_s
        else:
            # Fingerprint mode (legacy / single-milestone)
            self._history.append(self._fingerprint(state))
            if len(self._history) > self.window:
                self._history.pop(0)
            if len(self._history) < self.window:
                return False
            return len(set(self._history)) == 1


# ---------------------------------------------------------------------------
# Failure context -- compact death-scene dump for fast post-mortem
# ---------------------------------------------------------------------------

def _write_failure_context(
    out_dir: Path,
    history: list[dict],
    result_outcome: str,
    result_reason: str,
    milestone_timeline: list[dict],
    furthest_milestone: Optional[str],
    pending_milestone: Optional[str],
    client: Optional[StateServerClient],
    tail: int = 50,
) -> Path:
    """Write failure_context.json: last N ticks + milestone timeline + screenshot."""
    ctx: dict = {
        "outcome": result_outcome,
        "reason": result_reason,
        "furthest_milestone": furthest_milestone,
        "pending_milestone": pending_milestone,
        "milestone_timeline": milestone_timeline,
        "last_ticks": history[-tail:] if len(history) > tail else history,
    }

    # One terminal screenshot (outside the hot loop -- does not violate zero-vision-in-loop)
    if client is not None:
        try:
            resp = client.send({"command": "screenshot_b64"})
            b64 = resp.get("image_base64") if isinstance(resp, dict) else None
            if b64:
                screenshot_path = out_dir / "failure_screenshot.png"
                screenshot_path.write_bytes(base64.b64decode(b64))
                ctx["screenshot"] = str(screenshot_path)
        except Exception as e:
            ctx["screenshot_error"] = str(e)

    ctx_path = out_dir / "failure_context.json"
    ctx_path.write_text(json.dumps(ctx, default=str, indent=2))
    return ctx_path


# ---------------------------------------------------------------------------
# Trace output
# ---------------------------------------------------------------------------

def write_trace(path: Path, ticks: list[dict]) -> None:
    # Rotate previous trace so agents can compare runs
    if path.exists():
        prev = path.with_suffix(".jsonl.prev")
        path.rename(prev)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for tick in ticks:
            f.write(json.dumps(tick, default=str) + "\n")


# ---------------------------------------------------------------------------
# Game process launch
# ---------------------------------------------------------------------------

def _launch(project_path: str, port: int) -> tuple[StateServerClient, Optional[subprocess.Popen]]:
    """Launch the game and return a connected client + process handle."""
    try:
        swarm_root = Path(__file__).parent.parent.parent
        sys.path.insert(0, str(swarm_root))
        from swarm.qa_tools import launch_game
        from swarm import qa_tools as _qt
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
# Screen recorder (optional, --record flag)
# ---------------------------------------------------------------------------

def _get_godot_window_id() -> Optional[int]:
    """Return the CGWindowID of the frontmost Godot window on macOS, or None."""
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get unix id of first process '
             'whose name contains "godot" or name contains "Godot"'],
            capture_output=True, text=True, timeout=3,
        )
        pid = result.stdout.strip()
        if not pid:
            return None
        # Use CGWindowListCopyWindowInfo via python to find window by pid
        cg_script = (
            f"import Quartz, sys\n"
            f"wins = Quartz.CGWindowListCopyWindowInfo("
            f"Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)\n"
            f"for w in wins:\n"
            f"    if w.get('kCGWindowOwnerPID') == {pid}:\n"
            f"        wid = w.get('kCGWindowNumber')\n"
            f"        if wid: print(wid); sys.exit(0)\n"
        )
        r2 = subprocess.run(["python3", "-c", cg_script],
                            capture_output=True, text=True, timeout=3)
        wid = r2.stdout.strip()
        return int(wid) if wid else None
    except Exception:
        return None


class ScreenRecorder:
    """Captures gameplay frames and stitches them into an MP4 via ffmpeg.

    Two capture modes:
    - StateServer mode (default): requests screenshot_b64 from the game's
      TCP state server. Works when Godot renders to an off-screen buffer.
    - Window capture mode (window_id is set): uses macOS `screencapture -l`
      to grab the actual Godot window. Required when Godot is launched
      without --headless (dummy renderer can't export viewport texture).

    Frames land in a temp directory and are deleted after muxing.
    """

    def __init__(self, client: "StateServerClient", fps: float, out_path: Path,
                 window_id: Optional[int] = None):
        self._client = client
        self._fps = fps
        self._out_path = out_path
        self._window_id = window_id
        self._frame_dir = Path(tempfile.mkdtemp(prefix="playthrough_frames_"))
        self._frame_idx = 0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[str] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def _grab_frame(self) -> bool:
        """Capture one frame. Returns True if a frame was saved."""
        frame_path = self._frame_dir / f"frame_{self._frame_idx:06d}.png"
        if self._window_id is not None:
            result = subprocess.run(
                ["screencapture", "-x", "-l", str(self._window_id), str(frame_path)],
                capture_output=True, timeout=2,
            )
            if result.returncode == 0 and frame_path.exists() and frame_path.stat().st_size > 0:
                return True
            return False
        else:
            resp = self._client.send({"command": "screenshot_b64"})
            b64 = resp.get("image_base64", "")
            if b64:
                frame_path.write_bytes(base64.b64decode(b64))
                return True
            return False

    def _capture_loop(self) -> None:
        interval = 1.0 / self._fps
        while not self._stop_event.is_set():
            t0 = time.monotonic()
            try:
                if self._grab_frame():
                    self._frame_idx += 1
            except Exception as exc:
                self._error = str(exc)
            elapsed = time.monotonic() - t0
            remaining = interval - elapsed
            if remaining > 0:
                self._stop_event.wait(timeout=remaining)

    def stop(self) -> Optional[Path]:
        """Stop capture, mux frames into MP4, clean up. Returns output path or None."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

        if self._frame_idx == 0:
            print("[recorder] No frames captured — skipping mux", flush=True)
            shutil.rmtree(self._frame_dir, ignore_errors=True)
            return None

        if not shutil.which("ffmpeg"):
            print(f"[recorder] ffmpeg not found — frames left in {self._frame_dir}", flush=True)
            return None

        self._out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(self._fps),
            "-i", str(self._frame_dir / "frame_%06d.png"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "fast",
            "-crf", "23",
            str(self._out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[recorder] ffmpeg error:\n{result.stderr[-500:]}", flush=True)
            print(f"[recorder] frames left in {self._frame_dir}", flush=True)
            return None

        shutil.rmtree(self._frame_dir, ignore_errors=True)
        size_mb = self._out_path.stat().st_size / (1024 * 1024)
        print(f"[recorder] saved {self._out_path} ({size_mb:.1f} MB, {self._frame_idx} frames)", flush=True)
        return self._out_path


# ---------------------------------------------------------------------------
# CLI harness
# ---------------------------------------------------------------------------

def run_bot_cli(
    decide: Callable[[dict, dict, list], Action],
    is_complete: Optional[Callable[[dict], bool]] = None,
    default_out_dir: str = "/tmp/playthrough_bot",
    classify_failure: Optional[Callable[[dict], Optional[str]]] = None,
    progress: Optional[Callable[[dict], dict]] = None,
    milestones: Optional[List[Milestone]] = None,
    # Legacy alias kept for backward compat with bots written before milestones
    is_terminal: Optional[Callable[[dict], bool]] = None,
) -> None:
    """Parse standard CLI args, run the poll loop, exit 0 on completion or 1 on
    stuck/timeout/failure. Writes a trace file and, on failure, a compact
    failure_context.json with the last 50 ticks + a terminal screenshot.

    Exactly one of `milestones` or `is_complete` (or legacy `is_terminal`)
    must be supplied. Milestone form is recommended for any game with more
    than one meaningful progress step.

    `decide(state, a11y, history)` -- project-specific action selection.
    `is_complete(state)` -- simple single-predicate completion check.
    `milestones` -- ordered list of Milestone objects; last one = completion.
    `classify_failure(state)` -- optional terminal failure classifier.
    `progress(state)` -- optional structured progress evidence for receipt.
    """
    # Resolve completion gate
    if milestones:
        tracker = MilestoneTracker(milestones)
        _is_complete_fn: Callable[[dict], bool] = lambda s: tracker.complete
    elif is_complete is not None:
        tracker = None
        _is_complete_fn = is_complete
    elif is_terminal is not None:
        # legacy alias
        tracker = None
        _is_complete_fn = is_terminal
    else:
        print("[playthrough_kit] ERROR: supply milestones= or is_complete=", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="playthrough_kit: deterministic completion bot")
    parser.add_argument("--project-path", help="Path to Godot project (required unless --no-launch)")
    parser.add_argument("--port", type=int, default=11009, help="StateServer port (default 11009)")
    parser.add_argument("--max-ticks", type=int, default=2000,
                        help="Safety fuse: max ticks before aborting (not the primary governor)")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="Wall-clock seconds before giving up")
    parser.add_argument("--stuck-window", type=int, default=30,
                        help="Fingerprint-mode: ticks with no state change before declaring stuck")
    parser.add_argument("--milestone-stuck-seconds", type=float, default=60.0,
                        help="Milestone-mode: game-seconds with no milestone advance before stuck")
    parser.add_argument("--time-scale", type=float, default=1.0,
                        help="Engine.time_scale speedup (1.0=realtime, 4.0=4x faster). "
                             "Verify your game is physics-stable at chosen speed before using.")
    parser.add_argument("--out-dir", default=default_out_dir, help="Output directory for trace/context files")
    parser.add_argument("--no-launch", action="store_true", help="Skip launching the game")
    parser.add_argument("--record", action="store_true",
                        help="Record gameplay to an MP4 file (requires ffmpeg on PATH)")
    parser.add_argument("--record-fps", type=float, default=10.0,
                        help="Screenshot capture rate for recording (default 10). "
                             "Higher = smoother video but more disk I/O. 10fps is fine for demos.")
    parser.add_argument("--record-out", default="",
                        help="Output MP4 path. Defaults to <out-dir>/playthrough_<timestamp>.mp4")
    parser.add_argument("--record-windowed", action="store_true",
                        help="Capture the Godot window via screencapture instead of the StateServer "
                             "viewport. Use when Godot is launched without --headless.")
    args = parser.parse_args()

    time_scale = max(0.1, args.time_scale)  # sanity floor

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / "playthrough_trace.jsonl"

    process = None
    client: Optional[StateServerClient] = None
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

    # Apply time_scale once after ready (skip menu phase at high speed is fine;
    # the game is already on the menu at this point, not mid-physics)
    if time_scale != 1.0:
        try:
            client.send({"command": "set_time_scale", "scale": time_scale})
            print(f"[playthrough_kit] time_scale set to {time_scale}×")
        except Exception as e:
            print(f"[playthrough_kit] WARNING: set_time_scale failed ({e}); running at 1.0×")
            time_scale = 1.0

    # Optional screen recorder
    recorder: Optional[ScreenRecorder] = None
    video_path: Optional[Path] = None
    if args.record:
        ts = time.strftime("%Y%m%d_%H%M%S")
        if args.record_out:
            rec_out = Path(args.record_out)
        else:
            rec_out = Path(args.out_dir) / f"playthrough_{ts}.mp4"
        window_id: Optional[int] = None
        if getattr(args, "record_windowed", False):
            window_id = _get_godot_window_id()
            if window_id is None:
                print("[recorder] WARNING: could not find Godot window — falling back to StateServer capture", flush=True)
            else:
                print(f"[recorder] window capture mode (CGWindowID={window_id})", flush=True)
        recorder = ScreenRecorder(client=client, fps=args.record_fps, out_path=rec_out,
                                  window_id=window_id)
        recorder.start()
        print(f"[recorder] capturing at {args.record_fps}fps → {rec_out}", flush=True)

    stuck = StuckDetector(
        window=args.stuck_window,
        tracker=tracker,
        milestone_window_seconds=args.milestone_stuck_seconds,
    )
    history: list[dict] = []
    deadline = time.monotonic() + args.timeout
    game_time = 0.0          # accumulated game-seconds (wall / time_scale)
    wall_start = time.monotonic()
    result_ok = False
    result_reason = ""
    result_outcome = "unknown"
    final_progress: dict = {}

    try:
        for tick_num in range(args.max_ticks):
            wall_now = time.monotonic()
            game_time = (wall_now - wall_start) * time_scale

            if wall_now > deadline:
                result_outcome = "timeout"
                result_reason = f"timeout after {args.timeout}s wall-clock (tick {tick_num}, game-time {game_time:.1f}s)"
                break

            state = client.get_state()
            a11y = client.a11y_tree()
            progress_snapshot = progress(state) if progress else {}
            if isinstance(progress_snapshot, dict):
                final_progress = progress_snapshot

            if isinstance(state, dict) and state.get("error"):
                result_outcome = "transport_error"
                result_reason = str(state.get("error"))
                history.append({"tick": tick_num, "game_time": round(game_time, 2),
                                "state": state, "a11y": a11y, "action": None,
                                "progress": progress_snapshot, "transport_error": True})
                break

            # Check milestone advancement before failure/completion so we record
            # partial progress even on a run that's about to fail.
            if tracker is not None:
                if tracker.tick(state, tick_num, game_time):
                    stuck.reset_milestone_timer(game_time)
                    print(f"[playthrough_kit] ✓ milestone: {tracker.furthest} "
                          f"(tick {tick_num}, game-time {game_time:.1f}s)")

            failure = classify_failure(state) if classify_failure else None
            if failure:
                result_outcome = str(failure)
                result_reason = f"terminal failure at tick {tick_num}: {failure}"
                history.append({"tick": tick_num, "game_time": round(game_time, 2),
                                "state": state, "a11y": a11y, "action": None,
                                "progress": progress_snapshot, "failed_terminal": failure})
                break

            if _is_complete_fn(state):
                result_ok = True
                result_outcome = "complete"
                result_reason = f"completed at tick {tick_num} (game-time {game_time:.1f}s)"
                history.append({"tick": tick_num, "game_time": round(game_time, 2),
                                "state": state, "a11y": a11y, "action": None,
                                "progress": progress_snapshot, "terminal": True})
                break

            if stuck.tick(state, game_time):
                if tracker is not None:
                    result_reason = (
                        f"stuck: no milestone advanced in last {args.milestone_stuck_seconds}s "
                        f"game-time (tick {tick_num}, furthest={tracker.furthest}, "
                        f"pending={tracker.pending_name})"
                    )
                else:
                    result_reason = f"stuck: no state change in last {args.stuck_window} ticks (tick {tick_num})"
                result_outcome = "stuck"
                history.append({"tick": tick_num, "game_time": round(game_time, 2),
                                "state": state, "a11y": a11y, "action": None,
                                "progress": progress_snapshot, "stuck": True})
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
                # action.seconds is in game-seconds; divide by time_scale for wall-clock sleep
                time.sleep(action.seconds / time_scale)
            # "noop" falls through -- no side effect this tick

            history.append({"tick": tick_num, "game_time": round(game_time, 2),
                            "progress": progress_snapshot, "action": action_record,
                            "milestone_hwm": tracker.furthest if tracker else None})
        else:
            result_reason = f"exhausted max_ticks ({args.max_ticks}) safety fuse without completing"
            result_outcome = "max_ticks"

    finally:
        write_trace(trace_path, history)
        if recorder is not None:
            video_path = recorder.stop()

        if not result_ok:
            _write_failure_context(
                out_dir=out_dir,
                history=history,
                result_outcome=result_outcome,
                result_reason=result_reason,
                milestone_timeline=tracker.timeline() if tracker else [],
                furthest_milestone=tracker.furthest if tracker else None,
                pending_milestone=tracker.pending_name if tracker else None,
                client=client,
            )
        if process is not None:
            try:
                process.terminate()
            except Exception:
                pass

    # Build receipt
    milestone_summary: dict = {}
    if tracker is not None:
        milestone_summary = {
            "furthest_milestone": tracker.furthest,
            "milestones_fired": tracker.fired_names,
            "milestone_timeline": tracker.timeline(),
            "pending_milestone": tracker.pending_name,
        }

    receipt = {
        "status": "success" if result_ok else "failure",
        "outcome": result_outcome,
        "reason": result_reason,
        "trace": str(trace_path),
        "time_scale": time_scale,
        "progress": final_progress,
        **milestone_summary,
    }
    if video_path is not None:
        receipt["video"] = str(video_path)

    print()
    if result_ok:
        print(f"✓ PASSED: {result_reason}")
        if tracker:
            print(f"  Milestones: {' → '.join(tracker.fired_names)}")
        print(f"  Trace: {trace_path}")
        if video_path:
            print(f"  Video: {video_path}")
        print("PLAYTHROUGH_RESULT: " + json.dumps(receipt, sort_keys=True))
        sys.exit(0)
    else:
        print(f"✗ FAILED: {result_reason}")
        if tracker and tracker.furthest:
            print(f"  Furthest milestone: {tracker.furthest} (pending: {tracker.pending_name})")
        print(f"  Trace: {trace_path}")
        print(f"  Failure context: {out_dir / 'failure_context.json'}")
        if video_path:
            print(f"  Video: {video_path}")
        print("PLAYTHROUGH_RESULT: " + json.dumps(receipt, sort_keys=True))
        sys.exit(1)
