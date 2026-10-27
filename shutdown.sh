#!/bin/bash
# Shutdown both swarm and VLM servers cleanly.
cd "$(dirname "$0")"

_stop() {
    local name="$1" pid_file="$2" grep_pattern="$3"
    if [ -f "$pid_file" ]; then
        PID=$(cat "$pid_file")
        if kill -0 "$PID" 2>/dev/null; then
            echo "→ Stopping $name (PID $PID)..."
            kill "$PID"
            for i in $(seq 1 10); do
                sleep 0.5
                kill -0 "$PID" 2>/dev/null || break
            done
            kill -0 "$PID" 2>/dev/null && kill -9 "$PID"
            echo "✓ $name stopped"
        else
            echo "  $name PID $PID not running (stale)"
        fi
        rm -f "$pid_file"
    else
        # Fallback: grep for process
        PIDS=$(pgrep -f "$grep_pattern" 2>/dev/null || true)
        if [ -n "$PIDS" ]; then
            echo "→ Killing stray $name processes: $PIDS"
            echo "$PIDS" | xargs kill
            echo "✓ Done"
        else
            echo "  $name not running"
        fi
    fi
}

_stop "Swarm controller" ".swarm.pid"   "swarm_runner.py api"
_stop "VLM server"       ".vlm.pid"     "mlx_vlm.server"
_stop "Headroom proxy"   ".headroom.pid"       "headroom.*8888"
_stop "Headroom (Codex)" ".headroom-codex.pid" "headroom.*8877"
_stop "Shrimp router"    ".shrimp.pid"  "shrimp_router.app"
echo "All done."
