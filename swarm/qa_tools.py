"""QA tools extracted from agent_runtime.py.

Module-level config variables are set by agent_runtime.py at startup.
"""

import json
import os
import subprocess
import uuid
from pathlib import Path

from swarm.platform import (
    godot_binary_available,
    is_macos,
    kill_process_tree,
    macos_gui_automation_available,
    macos_gui_automation_error,
    popen_session_kwargs,
    resolve_godot_binary,
)

# Lazy import to avoid circular dependency
_db = None
def _get_db():
    global _db
    if _db is None:
        from swarm import db as _db_mod
        _db = _db_mod
    db_path = Path(DATA_DIR) / "swarm.db"
    try:
        _db.init(db_path)
    except Exception:
        # Safe to ignore if another caller already initialised it or if the
        # same path is re-initialised; db.init() is idempotent for our usage.
        pass
    return _db

# ---------------------------------------------------------------------------
# Config variables -- set by agent_runtime.py at startup
# ---------------------------------------------------------------------------

WORKSPACE: Path = Path(".")
DATA_DIR: str = "data"
PROJECT: str = ""
PROJECT_PATH_OVERRIDE: str = ""
TASK_TYPE: str = "qa"
TASK_ID: str = "unknown"
TASK_PRIORITY: int = 50
API_PORT: int = 9090
QA_CONFIG: dict = {}
QA_CYCLE: int = 0
QA_MAX_CYCLES: int = 5
READONLY: bool = False

# mcp_client is set by agent_runtime.py
mcp_client = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str):
    print(f"[QA] {msg}", flush=True)


def _unsupported_macos_gui(feature: str) -> dict:
    return {"ok": False, "error": macos_gui_automation_error(feature)}


def _project_root() -> str:
    if PROJECT_PATH_OVERRIDE:
        return PROJECT_PATH_OVERRIDE
    return str(WORKSPACE / PROJECT)


def _safe_cwd(cwd=None):
    if cwd:
        return cwd
    root = _project_root()
    if os.path.isdir(root):
        return root
    ws = str(WORKSPACE) if WORKSPACE else os.path.expanduser("~")
    return ws if os.path.isdir(ws) else os.getcwd()


def run(cmd: str, cwd=None, timeout: int = 60):
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_safe_cwd(cwd),
        **popen_session_kwargs(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        proc.communicate()
        return -1, "", f"Command timed out after {timeout}s"


# ---------------------------------------------------------------------------
# Runtime state (per-agent)
# ---------------------------------------------------------------------------

_qa_game_process = None
_state_port: int = 11009   # dynamically assigned per launch_game() call
_harness_port: int = 11010  # always _state_port + 1
_qa_window = {
    "x": 50, "y": 50,
    "w": 900, "h": 650,  # window size in logical screen points
    "viewport_w": 900, "viewport_h": 650,  # screenshot/input space in viewport pixels
    "pixel_ratio": 1.0,
}

_harness_game_process = None


# ---------------------------------------------------------------------------
# QA focus / window helpers
# ---------------------------------------------------------------------------

def qa_focus_game() -> dict:
    """Bring the Godot game window to the foreground."""
    if not macos_gui_automation_available():
        return _unsupported_macos_gui("qa_focus_game")
    script = '''
tell application "System Events"
    set procs to every process whose name starts with "godot"
    if procs is {} then
        set procs to every process whose name starts with "Godot"
    end if
    if procs is not {} then
        set frontmost of (item 1 of procs) to true
        return "ok"
    end if
    return "not_found"
end tell
'''
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        if "ok" in result.stdout:
            import time as _t; _t.sleep(0.5)
            return {"ok": True}
        return {"ok": False, "error": "Game window not found"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def qa_position_window() -> dict:
    """Move the Godot window to a fixed position (50,50) without forcing size.

    The window keeps its natural dimensions so portrait-oriented games are
    handled correctly. Actual window size is detected by qa_get_window_bounds
    and used for coordinate normalisation in click_element / vision_query.
    """
    if not macos_gui_automation_available():
        return _unsupported_macos_gui("qa_position_window")
    script = '''
tell application "System Events"
    set procs to every process whose name starts with "godot"
    if procs is {} then set procs to every process whose name starts with "Godot"
    if procs is {} then return "NOT_FOUND"
    set proc to item 1 of procs
    set frontmost of proc to true
    if (count of windows of proc) is 0 then return "NO_WINDOW"
    set w to window 1 of proc
    set position of w to {50, 50}
    return "ok"
end tell
'''
    try:
        import time as _t
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        raw = result.stdout.strip()
        if raw == "ok":
            _t.sleep(0.5)
            # Return partial bounds; size is read from the window, not forced
            return {"ok": True, "x": 50, "y": 50}
        return {"ok": False, "error": raw or result.stderr.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def qa_get_window_bounds(process_name: str = "godot") -> dict:
    """Get screen position/size of the game window via osascript."""
    if not macos_gui_automation_available():
        return _unsupported_macos_gui("qa_get_window_bounds")
    for name in [process_name, process_name.lower(), process_name.title()]:
        script = f'''
tell application "System Events"
    set procs to every process whose name starts with "{name}"
    if procs is {{}} then return "NOT_FOUND"
    set proc to item 1 of procs
    if (count of windows of proc) is 0 then return "NO_WINDOW"
    set w to window 1 of proc
    set pos to position of w
    set sz to size of w
    set x to item 1 of pos as integer
    set y to item 2 of pos as integer
    set ww to item 1 of sz as integer
    set wh to item 2 of sz as integer
    return (x as text) & "|" & (y as text) & "|" & (ww as text) & "|" & (wh as text)
end tell
'''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10,
            )
            raw = result.stdout.strip()
            if raw in ("NOT_FOUND", "NO_WINDOW", ""):
                continue
            if result.returncode != 0:
                continue
            parts = [int(float(p.strip())) for p in raw.split("|")]
            return {"ok": True, "x": parts[0], "y": parts[1], "w": parts[2], "h": parts[3]}
        except Exception:
            continue
    return {"ok": False, "error": "Game window not found (grant Accessibility permission to Terminal in System Settings)"}


def _qa_window_ratio() -> float:
    ratio = _qa_window.get("pixel_ratio")
    if isinstance(ratio, (int, float)) and ratio > 0:
        return float(ratio)
    vw = float(_qa_window.get("viewport_w", _qa_window.get("w", 0)) or 0)
    ww = float(_qa_window.get("w", 0) or 0)
    if vw > 0 and ww > 0:
        return vw / ww
    return 1.0


def _screen_to_viewport(x: int, y: int) -> tuple[float, float]:
    wx, wy = float(_qa_window.get("x", 0)), float(_qa_window.get("y", 0))
    ratio = _qa_window_ratio()
    return (float(x) - wx) * ratio, (float(y) - wy) * ratio


def _viewport_to_screen(vx: float, vy: float) -> tuple[int, int]:
    wx, wy = float(_qa_window.get("x", 0)), float(_qa_window.get("y", 0))
    ratio = _qa_window_ratio()
    if ratio <= 0:
        ratio = 1.0
    return int(round(wx + (float(vx) / ratio))), int(round(wy + (float(vy) / ratio)))


def _extract_first_coord_pair(text: str) -> tuple[int, int] | None:
    import re
    m = re.search(r"(\d+)\s*,\s*(\d+)", text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _resolve_image_coords_from_model(raw_x: int, raw_y: int, img_w: int, img_h: int, provider_name: str, raw_text: str = "") -> tuple[int, int, str]:
    """Map model-reported coords to image pixels.

    Returns: (img_x, img_y, mode) where mode is one of:
      - "normalized-1000"
      - "raw-pixels"
      - "fallback-clamped"
    """
    if img_w <= 0 or img_h <= 0:
        return max(0, raw_x), max(0, raw_y), "fallback-clamped"

    norm_candidate = None
    if 0 <= raw_x <= 1000 and 0 <= raw_y <= 1000:
        norm_candidate = (
            int(raw_x / 1000 * img_w),
            int(raw_y / 1000 * img_h),
        )

    raw_candidate = None
    if 0 <= raw_x < img_w and 0 <= raw_y < img_h:
        raw_candidate = (raw_x, raw_y)

    pname = (provider_name or "").lower()
    is_minimax = pname.startswith("minimax")

    # Minimax vision tools return 0..1000 normalized coords in practice.
    if is_minimax:
        if norm_candidate is not None:
            return norm_candidate[0], norm_candidate[1], "normalized-1000"
        if raw_candidate is not None:
            return raw_candidate[0], raw_candidate[1], "raw-pixels"
    else:
        # Local/other providers can vary. Prefer raw pixel coords when valid,
        # but fall back to normalized interpretation when raw is out of bounds.
        if raw_candidate is not None and norm_candidate is not None:
            text = (raw_text or "").lower()
            if ("normalized" in text) or ("0-1000" in text):
                return norm_candidate[0], norm_candidate[1], "normalized-1000"
            return raw_candidate[0], raw_candidate[1], "raw-pixels"
        if raw_candidate is not None:
            return raw_candidate[0], raw_candidate[1], "raw-pixels"
        if norm_candidate is not None:
            return norm_candidate[0], norm_candidate[1], "normalized-1000"

    # Last resort: clamp raw numbers into image bounds.
    return max(0, min(raw_x, img_w - 1)), max(0, min(raw_y, img_h - 1)), "fallback-clamped"


# ---------------------------------------------------------------------------
# Port allocation with file-based locking to prevent race conditions
# ---------------------------------------------------------------------------

import fcntl
import os

# Track ports used per project to avoid race conditions with concurrent launches
_project_port_cache: dict[str, tuple[int, int]] = {}

# Lock file for atomic port allocation
_PORT_LOCK_FILE = "/tmp/swarm_qa_port_lock.lock"

# ---------------------------------------------------------------------------
# Godot PID registry — survives agent crashes / SIGKILL
# ---------------------------------------------------------------------------
# Each launch_game* call registers its Godot PID here. kill_game* deregisters.
# sweep_godot_zombies() is called at server startup to reap any PIDs left over
# from agents that were SIGKILLed before their atexit handlers ran.

import threading as _threading
_pid_registry_lock = _threading.Lock()


def _godot_pid_registry_path() -> Path:
    return Path(DATA_DIR) / "godot_pids.json"


def _register_godot_pid(pid: int, project: str, state_port: int) -> None:
    """Record a live Godot PID so it can be reaped if the agent crashes."""
    import time as _time
    path = _godot_pid_registry_path()
    with _pid_registry_lock:
        try:
            registry = json.loads(path.read_text()) if path.exists() else {}
        except Exception:
            registry = {}
        registry[str(pid)] = {
            "project": project,
            "state_port": state_port,
            "spawned_at": _time.time(),
        }
        path.write_text(json.dumps(registry, indent=2))


def _deregister_godot_pid(pid: int) -> None:
    """Remove a Godot PID from the registry after clean shutdown."""
    path = _godot_pid_registry_path()
    with _pid_registry_lock:
        try:
            registry = json.loads(path.read_text()) if path.exists() else {}
            registry.pop(str(pid), None)
            path.write_text(json.dumps(registry, indent=2))
        except Exception:
            pass


def sweep_godot_zombies() -> list[int]:
    """Kill any Godot processes left in the PID registry from crashed agents.

    Called once at server startup. Returns list of PIDs that were reaped.
    """
    path = _godot_pid_registry_path()
    if not path.exists():
        return []
    with _pid_registry_lock:
        try:
            registry = json.loads(path.read_text())
        except Exception:
            return []
        reaped = []
        survivors = {}
        for pid_str, info in registry.items():
            pid = int(pid_str)
            # Check if still running
            try:
                os.kill(pid, 0)  # signal 0 = existence check
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True  # exists but owned by another user — leave it
            if alive:
                # Verify it's actually Godot before killing
                try:
                    result = subprocess.run(
                        ["ps", "-p", str(pid), "-o", "comm="],
                        capture_output=True, text=True, timeout=3,
                    )
                    comm = result.stdout.strip().lower()
                    if "godot" in comm:
                        os.kill(pid, 9)
                        reaped.append(pid)
                        print(f"[qa] sweep_godot_zombies: reaped PID {pid} "
                              f"({info.get('project','?')} port={info.get('state_port','?')})")
                    else:
                        # PID recycled to a non-Godot process — drop from registry
                        pass
                except Exception:
                    survivors[pid_str] = info
            # Dead or reaped — don't keep in registry
        path.write_text(json.dumps(survivors, indent=2))
        return reaped

def _find_free_port_pair(base: int = 11009, max_range: int = 200, project_key: str = None) -> tuple:
    """Find two consecutive free ports (state_port, harness_port = state_port+1).

    Scans even offsets so pairs never overlap with other pair allocations.
    Returns (state_port, harness_port).
    
    If project_key is provided, uses project-specific port derivation to avoid
    race conditions where concurrent launches get the same ports.
    
    Uses a file lock to ensure atomic port allocation across concurrent launches.
    """
    import socket as _socket
    import hashlib as _hashlib
    import time as _time
    
    # If project_key is provided, derive a consistent base from the project path
    # to reduce collisions with concurrent launches
    if project_key:
        # Use a hash of the project path to derive a unique offset
        path_hash = int(_hashlib.md5(project_key.encode()).hexdigest()[:6], 16)
        # Ensure we start at an even offset and stay within the range
        derived_base = base + (path_hash % (max_range // 2)) * 2
        log(f"_find_free_port_pair: project_key={project_key[:50]!r}, derived_base={derived_base}")
    else:
        derived_base = base
    
    # Use a file lock to ensure only one process allocates ports at a time
    lock_dir = os.path.dirname(_PORT_LOCK_FILE)
    if lock_dir and not os.path.exists(lock_dir):
        os.makedirs(lock_dir, exist_ok=True)
    
    lock_fd = None
    for retry in range(5):
        try:
            lock_fd = open(_PORT_LOCK_FILE, 'w')
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except (IOError, OSError):
            if lock_fd:
                lock_fd.close()
                lock_fd = None
            _time.sleep(0.1)  # Wait and retry
    
    if lock_fd is None:
        # Could not acquire lock, fall back to non-blocking allocation
        log("_find_free_port_pair: warning - could not acquire lock, using non-blocking allocation")
        return _find_free_port_pair_unlocked(base, max_range, derived_base, project_key)
    
    try:
        # Now we have the lock - allocate ports atomically
        result = _find_free_port_pair_unlocked(base, max_range, derived_base, project_key)
        return result
    finally:
        # Release the lock
        if lock_fd:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            lock_fd.close()


def _find_free_port_pair_unlocked(base: int, max_range: int, derived_base: int, project_key: str = None) -> tuple:
    """Internal function to find free ports without locking (must be called with lock held)."""
    import socket as _socket
    
    for offset in range(0, max_range, 2):
        sp = derived_base + offset
        # Wrap around if we exceed the max range
        if sp > base + max_range:
            sp = base + ((sp - base) % max_range)
        hp = sp + 1
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s1:
                s1.bind(("127.0.0.1", sp))
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s2:
                s2.bind(("127.0.0.1", hp))
            # Successfully bound both ports - cache for this project
            if project_key:
                _project_port_cache[project_key] = (sp, hp)
            return (sp, hp)
        except OSError:
            continue
    raise RuntimeError(f"No free port pair found in range {base}-{base + max_range}")


# ---------------------------------------------------------------------------
# StateServer helper
# ---------------------------------------------------------------------------

def _state_server_send(command: dict, port: int = None, timeout: float = 5.0) -> dict:
    """Send a JSON command to the StateServer and return the parsed response."""
    import socket as _socket
    import json as _json
    if port is None:
        port = _state_port
    recv_timeout = 30.0 if command.get("command", "") in ("screenshot_b64", "screenshot_base64") else timeout
    try:
        with _socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall((_json.dumps(command) + "\n").encode())
            sock.settimeout(recv_timeout)
            data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
        return _json.loads(data.decode().strip())
    except ConnectionRefusedError:
        return {"error": "StateServer not available"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# QA game lifecycle
# ---------------------------------------------------------------------------

def launch_game(project_path: str) -> dict:
    """Launch Godot for the given project. Returns pid + best-effort window bounds."""
    global _qa_game_process, _qa_window, _state_port, _harness_port
    import time as _time

    # Always resolve to absolute path so Godot cannot resolve to the wrong project
    # when the agent's cwd happens to be inside another Godot project tree.
    resolved_path = str(Path(project_path).resolve())
    if not Path(resolved_path, "project.godot").exists():
        return {"ok": False, "error": f"No project.godot found at {resolved_path!r}"}

    # Allocate a free port pair for this agent's StateServer and TestHarness.
    # Each concurrent QA agent gets its own ports so they don't collide.
    resolved_path = str(Path(project_path).resolve())
    if not Path(resolved_path, "project.godot").exists():
        return {"ok": False, "error": f"No project.godot found at {resolved_path!r}"}

    # Use project path as key to derive project-specific ports and reduce race conditions
    # with concurrent launches of other projects
    _state_port, _harness_port = _find_free_port_pair(project_key=resolved_path)
    log(f"launch_game: allocated ports state={_state_port} harness={_harness_port}")

    # _find_free_port_pair() already skips busy ports, so the allocated pair is
    # guaranteed free -- no need to kill anything here. Killing by port would
    # destroy OTHER agents' games running on nearby ports (e.g. harness runner).

    godot_bin = resolve_godot_binary()
    if not godot_binary_available(godot_bin):
        return {"ok": False, "error": f"Godot binary not found at {godot_bin}"}

    try:
        # Run without --headless so Metal/GPU rendering is active and the viewport
        # texture is populated. StateServer.screenshot_b64 requires a rendered frame --
        # headless mode skips rendering so get_image() always returns null.
        # The game window may briefly appear on screen; this is expected.
        # Clicks go through StateServer press_button/input commands (no window focus needed).
        _qa_game_process = subprocess.Popen(
            [godot_bin, "--path", resolved_path,
             "--resolution", "1280x720",
             "--", "--state-port", str(_state_port), "--harness-port", str(_harness_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=resolved_path,
            env=dict(os.environ),
        )
        pid = _qa_game_process.pid
        _register_godot_pid(pid, resolved_path, _state_port)
        log(f"Launched Godot PID {pid} for {resolved_path} (state={_state_port} harness={_harness_port})")
        _time.sleep(6)

        if _qa_game_process.poll() is not None:
            _deregister_godot_pid(pid)
            return {"ok": False, "error": "Godot exited immediately", "pid": pid}

        # Try StateServer screenshot first -- it tells us actual pixel dimensions
        # including the pixel_ratio, which is the authoritative source for
        # coordinate normalisation (works even when screencapture permissions
        # are not granted).
        state_resp = _state_server_send({"command": "screenshot_b64"}, timeout=30.0)
        if "image_base64" in state_resp:
            import base64 as _b64
            import struct as _struct
            try:
                img_bytes = _b64.b64decode(state_resp["image_base64"])
                if len(img_bytes) >= 24 and img_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                    img_w = _struct.unpack('>I', img_bytes[16:20])[0]
                    img_h = _struct.unpack('>I', img_bytes[20:24])[0]
                    # Use OS-detected window position/size for screencapture coords;
                    # StateServer PNG gives us the true viewport pixel dimensions
                    # (may differ when editor viewport != OS window due to DPI or
                    # a screen-size change saved in project settings).
                    win_x = _qa_window.get("x", 50)
                    win_y = _qa_window.get("y", 50)
                    logical_w = _qa_window.get("w", img_w)
                    logical_h = _qa_window.get("h", img_h)
                    pixel_ratio = (img_w / logical_w) if logical_w > 0 else 1.0
                    _qa_window = {
                        "x": win_x, "y": win_y,
                        "w": logical_w, "h": logical_h,
                        "viewport_w": img_w, "viewport_h": img_h,
                        "pixel_ratio": pixel_ratio,
                    }
                    log(f"launch_game: window at ({win_x},{win_y}) size {logical_w}x{logical_h}, "
                        f"viewport {img_w}x{img_h}, pixel_ratio={pixel_ratio:.2f}")
                    return {"ok": True, "pid": pid, "x": win_x, "y": win_y,
                            "w": img_w, "h": img_h, "pixel_ratio": pixel_ratio,
                            "state_port": _state_port}
            except Exception as e:
                log(f"StateServer bounds decode failed: {e}")

        # Fallback: read window bounds via osascript when available
        bounds = qa_get_window_bounds("godot")
        bounds["pid"] = pid
        if not bounds.get("ok"):
            log("Window bounds unavailable \u2014 screenshots will capture full screen")
            return {"ok": True, "pid": pid, "x": 0, "y": 0, "w": 0, "h": 0,
                    "state_port": _state_port, "note": "Window bounds unavailable"}
        _qa_window = {
            "x": bounds["x"], "y": bounds["y"],
            "w": bounds["w"], "h": bounds["h"],
            "viewport_w": bounds["w"], "viewport_h": bounds["h"],
            "pixel_ratio": 1.0,
        }
        bounds["state_port"] = _state_port
        return bounds
    except Exception as e:
        return {"ok": False, "error": str(e)}


def launch_game_headless(project_path: str) -> dict:
    """Launch Godot headlessly so the StateServer starts without opening a window.

    For use by non-QA agents that only need get_game_state() \u2014 no desktop window,
    no focus stealing.  The process is tracked in _qa_game_process so that
    get_game_state() works normally via the shared StateServer port.
    """
    global _qa_game_process, _state_port, _harness_port
    import time as _time

    resolved_path = str(Path(project_path).resolve())
    if not Path(resolved_path, "project.godot").exists():
        return {"ok": False, "error": f"No project.godot found at {resolved_path!r}"}

    # Use project path as key to derive project-specific ports and reduce race conditions
    # with concurrent launches of other projects
    _state_port, _harness_port = _find_free_port_pair(project_key=resolved_path)
    log(f"launch_game_headless: allocated ports state={_state_port} harness={_harness_port}")

    godot_bin = resolve_godot_binary()
    if not godot_binary_available(godot_bin):
        return {"ok": False, "error": f"Godot binary not found at {godot_bin}"}

    try:
        _qa_game_process = subprocess.Popen(
            [godot_bin, "--headless", "--path", resolved_path, "--",
             "--state-port", str(_state_port),
             "--harness-port", str(_harness_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=resolved_path,
        )
        pid = _qa_game_process.pid
        _register_godot_pid(pid, resolved_path, _state_port)
        log(f"Launched Godot headless PID {pid} for {resolved_path}")
        _time.sleep(4)

        if _qa_game_process.poll() is not None:
            _deregister_godot_pid(pid)
            return {"ok": False, "error": "Godot exited immediately", "pid": pid}

        return {"ok": True, "pid": pid, "headless": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def kill_game() -> dict:
    """Kill the launched game process."""
    global _qa_game_process
    if _qa_game_process is not None:
        pid = _qa_game_process.pid
        try:
            _qa_game_process.kill()
            _qa_game_process = None
            _deregister_godot_pid(pid)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "note": "No game process running"}


# ---------------------------------------------------------------------------
# QA screenshot / click
# ---------------------------------------------------------------------------

def take_screenshot(filename: str) -> dict:
    """Take a screenshot of the game window and save to project path."""
    global _qa_window
    import base64 as _base64
    if not Path(filename).suffix:
        filename = filename + ".png"
    out_path = str(Path(_project_root()) / "qa_screenshots" / filename)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    # Try StateServer screenshot (no window focus needed)
    resp = _state_server_send({"command": "screenshot_b64"}, timeout=30.0)
    if "image_base64" in resp:
        try:
            img_bytes = _base64.b64decode(resp["image_base64"])
            with open(out_path, "wb") as f:
                f.write(img_bytes)
            w, h = _qa_window.get("viewport_w", _qa_window["w"]), _qa_window.get("viewport_h", _qa_window["h"])
            import struct as _struct
            if len(img_bytes) >= 24 and img_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                img_w = _struct.unpack('>I', img_bytes[16:20])[0]
                img_h = _struct.unpack('>I', img_bytes[20:24])[0]
                log(f"StateServer screenshot: {img_w}x{img_h} (window {_qa_window['w']}x{_qa_window['h']})")
                logical_w = _qa_window["w"]
                _qa_window["pixel_ratio"] = img_w / logical_w if logical_w > 0 else 1.0
                _qa_window["viewport_w"] = img_w
                _qa_window["viewport_h"] = img_h
                w, h = img_w, img_h
            return {"ok": True, "path": out_path, "source": "state_server", "w": w, "h": h}
        except Exception as e:
            log(f"StateServer b64 decode failed: {e}, falling back to screencapture")

    # StateServer path updates viewport_w/viewport_h above. _qa_window["w"]/["h"]
    # remains logical window points for robust cross-DPI coordinate conversion.

    # Fallback: screencapture -- always re-query live bounds so a screen-size
    # change in the editor doesn't leave us with stale coordinates.
    live = qa_get_window_bounds("godot")
    if live.get("ok"):
        _qa_window["x"] = live["x"]
        _qa_window["y"] = live["y"]
        _qa_window["w"] = live["w"]
        _qa_window["h"] = live["h"]
    x, y, w, h = _qa_window["x"], _qa_window["y"], _qa_window["w"], _qa_window["h"]
    if not is_macos():
        return {"ok": False, "error": "take_screenshot fallback requires macOS screencapture; use StateServer screenshots on this platform."}
    try:
        if w > 0 and h > 0:
            cmd = ["screencapture", "-x", "-R", f"{x},{y},{w},{h}", out_path]
        else:
            cmd = ["screencapture", "-x", out_path]
        subprocess.run(cmd, check=True, timeout=10)
        try:
            if w > 0 and h > 0:
                subprocess.run(["sips", "-z", str(h), str(w), out_path],
                                check=True, capture_output=True, timeout=10)
            else:
                subprocess.run(["sips", "-Z", "1280", out_path],
                                check=True, capture_output=True, timeout=10)
        except Exception:
            pass
        return {"ok": True, "path": out_path, "source": "screencapture",
                "window_x": x, "window_y": y, "w": w, "h": h}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def screenshot_burst(filename_prefix: str, count: int = 4, interval: float = 0.25) -> dict:
    """Take a rapid burst of screenshots to capture motion or state changes over time.
    Returns a list of paths that can be passed individually to vision_query.

    Args:
        filename_prefix: base name, e.g. "driving" → driving_0.png, driving_1.png, ...
        count: number of frames to capture (default 4)
        interval: seconds between frames (default 0.25s = 4fps)

    Example: capture 6 frames over 1.5s to verify the car is moving:
        burst = screenshot_burst("car_move", count=6, interval=0.25)
        vision_query(burst["paths"][0], "Where is the car?")
        vision_query(burst["paths"][-1], "Has the car moved compared to the first frame?")
    """
    import time as _t
    paths = []
    for i in range(int(count)):
        fname = f"{filename_prefix}_{i}"
        result = take_screenshot(fname)
        if result.get("ok"):
            paths.append(result["path"])
        if i < int(count) - 1:
            _t.sleep(float(interval))
    return {"ok": bool(paths), "paths": paths, "count": len(paths)}


def _qa_click_viewport(vx: float, vy: float) -> dict:
    """Click at viewport pixel coordinates (StateServer input space)."""
    import time as _t
    vpx = float(vx)
    vpy = float(vy)
    resp = _state_server_send({"command": "input", "type": "click", "x": vpx, "y": vpy})
    if resp.get("ok"):
        sx, sy = _viewport_to_screen(vpx, vpy)
        log(f"StateServer click at viewport ({vpx:.1f},{vpy:.1f}) \u2192 screen ({sx},{sy})")
        return {"ok": True, "source": "state_server"}
    if "error" not in resp or "not available" not in resp.get("error", ""):
        log(f"StateServer error: {resp.get('error')} \u2014 falling back to cliclick")

    sx, sy = _viewport_to_screen(vpx, vpy)
    qa_focus_game()
    _t.sleep(0.3)
    try:
        subprocess.run(["cliclick", f"c:{sx},{sy}"], check=True, timeout=5)
        return {"ok": True, "source": "cliclick"}
    except FileNotFoundError:
        if not macos_gui_automation_available():
            return _unsupported_macos_gui("viewport click fallback")
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "System Events" to click at {{{sx}, {sy}}}'],
                check=True, timeout=5,
            )
            return {"ok": True, "source": "osascript"}
        except Exception as e:
            return {"ok": False, "error": f"cliclick not found and osascript failed: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _qa_move_mouse_viewport(vx: float, vy: float) -> dict:
    """Move the virtual mouse to viewport pixel coordinates."""
    import time as _t
    vpx = float(vx)
    vpy = float(vy)
    resp = _state_server_send({"command": "input", "type": "move", "x": vpx, "y": vpy})
    if resp.get("ok"):
        sx, sy = _viewport_to_screen(vpx, vpy)
        log(f"StateServer move mouse at viewport ({vpx:.1f},{vpy:.1f}) -> screen ({sx},{sy})")
        return {"ok": True, "source": "state_server"}
    if "error" not in resp or "not available" not in resp.get("error", ""):
        log(f"StateServer error: {resp.get('error')} -- falling back to cliclick move")

    sx, sy = _viewport_to_screen(vpx, vpy)
    qa_focus_game()
    _t.sleep(0.2)
    try:
        subprocess.run(["cliclick", f"m:{sx},{sy}"], check=True, timeout=5)
        return {"ok": True, "source": "cliclick"}
    except FileNotFoundError:
        if not macos_gui_automation_available():
            return _unsupported_macos_gui("viewport mouse move fallback")
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "System Events" to set the position of the mouse to {{{sx}, {sy}}}'],
                check=True, timeout=5,
            )
            return {"ok": True, "source": "osascript"}
        except Exception as e:
            return {"ok": False, "error": f"cliclick not found and osascript failed: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _coerce_coord(value, name: str) -> int:
    """Accept numeric tool args even when providers emit them as strings."""
    try:
        return int(round(float(value)))
    except Exception:
        raise ValueError(f"{name} must be numeric, got {value!r}")


def _qa_click_at(x: int, y: int) -> dict:
    """Click at absolute screen coordinates."""
    import time as _t
    try:
        x = _coerce_coord(x, "x")
        y = _coerce_coord(y, "y")
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    # Agents frequently confuse coordinate spaces. If values look like
    # viewport/image coordinates (especially on HiDPI where viewport is larger
    # than logical window size), compensate automatically.
    wx, wy = int(_qa_window.get("x", 0)), int(_qa_window.get("y", 0))
    logical_w = int(_qa_window.get("w", 0))
    logical_h = int(_qa_window.get("h", 0))
    viewport_w = int(_qa_window.get("viewport_w", logical_w))
    viewport_h = int(_qa_window.get("viewport_h", logical_h))

    looks_like_viewport = (
        0 <= x < max(1, viewport_w) and
        0 <= y < max(1, viewport_h) and
        # Strong signal: outside logical window bounds but still valid viewport coords
        (x >= max(1, logical_w) or y >= max(1, logical_h))
    )
    if looks_like_viewport:
        vx, vy = float(x), float(y)
        sx, sy = _viewport_to_screen(vx, vy)
        log(
            f"click_at auto-space: interpreted ({x},{y}) as viewport "
            f"(window {logical_w}x{logical_h}, viewport {viewport_w}x{viewport_h})"
        )
    else:
        vx, vy = _screen_to_viewport(x, y)
        sx, sy = x, y

    resp = _state_server_send({"command": "input", "type": "click", "x": vx, "y": vy})
    if resp.get("ok"):
        log(f"StateServer click at screen ({sx},{sy}) \u2192 viewport ({vx},{vy})")
        return {"ok": True, "source": "state_server"}
    if "error" not in resp or "not available" not in resp.get("error", ""):
        log(f"StateServer error: {resp.get('error')} \u2014 falling back to cliclick")

    qa_focus_game()
    _t.sleep(0.3)
    try:
        subprocess.run(["cliclick", f"c:{sx},{sy}"], check=True, timeout=5)
        return {"ok": True, "source": "cliclick"}
    except FileNotFoundError:
        if not macos_gui_automation_available():
            return _unsupported_macos_gui("screen click fallback")
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "System Events" to click at {{{sx}, {sy}}}'],
                check=True, timeout=5,
            )
            return {"ok": True, "source": "osascript"}
        except Exception as e:
            return {"ok": False, "error": f"cliclick not found and osascript failed: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _qa_move_mouse(x: int, y: int) -> dict:
    """Move the mouse to absolute screen coordinates."""
    try:
        x = _coerce_coord(x, "x")
        y = _coerce_coord(y, "y")
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    logical_w = int(_qa_window.get("w", 0))
    logical_h = int(_qa_window.get("h", 0))
    viewport_w = int(_qa_window.get("viewport_w", logical_w))
    viewport_h = int(_qa_window.get("viewport_h", logical_h))

    looks_like_viewport = (
        0 <= x < max(1, viewport_w) and
        0 <= y < max(1, viewport_h) and
        (x >= max(1, logical_w) or y >= max(1, logical_h))
    )
    if looks_like_viewport:
        vx, vy = float(x), float(y)
        sx, sy = _viewport_to_screen(vx, vy)
        log(
            f"move_mouse auto-space: interpreted ({x},{y}) as viewport "
            f"(window {logical_w}x{logical_h}, viewport {viewport_w}x{viewport_h})"
        )
    else:
        vx, vy = _screen_to_viewport(x, y)
        sx, sy = x, y

    resp = _state_server_send({"command": "input", "type": "move", "x": vx, "y": vy})
    if resp.get("ok"):
        log(f"StateServer move mouse at screen ({sx},{sy}) -> viewport ({vx},{vy})")
        return {"ok": True, "source": "state_server"}
    return _qa_move_mouse_viewport(vx, vy)


def _find_color_region(image_path, color_hints, skip_top=50):
    """Find the centroid of pixels matching any of the given (r,g,b,tolerance) tuples."""
    try:
        from PIL import Image as _Image
        img = _Image.open(image_path).convert("RGB")
        pixels = img.load()
        w, h = img.size
        xs, ys = [], []
        for y in range(skip_top, h):
            for x in range(w):
                r, g, b = pixels[x, y]
                for tr, tg, tb, tol in color_hints:
                    if abs(r - tr) <= tol and abs(g - tg) <= tol and abs(b - tb) <= tol:
                        xs.append(x)
                        ys.append(y)
                        break
        if xs:
            return int(sum(xs) / len(xs)), int(sum(ys) / len(ys))
    except Exception:
        pass
    return None


def click_element(image_path: str, element_description: str) -> dict:
    """Find a UI element by description and click it.

    Grounding-first approach:
      1. Query a11y_tree from StateServer to get flat element list.
      2. Search for element whose label or qa_label matches description
         (case-insensitive, partial match).
      3. If found, press via StateServer using element path.
      4. Fall back to color detection or VLM coordinate prediction on failure.
    """
    import re
    desc_lower = element_description.lower()
    iw = _qa_window.get("viewport_w", _qa_window.get("w", 0))
    ih = _qa_window.get("viewport_h", _qa_window.get("h", 0))

    # -------------------------------------------------------------------
    # 1. GROUNDING-FIRST: a11y_tree
    # -------------------------------------------------------------------
    tree_resp = _state_server_send({"command": "a11y_tree"})
    if "error" not in tree_resp and isinstance(tree_resp.get("a11y_tree"), list):
        desc_tokens = {tok for tok in re.findall(r"[a-z0-9]+", desc_lower) if len(tok) >= 3}
        best_elem = None
        best_score = 0
        for elem in tree_resp["a11y_tree"]:
            label = elem.get("label", "") or elem.get("name", "") or ""
            qa_label = elem.get("qa_label", "") or ""
            role = (elem.get("role", "") or "").lower()
            if not label and not qa_label:
                continue
            label_lower = label.lower()
            qa_lower = qa_label.lower()
            score = 0
            if role == "button":
                score += 2
            if desc_lower == label_lower or desc_lower == qa_lower:
                score += 10
            if label_lower and (desc_lower in label_lower or label_lower in desc_lower):
                score += 6
            if qa_lower and (desc_lower in qa_lower or qa_lower in desc_lower):
                score += 6
            elem_tokens = {tok for tok in re.findall(r"[a-z0-9]+", f"{label_lower} {qa_lower}") if len(tok) >= 3}
            shared = len(desc_tokens & elem_tokens)
            score += shared * 2
            # Avoid matching the entire menu/container when the request is for a button.
            if "button" in desc_lower and role != "button":
                score -= 4
            if score > best_score:
                best_score = score
                best_elem = elem

        if best_elem and best_score > 0:
            elem_path = best_elem.get("path", "")
            if elem_path:
                press_resp = _state_server_send({"command": "press_button", "id": elem_path})
                log(
                    f"click_element '{element_description}' via a11y_tree: "
                    f"matched '{best_elem.get('label', '') or best_elem.get('qa_label', '')}', "
                    f"path={elem_path}, score={best_score}, resp={press_resp}"
                )
                if press_resp.get("ok"):
                    return {"ok": True, "source": "a11y_tree", "element": best_elem}

    # -------------------------------------------------------------------
    # 2. COLOR-BASED DETECTION (for common buttons)
    # -------------------------------------------------------------------
    color_map = {
        "play": [(83, 175, 89, 30), (76, 153, 0, 40), (100, 200, 100, 40)],
        "quit": [(244, 67, 54, 40), (200, 50, 50, 40)],
        "exit": [(244, 67, 54, 40), (200, 50, 50, 40)],
        "retry": [(33, 150, 243, 40), (30, 140, 240, 40)],
    }
    for keyword, hints in color_map.items():
        if keyword in desc_lower:
            centroid = _find_color_region(image_path, hints)
            if centroid:
                img_x, img_y = centroid
                screen_x, screen_y = _viewport_to_screen(img_x, img_y)
                log(f"click_element '{element_description}' color-detect at viewport ({img_x},{img_y}) screen ({screen_x},{screen_y})")
                return _qa_click_viewport(img_x, img_y)

    # -------------------------------------------------------------------
    # 3. VISION FALLBACK
    # -------------------------------------------------------------------
    cfg = QA_CONFIG if QA_CONFIG else {}
    provider_name = cfg.get("vision_provider", "minimax-mcp")
    providers = cfg.get("vision_providers", {})
    provider = providers.get(provider_name, {})

    question = (
        f"Where is the {element_description}? "
        f"Reply with ONLY the x,y image pixel coordinates of its center, "
        f"like: 450,340 \u2014 no other text."
    )

    try:
        if provider.get("format") == "mcp" and mcp_client is not None:
            from swarm.agent_runtime import mcp_call_tool
            server = provider.get("mcp_server", "minimax")
            tool_name = provider.get("mcp_tool", "understand_image")
            result = mcp_call_tool(server, tool_name, {"image_source": image_path, "prompt": question})
            content = result.get("content", str(result))
            if isinstance(content, dict):
                raw = content.get("text", str(content))
            elif isinstance(content, str):
                try:
                    import json as _json
                    raw = _json.loads(content).get("text", content)
                except Exception:
                    raw = content
            else:
                raw = str(content)
        else:
            from swarm.vision import call_vision
            rest_provider = "minimax-rest"
            rest_cfg = (cfg.get("vision_providers") or {}).get(rest_provider)
            if rest_cfg:
                raw = call_vision(image_path, question, rest_provider, cfg)
            else:
                raw = call_vision(image_path, question, provider_name, cfg)

        pair = _extract_first_coord_pair(raw)
        if not pair:
            return {"ok": False, "error": f"Could not parse coordinates from vision response: {raw[:200]}"}

        # Use the actual PNG dimensions (img_w × img_h) for coordinate
        # normalisation -- not _qa_window which may still hold stale values
        # from before the game launched in portrait mode.
        import struct as _struct
        img_w, img_h = iw, ih
        try:
            with open(image_path, "rb") as f:
                header = f.read(24)
                if len(header) >= 24 and header[:8] == b'\x89PNG\r\n\x1a\n':
                    img_w = _struct.unpack('>I', header[16:20])[0]
                    img_h = _struct.unpack('>I', header[20:24])[0]
        except Exception:
            pass  # Fall back to _qa_window dimensions

        nx, ny = pair
        img_x, img_y, coord_mode = _resolve_image_coords_from_model(
            nx, ny, img_w, img_h, provider_name, raw
        )
        screen_x, screen_y = _viewport_to_screen(img_x, img_y)
        log(
            f"click_element '{element_description}' via vision: "
            f"raw ({nx},{ny}) mode={coord_mode} -> viewport ({img_x},{img_y}) "
            f"screen ({screen_x},{screen_y})"
        )
        return _qa_click_viewport(img_x, img_y)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def vision_query(image_path, question: str, model_tier: str = "fast", timeout: int = 120) -> dict:
    """Send one or more screenshots to the configured vision model and return the analysis.

    image_path: a single path string, OR a list of paths (e.g. from screenshot_burst).
      When a list is passed, all images are sent in one call so the model can reason
      across frames -- useful for motion detection, before/after comparisons, etc.

    timeout: seconds before giving up on the VLM call (default 120).
    Returns {"ok": False, "error": "vision_query timed out after Xs"} on timeout.

    Multi-image example:
        burst = screenshot_burst("drive", count=4, interval=0.25)
        vision_query(burst["paths"], "Did the car move between frame 1 and frame 4?")
    """
    import concurrent.futures
    import re
    global _qa_window

    # Normalise image_path to a list
    image_paths = image_path if isinstance(image_path, list) else [image_path]
    primary_image = image_paths[0] if image_paths else ""

    def _run():
        try:
            from swarm.vision import get_provider_name
            cfg = QA_CONFIG if QA_CONFIG else {}
            provider_name = get_provider_name(model_tier, cfg)
            providers = cfg.get("vision_providers", {})
            provider = providers.get(provider_name, {})

            iw = _qa_window.get("viewport_w", _qa_window["w"])
            ih = _qa_window.get("viewport_h", _qa_window["h"])

            if provider.get("format") == "mcp" and mcp_client is not None:
                from swarm.agent_runtime import mcp_call_tool
                server = provider.get("mcp_server", "minimax")
                tool = provider.get("mcp_tool", "understand_image")
                result = mcp_call_tool(server, tool, {"image_source": primary_image, "prompt": question})
                answer = result.get("content", str(result))
                log(f"vision_query/mcp/{model_tier}: {str(answer)[:80]}")
                return {"ok": True, "answer": answer, "provider": provider_name}

            from swarm.vision import call_vision, call_vision_multi
            if len(image_paths) > 1:
                answer = call_vision_multi(image_paths, question, provider_name, cfg)
            else:
                answer = call_vision(primary_image, question, provider_name, cfg)
            log(f"vision_query/{model_tier}: {answer[:80]}")

            out = {"ok": True, "answer": answer, "provider": provider_name}

            pair = _extract_first_coord_pair(answer)
            if pair and iw > 0 and ih > 0:
                # Use actual PNG dimensions for normalisation -- this is critical
                # for portrait-oriented games where the window is taller than wide.
                import struct as _struct
                img_w, img_h = iw, ih
                try:
                    with open(primary_image, "rb") as _f:
                        _hdr = _f.read(24)
                        if len(_hdr) >= 24 and _hdr[:8] == b'\x89PNG\r\n\x1a\n':
                            img_w = _struct.unpack('>I', _hdr[16:20])[0]
                            img_h = _struct.unpack('>I', _hdr[20:24])[0]
                except Exception:
                    pass
                nx, ny = pair
                img_px, img_py, coord_mode = _resolve_image_coords_from_model(
                    nx, ny, img_w, img_h, provider_name, answer
                )
                screen_x, screen_y = _viewport_to_screen(img_px, img_py)
                out["coords_mode"] = coord_mode
                out["coords_image"]  = [img_px, img_py]
                out["coords_viewport"] = [img_px, img_py]
                out["coords_screen"] = [screen_x, screen_y]
                log(
                    f"coords ({nx},{ny}) mode={coord_mode} "
                    f"\u2192 viewport ({img_px},{img_py}) \u2192 screen ({screen_x},{screen_y})"
                )

            return out
        except Exception as e:
            return {"ok": False, "error": str(e)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_run)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return {"ok": False, "error": f"vision_query timed out after {timeout}s"}


# ---------------------------------------------------------------------------
# QA keyboard / wait
# ---------------------------------------------------------------------------

def qa_key_press(key: str) -> dict:
    """Press a keyboard key / Godot action."""
    resp = _state_server_send({"command": "input", "type": "action", "action": key})
    if resp.get("ok"):
        log(f"StateServer action '{key}'")
        return {"ok": True, "source": "state_server"}
    if "error" not in resp or "not available" not in resp.get("error", ""):
        log(f"StateServer error: {resp.get('error')} \u2014 falling back to cliclick")

    try:
        subprocess.run(["cliclick", f"kp:{key}"], check=True, timeout=5)
        return {"ok": True, "source": "cliclick"}
    except FileNotFoundError:
        if not macos_gui_automation_available():
            return _unsupported_macos_gui("qa_key_press fallback")
        try:
            subprocess.run(
                ["osascript", "-e", f'tell application "System Events" to keystroke "{key}"'],
                check=True, timeout=5,
            )
            return {"ok": True, "source": "osascript"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def qa_wait(seconds: float) -> dict:
    """Wait for a given number of seconds."""
    import time as _time
    seconds = float(seconds)  # coerce str -> float in case LLM passes "3" instead of 3
    _time.sleep(seconds)
    return {"ok": True, "waited": seconds}


def qa_press_button(text: str, port: int = None) -> dict:
    """Press a UI Button in the running Godot game by its visible text label."""
    # StateServer expects `id`; include `text` for compatibility with older clients.
    return _state_server_send({"command": "press_button", "id": text, "text": text}, port=port)


# ---------------------------------------------------------------------------
# QA game state polling
# ---------------------------------------------------------------------------

def get_game_state(port: int = None, command: str = "state") -> dict:
    """Read live game state from the running Godot instance via StateServer.

    command="state"           → full game state + scene tree (default)
    command="screenshot_b64"  → {"image_base64": "<png>"} from Godot renderer
    command="a11y_tree"       → flat list of interactive UI elements
    """
    return _state_server_send({"command": command}, port=port)


def qa_wait_for_idle(timeout: float = 10.0, interval: float = 0.5) -> dict:
    """Poll game state until the ball is no longer in flight or game_over becomes true."""
    import time as _time
    start = _time.monotonic()
    last_state: dict = {}
    while True:
        elapsed = _time.monotonic() - start
        state = get_game_state()
        last_state = state
        if "error" in state:
            return {"error": state["error"], "elapsed": round(elapsed, 2)}
        ball = state.get("ball") or {}
        in_flight = ball.get("in_flight", False)
        game_over = state.get("game_over", False)
        game_active = state.get("game_active", True)
        if not in_flight or game_over or not game_active:
            return {**state, "elapsed": round(elapsed, 2), "settled": True}
        if elapsed >= timeout:
            return {**state, "elapsed": round(elapsed, 2), "settled": False, "note": "timeout"}
        _time.sleep(interval)


def qa_poll_until(condition_key: str, condition_value, timeout: float = 10.0,
                  interval: float = 0.1, negate: bool = False) -> dict:
    """Poll get_game_state() until a field matches a value (or times out)."""
    import time as _time

    def _get_nested(d: dict, key: str):
        parts = key.split(".")
        val = d
        for p in parts:
            if not isinstance(val, dict):
                return None
            val = val.get(p)
        return val

    start = _time.monotonic()
    last_state: dict = {}
    polls = 0
    while True:
        elapsed = _time.monotonic() - start
        state = get_game_state()
        last_state = state
        polls += 1
        if "error" in state:
            return {"error": state["error"], "elapsed": round(elapsed, 2), "polls": polls}
        field_val = _get_nested(state, condition_key)
        matched = (field_val != condition_value) if negate else (field_val == condition_value)
        if matched:
            return {**state, "elapsed": round(elapsed, 2), "polls": polls,
                    "condition_met": True, "final_value": field_val}
        if elapsed >= timeout:
            return {**state, "elapsed": round(elapsed, 2), "polls": polls,
                    "condition_met": False, "final_value": field_val,
                    "note": f"timeout waiting for {condition_key}={'!' if negate else ''}{condition_value!r}"}
        _time.sleep(interval)


def qa_wait_until(state_key: str, target_value, timeout: float = 10.0,
                  interval: float = 0.1) -> dict:
    """Convenience alias for qa_poll_until."""
    return qa_poll_until(state_key, target_value, timeout=timeout, interval=interval)


# ---------------------------------------------------------------------------
# QA sequence execution
# ---------------------------------------------------------------------------

def qa_run_sequence(actions: list) -> dict:
    """Execute a list of input actions without LLM round-trips between steps."""
    _ALLOWED = {"click_at", "move_mouse", "key_press", "wait", "focus_game"}

    results = []
    failed = 0
    for i, step in enumerate(actions):
        tool = step.get("tool", "")
        sargs = step.get("args", {})
        if tool not in _ALLOWED:
            results.append({"step": i, "tool": tool, "ok": False,
                             "error": f"tool '{tool}' not allowed in sequences"})
            failed += 1
            continue
        try:
            if tool == "click_at":
                r = _qa_click_at(sargs.get("x", 0), sargs.get("y", 0))
            elif tool == "move_mouse":
                r = _qa_move_mouse(sargs.get("x", 0), sargs.get("y", 0))
            elif tool == "key_press":
                r = qa_key_press(sargs.get("key", ""))
            elif tool == "wait":
                r = qa_wait(sargs.get("seconds", 0.1))
            elif tool == "focus_game":
                r = qa_focus_game()
            else:
                r = {"ok": False, "error": "unhandled"}
            ok = r.get("ok", True)
            results.append({"step": i, "tool": tool, "ok": ok})
            if not ok:
                failed += 1
        except Exception as e:
            results.append({"step": i, "tool": tool, "ok": False, "error": str(e)})
            failed += 1

    return {
        "steps_run": len(actions),
        "steps_failed": failed,
        "ok": failed == 0,
        "results": results,
    }


# ---------------------------------------------------------------------------
# QA task creation
# ---------------------------------------------------------------------------

def qa_create_bug_task(description: str, evidence_path: str = "", priority=80, dependencies: list = None) -> dict:
    """Create a bug task in the swarm controller for a QA-discovered issue."""
    import urllib.request as _ur
    _priority_map = {"high": 80, "medium": 50, "low": 30, "critical": 100}
    if isinstance(priority, str):
        priority = _priority_map.get(priority.lower(), 80)
    full_desc = description
    if evidence_path:
        full_desc += f"\n\nEvidence screenshot: {evidence_path}"
    task_id = f"qa-bug-{PROJECT}-{uuid.uuid4().hex[:12]}"
    task = {
        "id": task_id,
        "project": PROJECT,
        "type": "bug",
        "priority": priority,
        "description": full_desc,
        "dependencies": dependencies or [],
        "max_attempts": 3,
        "metadata": {"created_by": "qa_agent"},
    }
    try:
        body = json.dumps(task).encode()
        req = _ur.Request(
            f"http://localhost:{API_PORT}/api/tasks",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        log(f"Created bug task {task_id}")
        return {"ok": True, "task_id": task_id, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def qa_requeue_self(bug_task_ids: list) -> dict:
    """Re-queue this QA task to run again after the given bug tasks complete."""
    global QA_CYCLE, QA_MAX_CYCLES
    next_cycle = QA_CYCLE + 1
    if next_cycle > QA_MAX_CYCLES:
        msg = (
            f"QA cycle limit reached ({QA_MAX_CYCLES} cycles). "
            f"Remaining bugs ({len(bug_task_ids)}) handed off to swarm."
        )
        log(msg)
        return {"ok": False, "error": msg, "cycle_limit_reached": True}

    import urllib.request as _ur
    new_task_id = f"qa-{PROJECT}-rerun-{uuid.uuid4().hex[:12]}"
    # Chain to project HEAD so the re-queued QA task always connects to the
    # project's dependency chain rather than floating if no bug tasks exist.
    qa_deps = list(bug_task_ids)
    from swarm.task_chains import get_project_head
    head = get_project_head(_get_db(), PROJECT)
    if head and head not in qa_deps:
        qa_deps.append(head)
    task = {
        "id": new_task_id,
        "project": PROJECT,
        "type": TASK_TYPE,
        "priority": 60,
        "description": f"QA re-run for {PROJECT} after bug fixes (cycle {next_cycle}/{QA_MAX_CYCLES})",
        "dependencies": qa_deps,
        "max_attempts": 2,
        "metadata": {"created_by": "qa_agent", "is_rerun": True, "qa_cycle": next_cycle},
    }
    try:
        body = json.dumps(task).encode()
        req = _ur.Request(
            f"http://localhost:{API_PORT}/api/tasks",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        log(f"Re-queued QA task {new_task_id} (cycle {next_cycle}/{QA_MAX_CYCLES}) depending on {bug_task_ids}")
        return {"ok": True, "task_id": new_task_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def requeue_self(bug_task_ids: list) -> dict:
    """Alias for qa_requeue_self."""
    return qa_requeue_self(bug_task_ids)


# ---------------------------------------------------------------------------
# Harness QA tools (harness_qa task type)
# ---------------------------------------------------------------------------

def harness_launch_game(project_path: str, extra_args: list = None) -> dict:
    """Launch a Godot game in headless test-harness mode."""
    global _harness_game_process, _state_port, _harness_port
    import os

    if _harness_game_process is not None and _harness_game_process.poll() is None:
        return {"ok": False, "error": "A harness game is already running"}

    # Validate this is actually a Godot project before launching
    resolved_path = str(Path(project_path).resolve())
    project_godot = os.path.join(resolved_path, "project.godot")
    if not os.path.exists(project_godot):
        return {"ok": False, "error": f"No project.godot found at {resolved_path!r} -- pass the absolute path to the Godot project"}

    # Use project path as key to derive project-specific ports and reduce race conditions
    # with concurrent launches of other projects
    _state_port, _harness_port = _find_free_port_pair(project_key=resolved_path)
    log(f"harness_launch_game: allocated ports state={_state_port} harness={_harness_port}")

    godot = resolve_godot_binary()
    cmd = [godot, "--headless", "--path", resolved_path, "--",
           "--test-harness",
           "--state-port", str(_state_port),
           "--harness-port", str(_harness_port)]
    if extra_args:
        cmd.extend(extra_args)

    try:
        _harness_game_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_session_kwargs(),
        )
        import time as _time
        _time.sleep(2.0)
        if _harness_game_process.poll() is not None:
            out, err = _harness_game_process.communicate()
            return {"ok": False, "error": f"Game exited immediately: {err.decode()[:300]}"}
        pid = _harness_game_process.pid
        _register_godot_pid(pid, resolved_path, _state_port)
        log(f"Harness game launched (pid={pid})")
        return {"ok": True, "pid": pid}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_HARNESS_FALLBACK_PORTS = [11011, 11012, 11013, 11014, 11015]

def _harness_connect(port: int, deadline: float):
    """Connect to a TestHarness port, retrying with fallback ports on TIME_WAIT.

    On macOS the kernel holds ports in TIME_WAIT for up to 60s after a
    previous process exits.  The TestHarness server retries its primary port
    up to 25 times (12.5s) then binds to a fallback port.  The Python client
    must also try all fallback ports so it finds the server regardless of
    which port it ended up binding to.
    """
    import socket as _socket
    import time as _time

    ports_to_try = [port] + [p for p in _HARNESS_FALLBACK_PORTS if p != port]
    for attempt_port in ports_to_try:
        for attempt in range(5):  # 5 short retries per port
            if _time.time() >= deadline:
                return None
            try:
                s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                s.settimeout(min(2.0, max(0.5, deadline - _time.time())))
                s.connect(("localhost", attempt_port))
                return s
            except (ConnectionRefusedError, OSError):
                if s:
                    s.close()
                _time.sleep(0.2)
    return None


def harness_step(action: dict, timeout: int = 30) -> dict:
    """Send an action to the paused game and receive the next state."""
    import socket
    import json as _json
    import time as _time

    deadline = _time.time() + timeout
    s = _harness_connect(_harness_port, deadline)

    if s is None:
        return {"error": f"harness_step: could not connect to TestHarness (primary={_harness_port}, fallbacks={_HARNESS_FALLBACK_PORTS}) within {timeout}s"}

    try:
        # Send action first -- works for both phases:
        # Nav phase: TestHarness reads command then responds.
        # Game phase: game sends state after connect; action arrives in TCP buffer, game reads it after sending state.
        s.sendall((_json.dumps(action) + "\n").encode())

        data = b""
        s.settimeout(min(10.0, deadline - _time.time()))
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break

        state = _json.loads(data.decode().strip())
        return state
    except Exception as e:
        return {"error": f"harness_step: {e}"}
    finally:
        s.close()


def harness_take_screenshot(filename: str) -> dict:
    """Capture a screenshot via TestHarness during the navigation phase.

    Sends {"type": "screenshot"} over port 11010 and saves the returned PNG.
    This uses the navigation-phase protocol (client sends first, harness responds).
    Returns {"ok": True, "path": filename} or {"ok": False, "error": "..."}.
    """
    import socket
    import json as _json
    import base64 as _b64
    import os
    import time as _time

    s = None
    try:
        deadline = _time.time() + 10.0
        s = _harness_connect(_harness_port, deadline)
        if s is None:
            return {"ok": False, "error": f"harness_take_screenshot: could not connect to TestHarness (primary={_harness_port}, fallbacks={_HARNESS_FALLBACK_PORTS}) within 10s"}
        # Nav-phase protocol: we send first, harness reads then responds
        s.sendall((_json.dumps({"type": "screenshot"}) + "\n").encode())
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        resp = _json.loads(data.decode().strip())
        if "image_base64" in resp:
            img_bytes = _b64.b64decode(resp["image_base64"])
            dirpart = os.path.dirname(filename)
            if dirpart:
                os.makedirs(dirpart, exist_ok=True)
            with open(filename, "wb") as f:
                f.write(img_bytes)
            return {"ok": True, "path": filename}
        return {"ok": False, "error": resp.get("error", "no image in response")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        if s:
            s.close()


def harness_get_state() -> dict:
    """Get current harness game state."""
    return harness_step({"type": "noop"})


def harness_run_test(test_name: str) -> dict:
    """Run a named test via harness protocol."""
    return harness_step({"type": "run_test", "test": test_name})


def harness_kill_game() -> dict:
    """Kill the harness game process."""
    global _harness_game_process
    if _harness_game_process is not None:
        pid = _harness_game_process.pid
        try:
            kill_process_tree(_harness_game_process)
            _harness_game_process = None
            _deregister_godot_pid(pid)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "note": "No harness game running"}


def harness_poll_state(timeout: int = 5) -> dict:
    """Poll game state from StateServer.

    Works on any Godot game with the StateServer autoload registered -- no
    TestHarness.checkpoint() calls needed in the game code.  Returns the full
    state dict (scene_tree + game_state) on success, or {"error": ...}.
    """
    import socket
    import json as _json
    try:
        with socket.create_connection(("127.0.0.1", _state_port), timeout=timeout) as s:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.sendall((_json.dumps({"command": "state"}) + "\n").encode())
            data = b""
            s.settimeout(timeout)
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
            return _json.loads(data.decode().strip())
    except Exception as e:
        return {"error": str(e)}


def harness_inject(command: dict, timeout: int = 5) -> dict:
    """Send an input command to StateServer.

    Use this to inject input into a game that uses StateServer polling rather
    than TestHarness checkpoints.  Accepts any StateServer command:
      {"command": "input", "type": "click", "x": 400, "y": 300}
      {"command": "input", "type": "action", "action": "ui_accept"}
      {"command": "press_button", "id": "PlayButton"}
    Returns the StateServer response dict, or {"error": ...}.
    """
    import socket
    import json as _json
    try:
        with socket.create_connection(("127.0.0.1", _state_port), timeout=timeout) as s:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.sendall((_json.dumps(command) + "\n").encode())
            data = b""
            s.settimeout(timeout)
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
            return _json.loads(data.decode().strip())
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Convenience aliases (for execute_tool dispatch)
# ---------------------------------------------------------------------------

def qa_run_sequence_disp(actions: list) -> dict:
    return qa_run_sequence(actions)


def qa_vision_query_disp(image_path: str, question: str, model_tier: str = "fast") -> dict:
    return vision_query(image_path, question, model_tier)



# ---------------------------------------------------------------------------
# Non-prefixed wrappers (call qa_-prefixed functions for compatibility)
# ---------------------------------------------------------------------------


def focus_game() -> dict:
    """Bring the Godot game window to the foreground."""
    return qa_focus_game()


def position_window() -> dict:
    """Move and resize the Godot window to a fixed, known position (top-left, 900x650)."""
    return qa_position_window()


def get_window_bounds(process_name: str = "godot") -> dict:
    """Get screen position/size of the game window via osascript."""
    return qa_get_window_bounds(process_name)


def click_at(x: int, y: int) -> dict:
    """Click at absolute screen coordinates."""
    return _qa_click_at(x, y)


def move_mouse(x: int, y: int) -> dict:
    """Move the mouse to absolute screen coordinates."""
    return _qa_move_mouse(x, y)


def key_press(key: str) -> dict:
    """Press a keyboard key / Godot action."""
    return qa_key_press(key)


def key_hold(key: str, duration: float = 1.0) -> dict:
    """Hold a key or Godot action for `duration` seconds, then release.
    Use this instead of key_press for movement keys in action/racing games
    where the character only moves while the key is held down.
    Example: key_hold("w", 2.0) to accelerate forward for 2 seconds."""
    import time as _time
    resp = _state_server_send({"command": "input", "type": "hold", "action": key, "duration": duration},
                              timeout=duration + 5.0)
    if resp.get("ok"):
        log(f"StateServer hold '{key}' for {duration}s")
        return {"ok": True, "source": "state_server"}
    # Fallback: rapid-fire key_press for the duration (simulates a held key)
    log(f"StateServer hold failed ({resp.get('error')}), falling back to repeated key_press for {duration}s")
    # Do a single probe first -- if the key name is invalid (e.g. a Godot action name like
    # "hard_drop" that cliclick doesn't know), bail immediately rather than silently looping.
    probe = qa_key_press(key)
    if not probe.get("ok"):
        log(f"key_hold fallback: key_press probe failed for '{key}': {probe.get('error')} -- key name may be a Godot action, not a physical key")
        return {"ok": False, "error": f"key_hold fallback failed: {probe.get('error', 'key_press returned ok=False')} -- use a physical key name (e.g. 'space', 'arrow-up') instead of a Godot action name"}
    interval = 0.05
    end = _time.time() + duration
    count = 1  # probe already fired one press
    while _time.time() < end:
        result = qa_key_press(key)
        if not result.get("ok"):
            break
        _time.sleep(interval)
        count += 1
    log(f"key_hold fallback: fired {count} key_press events over {duration}s")
    return {"ok": True, "source": "key_press_fallback", "presses": count}


def mouse_drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> dict:
    """Click-drag from (x1,y1) to (x2,y2) over `duration` seconds.
    Sends mousedown at start, interpolates mouse movement, mouseup at end.
    Useful for sliders, drag-and-drop, and swipe gestures."""
    resp = _state_server_send({
        "command": "input", "type": "drag",
        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        "duration": duration
    })
    if resp.get("ok"):
        log(f"StateServer drag ({x1},{y1})->({x2},{y2}) over {duration}s")
        return {"ok": True, "source": "state_server"}
    return {"ok": False, "error": resp.get("error", "drag not supported")}


def right_click(x: int, y: int) -> dict:
    """Right-click at absolute game coordinates. Useful for context menus and secondary actions."""
    resp = _state_server_send({"command": "input", "type": "right_click", "x": x, "y": y})
    if resp.get("ok"):
        log(f"StateServer right_click ({x},{y})")
        return {"ok": True, "source": "state_server"}
    return {"ok": False, "error": resp.get("error", "right_click not supported")}


def double_click(x: int, y: int) -> dict:
    """Double-click at absolute game coordinates."""
    resp = _state_server_send({"command": "input", "type": "double_click", "x": x, "y": y})
    if resp.get("ok"):
        log(f"StateServer double_click ({x},{y})")
        return {"ok": True, "source": "state_server"}
    return {"ok": False, "error": resp.get("error", "double_click not supported")}


def scroll(x: int, y: int, delta: float = 3.0) -> dict:
    """Scroll the mouse wheel at game coordinates.
    delta > 0 = scroll up/forward, delta < 0 = scroll down/back.
    Useful for inventory screens, maps, skill trees, and scrollable menus."""
    resp = _state_server_send({"command": "input", "type": "scroll", "x": x, "y": y, "delta": delta})
    if resp.get("ok"):
        log(f"StateServer scroll ({x},{y}) delta={delta}")
        return {"ok": True, "source": "state_server"}
    return {"ok": False, "error": resp.get("error", "scroll not supported")}


def key_combo(keys: list) -> dict:
    """Press multiple keys simultaneously (e.g. Shift+W for sprint, Ctrl+Z for undo).
    keys: list of key name strings, e.g. ['shift', 'w'] or ['ctrl', 'z'].
    All keys are pressed at once, held briefly, then released together."""
    resp = _state_server_send({"command": "input", "type": "key_combo", "keys": keys})
    if resp.get("ok"):
        log(f"StateServer key_combo {keys}")
        return {"ok": True, "source": "state_server"}
    return {"ok": False, "error": resp.get("error", "key_combo not supported")}


def play_macro(actions: list) -> dict:
    """Execute a timed sequence of input actions entirely server-side with no LLM round-trips.
    Use this to play out a scripted interaction over time -- the StateServer runs the full
    sequence end-to-end and returns when done.

    Each action is a dict with a 'type' key:
      {"type": "hold",      "action": "move_right", "duration": 2.0}  # hold a Godot action
      {"type": "key",       "key": "w",             "duration": 1.5}  # hold a physical key
      {"type": "key_combo", "keys": ["shift", "w"]}                   # simultaneous keys
      {"type": "click",     "x": 400, "y": 300}                       # left click
      {"type": "right_click","x": 400, "y": 300}                      # right click
      {"type": "drag",      "x1":100,"y1":200,"x2":300,"y2":200,"duration":0.3}
      {"type": "action",    "action": "ui_accept"}                     # one-shot Godot action
      {"type": "wait",      "seconds": 0.5}                            # pause

    Example -- drive a car forward, turn right, boost:
      play_macro([
        {"type": "hold",  "action": "move_forward", "duration": 2.0},
        {"type": "wait",  "seconds": 0.2},
        {"type": "hold",  "action": "turn_right",   "duration": 1.0},
        {"type": "action","action": "boost"},
        {"type": "wait",  "seconds": 0.5},
      ])
    """
    resp = _state_server_send({"command": "play_macro", "actions": actions})
    if resp.get("ok"):
        log(f"StateServer play_macro: {len(actions)} actions")
        return {"ok": True, "source": "state_server", "actions": len(actions)}
    return {"ok": False, "error": resp.get("error", "play_macro not supported")}


def wait(seconds: float) -> dict:
    """Wait for a given number of seconds."""
    return qa_wait(seconds)


def run_sequence(actions: list) -> dict:
    """Execute a list of input actions without LLM round-trips between steps."""
    return qa_run_sequence(actions)
