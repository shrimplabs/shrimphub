#!/usr/bin/env python3
"""Sync STATE_PORT env-var fix to all Godot projects."""

import os
import re
import subprocess

TEMPLATE_PATH = "~USER/workspace/swarm-controller/templates/godot/autoload/state_server.gd"
PROJECTS = [
    "echoes-of-exile",
    "echoes-of-the-unmade",
    "ghost-circuit",
    "negative-space",
    "pacman-chase",
    "temporal-residue",
    "tetris-neon",
]
WORKSPACE = os.path.expanduser("~/workspace")

# Read the template _ready() block (lines 57-80 in template)
with open(TEMPLATE_PATH) as f:
    lines = f.readlines()
# Lines are 0-indexed; template line 57 = index 56
# _ready() ends at line 80 (before _exit_tree at line 81)
# So indices 56-80 inclusive (lines 57-80)
template_ready = "".join(lines[56:80])
print("TEMPLATE _ready():")
print(repr(template_ready))
print()

for proj in PROJECTS:
    ss = os.path.join(WORKSPACE, proj, "autoload", "state_server.gd")
    with open(ss) as f:
        content = f.read()

    # Find _ready() function
    idx = content.find("func _ready()")
    if idx == -1:
        print(f"SKIP {proj}: no _ready() found")
        continue

    # Find the end of _ready() -- next top-level func at indent 0
    rest = content[idx + len("func _ready()"):]
    m = re.search(r"\nfunc ", rest)
    end_idx = idx + len("func _ready()") + (m.start() if m else len(content))

    old_ready = content[idx:end_idx]
    new_ready = template_ready

    if old_ready == new_ready:
        print(f"ALREADY OK: {proj}")
        continue

    new_content = content[:idx] + new_ready + content[end_idx:]
    with open(ss, "w") as f:
        f.write(new_content)
    print(f"PATCHED: {proj}")

    # Commit and push
    proj_path = os.path.join(WORKSPACE, proj)
    try:
        subprocess.run(["git", "-C", proj_path, "add", "autoload/state_server.gd"], check=True)
        subprocess.run(
            ["git", "-C", proj_path, "commit", "-m",
             "fix(qa): read STATE_PORT env var in StateServer autoload"],
            check=True,
            capture_output=True
        )
        subprocess.run(["git", "-C", proj_path, "push"], check=True, capture_output=True)
        print(f"  COMMITTED + PUSHED: {proj}")
    except subprocess.CalledProcessError as e:
        print(f"  GIT ERROR: {proj}: {e.stderr.decode() if e.stderr else e}")

print("\nDone.")
