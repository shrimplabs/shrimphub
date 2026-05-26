#!/usr/bin/env python3
"""Sync StateServer and TestHarness templates to ALL Godot projects.

Scans every directory in SEARCH_ROOTS for a project.godot file, regardless
of whether the project is in managed_projects. This ensures old projects in
~/workspace/ and any other locations stay up to date.

Skip patterns: worktree dirs (.wt-*), backup dirs (*-backup-*), hidden dirs.
"""

import json
import os
import subprocess
import sys

WORKSPACE = os.environ.get("WORKSPACE", os.path.expanduser("~/workspace"))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(SCRIPT_DIR, "templates", "godot", "autoload")
TEMPLATE_STATE_SERVER = os.path.join(TEMPLATE_DIR, "state_server.gd")
TEMPLATE_TEST_HARNESS = os.path.join(TEMPLATE_DIR, "test_harness.gd")

# All root directories to scan for Godot projects (one level deep)
SEARCH_ROOTS = [
    WORKSPACE,
    os.path.expanduser("~/workspace"),
]

# Directory name patterns to skip
SKIP_PATTERNS = (".wt-", "-backup-", "swarm-controller", "game-harness",
                 "agent-architecture", "validation-test")


def read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def files_identical(a: str, b: str) -> bool:
    try:
        return read_file(a) == read_file(b)
    except Exception:
        return False


def copy_file(src: str, dst: str) -> None:
    content = read_file(src)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(content)


def git_commit(project_path: str, files: list[str], message: str) -> None:
    for f in files:
        subprocess.run(["git", "-C", project_path, "add", f], check=True)
    subprocess.run(["git", "-C", project_path, "commit", "-m", message], check=True)


def is_godot_project(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "project.godot"))


def should_skip(name: str) -> bool:
    if name.startswith("."):
        return True
    return any(pat in name for pat in SKIP_PATTERNS)


def find_godot_projects() -> list[tuple[str, str]]:
    """Return list of (name, path) for all Godot projects found in SEARCH_ROOTS."""
    found = []
    seen_paths = set()
    for root in SEARCH_ROOTS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if should_skip(name):
                continue
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            real = os.path.realpath(path)
            if real in seen_paths:
                continue
            seen_paths.add(real)
            if is_godot_project(path):
                found.append((name, path))
    return found


def sync_project(name: str, project_path: str) -> tuple[list, list, list]:
    """Sync templates for one project. Returns (updated, identical, missing) file lists."""
    updated_files = []
    identical = []
    missing = []

    for template, filename in [
        (TEMPLATE_STATE_SERVER, "autoload/state_server.gd"),
        (TEMPLATE_TEST_HARNESS, "autoload/test_harness.gd"),
    ]:
        dst = os.path.join(project_path, filename)
        if os.path.isfile(dst):
            if not files_identical(dst, template):
                copy_file(template, dst)
                updated_files.append(filename)
            else:
                identical.append(filename)
        else:
            missing.append(filename)

    return updated_files, identical, missing


def main() -> None:
    projects = find_godot_projects()
    print(f"Found {len(projects)} Godot projects across {len(SEARCH_ROOTS)} search roots\n")

    updated = []
    skipped_identical = []
    skipped_missing = []
    errors = []

    for name, project_path in projects:
        updated_files, identical, missing = sync_project(name, project_path)

        for f in identical:
            skipped_identical.append((name, f"{f} identical"))
        for f in missing:
            skipped_missing.append((name, f"{f} not present"))

        if updated_files:
            try:
                git_commit(
                    project_path,
                    updated_files,
                    "chore: sync StateServer/TestHarness from swarm-controller templates",
                )
                updated.append((name, updated_files))
                print(f"  ✓ {name}: updated {', '.join(updated_files)}")
            except subprocess.CalledProcessError as e:
                errors.append((name, str(e)))
                print(f"  ✗ {name}: git commit failed: {e}", file=sys.stderr)

    print(f"\n=== Sync Summary ===")
    print(f"Updated (committed): {len(updated)}")
    for proj, files in updated:
        print(f"  {proj}: {', '.join(files)}")
    if errors:
        print(f"\nErrors: {len(errors)}")
        for proj, err in errors:
            print(f"  {proj}: {err}")
    print(f"\nIdentical (skipped): {len(skipped_identical)}")
    print(f"Not present (skipped): {len(skipped_missing)}")


if __name__ == "__main__":
    main()
