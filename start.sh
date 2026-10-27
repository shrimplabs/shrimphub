#!/bin/bash
# Start the swarm controller API server (port 5001)
set -e
cd "$(dirname "$0")"

PID_FILE=".swarm.pid"

if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Swarm already running (PID $OLD_PID). Use ./stop.sh first."
        exit 1
    fi
    rm -f "$PID_FILE"
fi

echo "Starting swarm controller..."
ulimit -n 65536
nohup .venv/bin/python3 swarm_runner.py api > data/swarm.log 2>&1 &
echo $! > "$PID_FILE"
echo "Started (PID $(cat $PID_FILE)). Logs: data/swarm.log"
echo "Dashboard: http://localhost:5001"
