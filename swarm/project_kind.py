"""Single source of truth for "is this a Godot / Python / TypeScript project?".

Prompt selection (swarm_runner, agent_runtime), validation and the
validation-baseline diff each had their own rules and could disagree: a
folder of bare *.py files got generic prompts but Python validation, and a
Godot project with a requirements.txt got Python prompts but Godot
validation.  Kept dependency-free so the agent subprocess can import it.
"""

from __future__ import annotations

from pathlib import Path

PYTHON_MANIFESTS = ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg")


def _has_python_sources(path: Path) -> bool:
    for candidate in (path, path / "src", path / "tests"):
        try:
            if candidate.is_dir() and any(candidate.glob("*.py")):
                return True
        except OSError:
            continue
    return False


def is_godot(path) -> bool:
    return (Path(path) / "project.godot").exists()


def is_typescript(path) -> bool:
    p = Path(path)
    return (p / "package.json").exists() and (
        (p / "tsconfig.json").exists() or (p / "tsconfig.app.json").exists()
    )


def is_python(path) -> bool:
    """Python project: a Python manifest, or bare .py sources in root/src/tests.

    A Godot project is never Python (helper scripts or a test-only
    requirements.txt do not change what the project is)."""
    p = Path(path)
    if is_godot(p):
        return False
    if any((p / m).exists() for m in PYTHON_MANIFESTS):
        return True
    return not is_typescript(p) and _has_python_sources(p)
