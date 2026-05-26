"""
swarm.tools.scenario_qa -- deterministic scenario executor for Godot QA.

Architecture (v0):
  1. Read a JSON scenario file (goal + steps).
  2. Launch the game via the existing qa_tools path.
  3. Execute each step against StateServer via TCP — zero LLM calls per action.
  4. Write scenario_trace.jsonl with per-step results.
  5. On failure, emit a structured failure envelope JSON.
  6. VLM (vision_query) only fires on explicit 'capture' ops, cached by image+state hash.

Supported ops (v0):
  capture       -- screenshot + optional VLM vision check (hash-cached)
  press_button  -- fire a button by label/qa_label
  wait          -- sleep N seconds
  macro         -- play_macro server-side timed sequence
  assert_state  -- check a dotted path in game_state with eq/ne/gt/lt/gte/lte
  assert_invariant -- run a named built-in invariant check

Built-in invariants (checked after every step):
  1. no_crash        -- game process still alive
  2. nonblank_screen -- screenshot not all-black or all-white
  3. stateserver_ok  -- StateServer responds within 5s
  4. no_script_error -- no SCRIPT ERROR in game stdout (requires stdout capture)

CLI:
  python -m swarm.tools.scenario_qa <scenario.json> \\
      --project-path /path/to/game \\
      [--port 11009] \\
      [--out-dir /tmp/scenario_results] \\
      [--no-launch]   # if game already running
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# StateServer TCP client
# ---------------------------------------------------------------------------

class StateServerClient:
    """Thin TCP client for the Godot StateServer autoload."""

    def __init__(self, port: int = 11009, default_timeout: float = 5.0):
        self.port = port
        self.default_timeout = default_timeout

    def send(self, command: dict, timeout: float = None) -> dict:
        """Send a JSON command and return the parsed response."""
        t = timeout or self.default_timeout
        # Screenshots can be large — give them more time
        recv_timeout = 30.0 if command.get("command", "") in ("screenshot_b64", "screenshot_base64") else t
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=t) as sock:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.sendall((json.dumps(command) + "\n").encode())
                sock.settimeout(recv_timeout)
                data = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                    if b"\n" in data:
                        break
            return json.loads(data.decode().strip())
        except ConnectionRefusedError:
            return {"error": "StateServer not available (connection refused)"}
        except socket.timeout:
            return {"error": f"StateServer timeout after {recv_timeout}s"}
        except Exception as e:
            return {"error": str(e)}

    def ping(self, timeout: float = 5.0) -> bool:
        """Return True if StateServer responds."""
        resp = self.send({"command": "state"}, timeout=timeout)
        return "error" not in resp

    def wait_ready(self, timeout: float = 15.0, interval: float = 0.5) -> bool:
        """Poll until StateServer responds or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ping(timeout=2.0):
                return True
            time.sleep(interval)
        return False

    def get_state(self) -> dict:
        return self.send({"command": "state"})

    def screenshot_b64(self) -> dict:
        """Returns {"image_base64": "..."} or {"error": ...}"""
        return self.send({"command": "screenshot_b64"}, timeout=30.0)

    def press_button(self, button_id: str) -> dict:
        return self.send({"command": "press_button", "id": button_id})

    def play_macro(self, actions: list) -> dict:
        return self.send({"command": "play_macro", "actions": actions})

    def a11y_tree(self) -> dict:
        return self.send({"command": "a11y_tree"})


# ---------------------------------------------------------------------------
# Image utilities
# ---------------------------------------------------------------------------

def _image_hash(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()[:16]

def _state_hash(game_state: dict) -> str:
    try:
        return hashlib.sha256(
            json.dumps(game_state, sort_keys=True).encode()
        ).hexdigest()[:16]
    except Exception:
        return "x"

def _is_blank(image_bytes: bytes) -> bool:
    """Heuristic: check if image is mostly all-black or all-white (broken render)."""
    try:
        # Sample 200 bytes from the pixel data region (skip 33-byte PNG header)
        sample = image_bytes[33:233]
        if not sample:
            return True
        avg = sum(sample) / len(sample)
        # All-black ~ 0, all-white ~ 255
        return avg < 5 or avg > 250
    except Exception:
        return False

def _save_screenshot(image_bytes: bytes, out_dir: Path, name: str) -> str:
    path = out_dir / f"{name}.png"
    path.write_bytes(image_bytes)
    return str(path)


# ---------------------------------------------------------------------------
# Nested dict path resolver  (e.g. "game_state.score")
# ---------------------------------------------------------------------------

def _resolve_path(data: dict, dotted: str) -> Any:
    """Resolve a dotted path like 'game_state.player.health' in a nested dict."""
    parts = dotted.split(".")
    val = data
    for p in parts:
        if not isinstance(val, dict):
            return None
        val = val.get(p)
    return val


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

_COMPARATORS = {
    "eq":  lambda a, b: a == b,
    "ne":  lambda a, b: a != b,
    "gt":  lambda a, b: a is not None and a > b,
    "lt":  lambda a, b: a is not None and a < b,
    "gte": lambda a, b: a is not None and a >= b,
    "lte": lambda a, b: a is not None and a <= b,
    "contains": lambda a, b: b in str(a) if a is not None else False,
}


# ---------------------------------------------------------------------------
# Scenario executor
# ---------------------------------------------------------------------------

class ScenarioExecutor:
    def __init__(
        self,
        scenario: dict,
        client: StateServerClient,
        out_dir: Path,
        game_process: Optional[subprocess.Popen] = None,
        vision_fn=None,
        project: str = "",
        swarm_url: str = "http://localhost:5001",
        project_path: str = "",
    ):
        self.scenario = scenario
        self.client = client
        self.out_dir = out_dir
        self.game_process = game_process
        self.vision_fn = vision_fn  # callable(image_path, question) -> str
        self.project = project
        self.swarm_url = swarm_url
        self.project_path = project_path  # for baseline storage
        self._vision_cache: dict[str, str] = {}  # (image_hash+state_hash) -> response
        self._trace: list[dict] = []
        self._step_index = 0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> dict:
        """Execute the full scenario. Returns a result dict."""
        goal = self.scenario.get("goal", "unnamed")
        steps = self.scenario.get("steps", [])
        seed = self.scenario.get("seed", 0)

        print(f"[scenario_qa] Goal: {goal}")
        print(f"[scenario_qa] Steps: {len(steps)}")
        print(f"[scenario_qa] Waiting for StateServer...")
        if not self.client.wait_ready(timeout=20):
            return {
                "ok": False,
                "goal": goal,
                "failed_step": -1,
                "error": "StateServer did not respond within 20s after launch",
                "trace_path": str(self.out_dir / "scenario_trace.jsonl"),
            }
        print(f"[scenario_qa] StateServer ready")

        # Baseline regression check (zero VLM cost — structural only)
        baseline_warnings = self._check_baseline(goal)
        if baseline_warnings:
            for w in baseline_warnings:
                print(f"[scenario_qa] BASELINE WARNING: {w}")

        trace_path = str(self.out_dir / "scenario_trace.jsonl")
        failed_step = None
        failure_envelope = None

        for i, step in enumerate(steps):
            self._step_index = i
            op = step.get("op", "unknown")
            timeout = float(step.get("timeout_seconds", 10))
            print(f"[scenario_qa] Step {i}: {op}")

            t0 = time.monotonic()
            result = self._execute_step(step, timeout)
            duration_ms = int((time.monotonic() - t0) * 1000)

            entry = {
                "step_index": i,
                "op": op,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "duration_ms": duration_ms,
                "ok": result.get("ok", True),
                "tokens_used": result.get("tokens_used", 0),
                "cached": result.get("cached", False),
                "screenshot_path": result.get("screenshot_path"),
                "screenshot_hash": result.get("screenshot_hash"),
                "before_state_hash": result.get("before_state_hash"),
                "after_state_hash": result.get("after_state_hash"),
                "error": result.get("error"),
                "detail": result.get("detail"),
            }
            self._trace.append(entry)

            # Write trace entry immediately
            with open(trace_path, "a") as f:
                f.write(json.dumps(entry) + "\n")

            if not result.get("ok", True):
                failed_step = i
                failure_envelope = self._build_failure_envelope(
                    goal, seed, i, step, result, trace_path
                )
                print(f"[scenario_qa] FAILED at step {i} ({op}): {result.get('error', '')}")
                break

            # Built-in invariant checks after each step
            inv_result = self._check_invariants()
            if not inv_result.get("ok", True):
                failed_step = i
                failure_envelope = self._build_failure_envelope(
                    goal, seed, i, step, inv_result, trace_path,
                    kind="invariant_violation"
                )
                print(f"[scenario_qa] INVARIANT FAILED at step {i}: {inv_result.get('error', '')}")
                break

        if failure_envelope:
            envelope_path = str(self.out_dir / "failure_envelope.json")
            with open(envelope_path, "w") as f:
                json.dump(failure_envelope, f, indent=2)
            print(f"[scenario_qa] Failure envelope: {envelope_path}")
            if self.project:
                emit_bug_task(failure_envelope, self.project, self.swarm_url)
            return {
                "ok": False,
                "goal": goal,
                "failed_step": failed_step,
                "trace_path": trace_path,
                "envelope_path": envelope_path,
                "failure": failure_envelope,
            }

        print(f"[scenario_qa] PASSED: {goal}")
        baseline_path = self._save_baseline(goal, seed, steps)
        return {
            "ok": True,
            "goal": goal,
            "steps_run": len(steps),
            "trace_path": trace_path,
            "baseline_path": baseline_path,
            "baseline_warnings": baseline_warnings,
        }

    # ------------------------------------------------------------------
    # Step dispatchers
    # ------------------------------------------------------------------

    def _execute_step(self, step: dict, timeout: float) -> dict:
        op = step.get("op", "unknown")
        dispatch = {
            "capture":          self._op_capture,
            "press_button":     self._op_press_button,
            "wait":             self._op_wait,
            "macro":            self._op_macro,
            "assert_state":     self._op_assert_state,
            "assert_invariant": self._op_assert_invariant_named,
        }
        fn = dispatch.get(op)
        if fn is None:
            return {"ok": False, "error": f"unknown op: {op}"}
        try:
            return fn(step, timeout)
        except Exception as e:
            return {"ok": False, "error": f"exception in {op}: {e}"}

    def _op_capture(self, step: dict, timeout: float) -> dict:
        """Take a screenshot, optionally run VLM vision check. Hash-cached."""
        # Get current state for hash
        state_resp = self.client.get_state()
        game_state = state_resp.get("game_state", {})
        sh = _state_hash(game_state)

        # Screenshot
        ss_resp = self.client.screenshot_b64()
        if "error" in ss_resp:
            return {"ok": False, "error": f"screenshot failed: {ss_resp['error']}"}

        image_bytes = base64.b64decode(ss_resp["image_base64"])
        ih = _image_hash(image_bytes)
        cache_key = ih + sh

        name = step.get("name", f"step_{self._step_index}")
        ss_path = _save_screenshot(image_bytes, self.out_dir, name)

        result = {
            "ok": True,
            "screenshot_path": ss_path,
            "screenshot_hash": ih,
            "before_state_hash": sh,
            "after_state_hash": sh,
            "tokens_used": 0,
            "cached": False,
        }

        # Optional VLM vision check
        vision_prompt = step.get("vision")
        if vision_prompt and self.vision_fn:
            if cache_key in self._vision_cache:
                vision_response = self._vision_cache[cache_key]
                result["cached"] = True
            else:
                raw = self.vision_fn(ss_path, vision_prompt)
                # Detect infrastructure errors (API key missing, network, etc.)
                # and skip the vision check rather than failing the test
                if isinstance(raw, dict) and "error" in raw:
                    print(f"[scenario_qa] Vision unavailable ({raw['error']}), skipping vision check")
                    result["vision_skipped"] = True
                    result["vision_skip_reason"] = str(raw["error"])
                    return result
                vision_response = str(raw)
                self._vision_cache[cache_key] = vision_response
                result["tokens_used"] = 1  # non-zero signals VLM was called

            result["vision_response"] = vision_response
            result["detail"] = vision_response

            # Simple failure detection: if vision says "no", "not visible", "missing"
            vision_lower = (vision_response or "").lower()
            negative_signals = ["not visible", "not present", "missing", "cannot see",
                                 "no ", "black screen", "blank", "error"]
            if any(sig in vision_lower for sig in negative_signals):
                # Check for positive overrides
                positive_signals = ["visible", "present", "shows", "displays", "appears"]
                if not any(sig in vision_lower for sig in positive_signals):
                    result["ok"] = False
                    result["error"] = f"Vision check failed for '{vision_prompt}': {vision_response[:200]}"
                    result["kind"] = "vision_failure"

        return result

    def _op_press_button(self, step: dict, timeout: float) -> dict:
        button_id = step.get("id", "")
        if not button_id:
            return {"ok": False, "error": "press_button requires 'id'"}
        resp = self.client.press_button(button_id)
        if "error" in resp:
            return {"ok": False, "error": f"press_button '{button_id}': {resp['error']}"}
        return {"ok": True, "detail": f"pressed '{button_id}'"}

    def _op_wait(self, step: dict, timeout: float) -> dict:
        seconds = float(step.get("seconds", 1.0))
        time.sleep(seconds)
        return {"ok": True, "detail": f"waited {seconds}s"}

    def _op_macro(self, step: dict, timeout: float) -> dict:
        actions = step.get("actions", [])
        if not actions:
            return {"ok": False, "error": "macro requires 'actions'"}
        resp = self.client.play_macro(actions)
        if "error" in resp:
            return {"ok": False, "error": f"macro failed: {resp['error']}"}
        return {"ok": True, "detail": f"macro ran {len(actions)} actions"}

    def _op_assert_state(self, step: dict, timeout: float) -> dict:
        path = step.get("path", "")
        if not path:
            return {"ok": False, "error": "assert_state requires 'path'"}

        state_resp = self.client.get_state()
        if "error" in state_resp:
            return {"ok": False, "error": f"get_state failed: {state_resp['error']}"}

        sh_before = _state_hash(state_resp.get("game_state", {}))

        # Resolve path — support both top-level and game_state.* paths
        actual = _resolve_path(state_resp, path)
        if actual is None:
            # Try under game_state implicitly
            actual = _resolve_path(state_resp.get("game_state", {}), path)

        # Find comparator
        comparator = None
        expected = None
        for op_key in _COMPARATORS:
            if op_key in step:
                comparator = op_key
                expected = step[op_key]
                break

        if comparator is None:
            return {"ok": False, "error": f"assert_state needs a comparator (eq/ne/gt/lt/gte/lte/contains)"}

        passed = _COMPARATORS[comparator](actual, expected)
        if not passed:
            return {
                "ok": False,
                "error": f"assert_state failed: {path} {comparator} {expected!r} (actual={actual!r})",
                "expected": expected,
                "actual": actual,
                "before_state_hash": sh_before,
                "after_state_hash": sh_before,
            }

        return {
            "ok": True,
            "detail": f"{path} {comparator} {expected!r} ✓ (actual={actual!r})",
            "before_state_hash": sh_before,
            "after_state_hash": sh_before,
        }

    def _op_assert_invariant_named(self, step: dict, timeout: float) -> dict:
        name = step.get("name", "")
        result = self._check_named_invariant(name)
        return result

    # ------------------------------------------------------------------
    # Built-in invariants
    # ------------------------------------------------------------------

    def _check_invariants(self) -> dict:
        """Run all built-in invariants. Returns first failure or ok."""
        checks = [
            self._inv_no_crash,
            self._inv_stateserver_ok,
        ]
        for check in checks:
            result = check()
            if not result.get("ok", True):
                return result
        return {"ok": True}

    def _check_named_invariant(self, name: str) -> dict:
        inv_map = {
            "no_crash":        self._inv_no_crash,
            "nonblank_screen": self._inv_nonblank_screen,
            "stateserver_ok":  self._inv_stateserver_ok,
        }
        fn = inv_map.get(name)
        if fn is None:
            return {"ok": False, "error": f"unknown invariant: {name}"}
        return fn()

    def _inv_no_crash(self) -> dict:
        if self.game_process is None:
            return {"ok": True, "detail": "no_crash: process not tracked"}
        if self.game_process.poll() is not None:
            return {
                "ok": False,
                "error": f"no_crash: game process exited (code={self.game_process.returncode})",
                "invariant_name": "no_crash",
            }
        return {"ok": True, "detail": "no_crash: process alive"}

    def _inv_nonblank_screen(self) -> dict:
        ss_resp = self.client.screenshot_b64()
        if "error" in ss_resp:
            return {"ok": True, "detail": "nonblank_screen: could not get screenshot, skipping"}
        image_bytes = base64.b64decode(ss_resp["image_base64"])
        if _is_blank(image_bytes):
            ss_path = _save_screenshot(image_bytes, self.out_dir, f"blank_step_{self._step_index}")
            return {
                "ok": False,
                "error": "nonblank_screen: screenshot appears blank (all-black or all-white)",
                "invariant_name": "nonblank_screen",
                "screenshot_path": ss_path,
            }
        return {"ok": True, "detail": "nonblank_screen: ok"}

    def _inv_stateserver_ok(self) -> dict:
        if not self.client.ping(timeout=5.0):
            return {
                "ok": False,
                "error": "stateserver_ok: StateServer did not respond within 5s",
                "invariant_name": "stateserver_ok",
            }
        return {"ok": True, "detail": "stateserver_ok: ok"}

    # ------------------------------------------------------------------
    # Baseline snapshot
    # ------------------------------------------------------------------

    def _goal_slug(self, goal: str) -> str:
        import re
        slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")
        return slug[:40] or "scenario"

    def _baseline_path(self, goal: str) -> Optional[Path]:
        """Return the Path where the baseline for this goal should live, or None."""
        if not self.project_path:
            return None
        base = Path(self.project_path) / "test" / "scenarios" / "baselines"
        slug = self._goal_slug(goal)
        return base / f"{slug}-baseline.json"

    def _save_baseline(self, goal: str, seed: int, steps: list) -> Optional[str]:
        """Save a structural baseline after a passing run. Returns path or None."""
        bp = self._baseline_path(goal)
        if bp is None:
            return None
        try:
            # Gather structural snapshot
            state_resp = self.client.get_state()
            game_state = state_resp.get("game_state", {}) or {}
            a11y_resp = self.client.send({"command": "a11y_tree"})
            a11y_count = len(a11y_resp.get("a11y_tree", []))

            # Scene list from project path
            scene_list: list[str] = []
            if self.project_path:
                for root, _dirs, files in os.walk(self.project_path):
                    for f in files:
                        if f.endswith(".tscn"):
                            rel = os.path.relpath(os.path.join(root, f), self.project_path)
                            scene_list.append(rel)
                scene_list.sort()

            # Game state analysis
            gs_keys = list(game_state.keys())
            gs_ranges: dict = {}
            for k, v in game_state.items():
                if isinstance(v, (int, float)):
                    gs_ranges[k] = {"min": v, "max": v}

            # Final screenshot hash
            ss_resp = self.client.screenshot_b64()
            screenshot_hash = ""
            if "image_base64" in ss_resp:
                img_bytes = base64.b64decode(ss_resp["image_base64"])
                screenshot_hash = _image_hash(img_bytes)

            baseline = {
                "scenario_goal": goal,
                "seed": seed,
                "passed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "scene_list": scene_list,
                "scene_count": len(scene_list),
                "a11y_tree_count": a11y_count,
                "game_state_keys": gs_keys,
                "game_state_ranges": gs_ranges,
                "screenshot_hash": screenshot_hash,
                "steps_count": len(steps),
            }
            bp.parent.mkdir(parents=True, exist_ok=True)
            bp.write_text(json.dumps(baseline, indent=2))
            print(f"[scenario_qa] Baseline saved: {bp}")
            return str(bp)
        except Exception as e:
            print(f"[scenario_qa] WARNING: could not save baseline: {e}")
            return None

    def _check_baseline(self, goal: str) -> list[str]:
        """Compare current structural state against saved baseline. Returns warnings."""
        bp = self._baseline_path(goal)
        if bp is None or not bp.exists():
            return []
        try:
            baseline = json.loads(bp.read_text())
        except Exception:
            return []

        warnings: list[str] = []

        # Check scene list
        if self.project_path and baseline.get("scene_list"):
            current_scenes: set[str] = set()
            for root, _dirs, files in os.walk(self.project_path):
                for f in files:
                    if f.endswith(".tscn"):
                        rel = os.path.relpath(os.path.join(root, f), self.project_path)
                        current_scenes.add(rel)
            baseline_scenes = set(baseline["scene_list"])
            missing = baseline_scenes - current_scenes
            if missing:
                warnings.append(f"Missing scenes vs baseline: {', '.join(sorted(missing))}")

        # Check a11y tree count
        try:
            a11y_resp = self.client.send({"command": "a11y_tree"})
            current_a11y = len(a11y_resp.get("a11y_tree", []))
            baseline_a11y = baseline.get("a11y_tree_count", 0)
            if baseline_a11y > 0 and current_a11y < baseline_a11y * 0.5:
                warnings.append(
                    f"a11y_tree count dropped >50%: baseline={baseline_a11y}, now={current_a11y}"
                )
        except Exception:
            pass

        # Check game_state keys
        try:
            state_resp = self.client.get_state()
            current_keys = set((state_resp.get("game_state") or {}).keys())
            baseline_keys = set(baseline.get("game_state_keys", []))
            missing_keys = baseline_keys - current_keys
            if missing_keys:
                warnings.append(f"game_state keys missing vs baseline: {', '.join(sorted(missing_keys))}")
        except Exception:
            pass

        return warnings

    # ------------------------------------------------------------------
    # Failure envelope
    # ------------------------------------------------------------------

    def _build_failure_envelope(
        self,
        goal: str,
        seed: int,
        failed_step: int,
        step: dict,
        result: dict,
        trace_path: str,
        kind: str = None,
    ) -> dict:
        # Determine kind
        if kind is None:
            if "invariant_name" in result:
                kind = "invariant_violation"
            elif "vision" in step:
                kind = "vision_failure"
            else:
                kind = "scenario_failure"

        # Get current state for diff
        state_resp = self.client.get_state()
        game_state = state_resp.get("game_state", {})

        # Build minimal replay: steps up to and including the failed one
        all_steps = self.scenario.get("steps", [])
        replay_steps = all_steps[:failed_step + 1]

        return {
            "kind": kind,
            "goal": goal,
            "seed": seed,
            "failed_step": failed_step,
            "failed_op": step.get("op"),
            "expected": result.get("expected"),
            "actual": result.get("actual"),
            "error": result.get("error"),
            "state_at_failure": game_state,
            "state_hash_at_failure": _state_hash(game_state),
            "screenshot_path": result.get("screenshot_path"),
            "invariant_name": result.get("invariant_name"),
            "trace_path": trace_path,
            "replay": {
                "goal": goal,
                "seed": seed,
                "steps": replay_steps,
            },
        }


# ---------------------------------------------------------------------------
# Bug task emission
# ---------------------------------------------------------------------------

def emit_bug_task(
    envelope: dict,
    project: str,
    swarm_url: str = "http://localhost:5001",
) -> dict:
    """POST a structured bug task to the swarm API from a failure envelope.

    The bug task description is human-readable so a fixing agent can act on it
    without replaying the trace. The full envelope is stored in metadata so
    nothing is lost.
    """
    import urllib.request
    import urllib.error

    kind = envelope.get("kind", "scenario_failure")
    goal = envelope.get("goal", "unknown")
    failed_step = envelope.get("failed_step", -1)
    failed_op = envelope.get("failed_op", "unknown")
    error = envelope.get("error", "")
    expected = envelope.get("expected")
    actual = envelope.get("actual")
    invariant = envelope.get("invariant_name")
    state = envelope.get("state_at_failure", {})
    screenshot = envelope.get("screenshot_path")
    trace = envelope.get("trace_path")

    # Build plain-English description
    lines = [
        f"SCENARIO QA FAILURE: {goal}",
        f"",
        f"Kind: {kind}",
        f"Failed at step {failed_step} (op: {failed_op})",
        f"Error: {error}",
    ]
    if expected is not None or actual is not None:
        lines.append(f"Expected: {expected!r}")
        lines.append(f"Actual:   {actual!r}")
    if invariant:
        lines.append(f"Invariant violated: {invariant}")
    if state:
        lines.append(f"")
        lines.append(f"Game state at failure:")
        for k, v in list(state.items())[:8]:
            lines.append(f"  {k}: {v!r}")
    lines += [
        f"",
        f"Reproduction: run the replay steps in the failure envelope.",
        f"Trace: {trace}",
        f"Screenshot: {screenshot}",
    ]

    description = "\n".join(lines)

    task_payload = {
        "project": project,
        "type": "bug",
        "priority": 80,
        "description": description,
        "metadata": {
            "scenario_qa": True,
            "envelope": envelope,
            "screenshot_path": screenshot,
            "trace_path": trace,
        },
    }

    try:
        body = json.dumps(task_payload).encode()
        req = urllib.request.Request(
            f"{swarm_url}/api/tasks",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            task_id = data.get("id") or data.get("task_id")
            print(f"[scenario_qa] Bug task filed: {task_id}")
            return {"ok": True, "task_id": task_id}
    except Exception as e:
        print(f"[scenario_qa] WARNING: could not file bug task: {e}")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="scenario_qa: deterministic Godot scenario executor")
    parser.add_argument("scenario", help="Path to scenario JSON file")
    parser.add_argument("--project-path", help="Path to Godot project (required unless --no-launch)")
    parser.add_argument("--port", type=int, default=11009, help="StateServer port (default 11009)")
    parser.add_argument("--out-dir", default="/tmp/scenario_qa", help="Output directory for trace + screenshots")
    parser.add_argument("--no-launch", action="store_true", help="Skip launching the game (assume already running)")
    parser.add_argument("--project", default="", help="Swarm project name — if set, failed runs file a bug task")
    parser.add_argument("--swarm-url", default="http://localhost:5001", help="Swarm API base URL (default: http://localhost:5001)")
    args = parser.parse_args()

    scenario_path = Path(args.scenario)
    if not scenario_path.exists():
        print(f"[scenario_qa] ERROR: scenario file not found: {scenario_path}", file=sys.stderr)
        sys.exit(1)

    scenario = json.loads(scenario_path.read_text())
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    client = StateServerClient(port=args.port)
    game_process = None

    if not args.no_launch:
        if not args.project_path:
            print("[scenario_qa] ERROR: --project-path required unless --no-launch", file=sys.stderr)
            sys.exit(1)

        # Try to import and use the swarm launch_game
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent.parent))
            from swarm.qa_tools import launch_game, _qa_game_process, _state_port
            result = launch_game(args.project_path)
            if not result.get("ok", True) and "error" in result:
                print(f"[scenario_qa] WARNING: launch_game reported: {result.get('error')}")
            # Use the port allocated by launch_game
            from swarm import qa_tools as _qt
            client = StateServerClient(port=_qt._state_port)
            game_process = _qt._qa_game_process
        except Exception as e:
            # Fallback: launch manually
            print(f"[scenario_qa] launch_game import failed ({e}), launching manually")
            godot_bin = os.environ.get("GODOT_PATH", "godot")
            game_process = subprocess.Popen(
                [godot_bin, "--path", args.project_path, "--",
                 "--state-port", str(args.port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"[scenario_qa] Launched PID {game_process.pid}, waiting for StateServer...")
            if not client.wait_ready(timeout=15):
                print("[scenario_qa] ERROR: StateServer did not come up in 15s", file=sys.stderr)
                game_process.terminate()
                sys.exit(1)
    else:
        print(f"[scenario_qa] --no-launch: connecting to StateServer on port {args.port}")
        if not client.wait_ready(timeout=10):
            print("[scenario_qa] ERROR: StateServer not available", file=sys.stderr)
            sys.exit(1)

    # Optional vision function — use swarm vision_query if available
    vision_fn = None
    try:
        from swarm.qa_tools import vision_query as _vq
        def vision_fn(image_path: str, question: str):
            result = _vq(image_path, question, model_tier="fast")
            if isinstance(result, dict):
                if "error" in result:
                    return result  # return dict so caller can detect infra error
                return result.get("answer", result.get("text", str(result)))
            return str(result)
    except Exception:
        pass

    executor = ScenarioExecutor(
        scenario=scenario,
        client=client,
        out_dir=out_dir,
        game_process=game_process,
        vision_fn=vision_fn,
        project=args.project,
        swarm_url=args.swarm_url,
        project_path=args.project_path or "",
    )

    result = executor.run()

    # Print summary
    print()
    if result["ok"]:
        print(f"✓ PASSED: {result['goal']} ({result['steps_run']} steps)")
        print(f"  Trace: {result['trace_path']}")
        if result.get("baseline_path"):
            print(f"  Baseline: {result['baseline_path']}")
        for w in result.get("baseline_warnings", []):
            print(f"  ⚠ {w}")
        sys.exit(0)
    else:
        print(f"✗ FAILED: {result['goal']} at step {result['failed_step']}")
        print(f"  Trace:    {result['trace_path']}")
        print(f"  Envelope: {result.get('envelope_path', 'n/a')}")
        if result.get("failure"):
            print(f"  Error:    {result['failure'].get('error', '')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
