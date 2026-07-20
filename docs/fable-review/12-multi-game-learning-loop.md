# 12. The Multi-Game Learning Loop

**Roadmap item #16, made concrete.** Design review, 2026-07.

---

## 0. What already exists (read this first)

The premise "none of this history feeds back into future agents" is not quite
true — and the design must start from that fact, because the system currently
has **five overlapping partial implementations** of learning, none of them
finished, two of them fighting over the same file:

| Mechanism | Location | Scope | Injected? | Decay? | Curation? |
|---|---|---|---|---|---|
| Per-(project, task_type) learnings | `swarm/learnings.py`, `data/learnings/<project>/<type>.md` | project | Yes, at prompt build (`swarm_runner.py:860`) — but **hard-disabled for bug/qa** | keeps last 5 entries | LLM log-tail summary, unreviewed |
| Shared knowledge file | `swarm/tools/knowledge.py:read/update_shared_knowledge`, `data/shared_knowledge.md` | global | On demand via agent tool, 4000-char cap, substring topic filter | none — append-only forever | none, any agent writes |
| Gardener pattern store | `swarm/gardener_knowledge.py`, `data/swarm_knowledge.jsonl` → renders `data/SWARM_KNOWLEDGE.md` | global | **No** — rendered markdown is read by nothing in the agent path | TTL (90d default), `expire_stale()`, confirmed/suspected/disputed | Yes: signature, confidence, evidence task IDs, affected projects, godot_version |
| Per-project AGENT_KNOWLEDGE.md / VALIDATION_STATE.md | `swarm/tools/knowledge.py` | project | Yes, at prompt build | LLM compaction at 40k chars | agent-written, uncurated |
| `audit_learnings` task type + AUDIT_LEARNINGS_REPORT.md | `prompts/audit_learnings.yaml` | global | No | none | agent-written report |

Plus two adjacent systems:
- **`agent_signals` table** (`swarm/db.py:417`) — structured per-agent outcome
  data: error snippets, tool sequences, mechanism fires, terminal status.
  Gated on `log_extract_signals`. This is the measurement substrate.
- **RAG backend** (`swarm/rag/__init__.py`) — generic ChromaDB/FAISS document
  retrieval, built for Godot docs (`godot_docs` collection), disabled by default.

**A live bug worth flagging:** `data/SWARM_KNOWLEDGE.md` has two writers with
incompatible ownership models. `gardener_knowledge.render_markdown()` stamps it
`AUTO-GENERATED FILE -- do not edit directly`, but the unified chat's
`write_swarm_memory` tool (`swarm/api_chat.py:856`) appends to it directly.
The next Gardener render will silently destroy anything the chat wrote. Any
unification must pick one owner. (The current file content — hand-curated
patterns with 65+ and 165+ confirmations — appears to be chat/human-written
over the top of the auto-generated format, i.e. the clobbering already happened
in the other direction.)

**The most important empirical fact in the codebase:** the existing
`SWARM_KNOWLEDGE.md` documents that *Pattern 3 (Godot `--script` mode can't
resolve autoloads) burned 165+ recovery tasks* and *Pattern 1 (GUT
resources-in-use false positive) burned 65+ agents* — across many projects,
repeatedly, because the knowledge existed but was never injected into agent
context. The learning loop's value is not hypothetical. The cost of not having
it is already measured in hundreds of wasted agent runs.

**The second most important empirical fact:** `swarm_runner.py:865` disables
per-project learnings injection for bug/qa task types with the comment *"stale
broad notes cause more harm than help there."* That is a documented,
already-experienced contamination failure. It teaches the central lesson of
this design: **the injection mechanism was never the problem; the curation
tier was.** Raw LLM log summaries hurt bug agents. Curated, signature-matched,
confirmed patterns are a different substance and would have saved those 165
recovery tasks.

---

## 1. What "learning" actually means here

### 1.1 The taxonomy, ranked by tractability

| Learnable thing | Tractable? | Why |
|---|---|---|
| **Environment/toolchain gotchas** (headless Godot quirks, validation false positives, GUT stderr noise, `--script` autoload limitation) | **Highly** — do this first | Small, discrete, signature-matchable, project-independent by nature, and empirically the biggest cost center (165+ wasted recovery tasks on one pattern) |
| **Recurring bug classes with known fixes** (`is_inside_tree()` guard in `_physics_process`, `_swarm_check.gd` deletion by refactor agents) | **Highly** | Error signatures normalize well; the fix is a paragraph, not a codebase |
| **Task-description phrasing that produces good agents** | Semi — gated on analytics (#7) | Needs statistical correlation between description features and outcomes across many runs; a per-task injection can't use it, but the planner and Librarian can |
| **Code patterns that work** (architecture, save systems, scene organization) | **Mostly not, via this loop** | The right channel already exists: `templates/godot/`. A working save system belongs in the template repo as code, not in a knowledge base as prose. "Copy from templates, never write from scratch" *is* the learning loop for code patterns — it just runs through git, not prompts |
| **QA findings that predict future bugs** | Weakly, later | Requires QA finding taxonomy that doesn't exist yet; fold into bug-class learning once signatures accumulate |
| **Design patterns players enjoy** | **Not now** | Gated on telemetry (#17). No player data exists. Anything built here today is speculation, which the roadmap's rule 2 explicitly forbids |

The tractable core is narrow and unglamorous: **operational knowledge about
the toolchain and recurring bug shapes.** That's fine. It's where the money is.

### 1.2 "Access to history" vs. "learning from history"

- **Access** = an agent *can* retrieve past outcomes (a tool, a RAG query, a
  file it may read). Pull-based, uncurated, unmeasured. The existing
  `read_shared_knowledge` tool is access — and agents essentially never use it
  unprompted, which is why Patterns 1 and 3 kept recurring.
- **Learning** = history *changes future behavior by default*, through a
  pipeline with (a) a curation/selection step, (b) automatic injection at the
  point of decision, and (c) feedback on whether the injection helped, closing
  the loop.

**What this document proposes is learning, not access**: push-based injection
of curated, scoped, decaying patterns at prompt-build time, with per-injection
outcome tracking. Access-style retrieval (RAG over raw history) is explicitly
rejected as the v1 substrate — see §2B.

### 1.3 The new failure mode cross-project learning introduces

Per-project operation has a natural blast radius: a bad note in
`AGENT_KNOWLEDGE.md` hurts one project. Cross-project learning creates
**systemic contagion**: one wrong "learning," injected into every agent on 29
projects, is a multiplier on error. Concrete shapes:

1. **Architecture contamination** — project A's unusual pattern (say, a
   singleton-heavy design that works for a 500-line puzzle game) is recorded as
   "what works" and injected into a 5000-line sim where it's poison.
2. **Version contamination** — a Godot 4.1 workaround injected into 4.3
   projects where the underlying bug is fixed, causing agents to apply
   unnecessary (or now-harmful) workarounds.
3. **Overgeneralized diagnosis** — "validation errors about resources are
   false positives" (true for GUT stderr noise) generalized into "ignore
   validation errors" (catastrophic).
4. **Confidence laundering** — a suspected pattern gets injected, an agent
   parrots it back into a log, the extractor re-learns it from the log, and it
   self-confirms without independent evidence. This is the sneakiest one and
   requires an explicit guard: **evidence must come from distinct projects and
   must not be counted when the pattern was injected into that very run.**

The entire architecture below is shaped by these four risks: scoping fields,
confidence tiers, injection gating on `confirmed`, TTL, dispute mechanics, and
the injected-pattern-ID audit trail.

---

## 2. Candidate architectures, evaluated honestly

### A. Shared knowledge base (flat markdown)

**As implemented** (`data/shared_knowledge.md` + `data/SWARM_KNOWLEDGE.md`):
append-only markdown, substring topic filter, 4000-char read cap, no decay, no
confidence, any agent writes, two conflicting writers on one file.

**Verdict: fails at this scale, but its cousin succeeds.** A flat file with
substring search cannot scope by version, project type, or confidence; it
cannot decay; it cannot dedupe; and at 29 projects × months of history it
becomes either a truncated random sample (the 4000-char cap) or prompt bloat.
The *content* currently in `SWARM_KNOWLEDGE.md` is excellent — because a human
curated it. The file format is the problem, not the idea.

**Disposition:** `data/swarm_knowledge.jsonl` (the Gardener store) becomes the
single source of truth; `SWARM_KNOWLEDGE.md` becomes a pure rendered view
(fix the dual-writer bug: `write_swarm_memory` in `api_chat.py` should append
a jsonl entry with `created_by: "operator"`, not write the markdown).
`data/shared_knowledge.md` and its two tools are deprecated — fold any live
content into the jsonl store and delete the tools in a later cleanup.

### B. Vector index / RAG over history

**The proposal:** index agent logs, completed task diffs, QA reports in
ChromaDB; agents query "find similar bugs."

**Verdict: not the v1 substrate. Three independent reasons:**

1. **The current implementation is prototype-grade and partially broken.**
   `swarm/rag/__init__.py` reloads a SentenceTransformer per query (~seconds of
   latency per call), generates colliding IDs (`doc_0..doc_n` — re-ingestion
   silently overwrites), and the FAISS backend passes Python lists where numpy
   arrays are required (`self._index.add(embeddings)` will throw). Building the
   learning loop on this means first rebuilding this.

2. **Semantic search on code/logs surfaces syntactic neighbors, not causal
   ones.** Embedding "fix save-game corruption in raccoon-city" retrieves
   other texts that *mention saving* — including failed attempts, unrelated
   save features, and QA reports about save UI. What the agent needs is "the
   root cause was X and the fix was Y," and that resides in *diagnosis text*
   (research feeder outputs, `meta_investigation` findings), not in raw logs
   or diffs. Retrieval quality over raw history at this corpus (~18K logs,
   ~8.4K task rows) would be dominated by boilerplate similarity — every agent
   log shares 80% of its structure with every other.

3. **RAG is access, not learning** (§1.2). It has no curation step, no decay,
   no confidence, no contamination defense. A retrieved failed-approach log
   ranks as highly as a retrieved fix.

**Where RAG legitimately fits, later:** once the pattern store has hundreds of
entries and keyword/signature matching starts missing paraphrases, embed the
**`fix_summary` and `pattern_signature` fields of curated entries only** (not
raw history) and use vector similarity as a *fallback matcher* behind exact
signature match. That's a ~50-line addition and reuses the existing backend
for what it's good at: fuzzy lookup over short, clean, curated text. Do not
index logs or diffs.

### C. Prompt library evolution (Librarian)

**The proposal:** Librarian extracts learnings from failure patterns and edits
prompt YAML files.

**Verdict: highest ceiling, highest risk, wrong storage layer for learnings.
Keep it human-gated.**

- Prompts are **global and unversioned-by-scope**: one edit to `bug.yaml`
  affects every bug agent on all 29 projects immediately. There is no prompt
  eval harness, so drift (the Librarian making prompts gradually worse —
  longer, more hedged, more contradictory as learnings accrete) is
  **undetectable until completion rates move**, which is weeks of lag and
  confounded by everything else. The failure mode isn't a bad edit; it's
  twenty individually-reasonable edits that sum to a worse prompt.
- Learnings stored *in prompts* lose everything the data layer gives them: no
  TTL, no confidence, no scoping, no dispute, no per-injection measurement.
  A stale Godot 4.1 workaround baked into `bug.yaml` lives forever; the same
  fact in the jsonl store expires in 90 days.

**Disposition:** the Librarian's role in the learning loop is **structural,
not content**: it may propose changes to how prompts *reference* the knowledge
system (e.g. "the injected KNOWN PATTERNS block should appear before the task
description, not after"), with `librarian_autonomous_edits: false` kept as-is
and human review of diffs. Facts and patterns live in data, never in YAML.
If the Librarian is ever allowed to edit autonomously, gate it on the
experiment infrastructure: prompt change → A/B arm → analytics verdict — which
is roadmap #16-meta's "earn its place" test, gated on #7.

### D. Per-task pattern injection (new — and the winner)

**The proposal:** before an agent starts, match its task against history and
inject the top-3 relevant outcomes into context.

**Verdict: build this. It is ~70% already built.** `gardener_knowledge.py`
has the store (signatures, confidence, TTL, evidence, version field);
`generate_task_script()` has four injection precedents (learnings, knowledge,
validation state, research context); what's missing is the wire between them
plus a matcher and capture hooks.

**The matching function — in priority order, cheap-first:**

1. **Error-signature match** (bug/retry tasks): the task's
   `metadata.last_failure` and validation error text is normalized the same
   way `capture_validation_baseline()` normalizes baseline errors, then
   matched against `pattern_signature`. This is exact-ish and high-precision —
   and it's precisely the match that would have killed Pattern 3's 165
   recovery tasks: every one of them had `Identifier not found` +
   `--script` in its failure context.
2. **Task-type + keyword overlap** (all tasks): tokenize description, overlap
   against pattern signature + fix_summary tokens, require ≥2 content-word
   hits. Low precision, so gate on `confidence == "confirmed"` only.
3. **Embedding similarity** (later, optional): only over curated
   `fix_summary` text, only as fallback when 1–2 miss (see §2B).

Task type matters as a *filter*, not a matcher: environment patterns
(validation false positives) are relevant to bug/qa/refactor; workflow
patterns (refactor deletes `_swarm_check.gd`) only to refactor; etc. Each
pattern entry gets a `task_types` field.

**Why this doesn't repeat the "stale broad notes" failure that got learnings
disabled for bug tasks:** the disabled mechanism injected *unreviewed LLM
summaries of the last 5 log tails*. This injects *confirmed patterns with ≥3
distinct-project confirmations, matched by error signature, capped at 3
entries / ~1500 chars*. Same pipe, different water.

---

## 3. The staleness problem

Learnings decay along at least three axes, and each needs a different model:

| Axis | Model | Mechanism |
|---|---|---|
| **Time** | TTL from `last_seen` | Already built: `ttl_days` (default 90) + `expire_stale()`. A pattern that stops being re-confirmed dies on its own. Wire `expire_stale()` into the monitor thread's daily housekeeping — today nothing calls it on a schedule |
| **Toolchain version** | Hard scope field | Already built: `godot_version` per entry. Injection filters on the target project's engine version (readable from `project.godot` `config/features`). On a workspace-wide Godot upgrade, all version-scoped entries demote from `confirmed` → `suspected` automatically (one function, triggered by a config hook or manual endpoint) |
| **Codebase scale/shape** | Soft scope + dispute | A `size_band` scope field (small <1k lines / medium / large >5k, from the registry's file counts) for the rare pattern that's scale-sensitive. Mostly, scale-inappropriateness is caught by the dispute mechanism, not predicted |

**Invalidation detection — how a learning gets *actively* killed rather than
passively expiring:**

1. **Contradiction by outcome.** Every injection records the pattern IDs in
   task metadata (`metadata.injected_patterns`). If a task fails *and* its
   failure text shows the agent followed an injected pattern's advice
   (cheap heuristic: the fix_summary's key tokens appear in the agent's
   actions), increment a `contradictions` counter. At 2 contradictions from
   distinct projects → auto-demote to `disputed`, which removes it from
   injection. This is fuzzy and will have false negatives; that's acceptable —
   TTL is the backstop.
2. **Fix landed upstream.** Patterns about template files (`state_server.gd`,
   `check_scripts.gd`) should carry an optional `retires_when` note (free
   text, human-read). When the template fix ships via `sync_templates.py`, the
   operator disputes the entry. Automating this is not worth it at current
   scale.
3. **Re-confirmation refreshes.** Any new capture whose signature matches an
   active entry bumps `last_seen` and appends the evidence task ID — the TTL
   clock resets. Genuinely recurring patterns are therefore effectively
   immortal *while they keep recurring*, which is correct.

The TTL default of 90 days is right for toolchain gotchas (they outlive most
sprints but not engine upgrades). Bug-class patterns (`is_inside_tree` guard)
are near-timeless; give those `ttl_days: 365`. The field is per-entry; use it.

---

## 4. The contamination problem

### 4.1 Scoping model

Every entry carries a `scope` object; injection intersects it with the target
task's context. All fields optional — absent means unrestricted:

```json
"scope": {
  "godot_version": "4.3",          // exact-or-prefix match against project engine
  "project_types": ["godot"],      // godot | python | swift | ... (validation.py's detector)
  "projects": [],                   // non-empty = ONLY these projects (project-specific learning)
  "exclude_projects": [],           // blocklist for known-inapplicable projects
  "size_band": null,                // "small" | "medium" | "large" | null
  "task_types": ["bug", "qa", "refactor"]
}
```

Genre scoping is deliberately omitted: no reliable genre metadata exists, and
the tractable pattern classes (§1.1) are genre-independent. Add it only if a
genre-specific pattern actually shows up disputed for genre reasons.

### 4.2 Confidence as the primary contamination valve

The existing three tiers do the heavy lifting, with one injection rule:

- **`confirmed`** (≥3 distinct projects' evidence, none from injected runs) —
  injected everywhere its scope allows.
- **`suspected`** — injected **only into projects already in
  `affected_projects`** (the pattern can help where it was seen, but cannot
  spread). This is the anti-contagion rule: a pattern earns cross-project
  reach only by being independently observed cross-project.
- **`disputed`** — never injected; kept for the record and for the dashboard.

Anti-laundering rule (§1.3, item 4): when capture matches an existing pattern,
the evidence only counts toward promotion if that pattern's ID is **not** in
the source task's `metadata.injected_patterns`. Confirmation requires
independent rediscovery.

### 4.3 Human override

- `POST /api/learnings/<id>/dispute {reason}` — one call, sets
  `confidence: disputed`, records reason and `disputed_by: "operator"`.
- `PATCH /api/learnings/<id>` — edit scope (e.g. add a project to
  `exclude_projects` when a learning is right globally but wrong for one
  project's weird architecture — the precise "project A contaminates B" case).
- Dashboard: a Learnings panel listing active entries with confidence,
  injection count, contradiction count, and one-click dispute. This is a small
  table view, not a new subsystem.
- The unified chat's `write_swarm_memory` becomes `add_learning` /
  `dispute_learning` tool calls against the same store (fixing the dual-writer
  bug as a side effect).

---

## 5. Minimum viable version

**The simplest thing that meaningfully reduces repeated mistakes:** inject the
already-curated confirmed patterns into agent prompts, and auto-capture new
candidates from the two places that already produce *diagnoses* (not raw
logs): research feeder results and meta-investigation findings.

Concretely, four changes:

1. **Injection** (~½ day): in `generate_task_script()`, after the existing
   learnings block: load active entries from `swarm_knowledge.jsonl`, filter
   by scope + confidence rule (§4.2), match by signature/keywords (§2D), take
   top 3, render as a `KNOWN PATTERNS (cross-project, confirmed)` block capped
   at ~1500 chars, record IDs in `task.metadata.injected_patterns`.
2. **Capture** (~1 day): when `_apply_research_feeder_result()` injects
   `research_context`, and when `meta_investigation` produces a finding, also
   run a signature-extraction step (one cheap LLM call, same shape as
   `learnings._summarise_log`) that either (a) matches an existing entry →
   bump `last_seen` + evidence, or (b) appends a new `suspected` entry. These
   two sources are chosen because their text is already a *root-cause
   diagnosis* — the highest-quality, lowest-noise capture points in the
   system. Do **not** capture from every finished agent in v1.
3. **Promotion + decay** (~½ day): promotion to `confirmed` at 3 distinct
   non-injected projects (automatic, in the capture path); wire
   `expire_stale()` into monitor housekeeping; version-bump demotion function.
4. **Measurement** (~1 day): extend `swarm/analytics.py` with two queries
   (below); dashboard tile.

**Cost: roughly 3–4 days of work**, nearly all of it glue between existing
components. No new services, no new dependencies, no RAG, no schema migration
beyond additive jsonl fields.

**Expected benefit:** Pattern 3 alone represents 165+ recovery-task runs — at
even ~50 LLM loops each, that is thousands of wasted API calls against a
quota-bound system, plus days of wall-clock queue time. If injection prevents
even one such pattern from re-burning at a tenth that scale, the loop pays
for itself many times over. The benefit is front-loaded: the store already
contains the four most expensive known patterns, hand-curated.

**The metric — is it working or adding noise?** Two numbers, both computable
from existing tables (`agent_signals.error_snippets` + task metadata), both
belonging in the roadmap-#7 analytics panel:

1. **Repeat-failure rate** (primary): of all failed attempts this week, what
   fraction have failure text matching an *active confirmed pattern's*
   signature? Learning works ⇒ this trends toward zero (known mistakes stop
   recurring). It rising or holding flat while patterns exist = injection is
   being ignored or mismatched.
2. **Injection lift** (guard against noise): completion rate and mean loop
   count for tasks that received injections vs. tasks that *matched a pattern
   but were randomly held out* (10% holdout, one `random()` call at injection
   time, flag in metadata). If lift is ~zero or negative after ~100 injected
   tasks, the block is prompt bloat — shrink it or raise the match threshold.
   The holdout is what distinguishes "we built a learning loop" from "we added
   1500 chars to every prompt and told ourselves a story."

Roadmap discipline check: rule 2 says nothing speculative before analytics
(#7) can judge it. The MVP satisfies this *by construction* — the measurement
queries ship in the same change as the injection, and `agent_signals`
(the substrate) already exists. Turn on `log_extract_signals` first.

---

## 6. Build spec

### 6.1 New / changed code

```
swarm/learning_loop.py          NEW (~350 lines)
  match_patterns(task, project_ctx, entries) -> list[Entry]   # signature → keyword tiers
  render_injection(entries, char_cap=1500) -> str
  capture_from_diagnosis(text, task, source) -> str|None      # dedupe-or-append, returns entry id
  maybe_promote(entry, evidence_task) -> bool                 # 3-distinct-project rule, laundering guard
  record_contradiction(entry_id, task_id) -> None
  demote_for_version_bump(new_version) -> int

swarm/gardener_knowledge.py     EXTEND (~60 lines)
  additive fields (schema below); update_entry(); keep all existing functions.
  Rename consideration: this module is no longer gardener-specific — alias it
  as the storage layer under learning_loop and leave the old name importable.

swarm_runner.py                 EDIT (~25 lines)
  generate_task_script(): KNOWN PATTERNS block after RECENT COMMITS / before
  NOTES FROM PREVIOUS RUNS; write metadata.injected_patterns + holdout flag.
  NOTE: bug/qa exclusion at line 865 applies to the OLD learnings only —
  confirmed patterns inject into bug/qa deliberately (that's their main value).

swarm/agent_recovery.py         EDIT (~10 lines)
  _apply_research_feeder_result(): call capture_from_diagnosis(research_summary, ...)

swarm/meta_investigation.py     EDIT (~10 lines)
  after a finding is produced: capture_from_diagnosis(finding, ...)

swarm/agent_finish.py           EDIT (~15 lines)
  on failure with injected_patterns present: record_contradiction heuristic

swarm/api.py (monitor)          EDIT (~5 lines)
  daily: expire_stale()

swarm/api_learnings.py          NEW (~150 lines)
  GET  /api/learnings                       list (filter: status, confidence, project)
  POST /api/learnings                       manual add (operator/chat)
  PATCH /api/learnings/<id>                 edit scope/ttl/confidence
  POST /api/learnings/<id>/dispute          {reason}
  GET  /api/learnings/metrics               repeat-failure rate + injection lift

swarm/api_chat.py               EDIT
  write_swarm_memory → add_learning/dispute_learning against the jsonl store
  (fixes the SWARM_KNOWLEDGE.md dual-writer clobber)

swarm/analytics.py              EXTEND (~80 lines)
  repeat_failure_rate(), injection_lift()   (reads agent_signals + task metadata)

dashboard                       Learnings panel (list + dispute button + 2 metric tiles)

tests/test_learning_loop.py     NEW — matcher precision, laundering guard,
                                promotion rule, holdout flag, scope filtering,
                                version demotion, capture dedupe
```

### 6.2 Data model (additive fields on `data/swarm_knowledge.jsonl` entries)

```json
{
  "id": "uuid",
  "pattern_signature": "godot-script-mode-cannot-resolve-autoloads",
  "confidence": "confirmed",                    // existing: confirmed|suspected|disputed
  "status": "active",                           // existing: active|expired
  "godot_version": "4.3",                       // existing
  "first_seen": "2026-05-30", "last_seen": "2026-07-10", "ttl_days": 90,   // existing
  "affected_projects": [...], "evidence_task_ids": [...],                   // existing
  "fix_summary": "…",  "created_by": "research_feeder",                     // existing

  "scope": { "project_types": ["godot"], "projects": [], "exclude_projects": [],
             "size_band": null, "task_types": ["bug","qa","refactor"] },    // NEW
  "match_keywords": ["identifier not found", "--script", "autoload"],       // NEW: matcher tier-2 tokens
  "injection_count": 0,                                                     // NEW
  "contradictions": 0, "contradiction_task_ids": [],                        // NEW
  "disputed_by": null, "dispute_reason": null,                              // NEW
  "retires_when": "check_scripts.gd template stops using --script mode"     // NEW, free text
}
```

No SQLite change. Task rows gain two metadata keys only:
`injected_patterns: [ids]`, `learning_holdout: bool`.

### 6.3 Interaction with existing systems

| System | Relationship |
|---|---|
| **`gardener_knowledge.py` / jsonl store** | Becomes the single source of truth. Gardener remains a *writer* (when enabled); the learning loop adds capture writers and the injection reader |
| **`SWARM_KNOWLEDGE.md`** | Pure rendered view, regenerated after every store mutation; never written directly by anything (chat included) |
| **`learnings.py` (per-project)** | Untouched in v1 — it serves a different niche (project-local recent-run notes). Revisit for deletion after the loop proves out; if injection lift is positive and per-project learnings show none, delete them and simplify |
| **`shared_knowledge.md` + tools** | Deprecated; migrate content into the store manually, remove `read/update_shared_knowledge` tools in a follow-up |
| **`AGENT_KNOWLEDGE.md` / `VALIDATION_STATE.md`** | Untouched — per-project structural facts are not cross-project learnings |
| **RAG backend** | Not used in v1. Later: optional tier-3 fuzzy matcher over `fix_summary` text of curated entries only (config `learning_semantic_match`). Never index raw logs/diffs |
| **Librarian** | May propose structural prompt changes referencing the injection block; never writes facts into YAML; `librarian_autonomous_edits` stays false |
| **Research feeder / meta-investigation** | The two v1 capture sources (already-produced diagnoses) |
| **`agent_signals` / analytics (#7)** | Measurement substrate; requires `log_extract_signals: true` |
| **Telemetry (#17), player-preference learning** | Out of scope until telemetry exists; the store's schema (a new `source: "telemetry"` and pattern class) can absorb it later without redesign |

### 6.4 Config keys

```json
{
  "learning_loop_enabled": false,        // master gate, off by default
  "learning_inject_max": 3,              // max patterns per prompt
  "learning_inject_char_cap": 1500,
  "learning_min_confidence": "confirmed",// injection floor for cross-project spread
  "learning_capture_enabled": true,      // capture hooks (can run before injection is enabled)
  "learning_holdout_rate": 0.1,          // measurement holdout
  "learning_ttl_days": 90,               // default for new entries
  "learning_promote_threshold": 3        // distinct non-injected projects to confirm
}
```

Rollout: enable `learning_capture_enabled` alone for a week (store fills,
nothing injected, zero risk) → verify captured entries look sane in the
dashboard → enable `learning_loop_enabled` → read the two metrics after ~100
injected tasks → decide.

---

## 7. Summary judgment

- The learning loop is **mostly a unification and wiring problem**, not a
  research problem. Four of five needed components exist; the fifth (the
  matcher) is a few hundred lines.
- **Reject** flat-file knowledge (unscalable), raw-history RAG (access, not
  learning; broken implementation; wrong retrieval semantics), and autonomous
  prompt evolution (unmeasurable drift, wrong storage layer for facts).
- **Build** per-task pattern injection on top of the Gardener store:
  curated entries, signature-first matching, confidence-gated spread,
  TTL + dispute decay, laundering-guarded promotion, and a randomized holdout
  so the system can prove — not assert — that it's learning.
- Fix the `SWARM_KNOWLEDGE.md` dual-writer clobber regardless of everything
  else in this document; it's corrupting the one good knowledge asset today.
- The single most valuable near-term act needs no code at all: the four
  patterns already in `SWARM_KNOWLEDGE.md` have cost 230+ agent runs between
  them. Getting them in front of bug agents is the whole game; everything
  above is the machinery to keep doing that automatically.
