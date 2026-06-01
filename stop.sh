#!/bin/bash
# Stop the swarm controller API server
cd "$(dirname "$0")"

PID_FILE=".swarm.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "No PID file found. Checking for stray processes..."
    PIDS=$(pgrep -f "swarm_runner.py api" 2>/dev/null || true)
    if [ -z "$PIDS" ]; then
        echo "No swarm server process found."
    else
        echo "Found stray processes: $PIDS"
        echo "$PIDS" | xargs kill
        echo "Killed."
    fi
    exit 0
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
    echo "Stopping swarm (PID $PID)..."
    kill "$PID"
    # Wait up to 5 seconds for clean exit
    for i in $(seq 1 10); do
        sleep 0.5
        kill -0 "$PID" 2>/dev/null || break
    done
    if kill -0 "$PID" 2>/dev/null; then
        echo "Still running, force-killing..."
        kill -9 "$PID"
    fi
    echo "Stopped."
else
    echo "Process $PID not running (stale PID file)."
fi
rm -f "$PID_FILE"
