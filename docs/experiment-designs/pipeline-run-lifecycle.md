# Pipeline Experiment Run Lifecycle

> **Tooling**: Use the `/swarm-experiment` Claude Code skill (`~/.claude/skills/swarm-experiment/SKILL.md`) to design and launch new experiment runs. It codifies arm patterns, flat adaptive config, run history, and the checklist below. Invoke with `/swarm-experiment [run name and hypothesis]`.

This is the repeatable process for freezing a pipeline experiment run, preserving
its data, disabling the old projects, and starting a fresh batch.

The goal is simple: the past is immutable, the future can change. Once a run is
saved, do not mutate its tasks or project folders except to add clearly labeled
post-run notes.

## Names Used Here

Use these names consistently:

- `SOURCE_PROJECT`: source project cloned for the experiment, for example
  `void-patrol`
- `SOURCE_TAG`: source snapshot tag, for example `v0.0.0-scaffold`
- `OLD_EXPERIMENT_ID`: run being frozen, for example
  `void-patrol-pipeline-ab-run1-20260606`
- `NEW_EXPERIMENT_ID`: new run being started, for example
  `void-patrol-pipeline-ab-run2-20260606`
- `OLD_PROJECTS`: all projects in the old run
- `NEW_PROJECTS`: all projects in the new run

Run projects should use a run suffix:

```text
void-patrol-control-run2
void-patrol-variant-a-run2
void-patrol-variant-b-run2
void-patrol-variant-c-run2
void-patrol-variant-d-run2
void-patrol-variant-e-run2
void-patrol-variant-f-run2
```

## 1. Freeze Scheduling

First stop the scheduler from starting new work while the archive is being
prepared.

```bash
curl -fsS -X POST http://localhost:5001/api/auto-mode \
  -H 'Content-Type: application/json' \
  -d '{"enabled": false}'
```

Pause every project in the run in both live config and `config.json`:

```bash
python3 - <<'PY'
import json, urllib.request
from pathlib import Path

projects = [
    "void-patrol-control-run1",
    "void-patrol-variant-a-run1",
    "void-patrol-variant-b-run1",
    "void-patrol-variant-c-run1",
    "void-patrol-variant-d-run1",
    "void-patrol-variant-f-run1",
]

url = "http://localhost:5001/api/config"
cfg = json.load(urllib.request.urlopen(url, timeout=10))
paused = set(cfg.get("paused_projects") or [])
paused.update(projects)
cfg["paused_projects"] = sorted(paused)
req = urllib.request.Request(
    url,
    data=json.dumps(cfg).encode(),
    method="POST",
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=10):
    pass

path = Path("config.json")
disk = json.loads(path.read_text())
disk_paused = set(disk.get("paused_projects") or [])
disk_paused.update(projects)
disk["paused_projects"] = sorted(disk_paused)
path.write_text(json.dumps(disk, indent=2, sort_keys=True) + "\n")
PY
```

Check active agents. Prefer waiting for in-flight agents to finish. Killing them
is allowed for a shakedown run, but record it in the run README.

```bash
curl -fsS http://localhost:5001/api/agents | python3 -m json.tool
```

## 2. Capture Run State

Create an immutable archive directory:

```bash
RUN_ID="void-patrol-pipeline-ab-run1-20260606"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="data/experiments/${RUN_ID}-frozen-${STAMP}"

mkdir -p "$ARCHIVE"/{events,pipeline_states,phase_artifacts,agent_logs,manifests}
```

Copy the durable event stream and controller state:

```bash
cp "data/experiments/${RUN_ID}/events.jsonl" "$ARCHIVE/events/events.jsonl" 2>/dev/null || true
cp data/experiment_metrics.jsonl "$ARCHIVE/experiment_metrics.snapshot.jsonl" 2>/dev/null || true
cp data/swarm.db "$ARCHIVE/swarm.db"
cp config.json "$ARCHIVE/config.json"
```

Copy pipeline and phase artifacts for the run by reading the experiment event
log. This catches `agent_*_pipeline.json` plus sibling phase files such as
`_plan.json`, `_scout.json`, `_synthesize.json`, `_work.json`, and
`_validate.json`.

```bash
python3 - <<'PY'
import json, shutil
from pathlib import Path

run_id = "void-patrol-pipeline-ab-run1-20260606"
archive = sorted(Path("data/experiments").glob(f"{run_id}-frozen-*"))[-1]
events = Path("data/experiments") / run_id / "events.jsonl"

copied = set()
if events.exists():
    for line in events.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        state_path = record.get("pipeline_state_path")
        if not state_path:
            continue
        path = Path(state_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        candidates = [path]
        if path.name.endswith("_pipeline.json"):
            stem = path.name[:-len("_pipeline.json")]
            candidates.extend(path.parent.glob(stem + "_*.json"))
        for candidate in candidates:
            if candidate.exists() and candidate not in copied:
                target_dir = "pipeline_states" if candidate.name.endswith("_pipeline.json") else "phase_artifacts"
                shutil.copy2(candidate, archive / target_dir / candidate.name)
                copied.add(candidate)

print(f"copied_artifacts={len(copied)}")
PY
```

Snapshot task/project status into a manifest:

```bash
python3 - <<'PY'
import json, urllib.request, collections
from pathlib import Path

run_id = "void-patrol-pipeline-ab-run1-20260606"
projects = [
    "void-patrol-control-run1",
    "void-patrol-variant-a-run1",
    "void-patrol-variant-b-run1",
    "void-patrol-variant-c-run1",
    "void-patrol-variant-d-run1",
    "void-patrol-variant-f-run1",
]
archive = sorted(Path("data/experiments").glob(f"{run_id}-frozen-*"))[-1]
tasks = json.load(urllib.request.urlopen(
    "http://localhost:5001/api/tasks?include_completed=true", timeout=20
))["tasks"]

manifest = {"experiment_id": run_id, "projects": {}}
for project in projects:
    subset = [
        t for t in tasks
        if (t.get("project") or t.get("project_name") or (t.get("metadata") or {}).get("project")) == project
    ]
    manifest["projects"][project] = {
        "task_count": len(subset),
        "status_counts": dict(collections.Counter(t.get("status", "unknown") for t in subset)),
        "tasks": subset,
    }

(archive / "manifests" / "tasks.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
print(json.dumps({p: manifest["projects"][p]["status_counts"] for p in projects}, indent=2))
PY
```

Copy agent logs for old-run agents from the history table:

```bash
python3 - <<'PY'
import json, shutil, urllib.request
from pathlib import Path

run_id = "void-patrol-pipeline-ab-run1-20260606"
projects = {
    "void-patrol-control-run1",
    "void-patrol-variant-a-run1",
    "void-patrol-variant-b-run1",
    "void-patrol-variant-c-run1",
    "void-patrol-variant-d-run1",
    "void-patrol-variant-f-run1",
}
archive = sorted(Path("data/experiments").glob(f"{run_id}-frozen-*"))[-1]
history = json.load(urllib.request.urlopen("http://localhost:5001/api/history", timeout=20))["agents"]

copied = 0
for agent in history:
    if agent.get("project") not in projects:
        continue
    log_path = agent.get("log_path")
    if not log_path:
        continue
    path = Path(log_path)
    if path.exists():
        shutil.copy2(path, archive / "agent_logs" / path.name)
        copied += 1

print(f"copied_logs={copied}")
PY
```

Add a human README before considering the run saved:

```bash
cat > "$ARCHIVE/README.md" <<'EOF'
# Pipeline Experiment Run Archive

Status: frozen.

Use this run for:
- qualitative behavior review
- artifact inspection
- preliminary metrics

Do not use this run as clean confirmatory evidence if agents were already active
while phase code, metadata propagation, or project pause state changed.

Notes:
- Fill in why the run was stopped.
- Fill in any known contamination.
- Fill in whether active agents were allowed to finish or were stopped.
EOF
```

## 3. Archive Project Folders

If the old project folders are no longer needed live, move them under the shared
workspace archive. Moving is preferable to deleting because it preserves the git
working trees exactly as agents left them.

```bash
RUN_ID="void-patrol-pipeline-ab-run1-20260606"
DEST="~/workspace/_archive/${RUN_ID}"
mkdir -p "$DEST"

for project in \
  void-patrol-control-run1 \
  void-patrol-variant-a-run1 \
  void-patrol-variant-b-run1 \
  void-patrol-variant-c-run1 \
  void-patrol-variant-d-run1 \
  void-patrol-variant-f-run1
do
  if [ -d "~/workspace/$project" ]; then
    mv "~/workspace/$project" "$DEST/"
  fi
done
```

If active agents are still running, do not move their project folders. Either
wait, or stop the agents and mark the run as interrupted in the README.

## 4. Disable The Old Run

Keep old projects paused. Optionally remove them from `managed_projects` only
after the archive is complete and no active agents reference them.

Minimum disable state:

- old projects present in `paused_projects`
- no active agents for old projects
- archive directory exists
- `config.json` copied into the archive
- project folders moved or explicitly left in place with a note

Optional managed-project removal:

```bash
python3 - <<'PY'
import json, urllib.request
from pathlib import Path

old_projects = {
    "void-patrol-control-run1",
    "void-patrol-variant-a-run1",
    "void-patrol-variant-b-run1",
    "void-patrol-variant-c-run1",
    "void-patrol-variant-d-run1",
    "void-patrol-variant-f-run1",
}

url = "http://localhost:5001/api/managed-projects"
cfg = json.load(urllib.request.urlopen(url, timeout=10))
cfg["managed_projects"] = [p for p in cfg.get("managed_projects", []) if p not in old_projects]
cfg["paused_projects"] = sorted(set(cfg.get("paused_projects") or []) | old_projects)
req = urllib.request.Request(
    url,
    data=json.dumps(cfg).encode(),
    method="POST",
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=10):
    pass

path = Path("config.json")
disk = json.loads(path.read_text())
disk["managed_projects"] = [p for p in disk.get("managed_projects", []) if p not in old_projects]
disk["paused_projects"] = sorted(set(disk.get("paused_projects") or []) | old_projects)
path.write_text(json.dumps(disk, indent=2, sort_keys=True) + "\n")
PY
```

## 5. Start A Fresh Batch

Keep auto-mode off while cloning so the batch starts from the same initial
condition.

```bash
curl -fsS -X POST http://localhost:5001/api/auto-mode \
  -H 'Content-Type: application/json' \
  -d '{"enabled": false}'
```

### Recommended Smaller Variant Set

For future runs, prefer a smaller default batch unless there is enough budget
and capacity to run every historical variant. Run 4 showed enough infra pressure
that seven arms can spend too much budget on low-signal comparisons.

Default future set:

- `control`: keep the baseline anchor.
- `variant-a`: keep `plan -> work -> validate` because it is the intuitive
  structured hypothesis and exposes whether planning is actually useful.
- `variant-d`: keep the randomized/chaos arm because it has been unexpectedly
  strong and can reveal assumptions fixed-order variants miss.
- One rotating challenger: start with `scout -> plan -> work -> validate`.
  The hypothesis is that scout-first gives the planner real project state before
  it forms a plan.

Pause or skip the other variants unless they carry a specific hypothesis for
that run. In later runs, keep `control`, the best structured variant, `variant-d`,
and one new challenger derived from either chaos successes or observed failure
modes.

For analysis, treat the core question as:

```text
Does any fixed phase order beat chaos/randomization, and under what task
conditions?
```

Clone each arm from the same source snapshot and give every clone the same new
experiment id.

```bash
SOURCE_PROJECT="void-patrol"
SOURCE_TAG="v0.0.0-scaffold"
EXPERIMENT_ID="void-patrol-pipeline-ab-run2-20260606"

clone_arm() {
  local new_name="$1"
  local pipeline="$2"
  curl -fsS -X POST "http://localhost:5001/api/projects/${SOURCE_PROJECT}/clone" \
    -H 'Content-Type: application/json' \
    -d "{
      \"tag\": \"${SOURCE_TAG}\",
      \"new_name\": \"${new_name}\",
      \"pipeline\": \"${pipeline}\",
      \"experiment_id\": \"${EXPERIMENT_ID}\"
    }" | python3 -m json.tool
}

clone_arm "void-patrol-control-run2" "control"
clone_arm "void-patrol-variant-a-run2" "variant-a"
clone_arm "void-patrol-variant-b-run2" "variant-b"
clone_arm "void-patrol-variant-c-run2" "variant-c"
clone_arm "void-patrol-variant-d-run2" "variant-d"
clone_arm "void-patrol-variant-e-run2" "variant-e"

curl -fsS -X POST "http://localhost:5001/api/projects/${SOURCE_PROJECT}/clone" \
  -H 'Content-Type: application/json' \
  -d "{
    \"tag\": \"${SOURCE_TAG}\",
    \"new_name\": \"void-patrol-variant-f-run2\",
    \"pipeline\": \"variant-f\",
    \"flat_provider\": \"minimax\",
    \"experiment_id\": \"${EXPERIMENT_ID}\"
  }" | python3 -m json.tool
```

Immediately pause the new projects until metadata is verified:

```bash
python3 - <<'PY'
import json, urllib.request
from pathlib import Path

projects = [
    "void-patrol-control-run2",
    "void-patrol-variant-a-run2",
    "void-patrol-variant-b-run2",
    "void-patrol-variant-c-run2",
    "void-patrol-variant-d-run2",
    "void-patrol-variant-e-run2",
    "void-patrol-variant-f-run2",
]
url = "http://localhost:5001/api/config"
cfg = json.load(urllib.request.urlopen(url, timeout=10))
paused = set(cfg.get("paused_projects") or [])
paused.update(projects)
cfg["paused_projects"] = sorted(paused)
req = urllib.request.Request(
    url,
    data=json.dumps(cfg).encode(),
    method="POST",
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=10):
    pass

path = Path("config.json")
disk = json.loads(path.read_text())
disk_paused = set(disk.get("paused_projects") or [])
disk_paused.update(projects)
disk["paused_projects"] = sorted(disk_paused)
path.write_text(json.dumps(disk, indent=2, sort_keys=True) + "\n")
PY
```

## 6. Verify Before Unpausing

Check that every new task has the correct experiment id, variant, pipeline, and
source linkage. Variant D should show multiple phase orders.

```bash
python3 - <<'PY'
import json, urllib.request, collections

experiment_id = "void-patrol-pipeline-ab-run2-20260606"
projects = [
    "void-patrol-control-run2",
    "void-patrol-variant-a-run2",
    "void-patrol-variant-b-run2",
    "void-patrol-variant-c-run2",
    "void-patrol-variant-d-run2",
    "void-patrol-variant-e-run2",
    "void-patrol-variant-f-run2",
]
tasks = json.load(urllib.request.urlopen(
    "http://localhost:5001/api/tasks?include_completed=true", timeout=20
))["tasks"]

for project in projects:
    subset = [t for t in tasks if t.get("project") == project]
    bad = []
    phase_orders = collections.Counter()
    for task in subset:
        meta = task.get("metadata") or {}
        if meta.get("experiment_id") != experiment_id:
            bad.append((task.get("id"), "experiment_id", meta.get("experiment_id")))
        if not meta.get("source_task_id"):
            bad.append((task.get("id"), "missing source_task_id", None))
        phase_orders[tuple(meta.get("phase_order") or meta.get("pipeline") or [])] += 1
    print(project, "tasks", len(subset), "bad", len(bad), "phase_orders", dict(phase_orders))
    if bad[:5]:
        print("  sample_bad", bad[:5])
PY
```

Also verify dependency integrity:

```bash
for project in \
  void-patrol-control-run2 \
  void-patrol-variant-a-run2 \
  void-patrol-variant-b-run2 \
  void-patrol-variant-c-run2 \
  void-patrol-variant-d-run2 \
  void-patrol-variant-e-run2 \
  void-patrol-variant-f-run2
do
  curl -fsS "http://localhost:5001/api/dependencies/integrity?project=${project}&include_history=true" \
    | python3 -m json.tool \
    | sed -n '1,80p'
done
```

Do not unpause unless:

- all six projects exist and are managed
- every task has the new `experiment_id`
- every task has `source_task_id` and `source_project`
- fixed variants have the intended `phase_order`
- variant D has randomized task-level `phase_order`
- variant E has `scout -> plan -> work -> validate`
- variant F has an empty pipeline and `flat_provider`
- recovery/meta tasks created during the run are pinned to stable recovery/meta
  pipelines while preserving experiment labels; they must not inherit variant D
  randomized phase order unless the run is explicitly testing chaotic recovery
- dependency integrity shows no missing dependencies or cycles
- auto-mode is still disabled

## 7. Unpause And Start

Clear pause flags for the new batch and re-enable auto-mode:

```bash
python3 - <<'PY'
import json, urllib.request
from pathlib import Path

projects = [
    "void-patrol-control-run2",
    "void-patrol-variant-a-run2",
    "void-patrol-variant-b-run2",
    "void-patrol-variant-c-run2",
    "void-patrol-variant-d-run2",
    "void-patrol-variant-e-run2",
    "void-patrol-variant-f-run2",
]
url = "http://localhost:5001/api/config"
cfg = json.load(urllib.request.urlopen(url, timeout=10))
paused = set(cfg.get("paused_projects") or [])
for project in projects:
    paused.discard(project)
cfg["paused_projects"] = sorted(paused)
req = urllib.request.Request(
    url,
    data=json.dumps(cfg).encode(),
    method="POST",
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=10):
    pass

path = Path("config.json")
disk = json.loads(path.read_text())
disk_paused = set(disk.get("paused_projects") or [])
for project in projects:
    disk_paused.discard(project)
disk["paused_projects"] = sorted(disk_paused)
path.write_text(json.dumps(disk, indent=2, sort_keys=True) + "\n")
PY

curl -fsS -X POST http://localhost:5001/api/auto-mode \
  -H 'Content-Type: application/json' \
  -d '{"enabled": true}'
```

Watch the first few tasks:

```bash
curl -fsS http://localhost:5001/api/agents | python3 -m json.tool
curl -fsS "http://localhost:5001/api/tasks?include_completed=false" | python3 -m json.tool
tail -f data/swarm_api.log
```

## 8. First-Hour Checks

During the first hour, check:

- per-run events are being appended:
  `data/experiments/<NEW_EXPERIMENT_ID>/events.jsonl`
- per-phase artifacts are appearing:
  `data/agent_<task_id>_plan.json`, `_scout.json`, `_work.json`, `_validate.json`
- no task in the new batch is missing experiment metadata
- variant D follow-on tasks receive fresh randomized phase orders
- research feeders and other recovery/meta tasks do not receive randomized
  phase orders by default; if any do, pause and document the run as
  recovery-contaminated
- no old run project becomes unpaused
- no old project folder is being written by active agents

If any of those fail, pause the new batch, archive it as a shakedown, document
the contamination, fix the controller, and start the next run with a new
`experiment_id`.
