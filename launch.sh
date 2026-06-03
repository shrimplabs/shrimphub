#!/bin/bash
# Launch swarm: start both servers and open the dashboard in your browser.
# Run this once in the morning; use stop.sh / stop-vlm.sh to shut down.
set -e
cd "$(dirname "$0")"

# ── Swarm server ────────────────────────────────────────────────────────────
PID_FILE=".swarm.pid"
if [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
    echo "✓ Swarm already running (PID $(cat $PID_FILE))"
else
    rm -f "$PID_FILE"
    echo "→ Starting swarm controller..."
    nohup .venv/bin/python3 swarm_runner.py api > data/swarm.log 2>&1 &
    echo $! > "$PID_FILE"
    echo "✓ Swarm started (PID $(cat $PID_FILE))"
fi

# ── VLM server ───────────────────────────────────────────────────────────────
VLM_PID_FILE=".vlm.pid"
if [ -f "$VLM_PID_FILE" ] && kill -0 "$(cat $VLM_PID_FILE)" 2>/dev/null; then
    echo "✓ VLM server already running (PID $(cat $VLM_PID_FILE))"
elif curl -s --max-time 1 http://localhost:8080/v1/models >/dev/null 2>&1; then
    echo "✓ VLM server already responding on port 8080"
else
    rm -f "$VLM_PID_FILE"
    echo "→ Starting mlx-vlm server..."
    nohup python3 -m mlx_vlm.server --port 8080 > data/vlm.log 2>&1 &
    echo $! > "$VLM_PID_FILE"
    echo "✓ VLM started (PID $(cat $VLM_PID_FILE)) — model loads on first request"
fi

# ── Headroom proxy (MiniMax compression) ─────────────────────────────────────
HEADROOM_PID_FILE=".headroom.pid"
HEADROOM_VENV="$HOME/workspace/headroom-venv"
if [ -f "$HEADROOM_PID_FILE" ] && kill -0 "$(cat $HEADROOM_PID_FILE)" 2>/dev/null; then
    echo "✓ Headroom already running (PID $(cat $HEADROOM_PID_FILE))"
elif curl -s --max-time 1 http://localhost:8888/livez >/dev/null 2>&1; then
    echo "✓ Headroom already responding on port 8888"
elif [ -f "$HEADROOM_VENV/bin/headroom" ]; then
    rm -f "$HEADROOM_PID_FILE"
    echo "→ Starting headroom proxy..."
    nohup "$HEADROOM_VENV/bin/headroom" proxy \
        --port 8888 --mode cache \
        --anthropic-api-url https://api.minimax.io/anthropic/v1 \
        --no-telemetry \
        --log-file data/headroom.log \
        > data/headroom-server.log 2>&1 &
    echo $! > "$HEADROOM_PID_FILE"
    echo "✓ Headroom started (PID $(cat $HEADROOM_PID_FILE))"
else
    echo "  Headroom not found at $HEADROOM_VENV — skipping"
fi

# ── Headroom proxy (Codex) ───────────────────────────────────────────────────
HEADROOM_CODEX_PID_FILE=".headroom-codex.pid"
if [ -f "$HEADROOM_CODEX_PID_FILE" ] && kill -0 "$(cat $HEADROOM_CODEX_PID_FILE)" 2>/dev/null; then
    echo "✓ Headroom (Codex) already running (PID $(cat $HEADROOM_CODEX_PID_FILE))"
elif curl -s --max-time 1 http://localhost:8877/livez >/dev/null 2>&1; then
    echo "✓ Headroom (Codex) already responding on port 8877"
elif [ -f "$HEADROOM_VENV/bin/headroom" ]; then
    rm -f "$HEADROOM_CODEX_PID_FILE"
    echo "→ Starting headroom proxy (Codex)..."
    nohup "$HEADROOM_VENV/bin/headroom" proxy \
        --port 8877 --mode token \
        --no-telemetry \
        --log-file data/headroom-codex.log \
        > data/headroom-codex-server.log 2>&1 &
    echo $! > "$HEADROOM_CODEX_PID_FILE"
    echo "✓ Headroom (Codex) started (PID $(cat $HEADROOM_CODEX_PID_FILE))"
else
    echo "  Headroom not found at $HEADROOM_VENV — skipping Codex proxy"
fi

# ── Shrimp router ────────────────────────────────────────────────────────────
SHRIMP_PID_FILE=".shrimp.pid"
SHRIMP_DIR="${SHRIMP_ROUTER_DIR:-$HOME/workspace/shrimp-router}"
if [ -f "$SHRIMP_PID_FILE" ] && kill -0 "$(cat $SHRIMP_PID_FILE)" 2>/dev/null; then
    echo "✓ Shrimp router already running (PID $(cat $SHRIMP_PID_FILE))"
elif curl -s --max-time 1 http://localhost:8090/health >/dev/null 2>&1; then
    echo "✓ Shrimp router already responding on port 8090"
elif [ -d "$SHRIMP_DIR" ]; then
    rm -f "$SHRIMP_PID_FILE"
    echo "→ Starting shrimp-router..."
    nohup env SHRIMP_ROUTER_CONFIG="$SHRIMP_DIR/config.yaml" "$SHRIMP_DIR/.venv/bin/shrimp-router" > data/shrimp.log 2>&1 &
    echo $! > "$SHRIMP_PID_FILE"
    echo "✓ Shrimp router started (PID $(cat $SHRIMP_PID_FILE))"
else
    echo "  Shrimp router not found at $SHRIMP_DIR — skipping"
fi

# ── Wait for swarm to be ready, then open browser ───────────────────────────
echo "→ Waiting for dashboard..."
for i in $(seq 1 20); do
    sleep 0.5
    if curl -s --max-time 1 http://localhost:5001/api/health >/dev/null 2>&1; then
        echo "✓ Dashboard ready"
        open http://localhost:5001
        exit 0
    fi
done
echo "  Timeout waiting for server. Opening anyway..."
open http://localhost:5001
