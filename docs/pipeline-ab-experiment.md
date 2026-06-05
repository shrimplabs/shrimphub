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

### Variants (proposed)

| Variant | Pipeline | Hypothesis being tested |
|---------|----------|------------------------|
| Control | `plan → scout → work → validate` | Current baseline |
| A | `plan → work → validate` | Is scout overhead worth it? |
| B | `plan → scout → synthesize → work → validate` | Does synthesize improve work quality? |
| C | `scout → work → validate` | Is planning necessary, or does exploration suffice? |

Additional variants to consider if bandwidth allows:
- D: `scout → plan → work → validate` (scout first, then frame)
- E: `plan → work` (no validate — does validate feedback loop matter?)

### Metrics

Per completed task, record:
- `pipeline_variant` — which pipeline ran (already tracked in metadata)
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
