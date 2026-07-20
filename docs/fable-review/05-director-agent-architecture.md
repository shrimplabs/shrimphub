# The Director Agent — Architecture

*Fable deep review, 2026-07-13. Companion to docs 01–04; assumes the two-year loop
from doc 04 (export → publish → telemetry → ingest) as context.*

## What the Director is, and what it is not

The Director is the component that makes the decisions a human currently makes:
which games exist, which ones die, where the budget goes, what "good enough to
ship" means, and when the studio changes direction. It is the CEO/creative
director role, mechanized.

Three framing decisions before any architecture:

**1. The Director is not a meta-agent.** Gardener, Librarian, Archaeologist et
al. are janitors — they maintain the machine. The Director *steers* it. If you
build it in the `api_gardener.py` pattern (interval scheduler + task spawner),
you will get a sixth janitor that occasionally kills a project, which is worse
than nothing. The Director needs a different substrate: a decision journal, an
action vocabulary, and a calibration loop — none of which the meta-agent
pattern has.

**2. The Director does not execute.** The orchestrator owns *execution
authority*: which pending task runs next, which agent gets which slot, retry
and escalation. The Director owns *portfolio authority*: which projects exist,
their life stage, their budget, their priority weight, their fate. The
interface between the two is a small set of declarative directives, not direct
task manipulation. This division is what prevents races and authority conflicts
(§ "Authority model" below).

**3. The Director's decisions are slow.** Strategic decisions have feedback
latencies of days to weeks. A Director that runs every monitor cycle is a
noise generator burning tokens on re-deciding things whose evidence hasn't
changed. The architecture below deliberately separates a *fast, LLM-free
ledger* (data) from a *slow, LLM-driven session* (decisions).

---

## The two-tier loop

### Tier 1: the Portfolio Ledger (mechanical, no LLM, cheap)

A pure-Python aggregation job — `swarm/director/ledger.py` — recomputed hourly
(or on demand). It joins ground truth that already exists into one row per
project:

| Field | Source (exists today?) |
|---|---|
| `closure_status` (open/stalled/frozen) | `closure/status.py::derive_closure_status` ✅ |
| gate summary, open regressions | `derive_gate_summary` ✅ |
| playthrough receipt (pass/fail/absent, age) | `agent_finish.py` receipt gate ✅ |
| value/repair ratio, 7d and 30d windows | `/api/metrics` `value_repair` ✅ |
| ship-candidate rank | `ship_candidates` analytics ✅ |
| research feeder ROI on this project | `research_feeder_roi` ✅ |
| tokens spent 7d/30d, per task type | `agents` table `input_tokens`/`output_tokens` ✅ (needs per-project rollup) |
| est. dollar cost 7d/30d | ❌ needs provider price table (roadmap #18) |
| tasks completed/failed 7d, queue depth | tasks table ✅ |
| last commit age, LOC delta 7d | git ✅ |
| QA state: cycles used, QA_REPORT.md open issues | ✅ (needs parsing) |
| telemetry summary: sessions, victory rate, funnel, quit points | ❌ until roadmap #15 ships |
| publish state: exported? published? version live? | ❌ until roadmap #14 ships |
| **stage** (see below) + weeks in stage | new column, `projects` table |
| Director's active predictions about this project | new, decision journal |

Stored as a snapshot table (`director_ledger`, one row per project per
computation, append-only). The ledger is *the only thing the Director is
allowed to see as fact.* Anything not in the ledger is not evidence. This is
the first anti-vibes mechanism: the input surface is enumerable, versioned,
and auditable, so every decision can be traced to specific ledger fields.

The ledger also computes **diffs since the last Director session** — the
agenda generator. "raccoon-city value/repair fell 2.1→0.8 over 14d",
"tetris-neon reached closure green", "portfolio spend $41 last 7d vs $28
budget." Diffs, not absolutes, are what trigger and structure sessions.

### Tier 2: the Director Session (LLM, expensive, rare)

A session is a "board meeting." It runs:

- **On schedule:** weekly. Not daily — the feedback loop on portfolio
  decisions is slower than a day, and a daily Director will oscillate.
- **On triggers**, evaluated by the ledger job (mechanical predicates, no LLM):
  - a project reaches closure green + playthrough receipt (ship decision needed)
  - a project's task queue empties and auto-replan is off (continue/kill decision)
  - portfolio spend crosses budget threshold (reallocation needed)
  - a project has been `stalled`/`frozen` > N days despite Archaeologist attempts
  - an experiment run completes (codify/discard decision)
  - post-telemetry: launch-week data lands, or an anomaly fires (quit-rate spike)
- **Never** mid-experiment for arm projects (check `experiment_metadata` tags —
  the Director re-prioritizing arms mid-run destroys the experiment).

One session at a time, serialized like the monitor thread. A session that
crashes leaves no partial directives (directives are written in one
transaction at session end).

Session shape:

1. **Calibration review** (injected, not optional): every past decision whose
   `review_by` date has arrived, with its recorded prediction and the actual
   ledger outcome. The Director must grade each: confirmed / falsified /
   inconclusive. Grades append to `data/director/calibration.md`, which is
   injected into every future session. *A Director that cannot see its own
   track record will confidently repeat its mistakes forever.*
2. **Agenda** (generated from ledger diffs + fired triggers).
3. **Deliberation** with read-only tools (query ledger history, read a
   QA_REPORT.md, read a design doc, view the quality dossier — doc 07).
4. **Decisions**, emitted only through the action vocabulary below.
5. **Journal entry**: full reasoning trace written to
   `data/director/journal/<date>.md`, append-only. The journal is for the
   human and for future calibration — it is never re-injected wholesale
   (that's how you get self-reinforcing narratives).

---

## The action vocabulary

The Director cannot do arbitrary things. It emits typed directives into a
`directives` table; each type has a schema, mandatory fields, and a validator.
Freeform output is journal-only, never actuated.

| Directive | Effect (applied by) | Mandatory fields |
|---|---|---|
| `set_stage(project, stage)` | projects table; orchestrator reads it | evidence[], prediction, review_by |
| `set_budget(project, tokens_per_week)` | orchestrator `fill_slots` weighting | evidence[], prediction, review_by |
| `set_priority_weight(project, w)` | task selection strategy input | evidence[] |
| `create_tasks(project, batch)` | existing `/api/tasks/batch` (chained to HEAD as always) | evidence[], rationale |
| `request_design(brief)` | wizard `/api/wizard/plan` → human review queue (v1) | rationale, portfolio_gap cited |
| `start_experiment(hypothesis, arms)` | `/swarm-experiment` machinery | hypothesis, success metric, budget cap |
| `kill_project(project)` | pause + closure freeze + queue cancel — **never deletes tasks** (immutable history rule) | evidence[], prediction, review_by, postmortem stub |
| `ship(project)` | flags for publish; pre-#14: item in human review queue | dossier reference, prediction of launch telemetry |
| `write_verdict(project, rubric_scores, text)` | quality dossier (doc 07) | rubric version, dossier reference |

**Stages** (new lifecycle, coarse on purpose):
`concept → production → polish → ship_candidate → live → sunset → killed`.
Stage is a *Director-owned* field; nothing else writes it. The orchestrator
maps stages to behavior it already has: `killed`/`sunset` ⇒ paused_projects;
`polish` ⇒ block new feature tasks (project_graph_policy already gates on
closure status — same hook); `live` ⇒ telemetry-driven tasks only.

**Every directive that changes resource allocation must carry a falsifiable
prediction with a date.** "Kill glowworm-cavern" requires something like:
"prediction: reallocating its ~180k tokens/week will raise portfolio
value/repair from 1.4 to ≥1.7 within 21 days; review 2026-08-03." The
validator rejects directives whose evidence cites ledger fields that don't
exist or whose prediction has no measurable term. This is crude — an LLM can
write vacuous predictions — but combined with the calibration review, vacuous
predictions become visible as a *pattern* ("6 of your last 8 predictions were
inconclusive; write tighter ones").

---

## Authority model — no races, no conflicts

Rules that keep the Director and orchestrator from fighting:

1. **The Director writes directives; the orchestrator applies them.**
   Directives have `status: proposed → approved → applied`, with an
   `applied_at`/`applied_by` handshake. The monitor thread picks up approved
   directives at the top of its cycle (before `fill_slots`), applies them via
   the same code paths the dashboard uses (pause, priority update, batch
   create), and stamps them. The Director never holds a DB write lock during
   deliberation, never touches `tasks.status`, never kills agents.
2. **In-flight work is never preempted.** `kill_project` cancels *pending*
   tasks and pauses the project; running agents finish their current task (or
   the human uses the existing kill switch). This keeps the dep-graph
   invariants (completed/failed rows immutable, chains intact) untouched.
3. **One writer per field.** Stage and budget: Director only. Task status:
   orchestrator only. Closure specs: **neither** — spec provenance is a
   prerequisite (doc 06); the Director grading the studio against specs the
   studio can edit is self-grading.
4. **Experiment projects are read-only to the Director** until the run's
   completion trigger fires.
5. **Human override is a directive too** — the human writes
   `override(directive_id, verdict, note)` rows through the review queue.
   Overrides feed calibration exactly like ground-truth outcomes do: an
   overridden decision is a falsified prediction about what the human would
   accept.

---

## What keeps it honest — the anti-"vibes dressed as reasoning" stack

This failure mode is the central design problem, so name the defenses
explicitly:

1. **Closed input surface.** Only ledger fields are admissible evidence.
   No "I sense this project has potential."
2. **Closed output surface.** Only the action vocabulary actuates. Eloquent
   journal entries move nothing.
3. **Mandatory falsifiable predictions** with review dates, mechanically
   scheduled back into future sessions.
4. **Calibration file** — the running score of confirmed vs falsified
   predictions, always injected. The Director's prompt literally begins with
   its own error rate.
5. **Shadow mode first** (see v1 below): weeks of decisions graded by the
   human before any decision actuates. The human's grades are the initial
   calibration corpus.
6. **Authority earned per directive type**, in order of blast radius:
   priority weights → budgets → task creation → stage changes → kill →
   design → ship. This is exactly the anti-roadmap #10 tripwire discipline
   ("autonomy earned per subsystem"), applied inside one component. Kill and
   ship stay human-approved the longest.
7. **Cross-family critique for taste inputs** (doc 07): the quality verdicts
   the Director consumes are produced by a different model family than the
   one that built the game, against a frozen rubric it cannot edit.
8. **Budget hard caps live outside the Director.** The quota guard and
   spend ceiling are orchestrator/config-level. The Director allocates
   *within* a budget; it cannot raise the budget. The entity optimizing
   spend must not control the spend limit.

What this stack does *not* solve: a Director can be perfectly calibrated on
its predictions and still have bad taste (doc 07), and it can satisfy every
mechanical check while drifting strategically (doc 08 §failure modes). The
stack bounds dishonesty; it does not manufacture judgment.

---

## The prompt, at altitude

```
SYSTEM (the charter — versioned, human-edited only):
  You are the Director of an autonomous game studio.
  Objective: maximize verified player-value per dollar across the portfolio,
    subject to: portfolio spend ≤ $B/week; ≥1 project in `live` telemetry
    collection; no more than N projects in `production` concurrently.
  [Pre-telemetry substitute objective: maximize the rate at which projects
    reach closure-green + playthrough-receipt per dollar. You are optimizing
    a proxy; say so in your journal when it matters.]
  You may only act through the directive vocabulary: <schemas>.
  Every allocation directive requires evidence (ledger field citations) and
    a falsifiable, dated prediction.
  Hard rules: never kill a project on data <14 days old; never exceed
    budget; never modify closure specs, rubrics, or your own charter;
    experiment arms are read-only until run completion.

INJECTED PER SESSION:
  1. Calibration report (your prediction track record, human override history)
  2. Portfolio ledger snapshot + diffs since last session
  3. Agenda (fired triggers)
  4. Active predictions coming due

TOOLS: ledger queries, dossier/QA-report/design-doc readers (read-only),
  directive emitters. No shell. No file writes outside data/director/.
```

Model routing: this is the one place in the system where the strongest
available model is unambiguously worth it. Director sessions are rare (~5–10
LLM conversations/week), so even Claude-class pricing is noise next to the
swarm's task spend — and a cheap model making kill decisions is a false
economy of the purest kind. Route Director sessions to the strongest
configured provider; keep the ledger and validators model-free.

---

## Minimal v1 vs the full vision

### v1 — the Board Report (build this first; ~all ingredients exist)

- `director/ledger.py`: per-project rollup from existing analytics + tokens.
  (The only new data work is per-project token/cost aggregation.)
- A weekly session, strongest model, that produces: portfolio ranking,
  proposed directives (recommend-only), predictions. Output lands in the
  human review queue (roadmap #11 — which this justifies building).
- Human approves/rejects each proposal with one click; verdicts append to
  the calibration file.
- **No actuation at all.** The deliverable is: after 4–6 weeks, a measured
  agreement rate between Director proposals and human decisions, per
  directive type.

This is genuinely useful on day one — it replaces the human's Monday-morning
"what state is everything in" survey — and it generates the evidence that
either earns autonomy or proves it premature. If the agreement rate on, say,
priority-weight proposals is >90% over six weeks, auto-apply that type and
keep the rest gated. That *is* the anti-roadmap #10 tripwire, made concrete.

### v2 — allocation authority
Auto-apply priority weights and budgets. Kill/ship/design still human-gated.
Requires: cost accounting (#18 slice), review queue live, calibration ≥6 weeks.

### v3 — lifecycle authority
Stage changes and kills actuate (with a 72h human veto window — kill
directives sit visible before applying). Requires: publish pipeline (#14) so
`ship` means something; telemetry (#15) so kill evidence includes player data,
not just pipeline metrics.

### v4 — creative authority
`request_design` goes straight to wizard; `ship` auto-publishes patches
(first publish of a new title stays human — legal/reputational surface, doc
06). This is the "full vision," and it is 12–18 months of prerequisites away
(doc 06 gives the order). There is no honest shortcut: v4 without telemetry
is a Director optimizing the studio's opinion of itself.

---

## Where it lives in the codebase

```
swarm/director/
  ledger.py        # tier-1 aggregation, snapshot writes, diff/agenda calc
  triggers.py      # mechanical predicates over ledger; enqueue sessions
  session.py       # tier-2 LLM loop (reuses llm_utils; own tool table,
                   #   NOT tool_dispatch's — write tools must be absent,
                   #   not blocked)
  directives.py    # schemas, validators, proposed→approved→applied state
  apply.py         # called from monitor cycle; maps directives onto
                   #   existing APIs (pause, priority, batch create, freeze)
swarm/api_director.py   # routes: ledger view, journal, proposals, approve/
                        # reject (the review-queue surface)
data/director/
  journal/                # append-only session transcripts
  calibration.md          # prediction scorecard, human overrides
db: director_ledger, directives tables (schema evolution as usual)
```

Deliberately *not* reused: the meta-agent scheduler pattern, the task-agent
tool loop (`agent_runtime.py`), and the task system as the Director's memory.
The Director's memory is its journal + calibration file + ledger history —
structured, small, and injected whole. It should never need context
compaction; if a session outgrows the window, the ledger is too verbose, not
the conversation too long.

---

## Addendum: market research as Director input (2026-07-13)

The portfolio ledger as designed above pulls only internal signals (cost,
completion rate, closure status, telemetry). A missing input is **external
market signal**: what genres and mechanics players are currently paying for,
independent of our portfolio.

This matters for two reasons:
1. **Cold start**: before any game ships, telemetry is empty. Market signal
   is the only taste prior the Director has.
2. **Genre selection**: without external signal, the Director can only compare
   our games against each other. It cannot detect that we're optimizing well
   within a dead category.

A **Market Research meta-agent** (weekly, web-search-based, no new tools
needed) writes `data/market/<genre>.md` files. The Director's ledger build
step reads these and adds a `market_signal` field per project (genre fit,
competitor sentiment, demand trend: rising/stable/falling).

The Director's `commission_brief` directive becomes much sharper with this
input: instead of "make another platformer because our last one did well," it
can say "puzzle games with relaxing themes are trending on itch.io; our
nearest entry is underperforming — either fix it or commission a stronger one."

*Fable to think through: full market research agent design — see queued questions.*
