#!/usr/bin/env bash
# Dead-man's switch: alert when swarm is idle with pending managed work.
# Run every 15 min via crontab: */15 * * * * /path/to/swarm-watchdog.sh
# Requires: curl, jq, osascript (macOS notification)

SWARM_URL="${SWARM_URL:-http://localhost:5001}"
IDLE_THRESHOLD_MINUTES=30

health=$(curl -sf --max-time 5 "$SWARM_URL/api/health" 2>/dev/null) || {
    osascript -e 'display notification "Swarm server unreachable" with title "Swarm Dead" sound name "Basso"' 2>/dev/null
    echo "$(date): swarm unreachable" >&2
    exit 1
}

active=$(echo "$health" | python3 -c "import json,sys; print(json.load(sys.stdin).get('active_agents',0))" 2>/dev/null)
frozen=$(echo "$health" | python3 -c "import json,sys; print(json.load(sys.stdin).get('frozen_handles',0))" 2>/dev/null)
frozen_age=$(echo "$health" | python3 -c "import json,sys; print(json.load(sys.stdin).get('frozen_max_age_seconds',0))" 2>/dev/null)
uptime=$(echo "$health" | python3 -c "import json,sys; print(json.load(sys.stdin).get('uptime_seconds',0))" 2>/dev/null)
code_stale=$(echo "$health" | python3 -c "import json,sys; print(json.load(sys.stdin).get('code_stale',False))" 2>/dev/null)

STATE_FILE="/tmp/swarm-watchdog-state"
IDLE_SECONDS=$((IDLE_THRESHOLD_MINUTES * 60))

# Alert on stale code running >2h (server not restarted after deploy)
if [[ "$code_stale" == "True" ]] && [[ "$uptime" -gt 7200 ]]; then
    osascript -e "display notification \"Running stale code for ${uptime}s\" with title \"Swarm: Stale Code\" sound name \"Funk\"" 2>/dev/null
fi

# Alert on frozen handles that are suspiciously old (>3h = leak)
if [[ "$frozen" -gt 0 ]] && python3 -c "exit(0 if $frozen_age > 10800 else 1)" 2>/dev/null; then
    osascript -e "display notification \"${frozen} handles frozen ${frozen_age}s — possible quota-freeze leak\" with title \"Swarm: Frozen Handles\" sound name \"Basso\"" 2>/dev/null
fi

if [[ "$active" -gt 0 ]]; then
    rm -f "$STATE_FILE"
    exit 0
fi

# No active agents — check if there are pending managed tasks
pending=$(curl -sf --max-time 5 "$SWARM_URL/api/dependencies/ready" 2>/dev/null | \
    python3 -c "
import json,sys,urllib.request
ready=json.load(sys.stdin)
ready=ready if isinstance(ready,list) else ready.get('tasks',ready.get('ready',[]))
managed=json.loads(urllib.request.urlopen('http://localhost:5001/api/managed-projects').read()).get('managed_projects',[])
print(sum(1 for t in ready if t.get('project') in managed))
" 2>/dev/null)

if [[ -z "$pending" ]] || [[ "$pending" -eq 0 ]]; then
    rm -f "$STATE_FILE"
    exit 0
fi

# Idle with pending work — track how long
now=$(date +%s)
if [[ ! -f "$STATE_FILE" ]]; then
    echo "$now" > "$STATE_FILE"
    exit 0
fi

idle_since=$(cat "$STATE_FILE")
idle_duration=$((now - idle_since))
if [[ "$idle_duration" -ge "$IDLE_SECONDS" ]]; then
    idle_min=$((idle_duration / 60))
    osascript -e "display notification \"${pending} managed tasks ready, 0 agents running for ${idle_min}m\" with title \"Swarm Idle\" sound name \"Basso\"" 2>/dev/null
    echo "$(date): swarm idle ${idle_min}m with ${pending} pending managed tasks" >&2
    # Reset so it re-alerts every IDLE_THRESHOLD if still stuck
    echo "$now" > "$STATE_FILE"
fi
