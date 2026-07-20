# 10 — Telemetry → Prompt Injection Pipeline

**Status:** Design review (fable-review series)
**Depends on:** `docs/telemetry-spec.md` (events, `telemetry.db`, `/api/telemetry/<game>/summary`)
**Relates to:** ROADMAP #15 (learning loop), #17 (telemetry spec), #7 (analytics)

The telemetry spec answers "how do we collect player data." This document answers
the question the spec dodges: **how does a quit-rate number become a different
task in the queue, and how do we know it helped?**

The one-sentence answer: **code computes signals, the planner interprets them,
task metadata carries the evidence, and a delayed comparator closes the loop.**
The LLM never sees raw event rows, and no threshold lives in a YAML prompt.

---

## 0. The uncomfortable precondition

Be honest about this first: the entire pipeline below is dead weight until games
have players. `sessions_total: 142` in the spec's example is aspirational — most
swarm games today have zero external sessions. The pipeline must therefore:

1. **No-op gracefully at n=0** — no injection, no signals, no "0% victory rate!!"
   panic tasks generated from an empty table.
2. **Treat QA-agent and playthrough-bot sessions as a separate population.**
   The Godot autoload fires during QA runs too. Tag sessions with
   `is_agent: true` when `SWARM_QA_RUN=1` is in the environment (the QA harness
   sets this; add it if it doesn't). Agent sessions are useful as a smoke test of
   the pipe but must be excluded from player-behavior signals, or the system will
   "learn" from its own bots — a feedback loop eating its own tail.
3. **Work identically at n=25 and n=25,000.** Thresholds are rates with minimum
   sample gates, never absolute counts.

Everything below assumes these three rules.

---

## 1. What the planner actually does with quit-point data

### The scenario: 20% quit at Level 3

**What must NOT happen:** the planner sees `{"scene": "Level_3", "pct": 0.20}`
and creates `"[bug] Make Level 3 easier"`. That task is unfalsifiable, probably
wrong, and an agent will "fix" it by nerfing something arbitrary.

**What happens instead — three stages:**

**Stage 1 (code): the signal classifier disambiguates before the LLM sees anything.**
A raw quit-point is ambiguous between difficulty, boredom, missing content, a bug,
and *successful completion*. But the event stream contains the disambiguating
evidence, and cross-referencing it is deterministic work, not judgment work:

| Co-occurring evidence at the quit scene | Classification | Hypothesis handed to planner |
|---|---|---|
| Deaths ≥ 2× per-scene median, retries then quit | `hard_wall` | difficulty spike |
| Deaths below median, time-in-scene ≥ 1.5× median, no errors | `boredom_exit` | pacing/length problem |
| Scene is the last implemented scene, no `victory` event exists in any session | `content_cliff` | players ran out of game |
| Session has `victory` event before the quit | `completion_exit` | **not a problem — excluded** |
| Session ends with no `quit` event at all | `crash_exit` | crash/hang — bug |
| `custom` error events in-window before quit | `crash_exit` | bug |
| None of the above | `unclassified_exit` | unknown — needs investigation, not a fix |

This table is the direct answer to "how does it know whether the problem is
difficulty, length, bugs, or missing content": **it usually can't know from the
quit event alone, but it can almost always know from the join** — deaths, retries,
durations, and victory events at the same scene. The join runs in SQL, not in the
LLM's head.

**Stage 2 (prompt): the planner receives classified signals, not raw stats.**
The `project_plan` prompt gets a block like:

```
## PLAYER TELEMETRY SIGNALS (n=142 sessions, 30d window, confidence: ACT)
- [S1] hard_wall @ Level_3: 20% of sessions quit here (29/142); deaths/session
  at Level_3 = 4.1 vs game median 1.6; median 3.2 retries before quit.
  Hypothesis: difficulty spike. Suggested action: investigate Level_3 balance.
- [S2] death_cause_skew: "enemy" causes 74% of all deaths (412/557).
  Hypothesis: one enemy type or damage value dominates.
Raw: victory_rate=0.31, median_playtime=312s, median_deaths=4.2
```

**Stage 3 (LLM): the planner investigates before it writes the task.** The
prompt instructs: *a signal is a lead, not a verdict.* The planner already reads
the codebase (STEP 2 of `project_plan.yaml`); with a signal in hand it reads
`Level_3.tscn`/`level_3.gd`, checks enemy counts/HP/spawn rates against earlier
levels, checks `GAME_DESIGN.md` for whether Level 3 is *supposed* to be a
difficulty gate. Then it writes a task like:

```
[bug, priority 80] Level_3 difficulty wall (telemetry S1): 20% of players quit
at Level_3 after median 3.2 retries; deaths/session 4.1 vs 1.6 game median.
Code review: Level_3 spawns 12 enemies vs 5 in Level_2 with no HP/weapon
progression between them (level_3.gd:34-51, GAME_DESIGN.md gives no ramp for
this jump). Reduce spawn count to 7-8 OR add a health pickup at the midpoint —
pick whichever GAME_DESIGN.md's pacing section supports. Acceptance: a
playthrough_bot run completes Level_3 in ≤ 3 attempts; deaths-at-Level_3
telemetry to be re-measured post-release (metadata.telemetry_evidence attached).
```

Specific files, specific evidence, a bounded change, a checkable proxy criterion.
That's the bar. If the planner's code read *contradicts* the signal (Level 3 looks
identical to Level 2), the correct output is a `research` task, not a fix task —
the prompt must say this explicitly.

### Priority, description, or both?

**Both, plus a third thing that matters more: sprint-goal selection.**
`project_plan` is generative — it decides *what work exists*, not just its order.
A confirmed `hard_wall` signal should be allowed to *become the sprint goal*
(STEP 3), displacing new-feature work. Mechanically:

- **Priority:** signal-driven tasks get the standard type priority (bug=80),
  bumped +10 when confidence is ACT-tier and the signal touches the core loop.
  Do not invent a new priority band; the existing scale works.
- **Description:** always carries the evidence inline (numbers, scene, window)
  plus `metadata.telemetry_evidence` (structured — see §5). The description is
  for the implementing agent; the metadata is for the comparator.
- **Sprint goal:** the prompt instructs that any ACT-confidence signal on the
  core loop must be addressed in the current sprint or explicitly deferred with
  a one-line reason in the plan output. Forcing the "or explicitly deferred"
  branch is what prevents the planner from being a telemetry slave (see §1
  failure mode below).

### Failure mode: misreading the signal

**"Quit at Level 3 is a feature, not a bug"** — players finished and exited.
Three layers of defense, in order of cheapness:

1. **SQL layer (catches most of it):** sessions containing a `victory` event are
   excluded from quit-point aggregation entirely. This is a required fix to the
   spec's `/summary` endpoint — as specced, `top_quit_scenes` counts post-victory
   quits, which guarantees the final level always "leaks" quit signal. Similarly,
   if Level 3 is the last implemented scene, the classifier emits `content_cliff`
   (build more game), never `hard_wall` (nerf the game).
2. **Prompt layer:** planner must corroborate against code + design doc before
   creating a fix task; contradiction → research task.
3. **Comparator layer (§5):** if a bad task ships anyway, the outcome record
   shows the metric didn't move (or regressed), the learning is written, and the
   signal-cooldown prevents an immediate re-fire of the same bad idea.

The residual risk is a signal that is *plausible but wrong* (Level 3 is meant to
be brutally hard; the 20% who quit were never the audience). No amount of
machinery fixes taste — this is where `GAME_DESIGN.md` earning a "## Difficulty
Intent" section is worth more than another threshold. Recommend the planner add
one when it first acts on a difficulty signal, so the *next* planner knows the
intent.

---

## 2. Per-game vs. cross-game learning

**Decision: per-game only for v1.** This matches the spec's non-goals list
("cross-game aggregate analytics — not first goal") and is right for three
reasons: (a) sample sizes per game will be tiny for months; cross-game
aggregation of tiny samples manufactures confidence that doesn't exist;
(b) "Level_3" is a name, not a semantic position — scene names don't align
across games without a normalization layer nobody has built; (c) the per-game
loop must demonstrably work before the meta-loop is worth anything.

### Minimum session gates (per-game trust levels)

| Tier | Sessions (30d, non-agent) | Behavior |
|---|---|---|
| `SILENT` | < 20 | No injection at all. Dashboard shows counts only. |
| `OBSERVE` | 20–49 | Signals injected, labeled `confidence: OBSERVE`. Prompt instructs: may inform prioritization among already-planned work; may NOT create new tasks or set the sprint goal. |
| `ACT` | ≥ 50, and the specific signal's numerator ≥ 10 | Full behavior per §1. |

The per-signal numerator gate matters: 50 sessions with 3 quits at Level 3 is a
6% rate built on 3 events — the *signal* stays OBSERVE even though the *game* is
ACT-tier. Rates without absolute floors are how you ship a balance patch because
two friends got bored on the same night.

### Cross-game: define the schema now, build it later

When ≥ 3 games reach ACT tier, cross-game patterns become worth mining. Reserve
the shape so per-game records are aggregatable later without a migration:

```json
{
  "pattern_id": "difficulty_wall_early",
  "abstraction": "quit spike co-occurring with death spike in first 3 scenes",
  "games": ["raccoon-city", "ashwalker", "glowworm-cavern"],
  "instances": [{"game": "...", "signal_id": "S1", "n": 29, "outcome_id": "..."}],
  "min_games": 3,
  "min_instances_per_game": 10,
  "verdict": null
}
```

Key rule for avoiding spurious correlation: a cross-game pattern requires the
signal to have been **independently classified in each game** (each with its own
sample gate met) — never pool raw events across games and classify the pool.
Pooling launders three games' noise into one game's worth of "signal."
Cross-game verdicts write to `data/SWARM_KNOWLEDGE.md` and the learnings system
(the existing cross-run memory), not into per-game prompts directly. Tripwire to
build it: three games at ACT tier for 30+ days, or the same classification
firing in 3+ games (goes in ROADMAP-NON-GOALS with the other tripwires).

---

## 3. The decision logic — code or prompt?

**Decision: thresholds and classification in code; interpretation and task
authorship in the prompt.** Split rationale:

- **In code** because: thresholds must be consistent across runs (an LLM asked
  "is 20% high?" answers differently at temperature), testable (`pytest` the
  classifier against synthetic event streams), tunable in one place, and cheap
  (SQL, not tokens). Also because the completion-exit exclusion (§1) is a
  correctness rule, and correctness rules do not belong in prose an LLM might
  creatively reinterpret.
- **In prompt** because: mapping a classified signal to a *task* requires
  reading the codebase, the design doc, and the existing queue — exactly what
  the planner already does, and exactly what code can't do. The LLM's job is
  causal narrative and remedy selection, not statistics.

The anti-pattern to reject explicitly: dumping the raw `/summary` JSON into the
prompt and adding "reason carefully about what this means." That outsources
statistics to the component worst at it, burns tokens re-deriving the same
medians every sprint, and makes signal quality unauditable (you can't test what
the LLM concluded from raw stats; you can test `classify_signals()`).

### The classifier (deterministic, in `swarm/telemetry_signals.py`)

```
eligibility:
  n = non-agent sessions, last 30d
  n < 20                                    → tier SILENT, emit nothing
  20 ≤ n < 50                               → tier OBSERVE
  n ≥ 50                                    → tier ACT (per-signal numerator ≥ 10 still required)

per-scene exits (post-victory sessions excluded):
  quit_pct(S) ≥ 0.15:
    scene is last implemented & no victory in any session → content_cliff(S)
    error events or missing-quit sessions clustered at S  → crash_exit(S)
    deaths(S) ≥ 2× median & retries ≥ 2                   → hard_wall(S)
    deaths(S) ≤ median & time(S) ≥ 1.5× median            → boredom_exit(S)
    else                                                  → unclassified_exit(S)

global:
  top death cause ≥ 60% of deaths, total ≥ 30             → death_cause_skew
  victory_rate < 0.05 (ACT tier)                          → completion_wall
  victory_rate > 0.90 AND median_playtime < 300s          → too_easy_or_short
  sessions ending with no quit AND no victory ≥ 10%       → crash_rate_high (priority 100 bug lead)
```

Each emitted signal: `{signal_id, kind, scene, metrics{}, hypothesis,
confidence_tier, window, n, numerator}`. `signal_id` is a stable hash of
`(game, kind, scene, window-month)` so cooldowns and outcome joins work.

### Tuning without overfitting to early noise

- Defaults above are deliberately conservative and live in `config.json` under
  `telemetry_thresholds{}` (same pattern as `escalation_policy`). **Global, not
  per-game** — per-game threshold knobs at n<100 is overfitting formalized.
- **No automatic tuning until the outcome table (§5) has ≥ 20 resolved
  outcomes.** Then the question "did acting on OBSERVE-tier signals produce
  worse outcomes than ACT-tier?" is answerable from data, and thresholds move
  by hand, once, with a written rationale. Resist closed-loop auto-tuning
  entirely — a system that adjusts its own sensitivity based on outcomes it
  caused is a two-loop stability problem this codebase does not need.
- Windows are rolling 30d. Version-split (§5) handles before/after comparison;
  don't try to encode it in thresholds.

---

## 4. Which prompts get injection, and how

### Targets

| Prompt | Injection | Rationale |
|---|---|---|
| `project_plan.yaml` | **Full signals block**, always (when tier ≥ OBSERVE) | The planner is the only agent that converts signals into work. Primary consumer. |
| `audit.yaml` | Full signals block | The audit writes `CONFORMANCE_REPORT.md`, which is STEP 1 input to the next planner — telemetry belongs in the same "state of the game" picture. Cheap, high leverage. |
| `qa.yaml` / `hybrid_qa` / `harness_qa` | **One line only:** `Player telemetry: top quit scenes Level_3 (20%), Level_5 (8%) — prioritize these in your test plan.` | Steers QA attention to where players actually break, without competing with the design-doc-driven test plan. |
| `bug.yaml` / `feature.yaml` / `polish.yaml` | **Only when the task carries `metadata.telemetry_evidence`**, and then only that task's own evidence — never the full game block. | An implementing agent fixing an unrelated HUD bug gains nothing from quit-rate tables; it's pure distraction in the shortest prompts. The evidence for *its own* task is already in the description (§1); the metadata block adds machine-readable numbers. |
| `research.yaml` | Same task-scoped rule as bug/feature | Research feeders investigating a telemetry-driven task need the numbers. |
| unified chat (`api_chat.py` `_build_state_snapshot()`) | Per-project one-liner (sessions, victory rate, top quit scene) | Spec already calls for this; it's for the human, not agents. |

Not injected: `refactor`, `art_pass`, `playthrough_bot`, `plan` (python),
meta-agent prompts. No plausible action path from player behavior to their work.

### Format: structured block with reasoning instructions

Fenced, labeled, delimited — matching the existing house style (`RETRY CONTEXT`
JSON blocks, `## PROJECT CONTEXT PACKET`, `## Recent Project Broadcast`):

```
## PLAYER TELEMETRY SIGNALS (n=142 sessions, 30d, tier: ACT)
Signals are classified leads, not verdicts. For each signal you act on:
corroborate against the code and GAME_DESIGN.md first; if the code contradicts
the signal, create a research task instead of a fix task. Copy the signal_id
into any task you create from it.

- [S1:hard_wall] Level_3 — quit 20% (29/142), deaths 4.1/session vs 1.6 median, ...
- [S2:death_cause_skew] "enemy" = 74% of deaths (412/557)

Raw aggregates: victory_rate=0.31, median_playtime=312s, median_deaths=4.2
```

Structured data + explicit reasoning contract. Not bare JSON (invites the LLM to
pattern-match "data blob, skim it"), not free prose (loses the signal_id
plumbing the comparator needs).

### Budget control

`render_telemetry_block()` takes a `max_chars` argument enforced in code:
- `project_plan` / `audit`: 2,400 chars (~600 tokens), top 5 signals by
  (tier, numerator), raw aggregates always last and droppable.
- `qa`: 200 chars, one line.
- Task-scoped evidence: 400 chars.

The block is prepended into the `description` pipeline in
`generate_task_script()` (see §6), which already stacks broadcast → validation
state → knowledge → learnings → commits → notes. Telemetry slots between the
context packet and project notes. For planner tasks the description is short
("Plan the next sprint"), so 600 tokens of telemetry cannot drown it. For
implementing tasks the task-scoped 400-char cap plus the "only own evidence"
rule keeps the ratio sane. One structural guard: telemetry is injected *below*
the retry-context prefix, so on retries the failure context still wins the
recency/primacy fight.

---

## 5. Feedback loop integrity

This is the part `telemetry-spec.md` is silent on, and it's the part that
separates "telemetry-themed prompt decoration" from a learning loop.

### Detecting that an agent acted on telemetry

Explicit plumbing, not inference:

1. Planner copies `signal_id` into created tasks → `create_tasks_file_aware`
   passes through `metadata.telemetry_evidence`:
   ```json
   {
     "signal_id": "S1-raccoon-city-hard_wall-Level_3-2026-07",
     "kind": "hard_wall",
     "metric": "quit_pct@Level_3",
     "baseline": 0.20,
     "baseline_n": 142,
     "baseline_window": "2026-06-13..2026-07-13",
     "target_direction": "down",
     "game_version_at_creation": "1.0.3"
   }
   ```
2. Task completion is detected the normal way (`_finish_agent()`); nothing new.
3. The commit that closes the task ships in some later `game_version` — and
   `session_start` already carries `game_version` (this is the join key; the
   spec included it, almost accidentally, and it's the most load-bearing field
   in the whole schema).

### Measuring whether it helped: the outcome comparator

A periodic job (piggybacks on the existing monitor-thread scheduler cadence, like
Cartographer's interval check — do **not** add a new thread):

```
for each completed task with telemetry_evidence and no resolved outcome:
    post = sessions where game_version > version_at_creation, non-agent
    if len(post) < 20: outcome stays PENDING (check again next cycle)
    else:
        recompute metric over post-window
        improved  = moved ≥ 25% relative in target_direction
        regressed = moved ≥ 25% against
        write outcome row {signal_id, task_id, baseline, post_value, post_n, verdict}
        append one-line verdict to learnings for (project, task_type)
```

Outcomes land in a `telemetry_outcomes` table in `telemetry.db` and surface in
the analytics dashboard next to `research_feeder_roi` — it's the same genre of
question ("do our reflexes actually help?") that `swarm/analytics.py:mechanisms()`
already answers for recovery mechanisms.

Two honest caveats. **Latency:** the loop closes in days-to-weeks (needs real
players on a new version), so outcomes tune the *system* (thresholds, prompt
wording, planner trust) — they cannot gate the *task* that produced them.
**Attribution:** if three telemetry tasks ship in the same version, per-task
attribution is confounded. Record all task_ids sharing a version window and mark
the outcome `confounded: true`; don't pretend otherwise. Version-batching
telemetry fixes (one per release) is the operational fix, not a code fix.

### The runaway-loop risk (bad task → worse metrics → more bad tasks)

Real risk, four guards:

1. **One open task per signal_id.** The classifier suppresses any signal that
   has a pending/in_progress task or a PENDING outcome. Identical to the
   research-feeder dedupe guard. This alone kills the tight loop.
2. **Cooldown after resolution:** a signal whose outcome resolved (either way)
   can't re-fire for 30 days or until a new game_version has ≥ 20 sessions,
   whichever is later.
3. **Regression escalation, not regression retry:** a `regressed` verdict does
   NOT auto-create a revert task (that's the runaway). It flags for human review
   via the existing `human_review_flag_enabled` path and writes a learnings
   entry. Two `regressed` verdicts on one game → telemetry injection for that
   game drops to OBSERVE tier until a human resets it.
4. **Budget:** max 2 telemetry-driven tasks per sprint plan, enforced in the
   prompt and checked in `create_tasks_file_aware` (count evidence-carrying
   tasks; reject the surplus).

### Interaction with the validation system

Blunt version: **post-task validation cannot verify "fixed the quit-point," and
must not try.** Validation checks compile/tests/GUT in a worktree, synchronously.
Player behavior change is measurable only after release, asynchronously, by the
comparator. Conflating these would either block merges for weeks or fake the
verification.

So telemetry-driven tasks are held to *proxy criteria* at validation time —
checkable now, correlated with the metric later:

- `hard_wall` fix → playthrough_bot completes the scene within N attempts
  (bot-checkable, and the planner writes it into the task, §1)
- `crash_exit` fix → error signature gone from headless run / GUT test added
- `boredom_exit` fix → weakest proxy; often just "change shipped + bot still
  completes" — accept that and let the comparator carry the real verdict

The comparator is the second, slower validation pass. Structurally this mirrors
what the codebase already believes (pre-flight baseline → act → post-diff →
delayed verdict); telemetry just stretches the timeline from minutes to weeks.

---

## 6. Build spec

### New code (beyond telemetry-spec.md's build order)

| # | Item | Where | Size |
|---|---|---|---|
| 1 | Signal classifier: `compute_signals(game, db) -> list[Signal]`, thresholds from config, tiers, dedupe/cooldown checks, completion-exit + agent-session exclusion | **`swarm/telemetry_signals.py`** (new, pure functions over `telemetry_db`, mirrors `analytics.py` style) | ~250 lines |
| 2 | Renderer: `render_telemetry_block(game, audience, max_chars) -> str`; `audience ∈ {planner, qa, task_evidence}` | same module | ~80 lines |
| 3 | Injection: one guarded block in `generate_task_script()` (`swarm_runner.py`), after `_project_context_packet`, gated by task type / `metadata.telemetry_evidence` | existing file | ~25 lines |
| 4 | `metadata.telemetry_evidence` pass-through in `create_tasks_file_aware` + per-plan budget check | `swarm/tools/core.py`, `api_tasks.py` | ~30 lines |
| 5 | Outcome comparator: `resolve_pending_outcomes()` on monitor cadence; `telemetry_outcomes` table | `swarm/telemetry_signals.py` + `telemetry_db.py`; call site in `api.py` monitor loop | ~120 lines |
| 6 | `GET /api/telemetry/<game>/signals` (dashboard + debugging: exactly what the planner will see) | `swarm/api_telemetry.py` (already in spec's build order) | ~30 lines |
| 7 | Spec fixes: exclude post-victory quits from `top_quit_scenes`; add `is_agent` session flag; keep `game_version` mandatory | amend `telemetry-spec.md` + `analytics_reporter.gd` | — |
| 8 | Prompt edits: `project_plan.yaml` STEP 2e "PLAYER SIGNAL CHECK" (+ signal→task rules, budget, defer-with-reason rule); one-line hooks in `qa.yaml`, `audit.yaml` | prompts | — |
| 9 | Optional, cut first: `telemetry_query` read-only tool for `plan`/`audit`/`research` types (drill into per-session data when a signal is `unclassified_exit`) | `tool_dispatch.py`, `tools/core.py` | ~60 lines |
| 10 | Tests: classifier against synthetic event streams (every row of the §3 table, both tier boundaries, victory-exclusion, agent-exclusion); comparator verdicts; injection gating | `tests/test_telemetry_signals.py` | ~300 lines |

Config additions: `telemetry_thresholds{}`, `telemetry_injection_enabled`
(global kill switch, default true once shipped), reuse `human_review_flag_enabled`.

### Where the injection logic lives — and where it doesn't

**`swarm/telemetry_signals.py`, called from `generate_task_script()`.** Not
inline in the prompt loader: `prompts.py` is a dumb template renderer and should
stay that way — it has no DB access and per-task-type context assembly already
lives in `generate_task_script()`'s prepend stack. Not in `agent_runtime.py`:
that's the child process; signals should be baked at script-generation time so
the log shows exactly what the agent saw (auditability — same reason retry
context is baked in).

### API surface

**The planner does not call a function; the context arrives pre-computed.**
Consistent with every other context source in the system (learnings, broadcast,
validation state, project notes — all injected, none pulled). The prompt
template pulls nothing; code pushes a rendered block into `description`. The
optional `telemetry_query` tool (item 9) is the only pull path, restricted to
read-only task types, and it is deliberately last on the build list: ship the
push path, watch a planner use it for two sprints, and only add the pull path if
`unclassified_exit` signals actually occur and stall on missing detail.

### Build order (supersedes items 6–7 of the spec's order)

1. Items 1, 2, 6, 7 + tests — signals computable and visible in dashboard,
   **nothing injected yet.** Run for 2+ weeks against QA-bot traffic to shake
   out the classifier (agent sessions are fine for pipe-testing; they're
   excluded from signals but the raw counts prove the plumbing).
2. Items 3, 8 — inject into `project_plan` + `audit` only, OBSERVE-tier
   semantics forced regardless of n (planner may reprioritize, not create).
   Read the plans it produces. This is the cheap place to catch prompt-level
   misreads.
3. Items 4, 5 — evidence metadata + comparator. Unlock ACT tier.
4. Item 9 if warranted; cross-game (§2) only on its tripwire.

### What won't work — stated plainly

- **Injecting raw `/summary` JSON and hoping** — untestable, inconsistent,
  token-expensive. This is the default lazy implementation; the classifier
  exists specifically to forbid it.
- **Validating "quit rate improved" at merge time** — timescale mismatch;
  proxy criteria + async comparator or nothing.
- **Auto-tuning thresholds from outcomes** — two coupled feedback loops on
  noisy, confounded, weeks-latency data. Hand-tune from the outcomes table.
- **Cross-game pooling of raw events** — noise laundering; classify per-game,
  aggregate verdicts only.
- **Any of this mattering before games have players.** The comparator needs
  ~20 post-fix sessions per verdict. Below that traffic level the honest
  description of this feature is "telemetry-flavored prompt text." Build
  stages 1–2, then go solve distribution (ROADMAP #14) before building stage 3
  expectations into anyone's mental model.
