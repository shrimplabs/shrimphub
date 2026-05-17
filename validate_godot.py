#!/usr/bin/env python3
"""Run Godot validation without any GUI popups"""
import subprocess
import os
import sys

project_path = sys.argv[1] if len(sys.argv) > 1 else "."

# Run completely detached with no window
result = subprocess.run(
    ["/opt/homebrew/bin/godot", "--headless", "--quit"],
    cwd=project_path,
    capture_output=True,
    text=True,
    env={**os.environ, "DISPLAY": ""},
    start_new_session=True  # Detach from terminal
)

# Print output for parsing
print(result.stdout)
print(result.stderr, file=sys.stderr)
sys.exit(0)  # Always exit 0 - we handle errors ourselves
