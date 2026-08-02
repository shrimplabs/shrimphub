# Pipeline Configuration

Every task runs through a **pipeline** — an ordered list of phases that
structure how the agent approaches work. The default pipelines are sensible
for most situations, but you can override them globally, per project, per task
type, or per individual task.

---

## How the pipeline works

Each phase is a named segment of the agent's tool loop. Within a phase the
agent calls the LLM repeatedly (up to a loop limit) using a system prompt
tailored to that phase's goal. At the phase boundary the conversation resets
and the next phase begins with a fresh context containing only the handoff
from the previous phase.

Available phases:

| Phase | Purpose |
|-------|---------|
| `plan` | Read the codebase, form a strategy, output a concrete work plan |
| `scout` | Fast read-only exploration — find the relevant files and surface findings |
| `synthesize` | Merge scout findings into actionable work packets |
| `work` | Write code, make changes, run tests |
| `validate` | Run post-task checks (Godot headless, pytest, etc.) |

**Adaptive flat** is a special mode with no phases — a single continuous loop
where the model provider is swapped per-call based on what tools were just
used (read-only calls → cheap/fast model; write/commit calls → strong model).

---

## Default pipelines

If no override is set, these defaults apply:

| Task type | Default pipeline |
|-----------|-----------------|
| `feature` | `scout → work → validate` |
| `bug` | `scout → work → validate` |
| `polish` | `scout → work → validate` |
| `art_pass` | `work → validate` |
| `refactor` | `scout → plan → work → validate` |
| everything else | `[]` (flat loop, no phases) |

As of run-12, **adaptive flat** is the system-wide default for all tasks
(`ADAPTIVE_FLAT = True` in `swarm/agent_runtime.py`). The phase-based
defaults above apply when adaptive flat is explicitly disabled.

---

## Resolution priority

When a task runs, the pipeline is resolved in this order (first match wins):

1. **Task metadata** — `pipeline` key set at task creation time (used for A/B experiments via clone)
2. **Per-project, per-task-type** — `project_pipelines.<project>.<task_type>` in `config.json`
3. **Per-project wildcard** — `project_pipelines.<project>.*` in `config.json`
4. **Global per-task-type** — `pipelines.<task_type>` in `config.json`
5. **Built-in default** — the table above
6. **Adaptive flat** — if `ADAPTIVE_FLAT=True` (the current default), phases are skipped entirely

---

## Global overrides (`config.json`)

### Override pipelines for all projects

```json
{
  "pipelines": {
    "bug":     ["plan", "scout", "work", "validate"],
    "feature": ["scout", "work", "validate"],
    "refactor": ["plan", "scout", "work", "validate"]
  }
}
```

### Global per-phase provider routing

Route specific phases to different LLM providers:

```json
{
  "scout_provider":      "minimax",
  "work_provider":       "athena",
  "plan_provider":       "minimax",
  "synthesize_provider": "minimax",
  "compaction_provider": "minimax"
}
```

Leave a key empty (`""`) to use the global `llm_provider` for that phase.

### Scout loop limits by task type

Control how many loops the scout phase gets before handing off to work:

```json
{
  "scout_loops": {
    "bug":     10,
    "feature": 20,
    "refactor": 25
  }
}
```

Defaults: `bug=15`, `feature=25`, `refactor=20`, `polish=15`, `art_pass=15`.

### Adaptive flat model routing

Configure the tool-based routing policy used in adaptive flat mode:

```json
{
  "loop_model_routing": {
    "enabled":   true,
    "strong":    "minimax",
    "fast":      "athena"
  }
}
```

When a loop's prior tool calls were all read-only (`read_file`, `search_code`,
etc.), the next call uses `fast`. When any write/commit/vision tool was called,
it uses `strong`. If a tool type is unknown, defaults to `strong`.

---

## Per-project overrides (`project_pipelines` in `config.json`)

The `project_pipelines` key lets you configure a specific project differently
from the rest of the swarm without touching global defaults.

```json
{
  "project_pipelines": {
    "my-game": {
      "bug":     ["scout", "work", "validate"],
      "feature": ["plan", "scout", "work", "validate"],
      "*":       ["scout", "work"]
    }
  }
}
```

The `"*"` key is a wildcard — it applies to any task type not explicitly listed.

### Per-project provider overrides

```json
{
  "project_pipelines": {
    "my-game": {
      "_work_provider": "athena",
      "_flat_provider": "minimax",
      "_pipeline_mode": "adaptive_flat"
    }
  }
}
```

Special keys (prefixed with `_`):

| Key | Description |
|-----|-------------|
| `_work_provider` | Provider for the work phase (overrides global `work_provider`) |
| `_flat_provider` | Provider for flat-loop mode (no phases) |
| `_pipeline_mode` | Set to `"adaptive_flat"` to enable adaptive flat for this project |
| `_phase_loop_limits` | Per-phase loop limit overrides (see below) |
| `_loop_model_routing` | Adaptive flat routing config for this project |

### Per-project phase loop limits

Limit how many loops each phase can run before being cut off:

```json
{
  "project_pipelines": {
    "my-game": {
      "_phase_loop_limits": {
        "plan":  15,
        "scout": 20,
        "work":  80
      }
    }
  }
}
```

---

## Per-task pipeline (at creation time)

Set the pipeline directly on a task when creating it. This is how the clone
system wires pipeline variants into A/B experiments — each cloned task gets
its pipeline baked into its metadata.

```bash
curl -X POST http://localhost:5001/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "project":     "my-game",
    "type":        "bug",
    "description": "Fix the collision bug",
    "metadata": {
      "pipeline":      ["scout", "work", "validate"],
      "flat_provider": "athena",
      "pipeline_mode": "adaptive_flat"
    }
  }'
```

Task-level metadata keys:

| Key | Description |
|-----|-------------|
| `pipeline` | Explicit phase list, e.g. `["scout","work","validate"]`. Empty list `[]` = flat loop |
| `pipeline_mode` | `"adaptive_flat"` to enable adaptive routing for this task |
| `flat_provider` | Provider when running flat (no phases) |
| `loop_model_routing` | Adaptive routing config merged with project/global routing |
| `phase_loop_limits` | Per-phase loop limits for this task only |

---

## Choosing a pipeline

| Situation | Recommended pipeline |
|-----------|---------------------|
| Simple bug fix, cause is known | `["work", "validate"]` |
| Bug with unknown root cause | `["scout", "work", "validate"]` (default) |
| Large feature, needs a plan | `["plan", "scout", "work", "validate"]` |
| Large refactor | `["scout", "plan", "work", "validate"]` |
| Cost-sensitive, mixed workload | `adaptive_flat` with `fast=athena`, `strong=minimax` |
| Speed test / cheap provider only | `[]` (flat) + `_flat_provider: "athena"` |
| A/B pipeline experiment | Use snapshot + clone with preset (see [snapshots-and-clones.md](snapshots-and-clones.md)) |

---

## Disabling adaptive flat (reverting to phase-based)

Adaptive flat is the current system default. To disable it globally and use
explicit phase pipelines instead:

```json
{
  "pipelines": {
    "bug":     ["scout", "work", "validate"],
    "feature": ["scout", "work", "validate"],
    "refactor": ["scout", "plan", "work", "validate"]
  }
}
```

Setting explicit pipelines in `config.json` takes priority over the
`ADAPTIVE_FLAT` default. You can also disable it for a single project:

```json
{
  "project_pipelines": {
    "my-game": {
      "_pipeline_mode": "fixed",
      "bug":     ["scout", "work", "validate"],
      "feature": ["plan", "scout", "work", "validate"]
    }
  }
}
```

---

## Related

- [Snapshots and Clones](snapshots-and-clones.md) — running pipeline A/B experiments across project clones
- [task-pipeline-architecture.md](task-pipeline-architecture.md) — original design rationale for the phase model
- `swarm_runner.py` lines 958–1030 — full resolution logic
- `swarm/model_routing.py` — adaptive flat routing policy implementation
