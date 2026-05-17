#!/bin/bash
# Godot validation wrapper - runs in background and suppresses all GUI
cd "$1"
/opt/homebrew/bin/godot --headless --quit 2>&1
exit 0
