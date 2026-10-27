#!/bin/bash
# Start the local mlx-vlm vision server (port 8080)
cd "$(dirname "$0")"

PID_FILE=".vlm.pid"

if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "VLM server already running (PID $OLD_PID)."
        echo "Check: curl -s http://localhost:8080/v1/models"
        exit 0
    fi
    rm -f "$PID_FILE"
fi

# Quick check via port
if curl -s --max-time 1 http://localhost:8080/v1/models >/dev/null 2>&1; then
    echo "VLM server already responding on port 8080."
    exit 0
fi

echo "Starting mlx-vlm server on port 8080..."
nohup python3 -m mlx_vlm.server --port 8080 > data/vlm.log 2>&1 &
echo $! > "$PID_FILE"
echo "Started (PID $(cat $PID_FILE)). Logs: data/vlm.log"
echo "Waiting for server to be ready..."

# Wait up to 30 seconds for the server to respond
for i in $(seq 1 30); do
    sleep 1
    if curl -s --max-time 1 http://localhost:8080/v1/models >/dev/null 2>&1; then
        echo "VLM server ready."
        exit 0
    fi
    printf "."
done
echo ""
echo "Warning: server may still be loading. Check: curl -s http://localhost:8080/v1/models"
