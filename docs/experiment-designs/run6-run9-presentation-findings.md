# Run 6-9 Presentation Findings

Generated: 2026-06-18

This note summarizes the clearest lessons from the cleaner later Void Patrol
pipeline runs. It is intended for presentation/storytelling use, not as a
replacement for the raw exports.

Primary supporting artifacts:

- `data/experiment_exports/run7-analysis-20260614/`
- `data/experiment_exports/run9-analysis-20260618/`
- `data/experiment_exports/token-time-analysis-20260618/`
- `docs/experiment-designs/run7-quantitative-findings.md`
- `docs/experiment-designs/run8-analysis-run9-proposal.md`
- `docs/experiment-designs/pipeline-ab-experiment.md`

## Executive Takeaway

Human-playable game quality is the target metric. Controller completion, loop
counts, validation pass rates, and task throughput are diagnostics. They matter
because they explain the system, but they are not the final goal.

By that standard, run 9 was a clear success. It produced more human-playable
games and higher artifact quality than earlier batches. The results point toward
a practical controller recipe:

```text
continuous implementation context
+ strong model for meaningful work
+ explicit art/polish/QA gates
+ enough uninterrupted loops for visual/UX iteration
+ recovery that does not derail the graph
+ cost controls for bounded scout/search work
```

## Core Findings

### 1. Playability tracks iterative quality pressure

The variants that felt best to a human were not merely the ones that completed
the source graph. They had repeated opportunities to inspect, polish, and repair
the actual game artifact.

Run 9 showed this most clearly:

- `void-patrol-adaptive-flat-run9` was the strongest human-playability and art
  signal. It had the fanciest graphics in run 9, working ship movement, enemies,
  and power-ups.
- `void-patrol-variant-f-tail-run9` was less visually intense but appeared more
  robust bug-wise and seemed to contain the most complete feature set.
- Several variants completed cleanly, but did not produce equally compelling
  games.

Interpretation: green graph closure is necessary, but not sufficient.

### 2. Flat-family execution is a serious baseline

The strongest artifacts repeatedly came from flat or flat-like workflows:

- run 4 `variant-f`
- run 8 `variant-f` / `variant-f-tail`
- run 9 `F-tail`
- run 9 `adaptive-flat`

The common thread is a larger continuous implementation context. Rigid phase
splitting can make behavior easier to measure, but for game-building it can
also interrupt the local design thread the agent needs to keep mechanics,
integration, tests, and user experience aligned.

### 3. Adaptive-flat bought quality with spend

Run 9 adaptive-flat produced the strongest artifact-quality signal, but it was
also the most expensive recorded variant.

Recorded run 9 token totals:

| Variant | Recorded tokens | Interpretation |
| --- | ---: | --- |
| `adaptive-flat-run9` | 258.3M | best human-playability/art signal; highest spend |
| `F-tail-run9` | 64.5M | strong robust artifact at about 25% of adaptive-flat |
| `adaptive-flat-parallel-run9` | 54.1M | cheaper adaptive arm; less compelling by playtest |
| `F-tail-parallel-run9` | 42.8M | cheapest strong flat-family comparator |

This suggests adaptive-flat may be a quality mode, while F-tail is the more
efficient production baseline.

### 4. Parallelism improves throughput but may reduce coherence

The parallel arms were clean and cheaper, but the best qualitative artifacts
came from less parallel, more coherent work histories.

Hypothesis: parallelism helps graph throughput and wall-clock capacity, but it
can reduce integrated design coherence when tasks need to coordinate mechanics,
visuals, UI, and game feel.

This does not mean parallelism is bad. It means the graph should use it
selectively:

- parallelize independent implementation tasks
- serialize integration-heavy gameplay and polish gates
- preserve one coherent design thread near major user-facing transitions

### 5. Validation pass rate is a safety signal, not the scoreboard

Several runs achieved green verification and full source-feature completion.
That did not automatically produce a good game.

Validation should remain a guardrail for:

- script correctness
- regressions
- scene loadability
- basic mechanics
- closure health

But the presentation metric should be human-playable quality. The system can
pass tests and still produce a game that is confusing, bland, or blocked by a
show-stopping gameplay issue.

### 6. Art/polish/QA gates are part of the intervention

Art, polish, screenshot/vision review, and playability QA are not measurement
noise. They are part of what makes the artifact usable.

The run 8 and run 9 results support giving art and polish enough uninterrupted
work budget. Short qualitative gates can complete without producing meaningful
visual or UX improvement.

Practical implication:

```text
Core feature work can be bounded.
Art and polish need enough loops to inspect, change, run, and iterate.
QA should feed actionable repair tasks instead of only marking pass/fail.
```

### 7. Recovery behavior is a hidden differentiator

Good runs recover without spiraling. Expensive or unstable runs generate many
bug, research, QA, and continuation tasks.

Useful recovery metrics:

- bug/research churn per completed source feature
- quality-gate retries per project
- cancelled cleanup tasks that do or do not block progress
- time and tokens spent after 8/8 source features complete
- whether recovery improves the artifact or only satisfies the graph

Run 9 adaptive-flat spent much more effort on iterative quality and recovery.
That effort bought visible quality, but at high token cost.

### 8. Chaos was useful for discovery, not production

Randomized and chaos-like runs were valuable because they showed that our
assumptions about phase order were incomplete. They helped reveal that some
non-obvious orders and flat-like behaviors could outperform more designed
pipelines.

However, the practical recipe emerging from clean later runs is not pure chaos.
It is controlled continuity:

```text
preserve implementation context,
add deliberate quality gates,
use adaptive routing for bounded low-risk loops,
and keep recovery stable.
```

## Candidate Derived Metrics

These are the metrics most likely to be useful in future charts and slides:

| Metric | Why it matters |
| --- | --- |
| Human playability score per 100M tokens | Connects cost to the actual goal |
| Quality tasks per source feature | Measures how much artifact-quality pressure was applied |
| Bug/recovery churn per source feature | Shows graph instability and repair burden |
| Art/polish/QA loops vs human visual score | Tests whether visual budget predicts quality |
| Feature completeness vs control clarity | Separates "features exist" from "humans can use them" |
| Token cost by task type | Identifies where spend is going |
| Post-feature-completion spend | Measures the cost of turning code into a playable game |
| Recovery tasks that improve playability | Separates useful repair from graph bookkeeping |

## Slide-Ready Claims

- We are no longer only measuring whether agents can complete tasks. We are
  measuring whether they can produce playable games.
- Run 9 was a breakthrough: more playable games, higher artifact quality, and
  clearer cost/quality tradeoffs.
- The best artifacts came from continuous implementation plus quality gates, not
  from rigid phase decomposition alone.
- Adaptive-flat produced the strongest run 9 artifact-quality signal, but at
  much higher token cost.
- F-tail is the most promising efficient baseline: robust, feature-complete, and
  far cheaper than adaptive-flat.
- Art, polish, QA, screenshots, and vision loops are not optional. They are part
  of the intervention that turns working code into a playable game.
- The next controller should optimize for playable artifact quality per token,
  not raw graph completion.

## Current Best Interpretation

The data does not say "flat always wins" or "phases are bad." It says the
controller should protect continuity where continuity matters.

For long-running game-building tasks, the strongest pattern is:

1. Let a capable implementation agent keep enough context to build coherent
   mechanics.
2. Add explicit qualitative gates that inspect and improve the running game.
3. Give art and polish enough budget to iterate.
4. Use cheaper/faster models for bounded scouting, not for final completion.
5. Score the result by human-playable quality, with token and time cost as
   efficiency constraints.

