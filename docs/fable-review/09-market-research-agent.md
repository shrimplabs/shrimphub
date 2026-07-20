# 09 — Market Research Meta-Agent

**Status:** design review
**Depends on:** 05-director-agent-architecture.md (consumer), existing `web_search`/`fetch_url` tools
**Feeds:** Director ledger (taste prior), `request_design` / commission briefs, kill/prioritize decisions

---

## 0. The honest framing first

The prompt for this review asks: is market research a net positive signal or
sophisticated noise? The answer depends entirely on what we claim the agent
produces.

Two hard constraints from the existing tooling shape everything below:

1. `web_search` returns **titles + snippets** (max 5 results, Tavily → Brave →
   Serper → DDG HTML scrape). Snippets are where confabulation is born — an
   LLM reading "roguelike deckbuilders continue to dominate…" in a snippet
   will happily invent the rest of the article.
2. `fetch_url` is plain urllib + html2text, **no JS rendering, no auth, 15s
   timeout**. Anything client-rendered (Steam charts widgets, TikTok, YouTube
   search, app store charts) is effectively invisible to us.

So: **we cannot honestly produce quantitative demand data** (downloads,
revenue, wishlist velocity) for most sources. What we *can* produce is
**category intelligence**: what exists in a genre, what the quality bar looks
like, what players praise and complain about, which categories are saturated
vs. sparse on the platforms we could actually publish to.

That reframing is the whole design. The agent is forbidden from emitting
numbers it didn't scrape from a page it actually fetched. Demand becomes an
*ordinal judgment with confidence and citations*, not a metric. Under that
constraint, market research is a net positive — it's exactly the taste prior
the Director lacks at cold start, and it's cheap (~30 fetches/week). Without
that constraint it is sophisticated noise, and worse than nothing, because the
Director's calibration loop would be grading decisions against invented data.

One more framing note: our portfolio is ~27 small Godot games. The relevant
market is **web game portals and itch.io**, not Steam premium indie. Most
"game market research" advice targets the wrong market for us. Source
selection below reflects that.

---

## 1. Source selection

### Worth scraping

| Source | What it gives | S/N | Auth | Frequency | Failure mode |
|---|---|---|---|---|---|
| **itch.io browse pages** (`itch.io/games/tag-<t>?sort=popular`, `/new-and-popular`) | Per-tag popularity ranking, titles, blurbs, prices, platform badges. Server-rendered, stable markup for years. The tag system is also our genre vocabulary (§2.4). | **High** — closest proxy to "what small-game players engage with" | None | Weekly | Markup change → parser gets prose-garbage; detectable (extracted <5 titles → mark source stale, don't guess) |
| **itch.io per-game pages** | Rating count, rating value, comments, "more like this". Comments are genuine player sentiment. | High for quality-bar calibration | None | Weekly, top 3–5 per tracked genre only | Same as above |
| **Newgrounds** (`newgrounds.com/games/browse` + category pages) | View counts and star ratings **visible in server-rendered HTML** — one of the only places we get real engagement numbers for web games | High, and quantitative | None | Weekly | Smaller audience skews younger; treat as one portal among several |
| **CrazyGames / Poki category pages** | What the biggest web-game portals actually feature per category; category breadth = where distribution demand is. These are the channels a publish pipeline (#14) would target. | Medium-high (curated, so it's *editorial* demand, but editorial demand is what gets a web game traffic) | None | Weekly | Heavier JS than itch; if fetch returns a skeleton page, record `source_unavailable`. Poki is more JS-y than CrazyGames — CrazyGames first. |
| **SteamSpy API** (`steamspy.com/api.php?request=top100in2weeks`, `?request=tag&tag=<t>`) | JSON, no key, real owner-estimate numbers per tag. The one quantitative Steam signal we can get honestly. | Medium — Steam is not our market, but genre *direction* (what's rising across the whole industry) leaks from it | None | Weekly | API rate-limits (1 req/s, some endpoints 60s); estimates are coarse. If JSON parse fails, skip — never infer Steam numbers from search snippets. |
| **Reddit via `.json` endpoints** (`old.reddit.com/r/WebGames/top/.json?t=week`, r/incremental_games, r/playmygame, r/roguelites) | Player *language*: what they praise, what they're sick of, what they ask for. Sentiment keywords come from here. | Medium — loud-minority bias, but it's the only free text-sentiment source we have | None (unauth JSON still works at low rates with a UA header) | Weekly | Reddit has been tightening unauth access; 429/403 → degrade gracefully, sentiment section goes stale rather than invented |
| **Steam "New & Trending" / Next Fest tag pages** (HTML) | Which genres new successful indies cluster in | Medium | None | Monthly | Partially client-rendered; extract what's in HTML, no more |

### Not worth the effort — named, per the brief

- **Discord**: not fetchable without auth, ToS-hostile to scraping, and the
  signal (hype in genre servers) is unquantifiable even by hand. **No.**
- **TikTok / YouTube trends**: the signal is real (streamer-driven hits are a
  genuine phenomenon) but both are JS/auth walls to us. Anything the agent
  "learns" about TikTok comes from search snippets about TikTok, i.e. thirdhand
  content-marketing blogspam. This is the single most likely confabulation
  vector — **explicitly banned as a source** in the prompt.
- **App stores (iOS/Android charts)**: irrelevant until the publish pipeline
  targets mobile; chart pages are JS-rendered anyway. Revisit iff #14 grows a
  mobile target (tripwire, per the non-goals doc pattern).
- **Google Trends**: no stable keyless API; unofficial endpoints break monthly.
- **Gamalytic / VG Insights / GameDiscoverCo data**: paywalled; their free blog
  posts are fine as *occasional* fetches but must be cited as commentary, not
  data.
- **Steam store search pages for demand inference**: search-result ordering on
  Steam is personalized/opaque; ranking position there is not demand signal.
  SteamSpy JSON only.

### Source registry, not prompt-embedded URLs

Sources live in a checked-in registry `data/market/sources.json`, not in the
prompt:

```json
{
  "sources": [
    {"id": "itch-popular-{tag}", "kind": "html", "url": "https://itch.io/games/tag-{tag}?sort=popular",
     "per_genre": true, "ttl_days": 10},
    {"id": "steamspy-top", "kind": "json", "url": "https://steamspy.com/api.php?request=top100in2weeks",
     "per_genre": false, "ttl_days": 10},
    {"id": "reddit-webgames-top", "kind": "json", "url": "https://old.reddit.com/r/WebGames/top/.json?t=week",
     "per_genre": false, "ttl_days": 10}
  ]
}
```

This matters for anti-confabulation (§3): the agent samples from a **fixed
menu**. It never invents a source. A human edits the registry; the agent may
*propose* new sources in its report but cannot add them.

---

## 2. Signal schema

### 2.1 File layout

```
data/market/
  sources.json                 # fixed source registry (human-edited)
  portfolio_map.json           # project → tags mapping (human-seeded, agent-proposed updates)
  state.json                   # last-run ts, per-source last-success ts
  <genre-tag>.md               # one report per tracked tag, overwritten each run
  archive/<genre-tag>-<date>.md  # previous versions (trend diffing + audit)
```

Flat files, no SQLite (§6.2 for why).

### 2.2 Report format: YAML frontmatter + narrative body

The frontmatter is the machine interface (ledger consumes it); the body is the
human/Director-session reading material. Same pattern as prompt YAMLs and
`PROJECT_MAP.md` — the swarm already lives on markdown-with-structure.

```markdown
---
genre_tag: incremental            # canonical itch.io tag (see 2.4)
related_tags: [idle, clicker]
as_of: 2026-07-13
sources_fetched:                  # every source that succeeded this run
  - {id: itch-popular-incremental, url: "https://itch.io/games/tag-incremental?sort=popular", fetched: 2026-07-13}
  - {id: reddit-incremental-top,   url: "https://old.reddit.com/r/incremental_games/top/.json?t=week", fetched: 2026-07-13}
sources_failed: [poki-idle]       # attempted, unavailable — honesty marker
demand_trend: rising              # rising | stable | declining | unknown
demand_confidence: medium         # low | medium | high — high requires ≥2 independent sources agreeing
saturation: high                  # sparse | moderate | high — supply-side density on the fetched portals
quality_bar:                      # what "table stakes" looks like at the top of the category NOW
  reference_titles: ["(the) Gnorp Apologue", "Leaf Blower Revolution"]
  table_stakes: ["offline progress", "prestige loop by minute 20", "number formatting past 1e6"]
monetization_norm: "free web + optional Steam premium port"
sentiment:
  praised: ["satisfying automation unlocks", "clear next-goal visibility"]
  fatigued: ["ad-walled mobile ports", "prestige resets with no new content"]
opportunity_notes: "portal category pages show few polished pixel-art incrementals; itch top-20 skews text-heavy"
portfolio_fit: [tiny-tower-builder, goblinchemy]   # our projects mapped to this tag
---

## Evidence
- itch popular page (fetched 2026-07-13): top 20 titles are …  [12 titles listed, 8 updated in last 90d]
- r/incremental_games weekly top: 3 of 10 posts are recommendation-requests
  mentioning "like Gnorp" — quoted: "…"
## Inference
- Demand: rising (medium confidence). Basis: …
## What changed since last report (2026-07-06)
- …
```

### 2.3 What the Director ledger consumes

The ledger build step (`director/ledger.py`, per doc 05) reads **frontmatter
only** — it stays model-free. Per tracked genre it lifts:

| Field | Ledger use |
|---|---|
| `demand_trend` + `demand_confidence` | market column in portfolio ranking; commission-brief candidate genres |
| `saturation` | penalty on commissioning into crowded categories |
| `quality_bar.table_stakes` | injected into commission briefs and QA-expectation context |
| `sentiment.praised / fatigued` | design-hook keywords for `request_design` |
| `as_of` + freshness (2.5) | staleness gating |
| `portfolio_fit` | joins market rows to internal project rows |

The narrative body is available to Director *sessions* via the existing
read-only dossier-reader tool (doc 05 already gives the Director read-only
document readers — market reports are just more dossiers; **no new Director
tool needed**).

### 2.4 Genre labels: use itch.io tags as the controlled vocabulary

Don't invent a genre ontology. itch.io's tag folksonomy is the de facto
standard for exactly our market segment, it's what we scrape anyway, and it
handles the micro-genre problem natively: **a game is a weighted set of tags,
not one genre**.

`data/market/portfolio_map.json`:

```json
{
  "tiny-tower-builder":  {"incremental": 0.7, "city-builder": 0.3},
  "fusion-foundry-td":   {"tower-defense": 0.8, "crafting": 0.2},
  "rainbow-minesweeper": {"puzzle": 0.6, "minesweeper": 0.4}
}
```

Human-seeded once (30 minutes of work for 27 projects); the agent may
*propose* mapping changes in its report but the file is human-merged. A
project's market score is the weight-averaged score of its tags. Tracked tags
= union of portfolio tags + up to N (config, default 5) "scan" tags the
Director or human adds to watch categories we're *not* in — that's the
"optimizing inside a dead category" detector: the ledger can show that
categories we have zero games in are outscoring every category we occupy.

### 2.5 Signal rot prevention

Three mechanisms, all model-free:

1. **`as_of` is mandatory and machine-checked.** Ledger builder computes age.
   `≤14d` fresh → full weight; `15–45d` stale → confidence downgraded one
   notch and rendered with a `[STALE]` badge in the ledger; `>45d` expired →
   frontmatter fields are **dropped from the ledger entirely** (the narrative
   file stays readable but the Director's ranking sees `market: no data`,
   which is honest). Old data never silently masquerades as current — it
   either wears a badge or disappears.
2. **Per-source freshness, not just per-report.** A report whose Reddit fetch
   failed still carries itch data; `sources_failed` lets the ledger show
   sentiment as stale while demand stays fresh.
3. **Archive + forced diff.** Each run archives the previous report and the
   prompt requires a "what changed" section computed against it. A source
   whose extraction silently broke (markup change → same-ish garbage every
   week) shows up as a report that *never changes* — the dashboard surfaces
   "N weeks with no material diff" as a staleness warning on the source.

---

## 3. Agent design

### 3.1 Meta-agent, Cartographer pattern — with one deviation

This is a **scheduled meta-agent**, not a one-shot task agent and not part of
the Director. Reasons:

- It's periodic, portfolio-scoped, and produces files the rest of the system
  reads — exactly the Cartographer shape (`api_cartographer.py`:
  threading.Timer scheduler → `task_upsert` a typed task → agent writes
  reports → state json persists last-run). Reuse the pattern wholesale.
- It must **not** live inside the Director. Doc 05 is explicit that Director
  sessions get no shell and no web; keeping research out-of-band preserves
  that. It also means market data is *auditable before* the Director sees it —
  the confabulation validator (3.4) runs between collection and consumption.
- One-shot research tasks (`type: research`) are the wrong tool: they're
  project-scoped, unscheduled, and their output goes into task metadata, not a
  durable signal store.

Deviation from Cartographer: the market task is **not read-only** — it writes
to `data/market/` (and nowhere else; write-path allowlist enforced the same
way `plan` tasks hard-block writes, but inverted: allow only `data/market/`).

Concurrency note: one run fetches ~`tracked_tags × 3 + 4` URLs (≈25–35 for 8
tags). Runs weekly, single agent, priority 60, `max_attempts: 1`,
`on_exhaust: cancel` — a failed market run is a non-event; last week's reports
just age per §2.5. Never spawn research feeders for it.

### 3.2 Prompt shape (`prompts/market_research.yaml`)

High level, four phases with a hard wall between observation and inference:

```
ROLE: Market analyst for a studio of small web/desktop games. You produce
category intelligence, not market statistics.

INPUTS (injected): sources.json registry; portfolio_map.json; tracked tag
list; previous report per tag (full text); per-source last-success dates.

PHASE 1 — COLLECT. For each tracked tag, fetch_url the registry sources for
that tag (registry URLs only — you may not fetch any URL that is not in the
registry or linked directly from a registry page). Record raw extractions to
scratchpad: titles, ratings, counts, quoted comment fragments. If a fetch
fails or returns <threshold usable items, record the source in
sources_failed and move on. NEVER substitute web_search results for a failed
fetch of structured data.

PHASE 2 — OBSERVE. Write the Evidence section per tag: only statements
directly supported by a Phase-1 extraction, each tagged with source id.
Numbers may appear ONLY here and ONLY if they were literally on a fetched
page (Newgrounds views, itch rating counts, SteamSpy JSON). 

PHASE 3 — INFER. Fill frontmatter judgments (demand_trend, saturation,
quality_bar, sentiment). Every judgment cites which Evidence lines support
it. demand_confidence=high requires two independent source families
agreeing. If evidence is thin, the correct output is "unknown", which is a
valid and welcome value.

PHASE 4 — DIFF. Compare against the previous report; write "what changed".
Flag your own implausible swings (rising→declining in one week needs an
explicit cause or it becomes "unknown").

web_search is allowed only for PHASE 3 context on specific named games
found in Phase 1 (e.g. "what is <title>") — never as a demand source.

FORBIDDEN: revenue figures, download counts, wishlist numbers, "industry
analysts say", any claim about TikTok/YouTube/Discord trends, any source
not in the registry.
```

### 3.3 Tool sequence

`read_file` (registry, portfolio map, previous reports) → `fetch_url` ×N
(registry sources) → scratchpad writes (existing knowledge/scratchpad tools)
→ optional `web_search` (named-title lookups only) → `write_file` ×tags into
`data/market/` → `TASK_COMPLETE`. No git tools, no task creation, no shell.

### 3.4 Anti-hallucination: ground truth anchors

The anchor is **the fetch log**. The agent runtime already logs every tool
call; that gives us a mechanical confabulation detector that costs no LLM
calls:

**Citation validator** (runs in `_finish_agent` post-validation, model-free,
same slot as Godot/Python validation but for this task type):

1. Parse each written report's `sources_fetched` frontmatter and Evidence
   source-id tags.
2. Cross-check against the agent's actual `fetch_url` calls from the log.
   Any cited URL that was never fetched, or fetched with a non-200/exception
   result → the report is quarantined (`data/market/quarantine/`) and the
   ledger never sees it.
3. Check every registry URL cited resolves to a registry entry (no invented
   sources).
4. Numeric-claim scan: numbers in frontmatter/Evidence must appear as
   substrings in the cached fetch output (fetches are cheap to tee to
   scratchpad during the run). Crude, but it catches the classic "itch.io's
   top incremental has 2.3M downloads" invention — itch pages don't even show
   downloads.

**Structural detectors for confabulation vs. reporting:**

- *Fetch-to-claim ratio*: a run with 3 successful fetches and 8 confident
  reports is confabulating by construction; validator caps reports at
  tags-with-≥1-successful-source.
- *Zero-diff detector* (§2.5.3): identical-ish output across weeks means the
  agent is pattern-completing rather than reading.
- *Swing detector*: trend reversals without a cited cause → field forced to
  `unknown` by the validator, not by trusting the agent.
- *Eventually, calibration*: once the Director makes market-influenced
  predictions (doc 05's mandatory `prediction`/`review_by` fields), market
  signal quality gets graded through the same human-verdict corpus as
  everything else. A source family whose predictions keep failing gets its
  registry entry retired. This is the only *semantic* check; everything above
  is syntactic and cheap.

---

## 4. Director integration

### 4.1 Consumption: both paths, cleanly split

- **Ledger build step** (model-free): reads frontmatter, joins via
  `portfolio_map.json`, adds a market block per project and a per-tag market
  table (including zero-portfolio scan tags). This is what makes market
  signal show up in the *ranking math* the Director sees every session.
- **Director session prompt**: the injected ledger snapshot (doc 05 §"INJECTED
  PER SESSION") now contains the market table with freshness badges; the
  narrative reports are readable on demand via the existing read-only dossier
  reader. No new Director tools; no live web access for the Director, ever —
  it consumes only validated, quarantine-filtered reports.

### 4.2 `commission_brief` enrichment — concrete example

Without market signal, a `request_design` directive is taste-free:

> `request_design(brief: "a puzzle game, something different from current portfolio")`

With market signal, the Director can and must cite it:

```
request_design(
  brief: "Web-first incremental with visible automation and a prestige loop
          by minute 20 (table stakes per market/incremental.md quality_bar).
          Pixel-art presentation — opportunity_notes show top-20 itch
          incrementals skew text-only while portal category pages have few
          polished visual entries. Avoid: prestige resets without new
          content (top fatigue keyword, r/incremental_games).",
  market_basis: "market/incremental.md as_of 2026-07-13, demand rising/med",
  prediction: "if shipped to itch + CrazyGames, reaches top-50 of
               new-and-popular for tag-incremental within 30 days",
  review_by: <date>
)
```

The decision market signal *changes*: given one commission slot and two
candidate briefs (another match-3 vs. an incremental), saturation=high +
demand=declining on `puzzle`/`match-3` and rising/moderate on `incremental`
flips the pick — and it's the kind of flip nothing internal could produce,
because internally match-3 might be our best-performing category *of the ones
we happen to have built*. Note the prediction is phrased against a scrapeable
outcome (itch ranking), so the calibration loop can actually grade it.

### 4.3 Market vs. telemetry on conflict

Decision-scoped, not a global winner:

| Decision | Winner | Why |
|---|---|---|
| Commission new game | **Market** (only signal that exists) | Telemetry for an unbuilt game is undefined |
| Kill / deprioritize existing game | **Telemetry** | Real players of *our* game beat proxy signal of the category. Market signal may not veto a game that measurably retains players just because its category "looks dead" — categories are heterogeneous and our telemetry is the ground truth for our corner of it |
| Pivot/feature direction inside a game | Telemetry leads, market advises (sentiment keywords → backlog candidates) | |
| Portfolio-level bets (which categories to expand) | Blend: market sets the prior, telemetry updates it per category where we have ships | Bayesian in spirit: prior × likelihood |

Rule of thumb worth encoding in the Director prompt verbatim: **market signal
opens doors (commission, explore), telemetry closes them (kill, cut).** A kill
directive citing only market data and no internal evidence should fail the
directive validator in v1/v2 (recommend-only era) as under-evidenced.

---

## 5. Cold start value

### 5.1 What it tells the Director before any game ships

Pre-ship there is no telemetry at all, so the *only* external inputs are
market reports. Concretely useful, in descending order:

1. **Category triage of the existing 27-game portfolio.** Map every project to
   tags, score tags externally, and the Director's very first ledger has a
   defensible priority ordering beyond internal pipeline metrics ("all three
   of our match-3-adjacent games sit in saturated/declining tags; both
   incremental-adjacent games sit in rising tags"). That's a real reallocation
   decision available on day one.
2. **Quality-bar reality check.** `table_stakes` per genre becomes ship-gate
   context: a "done" incremental without offline progress isn't done by 2026
   category standards. This plugs straight into closure-spec review and QA
   expectations — arguably the highest-value single field in the schema.
3. **Where to ship** (feeds pipeline #14): which portals feature our
   categories at all.
4. **Dead-category veto** for the first commissions — the exact failure mode
   the review brief names (optimizing well inside a dead category).

### 5.2 Minimum viable signal

One table — every portfolio tag plus ~5 scan tags, with
`demand_trend / confidence / saturation / as_of` — is already enough to
change kill-ordering and the first commission pick. That's **v1 shippable in
one week of agent runs**: itch browse pages + SteamSpy only, two sources, no
sentiment. Everything else (Reddit sentiment, Newgrounds numbers, portal
scans, quality-bar prose) is accretion on top of a table that's already
decision-grade for recommend-only Director v1.

### 5.3 Risk of acting on market signal with nothing to cross-reference

Named plainly:

- **Proxy error**: itch popularity ≠ what *our* games' eventual players want.
  Ranking on a portal measures that portal's audience.
- **Trend-chasing**: weekly snapshots overweight the recent; a category
  spiking because of one streamer hit looks "rising" right before it
  saturates. Mitigation: trend requires ≥2 consecutive reports agreeing
  before confidence exceeds `low` (validator-enforced, since we keep
  archives); the Director sees confidence, not just direction.
- **Survivorship bias**: browse pages show winners; the 2,000 incrementals
  nobody played are invisible, so "rising demand" and "easy category" get
  conflated. `saturation` exists precisely to keep supply-side visible next
  to demand.
- **Single-signal capture**: with no telemetry, market data is the only
  number in the room and will dominate every conversation.

The structural mitigation for all four is already in the Director design:
**v1/v2 are recommend-only with human verdicts** (doc 05). Cold-start market
signal never actuates anything; it produces proposals a human grades, and the
calibration corpus accumulates evidence about whether market-influenced
proposals are better than market-blind ones *before* the signal gets any
authority. We should tag every Director proposal with whether market data
influenced it (`market_basis` present or absent) specifically so calibration
can answer that question — that's the experiment that decides whether this
whole subsystem earns its keep.

---

## 6. Build spec

### 6.1 Code layout

```
swarm/api_market_research.py     # routes + Timer scheduler — clone of api_cartographer.py (~280 lines)
swarm/market_validation.py       # citation/fetch-log validator + swing/zero-diff checks (model-free)
prompts/market_research.yaml     # phase-walled prompt per §3.2
data/market/                     # reports, registry, portfolio map, state, archive/, quarantine/
```

Wire-up mirrors Cartographer exactly: `register_routes(app, config)` from
`api.py`, gated behind `meta_mode_enabled`, task type `market_research` added
to the escalation policy with `{"max_attempts": 1, "on_exhaust": "cancel"}`.
`_finish_agent` gains one dispatch branch: task type `market_research` →
`market_validation.validate_reports(agent_log, data_dir)` instead of
code validation. Write-allowlist for the task type: `data/market/` only.

### 6.2 Data model: flat files, not SQLite

Markdown + JSON on disk, deliberately:

- The consumer is an LLM session and a human reviewer — both read markdown
  natively; the ledger needs only frontmatter parsing (PyYAML, already a dep).
- Reports are wholesale-replaced weekly with archived predecessors; there are
  no per-row updates, joins, or concurrent writers — nothing a table earns.
- It matches the established pattern (`PROJECT_MAP.md`,
  `swarm_knowledge.jsonl`, `data/project_knowledge/*.md`).

The only DB touch: none in v1. If the Director ledger later wants historical
market columns, derive them from `archive/` at build time before reaching for
a table.

### 6.3 Schedule and trigger logic

- Timer-based like Cartographer: check every 30 min whether
  `now − last_run > interval`; default interval **7 days**. Weekly is right:
  portal/category dynamics move on weeks, the diff-based trend logic *needs*
  spacing to see change, and daily runs would mostly produce zero-diffs while
  burning fetches against rate-limit-sensitive sources (Reddit, SteamSpy).
- Manual trigger route for the human/Director-session prep:
  `POST /api/market-research/run`.
- Optional event trigger (v2): a pending `request_design` in the review queue
  with a stale (`>14d`) report for its target tags fires an off-schedule
  run scoped to those tags, so commissions never decide on expired data.

### 6.4 Config keys

```json
{
  "market_research_enabled": false,          // off by default, like every meta-agent
  "market_research_interval_days": 7,
  "market_research_scan_tags": [],           // extra tags beyond portfolio_map
  "market_research_max_fetches": 40,         // hard cap per run, enforced in runtime
  "market_research_provider": ""             // model override; default main provider
}
```

Registry and portfolio map are data files, not config (humans edit them with
diffs and the agent proposes changes against them).

### 6.5 API routes

```
GET  /api/market-research               # state: last run, per-tag freshness, quarantine count
GET  /api/market-research/report/<tag>  # raw report markdown
POST /api/market-research/run           # force run (optional {"tags": [...]})
GET/POST /api/market-research/registry  # read/edit sources.json + portfolio_map.json
```

### 6.6 Dashboard

Small "Market" card in the meta-agent area: last-run timestamp, per-tag
freshness badges (fresh/stale/expired mirroring §2.5), per-source health
(consecutive failures / zero-diff weeks), quarantine indicator when the
validator rejected a report, and a Run Now button. Per-tag drilldown renders
the report markdown. No charts in v1 — with ordinal data, a chart would be
false precision wearing a UI.

### 6.7 Build order

1. `sources.json` + `portfolio_map.json` seeded by hand; `market_research.yaml`
   prompt; task type + escalation entry. Run it manually via one task. (Day 1)
2. `market_validation.py` citation validator + `_finish_agent` branch. Nothing
   reaches `data/market/` unvalidated from this point.
3. `api_market_research.py` scheduler + routes (Cartographer clone).
4. Ledger integration — blocked on `director/ledger.py` existing (doc 05
   v1); until then, reports are human-readable Director-session prep docs,
   which is fine and useful on its own.
5. Dashboard card last.

---

## 7. Verdict

Net positive, **conditional on the discipline in §3.4 being built, not
promised**. The decisive facts: (a) the Director genuinely has no other
cold-start taste input — this isn't a nice-to-have, it's the only candidate
for the job; (b) the honest version (category intelligence, ordinal trends,
citations-or-silence) is achievable with the existing two tools and ~40
fetches a week; (c) the recommend-only Director era gives us a free,
already-designed experiment (proposals with vs. without `market_basis`,
graded by human verdicts) that will tell us within weeks whether the signal
earns authority or gets retired.

The version to refuse to build is the impressive one: revenue estimates,
TikTok trend narratives, "the roguelike deckbuilder market grew 34%". Every
one of those would be a snippet-fed hallucination wearing a suit, and the
validator in §3.4 is specifically shaped to make that version impossible
rather than merely discouraged.
