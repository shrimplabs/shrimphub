#!/usr/bin/env python3
"""Sync StateServer and TestHarness templates to all managed Godot projects."""

import json
import os
import subprocess
import sys

WORKSPACE = os.environ.get("WORKSPACE", os.path.expanduser("~/workspace"))
CONFIG_PATH = os.path.join(WORKSPACE, "swarm-controller", "config.json")
TEMPLATE_DIR = os.path.join(WORKSPACE, "swarm-controller", "templates", "godot", "autoload")
TEMPLATE_STATE_SERVER = os.path.join(TEMPLATE_DIR, "state_server.gd")
TEMPLATE_TEST_HARNESS = os.path.join(TEMPLATE_DIR, "test_harness.gd")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def read_file(path: str) -> str:
    with open(path) as f:
        return f.read()


def files_identical(a: str, b: str) -> bool:
    return read_file(a) == read_file(b)


def copy_file(src: str, dst: str) -> None:
    content = read_file(src)
    with open(dst, "w") as f:
        f.write(content)


def git_commit(project_path: str, files: list[str], message: str) -> None:
    for f in files:
        subprocess.run(
            ["git", "-C", project_path, "add", f],
            check=True,
        )
    subprocess.run(
        ["git", "-C", project_path, "commit", "-m", message],
        check=True,
    )


def is_godot_project(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "project.godot"))


def autoload_exists(project_path: str, name: str) -> bool:
    return os.path.isfile(os.path.join(project_path, "autoload", name))


def main() -> None:
    config = load_config()
    projects = config.get("managed_projects", [])


    updated = []
    skipped_identical = []
    skipped_missing = []

    for project in projects:
        project_path = os.path.join(WORKSPACE, project)

        # Skip non-godot projects
        if not is_godot_project(project_path):
            skipped_missing.append((project, "not a godot project"))
            continue

        updated_files = []

        # state_server.gd
        ss_path = os.path.join(project_path, "autoload", "state_server.gd")
        if os.path.isfile(ss_path):
            if not files_identical(ss_path, TEMPLATE_STATE_SERVER):
                copy_file(TEMPLATE_STATE_SERVER, ss_path)
                updated_files.append("autoload/state_server.gd")
            else:
                skipped_identical.append((project, "state_server.gd identical"))
        else:
            skipped_missing.append((project, "state_server.gd not present"))

        # test_harness.gd
        th_path = os.path.join(project_path, "autoload", "test_harness.gd")
        if os.path.isfile(th_path):
            if not files_identical(th_path, TEMPLATE_TEST_HARNESS):
                copy_file(TEMPLATE_TEST_HARNESS, th_path)
                updated_files.append("autoload/test_harness.gd")
            else:
                skipped_identical.append((project, "test_harness.gd identical"))
        else:
            skipped_missing.append((project, "test_harness.gd not present"))

        # Commit if any files were updated
        if updated_files:
            try:
                git_commit(
                    project_path,
                    updated_files,
                    "Sync StateServer and TestHarness from swarm-controller templates (H1-H4)",
                )
                updated.append((project, updated_files))
            except subprocess.CalledProcessError as e:
                print(f"Git commit failed for {project}: {e}", file=sys.stderr)

    # Report
    print("\n=== Sync Summary ===")
    print(f"Updated (committed): {len(updated)}")
    for proj, files in updated:
        print(f"  {proj}: {', '.join(files)}")
    print(f"\nSkipped (identical): {len(skipped_identical)}")
    for proj, reason in skipped_identical:
        print(f"  {proj}: {reason}")
    print(f"\nSkipped (not present / not godot): {len(skipped_missing)}")
    for proj, reason in skipped_missing:
        print(f"  {proj}: {reason}")


if __name__ == "__main__":
    main()
