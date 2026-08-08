# LLM Providers

The swarm supports multiple LLM providers simultaneously. You can set a global
default, route specific phases or task types to different providers, register
custom or local providers, and configure automatic fallback when a provider
rate-limits.

---

## Built-in providers

| Name | Format | Env var | Default model | Notes |
|------|--------|---------|---------------|-------|
| `minimax` | `anthropic` | `MINIMAX_API_KEY` | MiniMax-M3 | Subscription; 200k context; default provider |
| `claude` | `anthropic_native` | `ANTHROPIC_API_KEY` | claude-sonnet-4-6 | Pay-per-token; native caching |
| `openrouter` | `openai` | `OPENROUTER_API_KEY` | claude-3.5-sonnet | Routes to many backends |
| `kimi` | `anthropic` | `KIMI_API_KEY` | k2p5 | Subscription; Anthropic-compatible wire format |
| `local` | `openai` | _(none)_ | your-local-model | Template for self-hosted servers |

Set `.env` in the project root:

```
MINIMAX_API_KEY=your_key
ANTHROPIC_API_KEY=your_key
OPENROUTER_API_KEY=your_key
KIMI_API_KEY=your_key
```

---

## Formats

`format` controls the HTTP wire protocol. Every provider uses one of three:

| Format | Auth header | Endpoint | Body shape |
|--------|-------------|----------|-----------|
| `anthropic` | `Authorization: Bearer <key>` | `/messages` | Anthropic messages API |
| `anthropic_native` | `x-api-key: <key>` + `anthropic-version: 2023-06-01` | `/messages` | Anthropic messages API |
| `openai` | `Authorization: Bearer <key>` | `/chat/completions` | OpenAI chat completions API |

Use `anthropic_native` only for the real Anthropic API — it sends the
`anthropic-version` header that Anthropic requires and other providers reject.
Use `anthropic` for any provider that speaks the Anthropic body format but
uses a Bearer token (MiniMax, Kimi, most proxies).

---

## Setting the active provider

### Via `config.json`

```json
{
  "llm_provider": "minimax"
}
```

### Live (no restart)

```bash
curl -X POST http://localhost:5001/api/provider \
  -H "Content-Type: application/json" \
  -d '{"provider": "claude"}'
```

### Check current provider

```bash
curl http://localhost:5001/api/provider
# → {"provider": "claude", "model": "claude-sonnet-4-6", "api_key_set": true}
```

---

## Overriding the model

Per-provider model override in `config.json`:

```json
{
  "llm_provider": "minimax",
  "llm_providers": {
    "minimax": {"model": "MiniMax-M3"},
    "claude":  {"model": "claude-opus-4-8"}
  }
}
```

Or live:

```bash
curl -X POST http://localhost:5001/api/provider \
  -H "Content-Type: application/json" \
  -d '{"provider": "claude", "model": "claude-opus-4-8"}'
```

---

## Registering a custom provider

Any provider can be registered if it speaks one of the three wire formats.
No restart required.

### Via API

```bash
curl -X POST http://localhost:5001/api/provider \
  -H "Content-Type: application/json" \
  -d '{
    "provider":    "athena",
    "base_url":    "http://headroom.local:8888/anthropic/v1",
    "model":       "MiniMax-M3",
    "format":      "anthropic",
    "api_key_env": "MINIMAX_API_KEY",
    "max_tokens":  32768
  }'
```

The registration is persisted to `config.json` under `llm_providers` so it
survives a restart.

### Via `config.json` (persisted from the start)

```json
{
  "llm_providers": {
    "athena": {
      "base_url":    "http://headroom.local:8888/anthropic/v1",
      "model":       "MiniMax-M3",
      "format":      "anthropic",
      "api_key_env": "MINIMAX_API_KEY",
      "max_tokens":  32768
    }
  }
}
```

### Provider config fields

| Field | Required | Description |
|-------|----------|-------------|
| `base_url` | yes | API endpoint root (e.g. `https://api.minimax.io/anthropic/v1`) |
| `model` | yes | Model ID sent in the request body |
| `format` | yes | Wire format: `anthropic`, `anthropic_native`, or `openai` |
| `api_key_env` | yes* | Environment variable name that holds the API key |
| `api_key` | alt | Inline API key (stored in env; use `api_key_env` for prod) |
| `max_tokens` | no | Max output tokens (default 8096) |
| `context_window` | no | Context window size in tokens (informational) |
| `thinking_budget` | no | Extended thinking token budget (MiniMax-specific; 0 = off) |

*Either `api_key_env` or `api_key` must be provided for non-built-in providers.

---

## Per-phase provider routing

Route specific pipeline phases to different providers without changing the
global default:

```json
{
  "scout_provider":      "athena",
  "work_provider":       "minimax",
  "plan_provider":       "athena",
  "synthesize_provider": "athena",
  "compaction_provider": "athena"
}
```

A phase with an empty or missing provider key falls back to `llm_provider`.

---

## Fallback providers

When a provider returns a rate-limit error (429), the swarm can automatically
retry with the next available provider in the fallback list.

```json
{
  "fallback_providers": ["minimax", "kimi", "claude"]
}
```

Resolution:
1. Primary provider 429s
2. `rotate_provider()` picks the first entry in `fallback_providers` that:
   - Is not the rate-limited provider
   - Has its API key set in the environment
3. The task retries with the new provider
4. On recovery the primary is not automatically restored — it stays on the
   fallback until the server restarts or the provider is manually changed

The currently rate-limited provider is tracked in-memory per monitor cycle.
`fallback_providers` acts as a priority list — put your most capable providers
first.

---

## Quota auto-pause

When `used_percent` reaches `quota_limit_percent` (default 90%), the quota watcher
thread automatically sets `suspended_for_quota=true`, stopping new agent spawns and
SIGSTOPping active agents. It lifts the suspension automatically once usage drops
back below the threshold.

To manually trigger the same paused state without hitting the quota threshold:

```bash
curl -X POST http://localhost:5001/api/auto-mode \
  -H "Content-Type: application/json" \
  -d '{"enabled": true, "suspend": true}'
```

Auto-mode stays enabled but suspended. The quota watcher will resume it
automatically. To fully disable auto-mode (watcher won't auto-resume):

```bash
curl -X POST http://localhost:5001/api/auto-mode \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

**Note:** `quota_limit_percent` applies to MiniMax usage regardless of which
provider (`llm_provider`) is currently active. The quota check always reads
directly from the MiniMax API using `MINIMAX_API_KEY`.

---

## Local fallback on quota exhaustion

When the quota limit is hit (`quota_limit_percent` threshold reached), you can
fall back to a local self-hosted server instead of stopping:

```json
{
  "local_fallback_on_quota": true,
  "llm_providers": {
    "local": {
      "base_url": "http://localhost:10098/v1",
      "model":    "your-local-model",
      "format":   "openai",
      "max_tokens": 32768
    }
  }
}
```

When the quota check fires, the monitor switches `LLM_PROVIDER` to `"local"`
for subsequent spawns until quota recovers.

---

## Adaptive flat routing (per-call provider switching)

When `adaptive_flat` is enabled, each LLM call is routed to either a
`fast_provider` (cheap, for read-only tool loops) or a `strong_provider`
(capable, for write/commit loops) — independently of the global `llm_provider`.

```json
{
  "adaptive_flat": true,
  "loop_model_routing": {
    "enabled":         true,
    "fast_provider":   "athena",
    "strong_provider": "minimax"
  }
}
```

See [adaptive-flat.md](adaptive-flat.md) for the full routing decision logic.

---

## Thinking / extended reasoning

MiniMax and Claude support extended thinking (chain-of-thought before
answering). Enable it per task type:

```json
{
  "thinking_task_types": ["bug", "qa"],
  "thinking_budget":     10000
}
```

`thinking_budget` is in tokens. Set to `0` to disable. High budgets
significantly increase output token usage and worsen rate limits — start at
5000 and measure before raising.

Toggle live:

```bash
# Enable
curl -X POST http://localhost:5001/api/thinking \
  -H "Content-Type: application/json" \
  -d '{"enabled": true, "budget_tokens": 8000, "task_types": ["bug"]}'

# Disable
curl -X POST http://localhost:5001/api/thinking \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'

# Check
curl http://localhost:5001/api/thinking
```

---

## Viewing all providers

```bash
curl http://localhost:5001/api/providers
```

Returns every registered provider with its config (API key value is redacted;
`api_key_set` indicates whether the env var is populated).

---

## Common setups

### Single subscription provider (simplest)

```json
{
  "llm_provider": "minimax"
}
```

`.env`: `MINIMAX_API_KEY=your_key`

### Cheap reads, strong writes (adaptive flat)

```json
{
  "llm_provider": "minimax",
  "adaptive_flat": true,
  "loop_model_routing": {
    "enabled":         true,
    "fast_provider":   "athena",
    "strong_provider": "minimax"
  },
  "llm_providers": {
    "athena": {
      "base_url":    "http://headroom.local:8888/anthropic/v1",
      "model":       "MiniMax-M3",
      "format":      "anthropic",
      "api_key_env": "MINIMAX_API_KEY",
      "max_tokens":  32768
    }
  }
}
```

### Redundant providers with fallback

```json
{
  "llm_provider":       "minimax",
  "fallback_providers": ["minimax", "kimi", "claude"]
}
```

`.env`: all three API keys set.

### Phase routing — fast scout, strong work

```json
{
  "llm_provider":   "minimax",
  "scout_provider": "athena",
  "work_provider":  "minimax"
}
```

---

## Related

- [Adaptive Flat](adaptive-flat.md) — per-call routing between fast and strong providers
- [Pipeline Configuration](pipeline-configuration.md) — per-phase and per-project provider overrides
- `swarm/provider_utils.py` — `LLM_PROVIDERS` defaults and `TOKEN_PRICING` table
- `swarm/api_config.py` — `/api/provider`, `/api/providers`, `/api/thinking` endpoints
- `swarm/orchestrator.py:rotate_provider()` — fallback rotation logic
