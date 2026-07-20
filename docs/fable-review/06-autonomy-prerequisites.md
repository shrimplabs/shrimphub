# Autonomy Prerequisites — What Has to Exist Before the Director Can Work

*Fable deep review, 2026-07-13. Read after doc 05; assumes the Director
architecture is fixed.*

## The honest starting point

The Director design in doc 05 is coherent on paper. But a Director is only as
good as its inputs, its budget accounting, and the hardness of the systems it
directs. Right now, several of those are missing entirely, several exist but
are brittle enough to fool a Director, and a few dependencies chain in
non-obvious ways. This document names them without softening, puts them in the
order they have to be built, and identifies what risks survive all the
engineering.

---

## Signals that don't exist yet

### 1. Per-project dollar cost

The Director's core decision primitive is return on investment — which
projects produce the most player value per dollar. Token counts exist
(`input_tokens`, `output_tokens` in the `agents` table). Dollar cost does not
— there is no provider price table, no per-task cost rollup, no portfolio
spend figure for the current week.

This matters more than it sounds. Without cost, the Director allocates by
task count and token count, which conflates cheap MiniMax calls with expensive
Claude calls. A project on the claude provider burns ~15–40× more per token
than the same project on minimax; routing decisions that look balanced by
count are actually very imbalanced by spend. And the monthly spend figure is
the only thing that prevents the Director from treating the API quota as free.

What's needed:
- A `provider_prices` table (model → $/million input tokens, $/million output)
  — static, human-maintained, updated when pricing changes.
- A `task_cost_usd` computed column on the `agents` table (or a derived view).
- Rollup: `project_cost_7d`, `project_cost_30d`, `portfolio_cost_7d` in the
  ledger. Per-task averages by type (feature tasks cost X, QA tasks cost Y).
- A hard portfolio spend ceiling in config — the budget that the Director
  allocates *within*, not controls.

This is roadmap #18 scoped to the Director's minimum needs. It does not
require the full economics dashboard, just enough to make "kill glowworm-cavern
and reallocate" mean something testable.

### 2. Telemetry — player signal

Until `docs/telemetry-spec.md` ships and games are live with sessions
accumulating, the Director has no signal about whether the games it oversees
are actually experienced by humans. It is optimizing a proxy (pipeline
metrics) while believing it is optimizing the objective (player value). This
is the most important gap.

Without telemetry:
- Kill decisions are made on cost + QA metrics alone. A game that burns budget
  and is hard to build might still be genuinely fun; the Director cannot know.
- Keep decisions have no ground truth. A game that passes QA cleanly and costs
  little might be unplayable in practice; the Director cannot know that either.
- The calibration loop has no player-verified outcomes — the Director's
  predictions about "this game will retain players" are never confirmed or
  falsified by actual player data.

Mitigation for v1–v2: the Director operates on pre-telemetry proxy
objectives, states this explicitly in its charter ("current objective is a
proxy: optimize closure-green + playthrough-receipt per dollar"), and defers
kill decisions that would be reversed by telemetry. In practice this means
kill authority should not be granted before telemetry ships and accumulates
for at least one game over 30+ days.

There is no engineering workaround for the absence of players. The Director
in pre-telemetry mode is a budget optimizer, not a studio director.

### 3. Publish state — "shipped" as a defined concept

Right now "done" means playthrough bot passed + closure green. There is no
export, no live URL, no published game. The Director's most consequential
decisions — when to ship, when to start the next game — have no clean
definition to act on.

What's needed is exactly the export + publish pipeline from doc 04 Phase 1:
a `releases` table, a butler wrapper, itch.io URLs. Once this exists, the
Director's `ship` directive maps to a concrete state transition rather than
"tell the human a game seems done."

### 4. Closure spec provenance

The closure system (`swarm/closure/specs.py`) lets closure specs be written by
agents. A Director is grading the studio's output against specs the studio can
edit. This is self-grading and it will produce grade inflation.

Closure specs must be human-authored or human-approved, with a clear
provenance trail, before the Director's closure-based inputs are trustworthy.
This is a one-paragraph policy decision with a `spec_approved_by` field and a
`spec_locked` flag — not a big implementation lift — but until it exists, a
sufficiently capable agent that "discovers" it can unblock itself by relaxing
its own closure spec has effectively broken the gate.

### 5. Playthrough receipt coverage

The playthrough bot is the best signal in the system. But as of now it covers
six games. The remaining ~20+ managed projects either lack bot scripts or have
incomplete ones. The Director's `ship_candidates` ranking is built on
playthrough receipt as a primary signal; if most projects lack it, the ranking
is based on whatever-else, which is much weaker.

This is not a new system to build — the bot infrastructure exists. It is task
work: write `playthrough_bot` scripts for the unscripted projects. Every
project that reaches `ship_candidate` stage without a bot receipt is evidence
gap the Director will fill with proxy reasoning.

---

## Systems that need to harden before the Director can trust them

### A. Closure verification — false positives

`derive_closure_status` is the Director's primary project health signal. It
currently has at least one confirmed broken test (`test_closure_status_green_
when_required_gates_pass` — a known bug in CLAUDE.md). A Director that reads
`closure_status: green` and acts on it is acting on the output of an untested
oracle. The known-failure bugs must be fixed and regression-gated before
closure status is a reliable Director input.

Beyond the test failure: the closure verification run validates against the
spec as-written. If a spec under-specifies what "done" means (too few required
gates, weak regression thresholds), a project can be genuinely incomplete but
read as green. This is the spec-provenance issue again: the human needs to
author specs that are demanding enough to mean something.

### B. The QA loop — convergence vs. budget consumption

The QA cycle cap (`qa_max_cycles = 3`) exists precisely because uncapped QA
loops consume budget without converging. But the current cap is per-run, not
across the project's lifetime — a project that cycles 3 times, fails, resets,
and cycles again has no lifetime budget ceiling on QA spend.

For the Director, a project that soaks all its assigned budget in endlessly
restarting QA cycles looks like a project "making progress" — agents are
running, tasks are completing. It is not. The Director needs a cross-lifetime
QA spend signal: `qa_tasks_completed_lifetime`, `qa_budget_fraction` (QA
spend as % of total project spend). Projects where QA fraction exceeds ~40%
over 30 days are trapped in a fix/regress/QA loop, not making forward
progress.

### C. Research feeder — ROI varies enormously by project

The aggregate `research_feeder_roi` of 70% across 581 feeders is a useful
portfolio signal. At the project level, some projects have feeders that
reliably unblock tasks; others have feeders that churn through attempts and
never produce actionable diagnoses (the feeder's `research_context` injection
doesn't help if the root problem is a design contradiction, not a coding bug).

The Director receiving `research_feeder_roi: 0.2` for a specific project needs
to know this means "research feeders are failing here." If it doesn't have
per-project feeder ROI (breakdowns exist in the metrics endpoint, worth
verifying), it might misread a project as "stuck on hard problems" rather than
"stuck on the wrong design."

What's needed: per-project feeder ROI in the ledger, with a threshold below
which the Director should flag a design problem rather than a technical one.

### D. Dependency graph integrity

`swarm/integrity.py` runs real-time authority validation. The known failure
modes — continuation tasks reparenting incorrectly, ghost deps surviving
restarts, two agents running the same chain after restart — can each make a
project's task queue appear healthier than it is. A project where the true
critical path is blocked by a ghost dep looks like "tasks pending, but nothing
starts" — which the Archaeologist should detect, but if the Archaeologist is
not enabled (`meta_mode_enabled: false` by default), the Director is reading a
stale picture.

Minimum: run integrity scans before each ledger computation, not just
periodically. The ledger snapshot should include `integrity_issues_count` so
the Director sees "this project has 3 known graph violations" before forming
any allocation opinion about it.

### E. The monitor thread's serialization bottleneck

Post-task validation blocks the monitor for up to 5 minutes (Godot headless +
GUT test run). During that time: no new agents spawn, no dep violations are
checked, the Director's ledger triggers don't fire. At high project count this
compounds — multiple validations queuing means the monitor can be blocked for
tens of minutes at a time.

This is doc 03's event-driven core (roadmap #9). It is a prerequisite for the
Director in the sense that a Director receiving stale ledger data because the
monitor was blocked during validation will make decisions based on the studio's
state from 30 minutes ago. At 3 agents this is tolerable; at 15–25 agents with
a Director running weekly sessions, the data freshness degrades noticeably.
The Director should know the `ledger_staleness_s` and flag if it's >N before
acting. This is cheap to add to the ledger job.

---

## The dependency order

This is the sequence in which things have to exist, reading "→" as "enables":

```
1. Fix known test failures (closure, script generation, orchestrator)
        → closure_status is trustworthy input

2. Closure spec provenance (human-authored / human-approved flag)
        → Director can use closure as evidence without self-grading risk

3. Per-project cost accounting (provider price table + rollup)
        → Director can make allocation decisions with a real denominator

4. Per-project research feeder ROI in metrics ledger
        → Director can distinguish "stuck technically" from "stuck by design"

5. QA budget fraction signal (lifetime QA spend %)
        → Director can identify fix/regress/QA loops

6. Director ledger v1 + shadow-mode session (no actuation)
        → 6+ weeks of calibration data accumulate

7. Review queue (roadmap #11 — built for publish gate in doc 04)
        → Director proposals get human verdicts that feed calibration

8. Export gate + publish step (doc 04 Phase 1)
        → "ship" becomes a defined concept; `ship` directive maps to state

9. Telemetry autoload + relay + ingestion (doc 04 Phase 2)
        → kill and keep decisions get player evidence

10. Director v2 allocation authority (priority weights + budgets auto-apply)
        → requires ≥6 weeks calibration at ≥80% agreement rate

11. Director v3 lifecycle authority (kill/ship actuate with 72h veto)
        → requires telemetry live for ≥1 game for ≥30 days;
          spec provenance locked;
          closure known-failures resolved

12. Director v4 creative authority (design requests → wizard without human)
        → requires portfolio analytics (doc 04 Phase 4);
          edit-distance metric showing human edits shrinking;
          ≥6 months telemetry across ≥5 live games
```

Steps 1–7 are all possible without new infrastructure. They are paperwork,
test fixes, and a data column. The 6–12 week calibration window at step 6 is
the real minimum lead time — you cannot shortcut it by running more sessions,
because the decisions being calibrated have week-to-month feedback latencies.

The Director cannot be trusted at v2 without completing steps 1–7. It cannot
be trusted at v3 without steps 1–11 in sequence. The calibration window is the
floor below which no amount of engineering moves the schedule.

---

## Irreducible risks that cannot be engineered away

### 1. Metric goodhart

Any signal the Director optimizes will be gamed — not by adversarial actors
but by the ordinary dynamics of optimization. Value/repair ratio improves when
you create fewer bug tasks; a Director told to maximize it might favor projects
where problems are never logged rather than projects where problems are fixed.
Telemetry improves when quit-point bugs get fixed; a Director targeting funnel
completion might systematically underfund games whose funnels are fine but
whose content is thin.

You cannot fix this by adding more metrics — that just multiplies the gaming
surface. The only partial defense is maintaining metrics that the Director
*cannot influence directly* and that require real player behavior (not pipeline
behavior). Telemetry is the best current candidate; itch.io ratings are a
second. Both are lagged and noisy and finite. The risk remains.

### 2. Cascading kill decisions

A Director empowered to kill projects will kill some projects that were right
on the edge of a breakthrough. The playthrough bot and closure system make the
edge observable, but they do not make the future observable. A project at 60%
completion with bad metrics might finish cleanly with one more sprint; a
Director with a hard budget constraint might kill it the week before. You
cannot tell these cases apart without building the game — which is exactly what
you were considering not doing.

The mitigation is the 72h veto window and the requirement for 14-day data
minimums on kill decisions. But the risk is irreducible: some kills will be
wrong. The honest acceptance of this is part of running a studio — human or
otherwise.

### 3. The calibration loop closes on the human's taste, not players' taste

During v1 shadow mode and v2 allocation, the Director's calibration is against
human overrides. The human's taste is the ground truth. If the human has bad
taste — prefers technically impressive but unengaging games, or overvalues
novelty, or has idiosyncratic genre preferences — the Director will calibrate
toward those preferences, which will persist into its telemetry-era decisions
as a strong prior. Taste bias from the calibration corpus is sticky and hard
to detect once baked in.

The partial defense is requiring calibration verdicts to cite ledger evidence
rather than preference, and switching to telemetry-verified outcomes as quickly
as possible. But the first year of calibration is inescapably human-flavored.

### 4. The cost floor for a meaningful session

Director sessions need the strongest configured model. At current MiniMax M3
pricing, a complex portfolio review is not expensive — a few dollars. But the
Director is making decisions that allocate tens or hundreds of dollars of agent
work. The asymmetry is healthy: cheap decisions, expensive execution. The risk
is the opposite case: a Director that runs too frequently, on thin data, runs
up session costs without enough ledger movement to change anything. The "weekly
schedule, trigger-gated" design in doc 05 is the guard, but the session
frequency needs to be empirically tuned against the portfolio's actual decision
velocity, not assumed.

### 5. Spec drift vs. design drift

The Director governs whether projects meet their closure specs. Closure specs
reference `GAME_DESIGN.md`. But the most common way a project goes wrong is
not that it fails its spec — it is that the spec and the design doc gradually
drift apart as agents make small implementation decisions that each seem
reasonable, until the game that exists shares only a surface resemblance to
the game that was planned. The Director cannot detect this without reading both
documents and judging their coherence, which requires exactly the kind of taste
and contextual judgment that is hardest to formalize (doc 07). A Director that
only checks spec-passing will miss entire categories of "wrong" that a human
would catch in ten seconds of playing the game.

This risk is managed by keeping design-doc changes human-gated (it's in the
non-goals; the non-goals were written for exactly this reason), but it is not
eliminated. Spec passing and design fidelity are different properties, and
right now only the former is measurable.
