# Project Snapshots and Clones

Snapshots let you save and restore a project's full task graph state. Clones
let you fork a saved snapshot into a new project under a different name — the
primary mechanism for running pipeline experiments in parallel.

---

## Snapshots

A snapshot captures:
- All tasks (status, metadata, dependencies, attempts, descriptions)
- The project row (head task, closure status, pipeline config)
- A git tag at the current HEAD of the project repo

Snapshots are stored as JSON files in `data/snapshots/` and are referenced by
a short tag string (e.g. `pre-run12`, `baseline-2026-08`).

### Save a snapshot

```bash
curl -X POST http://localhost:5001/api/projects/my-game/snapshot \
  -H "Content-Type: application/json" \
  -d '{"tag": "pre-run12"}'
```

- Fails if any agent is actively running on the project
- Tag must match `[a-zA-Z0-9_-]+`
- Auto-generates a timestamped tag (`snap-20260802-143000`) if omitted

### List snapshots

```bash
curl http://localhost:5001/api/projects/my-game/snapshots
```

### Restore a snapshot

Resets the project's task graph in-place to the saved state and checks out the
git tag in the project repo. The project name stays the same.

```bash
curl -X POST http://localhost:5001/api/projects/my-game/restore \
  -H "Content-Type: application/json" \
  -d '{"tag": "pre-run12", "reset_status": true}'
```

- `reset_status: true` (default) — all restored tasks start as `pending`
- `reset_status: false` — tasks restore with their saved statuses (useful for
  restoring a mid-run snapshot for inspection without re-running everything)
- Fails if any agent is actively running

### Delete a snapshot

```bash
curl -X DELETE http://localhost:5001/api/projects/my-game/snapshots/pre-run12
```

Deletes the JSON file only — does not remove the git tag from the repo.

---

## Clones

Cloning forks a snapshot into a **new project** with a different name. The
clone gets:
- A full `git clone` of the source project repo into the workspace
- The source snapshot's task graph imported under the new project name
- An optional different pipeline configuration baked into each task's metadata

This is the standard way to run pipeline experiments: snapshot a baseline
project, then clone it N times with different pipeline variants.

### Basic clone

```bash
curl -X POST http://localhost:5001/api/projects/my-game/clone \
  -H "Content-Type: application/json" \
  -d '{
    "tag": "pre-run12",
    "new_name": "my-game-variant-a-run12"
  }'
```

### Clone with a pipeline preset

```bash
curl -X POST http://localhost:5001/api/projects/my-game/clone \
  -H "Content-Type: application/json" \
  -d '{
    "tag": "pre-run12",
    "new_name": "my-game-scout-work-run12",
    "pipeline": "variant-a"
  }'
```

Available pipeline presets:

| Preset | Phases | Notes |
|--------|--------|-------|
| `control` | `plan → scout → work → validate` | Full pipeline baseline |
| `variant-a` | `plan → work → validate` | No scout |
| `variant-b` | `plan → scout → synthesize → work → validate` | Extra synthesis step |
| `variant-c` | `scout → work → validate` | No plan |
| `variant-d` | random per-task | Each task gets a random valid phase order |
| `variant-e` | `scout → plan → work → validate` | Plan after scout |
| `variant-f` | (flat) | No pipeline phases; single loop with `flat_provider` |
| `adaptive-flat` | (flat + adaptive routing) | Flat transcript; per-loop model routing |

Or pass an explicit list:

```bash
"pipeline": ["scout", "work"]
```

### Clone with a flat provider

For `variant-f` (flat pipeline), specify which LLM provider to use:

```bash
curl -X POST http://localhost:5001/api/projects/my-game/clone \
  -H "Content-Type: application/json" \
  -d '{
    "tag": "pre-run12",
    "new_name": "my-game-flat-athena",
    "pipeline": "variant-f",
    "flat_provider": "athena"
  }'
```

### Clone with adaptive-flat model routing

```bash
curl -X POST http://localhost:5001/api/projects/my-game/clone \
  -H "Content-Type: application/json" \
  -d '{
    "tag": "pre-run12",
    "new_name": "my-game-adaptive-run12",
    "pipeline": "adaptive-flat",
    "loop_model_routing": {
      "early": "minimax",
      "mid":   "athena",
      "late":  "minimax"
    }
  }'
```

### Clone body parameters

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tag` | string | required | Snapshot tag to clone from |
| `new_name` | string | required | Name for the new project |
| `pipeline` | string or list | inherit | Pipeline preset name or explicit phase list |
| `flat_provider` | string | `"minimax"` | Provider for variant-f flat pipeline |
| `loop_model_routing` | object | `{}` | Adaptive-flat per-loop routing config |
| `quality_gate_mode` | string | `"final_tail"` | `"final_tail"`, `"run9_mid_final"`, or `"none"` |
| `experiment_id` | string | auto | Tag stamped into task metadata for grouping |
| `dependency_overrides` | object | `{}` | Override dep lists on specific source tasks |

---

## Typical experiment workflow

```bash
# 1. Run your baseline project to a stable state, then snapshot it
curl -X POST http://localhost:5001/api/projects/void-patrol/snapshot \
  -d '{"tag": "run12-baseline"}'

# 2. Clone into multiple pipeline variants
for variant in control variant-a variant-c variant-f; do
  curl -X POST http://localhost:5001/api/projects/void-patrol/clone \
    -H "Content-Type: application/json" \
    -d "{\"tag\":\"run12-baseline\",\"new_name\":\"void-patrol-${variant}-run12\",\"pipeline\":\"${variant}\"}"
done

# 3. Add the new projects to managed_projects and let the swarm run them
curl -X POST http://localhost:5001/api/managed-projects \
  -H "Content-Type: application/json" \
  -d '{"add": ["void-patrol-control-run12","void-patrol-variant-a-run12","void-patrol-variant-c-run12","void-patrol-variant-f-run12"]}'

# 4. Turn on auto mode and let agents fill all slots
curl -X POST http://localhost:5001/api/auto-mode -d '{"enabled": true}'

# 5. After all variants finish, compare results
python3 tools/probe_analytics.py
```

---

## Storage

Snapshot JSON files are saved to `data/snapshots/<project>__<tag>.json`. They
are not gitignored — commit them if you want to preserve experiment baselines
across machines.

Git tags are created in the source project's repo at snapshot time. The clone
checks out that tag before cloning, so the new project's git history starts
at the exact commit the snapshot was taken from.
