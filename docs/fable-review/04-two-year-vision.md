# The Two-Year Arc — From Green Checkmarks to a Studio With Players

*Fable deep review, 2026-07-13.*

## Where the system actually is

The hard part is done and mostly unappreciated: **the system can prove a game is
playable without a human.** The playthrough bot (`swarm/tools/playthrough_kit.py` +
the receipt gate in `agent_finish.py`) is a machine-checkable "a stranger could play
this" oracle — zero LLM in the loop, real input injection, milestone ladders, agency
evidence required before a completion counts. Six games pass it. Adaptive-flat routing
is the economic engine (cheap loops for reading, strong for writing, cheap completion
blocked). Analytics (`ship_candidates`, `value_repair`, `research_feeder_roi` at 70%
across 581 feeders) can already rank the portfolio by shippability.

What does *not* exist: an export, a publish step, a single player, a single byte of
player data, and any mechanism by which game N+1 is better than game N for reasons
other than pipeline improvements. The studio builds games into a vault.

The two-year arc is one sentence: **extend the closure loop past the repo boundary,
through players, and back into GAME_DESIGN.md.** Everything below is sequencing that
sentence.

## The full loop, end state

```
   GAME_DESIGN.md ◄────────────────────────────────────────────┐
        │                                                       │
        ▼                                                       │
   plan → build → validate → QA → art → playthrough bot        │
        │              (all exists today)                       │
        ▼                                                       │
   EXPORT GATE  godot --headless --export-release "Web"        │
        │       artifact + checksum in closure spec             │
        ▼                                                       │
   PUBLISH      butler push → itch.io (web/HTML5)              │
        │       first publish: human approves; patches: auto    │
        ▼                                                       │
   PLAYERS      telemetry.gd autoload → public relay            │
        │       sessions, funnels, quit-points, crashes, FPS    │
        ▼                                                       │
   INGEST       controller pulls relay → player_sessions,       │
        │       player_events tables (same SQLite)              │
        ▼                                                       │
   SYNTHESIZE   funnel analysis → balance/bug tasks w/ evidence │
        │       comments/ratings → triage agent                 │
        ▼                                                       │
   PORTFOLIO    cross-game retention/mechanic analytics ────────┘
                → next design doc (human-reviewed)
```

Each arrow is a subsystem. Order of construction below is chosen so that every stage
is immediately useful without the stages after it, and so player data starts
accumulating as early as possible — it's the only asset in this system that cannot be
built retroactively.

---

## Phase 1 (months 0–3): Ship what's already done

### 1a. Export gate (weeks 1–3)
Godot 4 exports headlessly: `godot --headless --export-release "Web" build/index.html`
given export templates + an `export_presets.cfg`. Build this as **validation
infrastructure, not a new agent task type** (consistent with non-goals #1):

- `templates/godot/export_presets.cfg` — canonical Web preset, synced like
  state_server.gd. One-time `project_create` scaffold addition.
- `swarm/export.py` — `build_web_export(project) -> (ok, artifact_path, log)`.
  Runs in the heavy worker pool from doc 03 (or a daemon thread pre-Phase-3).
- Closure spec gains a required gate: `web_export_builds: true`. A project isn't
  closure-green until the export compiles. This catches a real bug class nothing else
  catches today: GDExtension/platform-specific code that parses fine but breaks WASM.
- Artifacts land in `data/builds/<project>/<git-sha>/` with the receipt pattern
  already used for playthrough traces.

Why first: it's a week of work, it's fully machine-checkable (fits the existing
closure machinery with zero new judgment calls), and every later phase needs the
artifact.

### 1b. Publish step (weeks 3–8)
itch.io + `butler` (its official CLI: `butler push build/ user/game:html5`) is the
right target: free hosting, HTML5-native, API for stats, no review process, and the
audience expects small weird games — a perfect match for an autonomous studio's output
distribution.

- `swarm/publish.py` — butler wrapper + a `releases` table (project, version, sha,
  channel, published_at, itch_url).
- **Human gate, structured:** first publish of any game enters the review queue
  (roadmap #11 — build that queue now, this is its first real customer). The human
  checks store page copy, screenshots, name collisions, and reputational floor.
  Subsequent patches to an already-published game auto-publish when: playthrough bot
  passed on the exact sha + closure green + export gate green. This is the correct
  autonomy boundary: *the system earns auto-publish per game, not globally.*
- Store assets (cover image, screenshots, description) are an `art_pass`-adjacent
  agent task — the vision stack already takes screenshots; GAME_DESIGN.md already
  contains the pitch.

Deliverable for Phase 1: **the six bot-passing games are live on itch.io.** This is
also roadmap #13's marketing moment — "an autonomous studio shipped these" is the
README demo.

### 1c. Do the doc-03 Phase 0–2 hardening in parallel
Per-project merge locks, atomic claims, worker pools. The publish/telemetry work adds
new long-running jobs (exports, butler pushes); they should land on the worker-pool
substrate, not as more daemon threads.

---

## Phase 2 (months 2–6): Players generate data

### 2a. Telemetry autoload (the StateServer pattern, aimed outward)
`templates/godot/autoload/telemetry.gd` — same discipline as state_server.gd: canonical
template, synced to projects, never hand-written per game.

- Events: `session_start/end`, `milestone` (reuse the playthrough bot's milestone
  ladder — **the bot's progress instrumentation and player analytics are the same
  schema**, which is the elegant part), `death`, `quit_point` (scene + game_state
  snapshot on exit), `error` (script errors caught via Godot's logger), `fps_bucket`.
- Batched, fire-and-forget POSTs; hard cap ~1 event/sec; anonymous session UUID, no
  PII, and a visible "anonymous analytics" line on the store page. Fail silent when
  offline — telemetry must never affect gameplay.

### 2b. The relay (the one piece that can't run on the Mac)
Players' browsers can't reach the local controller directly. Cheapest correct answer: a ~100-line
ingestion relay (Cloudflare Worker + KV/R2, or a $5 VPS with nginx + append-only
JSONL) that accepts POSTs, buffers, and lets the controller **pull** on a schedule.
The controller stays a pull-only system with no inbound exposure — consistent with the
security posture decision (roadmap #12). Controller-side: `swarm/telemetry.py` sync
job → `player_sessions` / `player_events` tables in the same SQLite file. At indie-web
scale (hundreds of sessions/day across the portfolio) this is nothing.

### 2c. First consumer: the live health gate
Add to closure specs: `crash_free_rate`, `median_session_seconds`,
`funnel_completion` per milestone. A published game whose live crash rate spikes gets
a regression opened by the existing closure/regression machinery — **the closure
system doesn't need new concepts, just a new evidence source.** This is deliberate:
every phase here feeds an existing mechanism rather than growing a parallel one.

Why now and not later: player data compounds and cannot be backfilled. Every month a
game is live without telemetry is a month of the only novel dataset this project can
produce, discarded.

---

## Phase 3 (months 4–10): Feedback becomes tasks

This is where "autonomous studio" stops being a metaphor. Three synthesizers, built in
order of signal quality:

1. **Crash → bug task (month 4–5).** Highest-precision signal. A recurring `error`
   event with a stack/scene fingerprint creates a bug task with the fingerprint,
   affected session count, and the game_state snapshots as evidence — exactly the
   shape the research-feeder pipeline already consumes. Dedupe by fingerprint (the
   validation-bug-task dedupe pattern already exists). **No human in this loop** —
   it's the same trust level as validation-spawned bug tasks today.
2. **Funnel → balance task (month 6–8).** "58% of sessions end at wave 3; design doc
   says waves 1–5 are the on-ramp" → a `polish`/`bug` task quoting the design doc
   section and the funnel numbers. Medium precision: cap at N open telemetry-sourced
   tasks per project, all tagged `metadata.telemetry_sourced` so analytics can measure
   whether they actually move retention (the same value/repair methodology as run-11).
   The design doc is the referee — the agent tunes *toward the doc*, never rewrites
   it. Doc changes are human-gated, full stop (non-goals #10).
3. **Comments/ratings → triage (month 8–10).** itch comments and ratings via API →
   the existing `triage` task type. Lowest precision, human-reviewed via the review
   queue before becoming work. This is also where "which games deserve more
   investment" gets its first external signal.

The agent pipeline change is small and prompt-level: tasks carry a **telemetry
context block** (this game's live funnel, crash rate, rating) injected at
prompt-build time the way project activity context already is. Agents fixing a
telemetry-sourced bug see the player evidence, not just the description.

---

## Phase 4 (months 9–18): The portfolio flywheel

With ~15–25 published games and 6–12 months of player data, the studio gets the thing
no other agent-orchestration project has: **ground truth about which of its outputs
humans actually like.**

- **Cross-game mechanic analytics.** GAME_DESIGN.md files are structured enough to
  tag mechanics (merge, tower-defense, roguelite, idle...). Join mechanics × retention
  × session length × completion across the portfolio. This is SQL over data that
  phases 2–3 accumulated, in the spirit of roadmap #7.
- **Design synthesis, human-gated.** A `design_brief` generator: "propose 3 one-page
  game concepts optimizing for what retained players across the portfolio, avoiding
  the mechanics that didn't." Output goes to the review queue; the human picks and
  edits; the wizard scaffolds it. The human is doing *taste*; the system is doing
  *evidence*. Over time the briefs get better and the edits get smaller — measure
  edit-distance as the autonomy metric (non-goals #10's "earned per subsystem" rule,
  applied to creative direction).
- **Design-level experiments.** The run4–run11 experiment harness generalizes from
  *pipeline* arms to *design* arms: same mechanic, two on-ramp difficulty curves,
  shipped as two games or one game with a variant flag, judged by real retention. The
  experiment muscle already exists; only the metric source changes.
- **Economics (roadmap #18) becomes real.** Cost per shipped game is already
  computable (`estimated_cost_usd` per agent is in the DB). Now the denominator gets a
  quality weight: cost per *retained player-hour*. Routing policy (#8), meta-agent
  keep/kill decisions (#16), and "which game gets a content update" all get judged by
  the same number. When the planner sees "this sprint costs ~$X and this game's
  player-hour value is Y," the swarm crosses from babysat to allocated.

---

## Phase 5 (months 18–24): Cadence and consequences

Not new systems — the discipline of running the loop:

- **Steady cadence** — e.g. one new game every 2 weeks, one content update per week
  across the live portfolio, fully pipeline-driven. Cadence is the forcing function
  that surfaces every remaining manual step; the review queue's items-per-week is the
  measure of what's left.
- **Sunset policy.** A studio also kills games. Telemetry-driven: no sessions in 60
  days and below portfolio-median rating → archive, delist, stop maintenance tasks.
  Autonomous studios accumulate zombie maintenance load otherwise — this is the
  closure system's terminal state that doesn't exist yet ("shipped, retired").
- **Decide the monetization question with data, not now.** If any game shows real
  traction (tripwire, non-goals #11), the managed-studio option opens. If not, the
  open-source studio identity (#13) is the payoff and the portfolio is the demo reel.
  Nothing in phases 1–4 forecloses either.
- **Federation only if quota stops being the ceiling** (roadmap #17's own tripwire).
  Two years out, local models on Apple Silicon plausibly take the cheap tier of
  adaptive-flat entirely — that changes the economics more than a second Mac would.

## Where the human is, and isn't

| Loop | Human role | Basis |
|---|---|---|
| crash → bug task → fix → auto-republish | none (after a game earns auto-publish) | same trust as validation bugs today |
| funnel → balance task | none to create; analytics judges outcomes | capped + tagged, design doc is referee |
| first publish of a game | approve (review queue) | reputation is unrecoverable; taste |
| design doc changes | approve, always | non-goals #10 |
| new game briefs | pick + edit | taste; edit-distance tracked as autonomy metric |
| pricing / store identity / sunset overrides | decide | judgment calls with external consequences |

## Build order, restated as a dependency graph

```
export gate ─► publish ─► telemetry autoload+relay ─► ingestion tables
   (1a)          (1b)              (2a/2b)                  │
                   │                                        ├─► live health gates (2c)
review queue ──────┘                                        ├─► crash→bug (3.1)
(roadmap #11, built for 1b)                                 ├─► funnel→balance (3.2)
                                                            └─► comments→triage (3.3)
doc-03 phases 0–2 (worker pools) ─► all long-running build/publish jobs      │
analytics #7 (exists, extend) ◄─────────────────────────────────────────────┘
                                        │
                                        ▼
                        portfolio analytics ─► design synthesis ─► design experiments
                              (4)                    (4)                 (4)
                                        │
                                        ▼
                          economics: cost per retained player-hour (4→5)
```

The two rules from the roadmap still govern: nothing structural before tests are
trustworthy (doc-03 hardening rides along in Phase 1), and nothing speculative before
analytics can judge it (every synthesizer in Phase 3 ships with its own outcome
metric, and Phase 4 is gated on Phase 2's data actually existing).

The one-line test for any proposed work item over the next two years: **does it move
a bit of player behavior into the loop, or does it polish the part of the loop that
already works?** The second kind is how this system spent its first year, and that was
correct — the pipeline had to be proven. It is proven. The scarce asset now is not
agent capability; it's evidence from players. Go get it.
