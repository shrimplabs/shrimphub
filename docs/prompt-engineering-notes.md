# Prompt Engineering Notes — Lessons from Production

## The Kill Language Problem

### What happened

The Scheduler meta-agent killed the swarm server, causing 7 hours of downtime. The prompt contained:

```
NEVER kill or signal running agents
```

The agent killed the server anyway — twice across 5 runs (once killing "zombie" agents it diagnosed via CPU heuristics, once killing the Flask server process itself after confusing the workspace copy with the live instance).

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

The scheduler ran from `/Users/costas/workspace/swarm-controller` (a managed project copy) and couldn't reach the server on its expected port (18792, a test port from an earlier config). It then found the real server on 5001, decided something was wrong with the setup, and "fixed" it.

**Lesson:** Agents operating on the swarm-controller project itself are in a uniquely dangerous position — they have shell access and can affect the orchestrator they're running inside. Consider whether swarm-controller should be a managed project at all, or whether it needs a separate permission profile that blocks process manipulation.

---

## The Zombie Heuristic Problem

In run 3, the scheduler correctly identified 10 agents that had been running for 3.5 hours with minimal CPU. It classified them as zombies and terminated them. This worked out — they were probably stuck — but the heuristic is unreliable:

- An agent waiting on an LLM response has near-zero CPU
- An agent mid-sleep between retries has near-zero CPU
- A legitimate long-running validation task has near-zero CPU

CPU usage is not a reliable proxy for agent health. The monitor thread uses heartbeat/timeout logic for a reason. Agents should not replicate this logic independently.
