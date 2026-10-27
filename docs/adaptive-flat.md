# Adaptive Flat Mode

Adaptive flat is an experimental routing policy that replaces the phase-based
pipeline (scout → work → validate) with a single continuous loop where the
LLM provider is swapped call-by-call based on what tools the agent just used.

The goal: pay cheap-provider rates for exploration, pay strong-provider rates
only when the agent is actually writing code or making commits.

**Status: experimental.** Toggle with `"adaptive_flat": false` in `config.json`
or `POST /api/adaptive-flat {"enabled": false}`.

---

## How it works

Every loop of the agent tool loop runs like this:

```
1. Look at the tool calls from the PREVIOUS loop (last_tools)
   and the tool calls ANNOUNCED in the current response (next_tools)
2. Run choose_adaptive_flat_provider() → RoutingDecision(provider, tier, reason)
3. Call the LLM with that provider
4. Receive full response (stream consumed, buffered to string)
5. Parse tool calls out of the response
6. INTERCEPTION CHECK: did the cheap provider emit a strong tool?
   → Yes: discard response, re-run with strong provider immediately
7. TASK_COMPLETE CHECK: did the cheap provider try to complete?
   → Yes: suppress completion, inject guard message, continue
8. Execute tool calls
9. last_tools = next_tools for the next iteration
```

No stream cancellation happens — the full response is always consumed before
any routing decision is made. Interception costs one wasted LLM call.

---

## Routing decision (`swarm/model_routing.py`)

`choose_adaptive_flat_provider()` runs before every LLM call. Priority order:

| Priority | Condition | Decision |
|----------|-----------|----------|
| 1 | `fast == strong` or neither configured | default provider, skip routing |
| 2 | Loop 0 (first call) | **strong** — sets intent |
| 3 | `next_tools` contains any strong tool (lookahead) | **strong** |
| 4 | `next_tools` all read-only (lookahead) | **cheap** |
| 5 | `next_tools` mixed | **strong** |
| 6 | Task type in `STRONG_TASK_TYPES` (bug, qa, art_pass, …) | **strong** unless pure read probe |
| 7 | No prior tool signal | **strong** |
| 8 | `last_tools` contains any strong tool | **strong** |
| 9 | `last_tools` all read-only | **cheap** |
| 10 | Fallback | **strong** |

**Consecutive cheap cap**: after 3 consecutive cheap loops, the next loop is
forced to strong regardless. Resets on any strong loop.

### Read-only tools (→ cheap provider)

`list_files`, `search_code`, `read_file`, `read_file_range`, `get_file_stats`,
`get_file_outline`, `list_tasks`, `list_subtasks`, `get_task_context`,
`scratchpad_read`, `read_agent_knowledge`, `read_shared_knowledge`,
`rag_query`, `web_search`, `fetch_url`, `broadcast_read`

### Strong tools (→ strong provider)

`write_file`, `patch_file`, `append_file`, `run_command`, `run_python`,
`git_commit`, `git_push`, `create_task`, `create_tasks`,
`create_tasks_file_aware`, `delegate_task_batch`, `annotate_downstream_tasks`,
`split_task`, `prune_task`, `insert_dependency`, `set_task_complexity`,
`vision_query`, `take_screenshot`, `screenshot_burst`, `launch_game`,
`kill_game`, `get_game_state`, `qa_run_harness`

Any unknown tool is treated as strong (conservative fallback).

### Strong task types (always strong unless pure read probe)

`art_pass`, `polish`, `qa`, `harness_qa`, `hybrid_qa`, `scenario_qa`,
`bug`, `research`

---

## Interception: cheap provider emits a strong tool

If the routing decision was `cheap` but the response contains a strong tool
call, the response is **discarded** and the exact same conversation is
immediately re-sent to the strong provider:

```python
# agent_runtime.py ~1184
if _adaptive_flat_enabled and _provider_tier == "cheap" \
        and any(t in STRONG_TOOLS for t in _next_tools_for_routing):
    response, tokens, thinking_blocks = call_llm(
        system_with_budget, conv_with_prefix, provider=_strong_prov
    )
    tool_calls = parse_tool_calls(response)
```

- The cheap response is never added to conversation history
- No synthetic messages injected — no history poisoning
- Costs one extra LLM call on the cheap provider (the wasted one)
- Logged as `[AdaptiveFlat] Cheap provider emitted strong tool(s) [...] -- escalating`
- Counted in `_adaptive_flat_stats["model_switches"]`

---

## Guard: cheap provider attempts TASK_COMPLETE

If the cheap provider's response contains `TASK_COMPLETE` (outside tool call
blocks), completion is suppressed and a guard message is injected into the
conversation:

```
[ADAPTIVE-FLAT GUARD]
A cheap/read-only routed loop attempted TASK_COMPLETE. Completion must be
confirmed by the strong provider. Continue with any needed tool calls, or
let the strong model confirm completion on the next loop.
```

- `last_tools` and `next_tools` are cleared so the next loop re-evaluates cleanly
- Counted in `_adaptive_flat_stats["cheap_completion_blocks"]`
- The strong provider picks up on the next loop and either completes or continues

---

## Stats and observability

Every agent running with adaptive flat accumulates stats written to
`data/agent_<task_id>_tokens.json`:

```json
{
  "adaptive_flat": {
    "cheap_loops": 12,
    "strong_loops": 8,
    "default_loops": 0,
    "model_switches": 4,
    "cheap_completion_blocks": 1,
    "last_provider": "minimax",
    "decisions": [
      {
        "loop": 3,
        "provider": "athena",
        "tier": "cheap",
        "reason": "lookahead_read_only",
        "last_tools": ["read_file"],
        "next_tools": ["search_code", "read_file"]
      }
    ]
  }
}
```

`decisions` keeps the last 40 entries. The full log contains
`[AdaptiveFlat] provider=... tier=... reason=... next_tools=... last_tools=...`
on every loop — use `swarm_agent_log(agent_id)` or tail the log file directly
to watch routing decisions in real time.

---

## Configuration

```json
{
  "adaptive_flat": true,
  "loop_model_routing": {
    "enabled": true,
    "fast_provider":   "athena",
    "strong_provider": "minimax"
  }
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `adaptive_flat` | `true` | Master toggle. Set `false` to revert to phase-based pipelines |
| `loop_model_routing.enabled` | `false` | Secondary enable path (either this or `adaptive_flat` activates routing) |
| `loop_model_routing.fast_provider` | `""` | Provider name for cheap/read-only loops |
| `loop_model_routing.strong_provider` | `""` | Provider name for write/commit loops |

If `fast_provider == strong_provider` or either is empty, routing is a no-op
and every call uses the default provider.

### Per-project override

```json
{
  "project_pipelines": {
    "my-game": {
      "_loop_model_routing": {
        "enabled": true,
        "fast_provider": "athena",
        "strong_provider": "minimax"
      }
    }
  }
}
```

### Per-task opt-out

```json
{
  "metadata": {
    "pipeline_mode": "fixed"
  }
}
```

Setting `pipeline_mode` to `"fixed"`, `"flat"`, or `"phase"` disables adaptive
flat for that task even when the global toggle is on.

### Live toggle (no restart required)

```bash
curl -X POST http://localhost:5001/api/adaptive-flat \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

Takes effect for the next agent spawned. Running agents are not affected.

---

## Known limitations

- **One wasted call per interception**: when a cheap provider emits a strong
  tool, you pay for both the cheap call and the strong re-run. High interception
  rates indicate the routing policy is mis-classifying the agent's intent.

- **Lookahead is from announced tools, not executed tools**: the next_tools
  signal comes from tool calls in the *current* LLM response, not what the
  agent will want to do after those tools run. One mismatch loop is normal.

- **TASK_COMPLETE guard adds a loop**: blocking a cheap completion costs one
  extra loop and one extra strong call to confirm. For very short tasks this
  overhead is noticeable.

- **No mid-stream cancellation**: the cheap provider's full response is always
  consumed before interception. Stream-level cancellation (which would save
  output tokens on the wasted call) would need to live in the proxy layer
  (headroom), not the swarm.

---

## Related

- `swarm/model_routing.py` — routing decision logic
- `swarm/agent_runtime.py` lines 1035–1209 — integration in the tool loop
- [Pipeline Configuration](pipeline-configuration.md) — phase-based alternative
- [Pipeline Probe](pipeline-probe.md) — tooling for measuring adaptive flat vs phase-based performance
