# Pipeline A/B Experiment Design

## Goal

Empirically determine the optimal pipeline phase composition and ordering for
swarm agents. We are operating at the edge of knowledge — no published research
covers dynamic phase ordering for LLM agent pipelines on real software tasks.
This experiment will generate that data.

## Hypothesis

We do not know which of the following assumptions are true:
- Planning before scouting is better than scouting before planning
- Scout phase reduces work loop count meaningfully
- Synthesize phase improves code quality or reduces work loops
- More phases = better outcomes (vs. added latency cancelling gains)

## Experiment Design

### Structure

Multiple identical projects run the same task DAG under different pipeline
configurations. One variant per project — not per-task — so each project
accumulates consistent data under a single treatment.

### Projects

Clone a stable, well-defined source project N times:
- `{project}-control`
- `{project}-variant-a`
- `{project}-variant-b`
- `{project}-variant-c`

Each clone is a separate git repo, registered with the swarm, seeded with
an identical task DAG from the same task list.

### Variants

| Label | Preset key | Pipeline | Hypothesis being tested |
|-------|------------|----------|------------------------|
| Control | `control` | `plan → scout → work → validate` | Current structured baseline |
| Variant A | `variant-a` | `plan → work → validate` | Is scout overhead worth it? |
| Variant B | `variant-b` | `plan → scout → synthesize → work → validate` | Does synthesize improve work quality? |
| Variant C | `variant-c` | `scout → work → validate` | Is planning necessary, or does exploration suffice? |
| Variant D | `variant-d` | random phase ordering per task (from pool: `plan`, `scout`, `work`, `validate`) | Exploratory chaos arm: what does structure beat, and which surprising orders break our assumptions? |
| Variant E | `variant-e` | `scout → plan → work → validate` | Does reconnaissance before planning produce more grounded plans without synthesize overhead? |
| Variant F | `variant-f` | `work` only, single model (MiniMax), no phases | Null hypothesis — flat loop baseline, no pipeline overhead |

**Variant D detail**: at clone time, each task in the project is assigned an independently randomised phase ordering drawn from the full phase pool. The ordering is baked into `task.metadata.pipeline` and recorded in `task.metadata.phase_order` in the experiment metrics. The clone also stores a project-level exploratory experiment config, so any new tasks created later by graph reflection, validation, recovery, or agent task creation receive fresh randomized phase orders instead of falling back to a default pipeline.

Variant D is not interpreted as an optimal valid workflow. It is an exploratory/hypothesis-breaking arm. Analysis should split chaos runs by exact phase order and by `is_valid_order` / `invalidity_reason` before drawing conclusions.

**Variant E detail**: scout runs before plan. This tests the hypothesis that a read-only reconnaissance pass can gather project-specific evidence first, then let the planner produce a more grounded implementation plan. It should be compared directly against Control (`plan → scout → work → validate`) and Variant C (`scout → work → validate`) to separate the value of planning from the value of scout ordering.

**Variant F detail**: no pipeline is run. The agent uses a single MiniMax model in the legacy continuous work loop — identical to pre-pipeline behaviour. Used to establish whether the pipeline adds value at all.

### Run 7 focused variants

Run 7 narrows the comparison to variants that remain useful after the run 4
and run 6 findings. It keeps one flat baseline, two simple structured
pipelines, one fixed chaos-derived order, and one adaptive routing arm.

Experiment id: `void-patrol-pipeline-ab-run7-20260613`

| Project | Variant | Main feature pipeline | Purpose |
|---------|---------|-----------------------|---------|
| `void-patrol-control-run7` | `variant-f` | flat legacy loop (`[]`) | Baseline against the run 4 success case; tests whether flat MiniMax-style continuous work remains competitive. |
| `void-patrol-variant-c-run7` | `variant-c` | `scout -> work -> validate` | Simple scout-first structured pipeline; tests whether scouting plus direct work beats planning overhead. |
| `void-patrol-variant-e-run7` | `variant-e` | `scout -> plan -> work -> validate` | Scout-grounded planning; tests whether planning is useful after evidence has been gathered. |
| `void-patrol-variant-g-run7` | `variant-g` | `work -> scout -> plan -> validate` | Fixed chaos-derived ordering from prior observations; tests whether surprising chaos orders retain value when made repeatable. |
| `void-patrol-adaptive-run7` | `variant-adaptive` | task-type dependent | Tests whether phase order should depend on task type rather than project-wide treatment. |

Run 7 also adds three post-feature quality tails to every arm:

```text
feature 8 -> art_pass -> polish -> harness_qa
          -> art_pass -> polish -> harness_qa
          -> art_pass -> polish -> harness_qa
```

Tail tasks are intentionally included because earlier runs showed that playable
game quality depends heavily on art, UX polish, live QA, and integration passes,
not only on feature completion.

Tail pipeline rules:

- Non-adaptive arms pin `art_pass`, `polish`, and `harness_qa` to
  `scout -> work -> validate`.
- The adaptive arm uses `scout -> work -> validate` for `feature`, `art_pass`,
  and `harness_qa`.
- The adaptive arm uses `scout -> plan -> work -> validate` for `polish` and
  `refactor`.
- The adaptive arm uses `plan -> work -> validate` for `bug`.

### Run 7 cleanliness note

Preliminary outlier audit on 2026-06-13 suggests run 7 may be analytically
clean if provider outages are treated as censored infrastructure events rather
than pipeline failures.

Observed terminal outliers:

| Project | Task shape | Terminal state | Classification |
| --- | --- | --- | --- |
| `void-patrol-adaptive-run7` | integration bug | failed | `minimax-fast` provider failure in `plan` loop 1 |
| `void-patrol-variant-c-run7` | integration bug | failed | `minimax-fast` provider failure in `plan` loop 1 |
| `void-patrol-variant-e-run7` | integration bug | failed | `minimax-fast` provider failure in `plan` loop 1 |
| `void-patrol-variant-g-run7` | integration bug | failed | `minimax-fast` provider failure in `plan` loop 1 |
| `void-patrol-variant-e-run7` | tail 1 QA | cancelled | `minimax` provider failure in `work`; old cancel path marked dependents |
| `void-patrol-variant-g-run7` | tail 1 QA | cancelled | `minimax` provider failure in `work`; old cancel path marked dependents |
| `void-patrol-variant-g-run7` | closure repair | cancelled | benign resolved-regression cleanup after validation passed |

Common failure signature:

```text
Gateway error 502 -> All backends failed: No backends available
-> rate limited after 7 attempts -> failure_kind=infrastructure_exception
```

Interpretation:

- The failed integration tasks did not reach meaningful project work; they
  should be excluded or explicitly labelled as infrastructure-censored.
- The cancelled QA tasks are graph-contaminating under the old retry policy,
  but their root cause is still provider availability, not game quality.
- The closure-repair cancellation should not count against the run because the
  repair had already completed and validated.
- After the infrastructure-failure recovery patch, these failures should no
  longer consume attempts or spawn research-feeder churn.

### Run 8 focused confirmation setup

Run 8 is a narrower confirmation run based on the run 7 artifact-quality
signal and the run 4 flat-loop outlier.

Experiment id: `void-patrol-pipeline-ab-run8-20260614`

Source snapshot: `void-patrol__void-patrol-basis`

| Project | Variant | Main feature pipeline | Tail shape | Purpose |
| --- | --- | --- | --- | --- |
| `void-patrol-variant-c-run8` | `variant-c` | `scout -> work -> validate` | 3x `art_pass -> polish -> harness_qa` | Tests whether scout plus direct work is enough without planning overhead. |
| `void-patrol-variant-e-run8` | `variant-e` | `scout -> plan -> work -> validate` | 3x `art_pass -> polish -> harness_qa` | Current structured winner from run 7. |
| `void-patrol-variant-e-long-plan-run8` | `variant-e-long-plan` | `scout -> plan -> work -> validate` | 3x `art_pass -> polish -> harness_qa` | Same as E, but plan phase receives 20 loops instead of the default 10. |
| `void-patrol-variant-f-run8` | `variant-f` | flat MiniMax-M3 loop | none | Pure flat baseline, preserving the run 4 F comparison. |
| `void-patrol-variant-f-tail-run8` | `variant-f-tail` | flat MiniMax-M3 loop | 3x `art_pass -> polish -> harness_qa` | Practical hybrid: flat implementation plus structured final quality tail. |

Run 8 deliberately omits chaos, adaptive, and `work -> scout -> plan ->
validate` arms. Those were useful for discovery, but this run is intended to
separate three specific effects:

- whether planning after scout beats scout-only structured work
- whether a deeper plan phase improves E
- whether the flat loop remains strong when paired with the same art/polish/QA
  quality tail used by structured arms

Setup note: `variant-e-long-plan` uses project-level
`_phase_loop_limits: {"plan": 20}`. This is intentionally code-backed in the
agent runtime so it is not just a metadata label.

Run 8 live stability note, 2026-06-14: after a swarm-controller restart,
the run was not constrained by provider 429s. Router health reported MiniMax
healthy, quota was not rate-limited, and recent `headroom.log` rows had no
rate-limit errors. The temporary under-fill (`6/9` active agents) was caused
by graph/dependency gating: all remaining pending tasks were blocked by
dependencies or `run_after`, not by quota.

The run did expose a controller lifecycle consistency bug: recovery/research
tasks could be marked terminal (`cancelled`) while their owning agent process
remained active for a short window. This was patched mid-run in
`swarm/maintenance/agents.py` by making reconciliation inspect live in-memory
handles and terminate agents whose task ownership no longer matches an
`in_progress` task. Treat `variant-e-run8` with extra caution in quantitative
analysis because it accumulated cancelled research feeders before/around this
fix. Other arms remain usable if they finish without terminal infrastructure
failures, but run 8 should be labeled "controller lifecycle bug discovered
and fixed mid-run."

Run 8 intervention ledger, 2026-06-15:

- **Code intervention: active-agent/task lifecycle repair.**
  `swarm/maintenance/agents.py` was changed mid-run so active in-memory agent
  handles are reconciled against task ownership. If a live agent is attached
  to a task that is no longer a valid `in_progress` owner, the watchdog now
  terminates the subprocess, removes the active handle, and marks the agent
  failed without resetting already-terminal task rows. This fixes the observed
  impossible state where a `cancelled` task still had an active agent.
- **Code intervention: art/polish soft quality gates.**
  `swarm/agent_recovery.py` was changed mid-run so exhausted `art_pass` and
  `polish` tasks are not hard blockers. On max-attempt exhaustion or research
  feeder cycle cap, these qualitative gates are marked `completed` with
  metadata flags (`soft_gate_failed`, `quality_gate_incomplete`,
  `needs_human_review`, `quality_gate_failure_reason`) and downstream tasks
  receive `soft_quality_gate_warnings`. This lets QA inspect quality risk
  instead of stranding the graph.
- **Live DB intervention: stranded run 8 quality gates soft-repaired.**
  Before modifying the live DB, a backup was created:
  `data/swarm.db.bak-soft-quality-gates-20260615093704`. Two already-stranded
  run 8 tasks were converted from hard terminal failures to soft-gate
  completions:
  `void-patrol-variant-c-run8-tail1-polish` and
  `void-patrol-variant-e-long-plan-run8-tail1-art`.
- **Analysis handling.**
  Do not treat those two repaired rows as ordinary successful art/polish
  completions. Count them separately as `soft_gate_failed` qualitative-pass
  failures that were allowed through to keep the run draining. Downstream QA
  spawned after this point is still useful, but should be interpreted as QA
  after incomplete qualitative gates.
- **Run cleanliness.**
  Run 8 remains useful as a stress/confirmation run, but it is not a pristine
  treatment run. Mark the whole run as "mid-run controller intervention".
  `variant-e-run8` remains the least clean arm because of earlier cancelled
  research feeders. `variant-c-run8` and `variant-e-long-plan-run8` now contain
  explicit soft-gate repairs in the tail. `variant-f-run8` appears effectively
  complete but includes cancelled QA/recovery rows and should be analyzed with
  terminal-state breakdowns, not only completed-task counts.

### Voxel Forge graph-quality note

For the `voxel-forge` production graph, art and polish gates were given a
task-level work-loop override:

```json
{"phase_loop_limits": {"work": 200}}
```

This preserves the larger contiguous work budget that earlier flat art agents
appeared to have (`MAX_TOOL_LOOPS=200`) while keeping the structured
`scout -> work -> validate` quality-gate shape. Harness QA remains on the
default structured work budget. This may be an important artifact-quality
variable: visual and UX work often needs enough uninterrupted loops to inspect
screenshots, generate or wire assets, retest, and iterate before completion.

### Run 9 adaptive-flat special-path cleanup note

Run 9 introduces an explicitly experimental `adaptive_flat` path. It is not a
new default pipeline. The goal is to test whether the flat-loop advantage comes
from preserving one continuous transcript while still allowing cheaper/faster
models to handle bounded read-only exploration.

Special code paths added for this experiment:

- `swarm/model_routing.py`: standalone adaptive-flat routing policy. It chooses
  between a fast provider and a strong provider using recent tool calls,
  task type, and a consecutive-cheap-loop cap.
- `swarm/agent_runtime.py`: legacy flat-loop hook. When `ADAPTIVE_FLAT` is
  enabled, the runtime disables the old scout summary/reset path, routes each
  loop through `choose_adaptive_flat_provider`, records routing stats in the
  agent token JSON, and blocks cheap-provider `TASK_COMPLETE` until a strong
  loop can confirm.
- `swarm_runner.py`: wrapper-generation hook. A project or task can opt in via
  `pipeline_mode: "adaptive_flat"` / `adaptive_flat: true`; this forces
  `pipeline=[]` and passes `LOOP_MODEL_ROUTING` into the agent wrapper.
- `swarm/api_snapshots.py`: clone API support for `pipeline:
  "adaptive-flat"`. This stamps cloned tasks and project config with the
  adaptive-flat treatment and preserves the routing metadata through clone
  reset.
- `swarm/experiment_metadata.py`: future graph-created tasks inherit
  `pipeline_mode: "adaptive_flat"`, `adaptive_flat: true`, and
  `loop_model_routing` so the graph does not silently fall back to a default
  pipeline mid-run.

Expected config shape:

```json
{
  "project_pipelines": {
    "void-patrol-adaptive-flat-run9": {
      "*": [],
      "_flat_provider": "minimax",
      "_experiment": {
        "pipeline_mode": "adaptive_flat"
      },
      "_loop_model_routing": {
        "enabled": true,
        "fast_provider": "minimax-fast",
        "strong_provider": "minimax",
        "cheap_phase": "scout",
        "max_consecutive_cheap_loops": 3
      }
    }
  }
}
```

Cleanup decision after run 9: either promote this into a first-class production
mode, merge the useful parts into the normal flat loop, or remove the special
paths above. Do not leave `adaptive_flat` as an undocumented hidden branch.

Metrics to inspect before deciding:

- `adaptive_flat.cheap_loops`, `strong_loops`, `model_switches`, and
  `cheap_completion_blocks` in `agent_<task_id>_tokens.json`
- completion/cancellation rates by task type
- artifact quality versus run 7 E and run 8 F/F-tail
- provider error rates split by fast and strong model
- whether cheap loops reduced cost/time without increasing rework,
  continuation tasks, or qualitative-gate failures

Run 9 qualitative gameplay note, 2026-06-18:

- Human-playable game quality is the end goal for these experiments. Controller
  completion, loop counts, and validation pass rates are useful diagnostics, but
  they are secondary to whether the resulting game is understandable, playable,
  and enjoyable for a human.
- On that primary goal, run 9 was a clear success relative to prior runs: it
  produced more human-playable games and higher overall artifact quality than
  earlier batches.
- `void-patrol-adaptive-flat-run9` was the most playable artifact by human
  inspection in run 9. It had the fanciest graphics in the run, working ship
  movement, enemies, and power-ups. It also had a critical gameplay bug that
  prevented normal play for much of the session, so this should be treated as
  strong artifact-quality signal but not as a clean robustness win.
- `void-patrol-variant-f-tail-run9` was less graphically intense than adaptive
  flat, but appeared more robust bug-wise. It also seemed to contain the most
  complete feature set. This matches the quantitative pattern: F-tail stayed
  closer to the run 4 F recipe of contiguous flat implementation plus quality
  gates, while adaptive flat spent much more graph effort on iterative
  art/polish/QA recovery.
- Comparison to `void-patrol-variant-f-run4`: the shared winning traits are
  flat-family execution, MiniMax main work, full source-feature completion, and
  real art/polish/QA pressure. The likely differentiator for artifact quality is
  not flat execution alone; it is flat execution with enough uninterrupted loops
  and repeated visual/UX passes that inspect and improve the running game.
- Token/time analysis was exported to
  `data/experiment_exports/token-time-analysis-20260618/`. Recorded token data
  is usable for runs 6-9 and undercounted before that. In run 9,
  `void-patrol-adaptive-flat-run9` was both the strongest human-playability
  signal and the most expensive recorded variant: about 258.3M recorded tokens,
  roughly 4.0x `F-tail` and 6.0x `F-tail parallel`. This supports the idea that
  adaptive flat bought quality through much heavier iterative art/polish/QA and
  recovery work, while `F-tail` remains the more efficient robust baseline.
- Presentation-ready cross-run findings are summarized in
  `docs/experiment-designs/run6-run9-presentation-findings.md`.

Smoke-test note, 2026-06-15:

- `void-patrol-adaptive-flat-run8-smoke` was created before the server restart
  and is contaminated by one old flat-loop task attempt. Leave it excluded from
  analysis.
- `void-patrol-adaptive-flat-run8-smoke2` is the clean adaptive-flat smoke arm.
  The first manual spawn found a wrapper-generation bug before any LLM call:
  `rt.LOOP_MODEL_ROUTING` was emitted with JSON booleans (`true`) instead of
  Python booleans (`True`). The task was reset to `pending`/`attempts=0` after
  fixing `swarm_runner.py` to emit Python literals for that config.
- This failure is classified as a controller smoke-test failure, not model or
  provider behavior. Retry requires a server restart so generated wrappers use
  the fixed code.
- The second smoke attempt proved adaptive-flat wrapper/runtime execution and
  stats recording, but exposed a router-policy issue: `minimax-fast` requests
  still reached shrimp-router with `X-Task-Type: feature`, so the router's
  task-type policy selected the `minimax` backend and returned MiniMax-M3.
  Adaptive-flat cheap loops now support `loop_model_routing.cheap_phase`; the
  smoke config uses `cheap_phase: "scout"` so shrimp-router phase routing can
  select the cheap backend before task-type routing.
- After server restart with the phase-header patch, the clean smoke task
  `void-patrol-adaptive-1781539405418-0001` started successfully. Loop 1 used
  `minimax`/MiniMax-M3 as the strong intent-setting call. Loop 2 selected
  `minimax-fast` with `X-Phase: scout` and returned from MiniMax-M2.7 for
  read-only follow-up. The token artifact recorded `strong_loops=1`,
  `cheap_loops=1`, and `model_switches=1`, so adaptive-flat routing and metric
  persistence are functioning.

### Metrics

Per completed task, record:
- `experiment_id` — stable run identifier used for durable storage
- `experiment_arm` — `confirmatory` or `exploratory`
- `pipeline_variant` — which pipeline ran (already tracked in metadata)
- `phase_order` — exact order used for this task
- `phase_random_seed` — reproducibility seed for randomized orders
- `work_loops_used` — loops consumed in work phase
- `validation_passed` — did first validate pass?
- `bug_task_spawned` — did a validation bug task spawn afterward?
- `diff_stat` — files changed, lines added/removed
- `attempts` — how many retries before success

Aggregate per project:
- Validation pass rate on first attempt
- Average work loops
- Bug task spawn rate
- Total tasks completed vs failed

### Success criteria

50–100 completed tasks per variant to see statistical signal. At current
swarm throughput (~5-8 tasks/day per project) this is 2-3 weeks of runtime.

## What Needs to Be Built

### 1. Project cloning tool

A script or API endpoint that:
1. Forks a source project repo (git clone + new remote)
2. Registers the clone with the swarm (`POST /api/managed-projects`)
3. Seeds an identical task DAG via `/api/tasks/batch` with the variant's
   pipeline baked into each task's metadata

### 2. Variant assignment

Each project gets a fixed pipeline written into its swarm config or as a
project-level metadata override — not per-task randomization. This ensures
the whole project runs consistently under one treatment.

Current per-task override already works:
```json
{"metadata": {"pipeline": ["plan", "scout", "work", "validate"]}}
```

Need: a project-level pipeline override that applies to all tasks spawned
for that project without requiring per-task metadata.

### 3. Metrics collection

Extend `_finish_agent()` or the task completion pipeline to write experiment
metrics to a structured log (`data/experiment_metrics.jsonl`) with:
```json
{
  "task_id": "...",
  "project": "...",
  "task_type": "...",
  "pipeline_variant": ["plan", "scout", "work", "validate"],
  "work_loops": 27,
  "validation_passed": true,
  "bug_spawned": false,
  "attempts": 1,
  "diff_insertions": 142,
  "diff_deletions": 38,
  "completed_at": "2026-06-05T..."
}
```

### 4. Analysis endpoint or script

A `GET /api/experiment/results` endpoint or standalone script that:
- Groups completed tasks by `pipeline_variant`
- Computes mean/median/stddev for each metric
- Surfaces statistical significance (basic t-test or Mann-Whitney U)
- Renders as JSON or markdown table

## Implementation Status (as of 2026-06-05)

All infrastructure is built and operational. The experiment is ready to run.

### What was built

**1. Project snapshot + clone API** (`swarm/api_snapshots.py`)
- `POST /api/projects/<name>/snapshot` — saves JSON export of all tasks + project row + git tag
- `POST /api/projects/<name>/clone` — clones snapshot into new project: git-clones repo, re-imports tasks under new name, bakes pipeline variant into every task's metadata
- `GET /api/projects/<name>/snapshots` — lists available snapshots
- `POST /api/projects/<name>/restore` — in-place restore (same project)
- All pipeline presets available via `pipeline` parameter: `"control"`, `"variant-a"` through `"variant-f"`
- Dashboard: 📸 Snapshots button on every project card

**2. Task identity tracking across clones**

IDs must be unique across the DB (`id TEXT PRIMARY KEY`), so cloned tasks get new IDs. Cross-variant linkage is preserved via metadata:
```json
{
  "source_task_id": "void-patrol-t01-9635",
  "source_project": "void-patrol"
}
```
Every cloned task carries `source_task_id` and `source_project` so results can be joined across variants for the same underlying task.

**3. Pipeline variant baking**

At clone time, each task's `metadata.pipeline` and `metadata.experiment_variant` are set:
- Fixed variants (control, A, B, C): same pipeline list on every task
- Variant D: `random.shuffle(["plan", "scout", "work", "validate"])` per task, independently
- Variant F: empty pipeline list + `flat_provider: "minimax"` → swarm_runner bypasses phase engine, uses legacy tool loop with MiniMax

**4. Synthesize phase** (`swarm/phases/synthesize.py`)
- Two modes auto-detected from pipeline config:
  - **Research mode** (→ `create_tasks`): produces proposed task list
  - **Implementation mode** (→ `work`): produces file-by-file implementation brief
- Work phase (`swarm/phases/work.py`) now injects `state.synthesis.implementation_steps` into the work prompt when present

**5. Metrics collection** (`swarm/agent_finish.py`, `data/experiment_metrics.jsonl`)

Records are also mirrored to append-only per-experiment logs:

```text
data/experiments/<experiment_id>/events.jsonl
```

The flat `experiment_metrics.jsonl` file is convenient for dashboards. The per-experiment event log is the durable research artifact: labeled, timestamped, and isolated from unrelated swarm metrics.

Per completed task:
```json
{
  "task_id": "...",
  "source_task_id": "void-patrol-t01-9635",
  "source_project": "void-patrol",
  "project": "void-patrol-control",
  "task_type": "feature",
  "experiment_id": "pipeline-ab-20260605",
  "experiment_arm": "confirmatory",
  "experiment_variant": "control",
  "pipeline_variant": ["plan", "scout", "work", "validate"],
  "phase_order": ["plan", "scout", "work", "validate"],
  "is_valid_order": true,
  "invalidity_reason": "",
  "phase_timings": {"plan": 4.2, "scout": 31.8, "work": 92.4, "validate": 10.1},
  "work_loops": 27,
  "validation_passed": true,
  "bug_spawned": false,
  "attempts": 1,
  "diff_insertions": 142,
  "diff_deletions": 38,
  "completed_at": "2026-06-05T..."
}
```

**6. Analysis endpoint** (`GET /api/experiment/results`)
- Groups by `experiment_variant`
- Per-variant: validation pass rate, bug spawn rate, avg work loops, avg attempts, diff stats, phase_order_counts (variant D)
- `per_task` array: joins on `source_task_id` to compare all variants on the same underlying task side by side
- Filter: `?project=void-patrol-control` or `?source_project=void-patrol`

### Source project

**void-patrol** — purpose-built vertical scroller (Godot 4, GDScript). 8 tasks (US-001 through US-008), clean dependency chain, clear acceptance criteria. Snapshot tagged `v0.0.0-scaffold` on the git repo and saved as `data/snapshots/void-patrol__<tag>.json`.

First test clone: **void-patrol-test** (cloned 2026-06-05, no pipeline variant set — used to verify clone correctness before experiment clones).

## Source Project Requirements

The source project must be:
- **Stable** — not currently in flight, no pending tasks
- **Well-defined** — clear GAME_DESIGN.md with measurable acceptance criteria
- **Reproducible** — tasks can be re-seeded from a fixed list without ambiguity
- **Medium complexity** — enough tasks to generate signal, not so large it runs forever

Candidates: a completed game project with a known task list, or a purpose-built
benchmark project with synthetic but realistic tasks.

**pale-cartography is not ready yet** — still in flight. Revisit once it completes
and QA passes.

## Timeline

1. Build project cloning tool
2. Build project-level pipeline override
3. Build metrics collection in agent finish pipeline
4. Select source project + finalize variant list
5. Clone + seed + run
6. Analyze after 50+ completions per variant

## Literature Review

Literature review conducted 2026-06-04. Key finding: **no published research
covers controlled ablations of dynamic phase ordering for LLM agent pipelines
on real software tasks.** This experiment is genuinely novel.

### What exists

**AutoCodeRover** (Zhang et al., ISSTA 2024 / arXiv 2404.05427) is the closest
structural analog. It uses a two-stage pipeline: a *context retrieval* phase
(iterative AST-based code search, up to 3 retries) followed by a *patch
generation* phase (up to 3 retries with test feedback). This is the earliest
published system to decompose software agent work into distinct phases with
per-phase retry budgets. Achieves 30.67% on SWE-bench Lite at <$0.70/task.
The paper does not ablate phase ordering or composition.

- Paper: https://arxiv.org/abs/2404.05427
- Code: https://github.com/AutoCodeRoverSG/auto-code-rover

**SWE-agent** (Yang et al., NeurIPS 2024) uses a flat ReAct loop — no phase
structure. Average trajectory: ~40 steps / 48.4K tokens per issue on SWE-bench
Verified. Up to 50 LLM calls and 49 tool calls per task. Serves as the
unstructured baseline for comparison.

- Paper: https://proceedings.neurips.cc/paper_files/paper/2024/file/5a7c947568c1b1328ccc5230172e1e7c-Paper-Conference.pdf

**AgentDiet** (arXiv 2509.23586, 2025) studies trajectory *compression* rather
than phase ordering — it reduces input tokens by 39.9–59.7% by pruning
redundant trajectory content mid-run, without harming pass rate (-1% to +2%).
This is evidence that most trajectory content is waste, not signal — which
motivates the hypothesis that better phase structure could eliminate waste
upfront rather than pruning it after the fact.

- Paper: https://arxiv.org/html/2509.23586v2

**OpenHands** supports multiple agent modes and is evaluated at up to 100
iterations per instance. No published ablation of phase ordering.

- Blog: https://openhands.dev/blog/evaluation-of-llms-as-coding-agents-on-swe-bench-at-30x-speed

### What is not known

Nobody has published a controlled experiment varying:
- Phase composition (which phases to include)
- Phase ordering (plan→scout vs scout→plan vs scout-only, etc.)
- Whether front-loading exploration reduces work loop count on implementation tasks

The AutoCodeRover data (retrieval-then-patch, ~6 total LLM calls) vs SWE-agent
(flat ~40 steps) suggests structured phases reduce iteration count, but this has
never been isolated — the systems differ in model, tools, and task distribution,
not just phase structure.

Our experiment will be the first direct comparison of pipeline variants on
identical tasks, identical projects, identical models.

## Open Questions

- Should variants be fully random (any valid ordering) or fixed per project?
  Fixed per project gives cleaner analysis. Random per task gives more coverage
  but harder to isolate causality.
- Should we test different models per phase, or hold model constant and only
  vary phase composition?
- How do we handle tasks that are inherently hard vs easy — do we need task
  difficulty stratification?
- Is validate always worth including? Its absence changes the feedback loop
  significantly and might be its own experiment axis.

## Run 4 Findings: Recovery Feeders

Findings recorded 2026-06-09 from `void-patrol-pipeline-ab-run4-20260606`.
These notes are intended to guide the next controller/tool overhaul, not to
serve as final statistical analysis.

### Research feeders inherited experiment variants

Research feeders are special in graph behavior: they block the original failed
task, run diagnosis, then feed findings back into the original task so it can
retry. They are not currently special in pipeline selection. `_spawn_research_feeder`
calls `stamp_experiment_metadata(project, research_meta)`, so feeders inherit
the project experiment config exactly like ordinary implementation tasks.

Observed run-4 feeder pipelines:

| Project | Variant | Research feeder pipeline | Completed | Cancelled |
| --- | --- | --- | ---: | ---: |
| `variant-a` | `variant-a` | `plan -> work -> validate` | 3 | 8 |
| `variant-b` | `variant-b` | `plan -> scout -> synthesize -> work -> validate` | 2 | 10 |
| `variant-c` | `variant-c` | `scout -> work -> validate` | 2 | 4 |
| `variant-d` | `variant-d` | randomized per feeder | 8 | 60 |
| `variant-e` | `variant-e` | `scout -> plan -> work -> validate` | 2 | 10 |
| `variant-f` | `variant-f` | flat / legacy prompt path | 1 | 0 |

For variant D, 38 research feeders received an explicitly invalid randomized
order where `validate` ran before `work`. This created a feedback cascade:
chaos-generated failures spawned chaos-mode recovery tasks, which often failed
or produced repeated diagnosis instead of stabilizing the original task.

### Interpretation split

Variant D should be split into two interpretations:

- For main task throughput and cross-variant scoring, D's recovery history is
  contaminated. The result mixes task difficulty, chaos phase ordering, and
  recovery amplification.
- For recovery-feeder design, D is highly valuable. It stress-tested the
  recovery mechanism and produced examples where failed/cancelled feeders still
  generated useful diagnosis.

Do not treat feeder `completed` status as the primary efficacy metric. Better
metrics are:

- Did the feeder cause the original task's next retry to complete?
- Did it reduce repeated feeder cycles?
- Did it produce a concrete, actionable root cause with exact files/lines?
- Did it avoid direct game-code edits when the task was supposed to be
  read-only diagnosis?
- Did it distinguish project-code failure from controller/tooling failure?

### Non-D feeder signal

The non-D variants suggest recovery feeders do not need a full implementation
pipeline:

- `variant-c` (`scout -> work -> validate`) had the best balance among pipeline
  feeders: low churn, and both completed feeders fed parents that completed.
- `variant-a` (`plan -> work -> validate`) also unblocked parents when it
  completed, but one parent required six feeder cycles, suggesting too little
  discovery before work.
- `variant-b` (`plan -> scout -> synthesize -> work -> validate`) looked too
  heavy for recovery; completion was low and one completed feeder fed a parent
  that still failed.
- `variant-e` (`scout -> plan -> work -> validate`) supported scout-first
  recovery, but did not clearly outperform the simpler `variant-c`.
- `variant-f` flat mode had too little data but was stable in the one observed
  case, which suggests the legacy research prompt remains a useful baseline.

Recommended recovery-feeder shape for future testing:

```text
scout -> work
```

or, if a validation/report artifact is needed:

```text
scout -> work -> validate
```

Here `work` must mean "write diagnosis/handoff", not "implement and commit".

### Design fix for future runs

Recovery/meta tasks should preserve experiment labels for analysis, but should
not inherit experimental phase ordering by default.

Recommended rule:

- Ordinary source tasks inherit the project variant pipeline.
- Recovery/meta tasks use a stable recovery pipeline.
- Metadata still records `experiment_id`, `experiment_variant`,
  `source_project`, parent task, and recovery reason.
- A separate explicit experiment arm can later test chaotic recovery behavior.

Candidate task types to pin to stable recovery/meta pipelines:

- `research` when `metadata.is_research_feeder == true`
- closure repair / closure triage
- librarian
- cartographer
- scheduler
- auditor
- pruner
- other controller/system tasks

This prevents the "chaos creates failure, chaotic recovery amplifies failure"
loop while preserving enough metadata to study recovery quality.
