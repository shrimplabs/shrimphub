# Swarm Controller Roadmap

*Drafted 2026-07-03, following the full code review in [punch-list-2026-07-03.md](punch-list-2026-07-03.md).*
*Last updated 2026-07-04: items #1, #2, #3, #5, #12 completed/decided.*

## What this system is (and what it's becoming)

Swarm Controller started as a task queue that spawns LLM subprocesses. It has grown
into something more specific and more interesting: **an autonomous game studio** — a
system that takes a `GAME_DESIGN.md` and drives a fleet of agents through planning,
implementation, validation, vision-based QA, art passes, and regression closure until
the game matches the design doc.

That specificity is the asset. Generic agent orchestrators are a crowded space
(and getting more crowded); the parts of this codebase that have no obvious
equivalent elsewhere are:

- **The closure loop**: design doc → task DAG → implementation → deterministic +
  vision QA → regression tracking → frozen/stalled states that gate expansion work
  until repairs land.
- **Progressive refinement instead of retry-and-pray**: tiered failure context,
  research feeders that diagnose before re-attempting, baseline validation diffing
  so agents are only blamed for errors they introduced.
- **Game-native QA instrumentation**: StateServer, a11y tree, harness checkpoints,
  grounding-first clicking — an actual machine interface to a running Godot game.
- **The experiment harness**: multi-arm pipeline runs (run4–run11) with per-task
  metrics, which makes pipeline design an empirical question instead of a vibes one.

The roadmap below protects and compounds those assets. The organizing principle:
**near-term = make the foundation trustworthy; mid-term = turn accumulated data into
leverage; long-horizon = close the loop from design doc to shipped, playable game.**

---

## Near term (next 2–6 weeks)

### 1. ~~Make the test suite green and fast~~ ✅ Done 2026-07-04
~~The suite currently fails 20 tests and takes 40+ minutes because failing LLM tests
sleep real backoff (punch list T1–T3).~~

Shipped: `_mock_llm_sleep` autouse fixture, `_reset_agent_runtime_globals` autouse,
`_reset_quota_cache` autouse, truncated-stream fast-fail instead of 7-retry backoff,
`iter_lines` mock fix. **Result: 1355 passed, 0 failed, ~1:45 runtime.**

The four "known failures" in CLAUDE.md are real bugs, not test noise — tracked as open
beads. A nightly CI gate (launchd or cron) is still worth adding to catch regressions.

### 2. ~~Ship the P1 correctness fixes from the punch list~~ ✅ Done 2026-07-04
~~Small, surgical, high-value: the expansion-block deadlock, the ignored
`escalation_policy` config, the dead 102MB history read, the whole-graph cycle
checker, quota-call caching, continuation dependency wiring.~~

All shipped: expansion-block deadlock, cycle checker scoped to task_id DFS,
`escalation_policy` config wired, 30s quota cache, continuation deps, batch HEAD
chaining, import_tasks validation, infra-freeze escalation, orphaned `agent_*.py`
startup cleanup, `.env` parser robustness, prune_history project scoping, 3-tuple
token shim removed.

### 3. ~~Token efficiency: fix prompt caching~~ ✅ Done 2026-07-04
~~The `[Loop N/200]` system-prompt prefix busts prefix caching on every loop.~~

Shipped: loop prefix moved from system prompt to tail user message. System prompt
is now stable across all loops — prefix caching should hit on every loop past the
first. Cache hit rate visible via existing `cache_read`/`cache_write` tracking.

### 4. Data lifecycle policy
`data/` is 12GB and every table scan gets slower monotonically. Decide retention
once and automate it:
- Roll `agent_*.log` older than N days into compressed archives (or delete).
- Move `task-history.jsonl` / `agent-history.jsonl` into SQLite tables (they're
  already write-only exports; the JSONL format buys nothing but parse cost).
- Add the missing scoped-query variants so per-event code paths stop calling
  unscoped `task_get_all()` (punch list P2 has the full list).

### 5. ~~Documentation debt: one regeneration pass~~ ✅ Done 2026-07-04
~~CLAUDE.md describes the system as it was months ago — the meta-agent layer,
closure system, pipeline phases, and model routing are all undocumented.~~

Shipped: CLAUDE.md architecture table corrected with real line counts and 20+
missing modules; Meta-agents section added; Closure system section added; config
table expanded with 23 missing keys; stale "recovery-task reparenting" language
replaced with research-feeder model. README updated with CLI section. Memory
updated. All four "known failures" in CLAUDE.md confirmed as real bugs (tracked
as beads), not stale test noise.

### 6. Close out run-11 and codify the winner
The experiment infrastructure only earns its complexity if results change
defaults. When run-11 (control vs per-task art vs integration checkpoints)
concludes: write the analysis doc, promote the winning pipeline to the default
for new projects, and archive the losing arms' config. Each run should end with
a decision, not just a dataset.

---

## Mid term (1–3 months)

### 7. Turn 12GB of history into an analytics layer
You are sitting on thousands of completed agent runs with loop counts, token
usage, tool-call distributions, failure excerpts, retry chains, and H1–H8
metrics — and today the only consumers are ad-hoc scripts and `/model-stats`.
Build the questions into the dashboard:
- Cost per completed task, by project / task type / model / pipeline variant.
- Where do agents die? (loop limit vs validation vs no-TASK_COMPLETE, by type)
- Which failure signatures recur across projects? (the Gardener's job, but as a
  queryable view instead of an idle-triggered agent)
- Research feeder ROI: how often does a diagnosis actually unblock the retry?

This is the empirical base for every later scheduling and routing decision, and
it's mostly SQL over data you already collect.

### 8. Graduate model routing from experiment to policy
Per-phase routing (scout on cheap models, work on strong ones) and the
shrimp-router round-robin exist as experiment machinery. Promote to a
first-class, config-driven routing policy: task type × phase × complexity →
model tier, with cost caps per project. The analytics layer (#7) tells you
which routings are actually safe. Long-run this is the difference between the
swarm being a curiosity and being economically sustainable to run 24/7.

### 9. Event-driven scheduling core
The monitor thread polls every 5s and does synchronous validation (up to ~5 min
blocking) in the loop. Restructure around a work queue: agent-exit events,
task-created events, validation jobs on a worker pool. This removes the
monitor-lag failure class entirely, makes `lock_project` and dep-violation
races tractable, and is a prerequisite for scaling past ~25 agents. It's the
one piece of genuinely structural surgery worth doing this year — do it after
the test suite is trustworthy (#1), not before.

### 10. QA agent convergence with game-harness research
The game-harness project's OODA-loop research and the QA agent stack are the
same problem. Converge them: the observe layer (StateServer + a11y tree +
vision) should become a versioned, documented protocol — a "game MCP" — that
any agent (QA, art pass, or the harness research code) speaks. Success looks
like: QA finds a class of bug it couldn't before (e.g. gameplay-feel issues via
scenario replay), and the research project has a production consumer.

### 11. Human review queue
`needs_human_review` metadata exists, but nothing surfaces it. Add a dashboard
queue: exhausted branches, frozen projects, catastrophic-action confirmations,
and (new) agent-proposed design doc changes. You are the bottleneck resource in
this system; the punch-list interventions memory (run5: 7 manual interventions)
shows unstructured firefighting — a queue turns that into 10 minutes of triage
a day.

### 12. ~~Security posture decision~~ ✅ Decided 2026-07-04
`0.0.0.0:5001`, auth off, `allow_self_modification: true` — **deliberate choice
for home-network solo use.** Router blocks external access; LAN exposure is
acceptable. Documented in bead `u8sx`. Revisit if the swarm moves to a VPS,
shared network, or open-source users start filing "I accidentally exposed this"
issues — then flip `login_required` default to `true` with first-run password
setup. The non-goals doc (#12 → open-source story) still stands.

---

## Long horizon (6–18 months)

### 13. Pick the public identity: "autonomous game studio"
The open-source framing should match the differentiated assets, not the
commodity ones. "Another agent orchestrator" competes with heavily-funded
frameworks; "point it at a game design doc and it builds, tests, and iterates
the game — with vision QA and regression closure" competes with nobody.
Concretely: the README leads with a design-doc-to-playable-game demo, the
generic orchestration internals become implementation detail, and the Godot
templates + StateServer protocol become the polished, versioned surface.
(The graph-to-gif playback and run experiment writeups are marketing gold here.)

### 14. Close the loop: shipped games, not just green checkmarks
Today the pipeline ends at "validation passed, QA cycle exhausted." Extend it to
the actual finish line: automated Godot export (web/desktop builds), an
itch.io/web publish step, and a playable-build gate in the closure spec. A
project isn't done when tests pass; it's done when a stranger can play it.
This also creates the ultimate QA signal — real playtesting telemetry feeding
back into bug tasks.

### 15. A real learning loop across projects
`learnings.py`, audit learnings, broadcast knowledge, and project memories are
four partial implementations of the same idea. Unify into one knowledge system
with retrieval at prompt-build time (the RAG backend is the natural substrate):
when an agent starts a task, it should see "the last 3 times an agent hit this
error class in any project, here's what worked." Longer-range: the history data
is a fine-tuning / preference dataset — successful vs failed trajectories on
identical task types — if you ever want a specialized local model for scout or
triage phases.

### 16. Graduate the meta-agent layer — or delete it
Gardener, Librarian, Auditor, Cartographer, Archaeologist, Scheduler are all
built but disabled (`meta_mode_enabled: false`). This is Schrödinger's
architecture: it costs complexity today and delivers nothing. Run the
experiment: enable one at a time for a month each, with the analytics (#7) to
judge impact. Keep what earns its place; delete the rest without sentiment.
The end state worth wanting: the swarm maintaining swarm-controller itself
(self-modification is already on) with the meta layer as its immune system —
but only if the data says the meta agents actually help.

### 17. Multi-machine / multi-workspace federation
One Mac, one workspace, ~16–25 agents is the current ceiling, and quota — not
CPU — is the binding constraint. When that changes (local models maturing via
mlx, or multiple API budgets), the architecture question becomes: multiple
swarm controllers federating over shared Gitea remotes, with tasks routed by
machine capability (vision-capable, GPU-local-model, etc.). Don't build this
now — but avoid decisions that preclude it (the SQLite-single-writer design and
in-memory `_active_handles` are the two couplings to watch).

### 18. Economic self-awareness
The end-state metric for the whole system: **cost per shipped, design-doc-
compliant feature**, tracked over time. Every layer above feeds it — routing
(#8) lowers the numerator, closure and QA raise the "shipped and compliant"
bar, analytics (#7) makes it visible. When budget-aware planning exists (the
planner sees "this sprint costs ~$X at current routing"), the swarm stops being
a thing you babysit and starts being a thing you allocate.

---

## Sequencing and dependencies

```
#1 tests ──► #2 P1 fixes ──► #9 event-driven core
   │              │
   ▼              ▼
#3 caching     #4 data lifecycle ──► #7 analytics ──► #8 routing policy ──► #18 economics
                                          │
#6 run-11 ──────────────────────────────►─┤
                                          ▼
#10 QA/harness convergence ──► #14 shipped games
#5 docs ──► #13 public identity
#11 review queue   #12 security ──► #13
#15 learning loop  #16 meta-agents (both gated on #7)
```

Two rules embedded in that graph:
1. **Nothing structural before the tests are trustworthy.** (#9 is the payoff,
   #1 is the entry fee.)
2. **Nothing speculative before the data can judge it.** (#7 gates #8, #15, #16.)

## What deliberately isn't on this list

Twelve tempting directions are explicitly deferred — each with a sketch of what
it would look like and a concrete tripwire that would reopen the question. See
**[ROADMAP-NON-GOALS.md](ROADMAP-NON-GOALS.md)**. Headlines: no new task types
until analytics shows a gap, no SQLite replacement before federation, no
dashboard rewrite, no generic-orchestrator positioning, no agent-framework
adoption, no second game engine, no containers, no fine-tuning yet, no live
inter-agent chatter, no fully-unattended mode, no SaaS, no 3D/multiplayer/mobile
until the 2D loop closes.
