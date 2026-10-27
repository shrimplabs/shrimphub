# Prompt Engineering Notes — Lessons from Production

## The Kill Language Problem

### What happened

The Scheduler meta-agent killed the swarm server, causing 7 hours of downtime. The prompt contained:

```
NEVER kill or signal running agents
```

The agent killed the server anyway — twice across 5 runs (once killing "zombie" agents it diagnosed via CPU heuristics, once killing the Flask server process itself after confusing the workspace copy with the live instance).

### The 5 runs

| Run | Time | Actions taken |
|-----|------|---------------|
| 1 | 08:15 | Repaired 1 phantom dep. Assessed 149 failed tasks. No action — recommended archaeologist. |
| 2 | 12:02 | Repaired 4 phantom deps, unblocked gem-blaster polish task. Identified 3 high-loop agents. No throttling. |
| 3 | 13:06 | **Killed 10 "zombie" agents** (3.5h stale, 0.39s CPU via SIGTERM). Repaired 3 phantom deps. Reset 9 tasks to pending. Monitor respawned 4 fresh agents. |
| 4 | 08:44 | Repaired 10 phantom deps across 2 passes. 11 agents healthy. No action needed. |
| 5 | 19:42 | Repaired 17 phantom deps. Then **killed the Flask server process** — 7 hours of downtime. |

Runs 1, 2, 4: correct and useful. Run 3: violated constraint but recovered. Run 5: catastrophic.

### The tool call that killed the server (run 5)

From the agent log (`agent_a207de9d`):

```python
# The agent tried port 18792 first (a test port, no response)
# Then found the real server on 5001
# Concluded "the service isn't running correctly" and ran:

kill $(ps aux | grep 'swarm_runner.py' | grep -v grep | awk '{print $2}' | head -1)

# Then followed up with:
kill -15 15253 14749 17390 17328 17281 15877 15730 15653 15368
```

The agent's reasoning (from its thinking block):

> *"The service isn't running. Let me start it up. The start script uses `.venv/bin/python3.12` and runs `swarm_runner.py api`."*

It didn't start it up — it killed the existing one first. Then when it tried to query the API to verify, it got errors back because it had just destroyed the thing it was querying.

### Why negative constraints fail

"NEVER kill" is priming, not blocking. The model reads the constraint and now *killing is a salient option in its context* — it just needs to rationalize an exception. LLMs are very good at rationalization. The agent found two:

1. "These agents have 0.39s CPU over 3.5 hours — they must be zombies" (run 3)
2. "The service isn't responding on port 18792, I should restart it" (run 5, the catastrophic one)

### The fix

Replace negative constraints with affirmative ones:

```
# Before
NEVER kill or signal running agents -- only affect new spawns

# After  
Only affect new spawns via API -- do not interfere with running processes
```

Same intent. No priming. The concept of killing is absent from the model's context.

### The real fix

Words don't constrain capability. `run_command` in the tool list means the agent can run `kill`, `pkill`, or anything else regardless of prompt instructions. The structural fix is removing `run_command` from agents that don't need shell access. The prompt fix is an experiment to observe whether language alone changes behavior.

### Experiment in progress

Scheduler re-enabled with updated prompt on 2026-05-30. Watching next runs to see if it reaches for `run_command` at all without the kill concept present.

---

## The Two-Workspace Confusion

The scheduler ran from the workspace copy of swarm-controller and couldn't reach the server on its expected port (18792, a test port from an earlier config). It then found the real server on 5001, decided something was wrong with the setup, and "fixed" it.

From the log:
```
[Agent] Result: {'ok': False, 'stdout': '', 'stderr': 'Service not responding on 18792'}
...
[Agent] [LLM] thinking: The service isn't running. Let me start it up.
```

It never considered that the service was running fine on a different port. It assumed failure and acted.

**Lesson:** Agents operating on the swarm-controller project itself are in a uniquely dangerous position — they have shell access and can affect the orchestrator they're running inside. Consider whether swarm-controller should be a managed project at all, or whether it needs a separate permission profile that blocks process manipulation.

---

## The Zombie Heuristic Problem

In run 3, the scheduler correctly identified 10 agents that had been running for 3.5 hours with minimal CPU. It classified them as zombies and terminated them. This worked out — they were probably stuck — but the heuristic is unreliable:

- An agent waiting on an LLM response has near-zero CPU
- An agent mid-sleep between retries has near-zero CPU
- A legitimate long-running validation task has near-zero CPU

CPU usage is not a reliable proxy for agent health. The monitor thread uses heartbeat/timeout logic for a reason. Agents should not replicate this logic independently.
