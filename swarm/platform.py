"""Platform-aware runtime helpers used by validation, QA, and docs-facing flows."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def is_windows() -> bool:
    return os.name == "nt"


def is_macos() -> bool:
    return sys.platform == "darwin"


def popen_session_kwargs() -> dict:
    """Return subprocess kwargs that create an isolated process group/session."""
    if is_windows():
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags} if flags else {}
    setsid = getattr(os, "setsid", None)
    return {"preexec_fn": setsid} if setsid else {}


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate a subprocess and its child tree in a platform-aware way."""
    if proc.poll() is not None:
        return
    if is_windows():
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return
        except Exception:
            proc.kill()
            return

    import signal

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        proc.kill()


def kill_pid_tree(pid: int) -> None:
    """Terminate a process tree by PID in a platform-aware way."""
    if is_windows():
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return
        except Exception:
            os.kill(pid, 9)
            return

    import signal

    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        os.kill(pid, 9)


def _candidate_godot_paths() -> list[Path]:
    candidates: list[Path] = []
    for env_var in ("GODOT_PATH",):
        value = os.environ.get(env_var)
        if value:
            candidates.append(Path(value).expanduser())

    for name in ("godot", "godot4", "Godot"):
        resolved = shutil.which(name)
        if resolved:
            candidates.append(Path(resolved))

    for raw in (
        "/opt/homebrew/bin/godot",
        "/usr/local/bin/godot",
        "/usr/bin/godot",
    ):
        candidates.append(Path(raw))

    windows_roots = [
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    ]
    for root in filter(None, windows_roots):
        base = Path(root)
        candidates.extend(
            [
                base / "Godot" / "Godot.exe",
                base / "Godot" / "godot.exe",
                base / "Programs" / "Godot" / "Godot.exe",
            ]
        )
        candidates.extend(base.glob("Godot*/*.exe"))

    return candidates


def _expanded_godot_candidates(candidate: Path) -> list[Path]:
    """Return executable candidates represented by a configured Godot path."""
    expanded = candidate.expanduser()
    candidates = [expanded]
    try:
        if not expanded.is_dir():
            return candidates
    except OSError:
        return candidates

    if is_windows():
        windows_exes = sorted(set(expanded.glob("Godot*.exe")) | set(expanded.glob("godot*.exe")))
        candidates.extend([path for path in windows_exes if path.name.lower().endswith("_console.exe")])
        candidates.extend([path for path in windows_exes if not path.name.lower().endswith("_console.exe")])
    elif is_macos():
        candidates.extend(sorted(expanded.glob("*.app/Contents/MacOS/Godot*")))
        candidates.extend(sorted(expanded.glob("Godot*.app/Contents/MacOS/Godot*")))
    else:
        candidates.extend(
            [
                expanded / "godot",
                expanded / "godot4",
                expanded / "Godot",
            ]
        )
    return candidates


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and (is_windows() or os.access(path, os.X_OK))
    except OSError:
        return False


def resolve_godot_binary() -> str:
    """Return the best available Godot executable path or a generic command name."""
    for candidate in _candidate_godot_paths():
        for expanded in _expanded_godot_candidates(candidate):
            if _is_executable_file(expanded):
                return str(expanded)
    return "godot"


def godot_binary_available(command: str | None = None) -> bool:
    candidate = command or resolve_godot_binary()
    if not candidate:
        return False
    path = Path(candidate).expanduser()
    if _is_executable_file(path):
        return True
    return shutil.which(candidate) is not None


def kill_godot_children(parent_pid: int) -> None:
    """Kill any Godot processes that are children of parent_pid.

    Called when an agent process is killed so its spawned game windows don't
    linger as orphans.  Uses pgrep -P on Unix; falls back to a full pkill only
    if pgrep is unavailable.
    """
    if is_windows():
        # On Windows just let kill_process_tree handle it via TASKKILL /T
        return
    try:
        result = subprocess.run(
            ["pgrep", "-P", str(parent_pid)],
            capture_output=True, text=True, timeout=5,
        )
        child_pids = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
        for child_pid in child_pids:
            # Only kill if the child is actually Godot
            comm = subprocess.run(
                ["ps", "-p", str(child_pid), "-o", "comm="],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip().lower()
            if "godot" in comm:
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass
    except Exception:
        pass


def kill_existing_godot() -> None:
    """Kill any running Godot processes before launching a new QA game instance.

    Needed so stale processes don't hold port 11009 (StateServer) and block the
    new game from binding it.
    """
    if is_windows():
        # taskkill /F /IM <name> /T — force-kill the process tree by image name.
        # Godot executables on Windows are named Godot_v*.exe; match both the
        # versioned name and a plain "godot.exe" fallback.
        for name in ("Godot_v*.exe", "godot.exe"):
            subprocess.run(
                ["taskkill", "/F", "/FI", f"IMAGENAME eq {name}", "/T"],
                capture_output=True,
            )
    else:
        # pkill matches any process whose command line contains "godot" (case-insensitive on Linux).
        subprocess.run(["pkill", "-9", "-f", "godot"], capture_output=True)


def shell_quote_command_arg(value: str | Path) -> str:
    """Quote a command argument for the host shell."""
    raw = str(value)
    if is_windows():
        escaped = raw.replace('"', r'\"')
        return f'"{escaped}"' if any(ch.isspace() for ch in raw) else escaped
    return shlex.quote(raw)


def project_venv_executable(project_root: str | Path, executable: str) -> Path:
    """Return the canonical project-local virtualenv executable path for this OS."""
    root = Path(project_root)
    if is_windows():
        suffixes = [".exe", ".bat", ".cmd", ""]
        for suffix in suffixes:
            candidate = root / ".venv" / "Scripts" / f"{executable}{suffix}"
            if candidate.exists():
                return candidate
        return root / ".venv" / "Scripts" / f"{executable}.exe"
    return root / ".venv" / "bin" / executable


def macos_gui_automation_available() -> bool:
    return is_macos() and shutil.which("osascript") is not None


def macos_gui_automation_error(feature: str) -> str:
    return (
        f"{feature} requires macOS GUI automation support "
        "(osascript/System Events or equivalent tooling)."
    )
