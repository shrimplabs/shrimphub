#!/bin/bash
# Stop the local mlx-vlm vision server
cd "$(dirname "$0")"

PID_FILE=".vlm.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping VLM server (PID $PID)..."
        kill "$PID"
        sleep 1
        if kill -0 "$PID" 2>/dev/null; then
            kill -9 "$PID"
        fi
        echo "Stopped."
    else
        echo "Process $PID not running (stale PID file)."
    fi
    rm -f "$PID_FILE"
else
    echo "No PID file. Killing any mlx_vlm.server processes..."
    pkill -f "mlx_vlm.server" && echo "Killed." || echo "None found."
fi
