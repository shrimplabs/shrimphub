#!/bin/bash
# Headroom watchdog — polls all headroom proxies every 30s.
# Restarts any instance that stops responding to /livez.
# Run once; stays in the background. Logs to data/watchdog.log.
#
# Usage:
#   ./watchdog.sh &         # start in background
#   kill $(cat .watchdog.pid)  # stop

cd "$(dirname "$0")"
echo $$ > .watchdog.pid

LOG="data/watchdog.log"
HEADROOM_VENV="$HOME/workspace/headroom-venv"

# Load .env
if [ -f ".env" ]; then
    set -a; source .env; set +a
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"
}

start_shrimp_router() {
    rm -f .shrimp.pid
    SHRIMP_DIR="${SHRIMP_ROUTER_DIR:-$HOME/workspace/shrimp-router}"
    nohup env SHRIMP_ROUTER_CONFIG="$SHRIMP_DIR/config.yaml" \
        MINIMAX_API_KEY="$MINIMAX_API_KEY" \
        OPENCODE_API_KEY="$OPENCODE_API_KEY" \
        OPENAI_API_KEY="$OPENCODE_API_KEY" \
        KIMI_API_KEY="$KIMI_API_KEY" \
        "$SHRIMP_DIR/.venv/bin/shrimp-router" > data/shrimp.log 2>&1 &
    echo $! > .shrimp.pid
    log "↺ Restarted shrimp-router:8090 — PID $(cat .shrimp.pid)"
}

start_headroom_minimax() {
    rm -f .headroom.pid
    ANTHROPIC_API_KEY="$MINIMAX_API_KEY" \
    nohup "$HEADROOM_VENV/bin/headroom" proxy \
        --port 8888 --mode cache --backend anthropic \
        --anthropic-api-url https://api.minimax.io/anthropic \
        --intercept-tool-results \
        --no-telemetry \
        --log-file data/headroom.log \
        > data/headroom-server.log 2>&1 &
    echo $! > .headroom.pid
    log "↺ Restarted headroom:8888 (MiniMax) — PID $(cat .headroom.pid)"
}

start_headroom_codex() {
    rm -f .headroom-codex.pid
    nohup "$HEADROOM_VENV/bin/headroom" proxy \
        --port 8877 --mode token \
        --no-telemetry \
        --log-file data/headroom-codex.log \
        > data/headroom-codex-server.log 2>&1 &
    echo $! > .headroom-codex.pid
    log "↺ Restarted headroom:8877 (Codex) — PID $(cat .headroom-codex.pid)"
}

start_headroom_opencode() {
    rm -f .headroom-opencode.pid
    OPENAI_API_KEY="$OPENCODE_API_KEY" \
    nohup "$HEADROOM_VENV/bin/headroom" proxy \
        --port 8886 --mode token \
        --backend anyllm --anyllm-provider openai \
        --openai-api-url https://opencode.ai/zen/go/v1 \
        --workers 4 \
        --no-telemetry \
        --log-file data/headroom-opencode.log \
        > data/headroom-opencode-server.log 2>&1 &
    echo $! > .headroom-opencode.pid
    log "↺ Restarted headroom:8886 (OpenCode) — PID $(cat .headroom-opencode.pid)"
}

kill_port() {
    local port="$1"
    # Kill any process holding this port (handles zombie processes)
    local pid
    pid=$(lsof -ti tcp:"$port" 2>/dev/null)
    if [ -n "$pid" ]; then
        kill -9 $pid 2>/dev/null
        log "  Killed stale process(es) on port $port: $pid"
    fi
}

check() {
    local url="$1"
    local name="$2"
    local restart_fn="$3"
    local port="$4"

    # Use a 5s connect + 8s total timeout — if livez hangs, something is stuck
    if ! curl -sf --max-time 8 --connect-timeout 5 "$url" >/dev/null 2>&1; then
        log "✗ $name not responding — restarting..."
        # Kill any zombie holding the port before restarting
        [ -n "$port" ] && kill_port "$port"
        sleep 1
        $restart_fn
        # Wait up to 20s for startup (headroom downloads tokenizer on first start)
        for i in $(seq 1 10); do
            sleep 2
            if curl -sf --max-time 5 "$url" >/dev/null 2>&1; then
                log "✓ $name recovered"
                return
            fi
        done
        log "✗ $name still not responding after restart — check logs"
    fi
}

log "Watchdog started (PID $$) — polling every 30s"

while true; do
    [ -f "$HEADROOM_VENV/bin/headroom" ] || { sleep 30; continue; }

    check "http://localhost:8090/health" "shrimp-router:8090"        start_shrimp_router     8090
    check "http://localhost:8888/livez"  "headroom:8888 (MiniMax)"   start_headroom_minimax  8888
    check "http://localhost:8877/livez"  "headroom:8877 (Codex)"     start_headroom_codex    8877
    [ -n "$OPENCODE_API_KEY" ] && \
    check "http://localhost:8886/livez"  "headroom:8886 (OpenCode)"  start_headroom_opencode 8886

    sleep 30
done
