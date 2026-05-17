"""Helpers for bootstrapping external Godot test dependencies.

This module keeps third-party Godot addons such as GUT out of the controller
repository itself while still allowing deterministic project bootstrap. The
controller downloads a pinned version into a local cache once, then copies from
that cache into new projects.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

GUT_VERSION = "v9.6.0"
GUT_REPO_URL = "https://github.com/bitwes/Gut.git"


def _gut_source_from_override(path: Path) -> Path:
    """Accept either a repo root or an addon root and return addons/gut."""
    if (path / "gut_cmdln.gd").exists() and (path / "plugin.cfg").exists():
        return path
    addon_dir = path / "addons" / "gut"
    if (addon_dir / "gut_cmdln.gd").exists() and (addon_dir / "plugin.cfg").exists():
        return addon_dir
    raise FileNotFoundError(f"Invalid GUT source override: {path}")


def gut_cache_root() -> Path:
    override = os.environ.get("SWARM_GUT_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "swarm-controller" / "gut"


def gut_cached_addon_dir(version: str = GUT_VERSION) -> Path:
    return gut_cache_root() / version / "addons" / "gut"


def ensure_gut_cached(version: str = GUT_VERSION) -> Path:
    """Ensure the pinned GUT addon exists in the local cache and return it."""
    cached = gut_cached_addon_dir(version)
    if _gut_installation_complete(cached):
        return cached

    cache_root = gut_cache_root()
    cache_root.mkdir(parents=True, exist_ok=True)

    override = os.environ.get("SWARM_GUT_SOURCE_DIR")
    if override:
        source = _gut_source_from_override(Path(override).expanduser())
        cached.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, cached, dirs_exist_ok=True)
        return cached

    with tempfile.TemporaryDirectory(prefix="swarm-gut-") as tmpdir:
        tmpdir_path = Path(tmpdir)
        repo_dir = tmpdir_path / "Gut"
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", version, GUT_REPO_URL, str(repo_dir)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to download GUT {version}: "
                f"{(result.stderr or result.stdout or '').strip()}"
            )

        source = _gut_source_from_override(repo_dir)
        cached.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, cached, dirs_exist_ok=True)
    return cached


def _gut_installation_complete(addon_dir: Path) -> bool:
    """Return True only if addon_dir contains a full GUT install, not just stubs."""
    required = ["gut_cmdln.gd", "plugin.cfg", "gut_loader.gd"]
    return all((addon_dir / f).exists() for f in required)


def install_gut_into_project(project_path: Path, version: str = GUT_VERSION) -> Path:
    """Copy the cached GUT addon into a project's addons directory."""
    project_addon = project_path / "addons" / "gut"
    if _gut_installation_complete(project_addon):
        return project_addon

    cached = ensure_gut_cached(version)
    project_addon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(cached, project_addon, dirs_exist_ok=True)

    # Register GUT class_names so headless GUT runs don't fail with
    # "Some GUT class_names have not been imported".
    from swarm.platform import resolve_godot_binary
    godot = resolve_godot_binary()
    subprocess.run(
        [godot, "--headless", "--path", str(project_path), "--import"],
        capture_output=True,
        timeout=120,
    )

    return project_addon
