# The Anti-Roadmap: What We're Deliberately Not Building

*Companion to [ROADMAP.md](ROADMAP.md), 2026-07-03.*

A roadmap says where the effort goes. This document says where it doesn't — and,
more importantly, **what would change each answer**. Every entry has three parts:
what the thing would look like if we built it (so the idea is captured, not
suppressed), why it loses to the roadmap today, and a *tripwire* — a concrete,
observable condition that reopens the question. When a tripwire fires, this
document is the appointment we made with ourselves to reconsider.

The meta-rule behind most entries: **this system's scarce resources are one
person's attention and an API quota.** Anything that spends either on
infrastructure rather than on shipped, verified game features has to clear a
high bar.

---

## 1. More task types and prompt files

**What it would look like:** `performance_pass`, `sound_pass`, `balance_tuning`,
`localization`, `docs_pass`, per-genre prompt variants — the prompt library
growing to 40+ files with per-type escalation policies and validators.

**Why not:** there are already 20+ prompt files, and the punch-list review found
the *existing* type system has unenforced edges (import endpoint bypassing
validation, escalation overrides silently ignored). Every new type multiplies
the routing, escalation, validation, and auto-spawn matrices. The evidence so
far (run5–run11) is that outcomes are dominated by pipeline structure and
failure handling, not prompt specialization. A generic `feature` task with a
good description outperforms a bespoke type with an unmaintained prompt.

**Tripwire:** the analytics layer (roadmap #7) shows a recurring failure
cluster that maps cleanly to a missing type — e.g. ≥10 failed tasks across ≥3
projects whose failure excerpts share a signature that an existing type's
prompt actively works against. Then add *one* type, with its own validator,
and measure.

---

## 2. Replacing SQLite

**What it would look like:** Postgres (or Dolt, given beads already flirts with
it), connection pooling, real migrations, a proper ORM layer replacing the
hand-rolled `f"{k}=:{k}"` SQL.

**Why not:** the observed problems — slow scans, whole-graph cycle checks,
serialized writes — are all *query discipline* problems, not engine problems.
8,900 task rows is nothing; WAL-mode SQLite handles orders of magnitude more.
Swapping engines would consume weeks, freeze feature work, and leave every
unscoped `task_get_all()` call exactly as slow as before. Fix the queries
(roadmap #4); keep the zero-ops, single-file, trivially-backupable store that
matches a solo deployment perfectly.

**Tripwire:** federation (roadmap #17) becomes real — two or more controller
processes on different machines need to write the same task graph. SQLite's
single-writer model is the genuine wall; that's the day this trade flips. A
softer early-warning: sustained `database is locked` errors in logs after the
query-discipline fixes land.

---

## 3. Dashboard rewrite (React/Vue/Svelte, build pipeline, component library)

**What it would look like:** the nine vanilla-JS dashboard files replaced by a
typed SPA with a build step, state management, and a design system; SSE
replaced by a websocket layer.

**Why not:** the dashboard's job is operator visibility, and it does it. Its
real deficiency is *missing views* (cost analytics, review queue, experiment
comparison), not missing framework. A rewrite is the classic displacement
activity: months of motion, zero new information on screen. Vanilla JS also
keeps the surface trivially serveable by Flask with no toolchain — which
matters for the open-source install story ("clone, pip install, run").

**Tripwire:** either (a) a second regular contributor joins and the
shared-mutable-DOM style measurably causes regressions (two broken-dashboard
incidents traced to it in a month), or (b) the analytics views from roadmap #7
genuinely exceed what hand-rolled JS can render — interactive DAG exploration,
brushable cost timelines. Even then: adopt incrementally per-view, no big bang.

---

## 4. General-purpose positioning ("orchestrate anything")

**What it would look like:** language-agnostic marketing, first-class prompt
packs for web apps / data pipelines / infra, "bring your own validator" SDK,
comparisons against LangGraph/CrewAI/AutoGen in the README.

**Why not:** the system's edge is everything that happens *after* code is
written — StateServer introspection, vision QA, harness checkpoints, closure
gating against a design doc. For a generic web app, none of that exists, and
the system degrades into an ordinary task loop competing with better-funded
generic frameworks on their turf. The multi-language validation support
(Rust/TS/Swift/Unity detection) stays as quiet capability; it just doesn't
lead.

**Tripwire:** a non-game domain shows up with an equivalent *closure signal* —
a machine-checkable, end-to-end "does the artifact match the spec" oracle as
strong as a running game + design doc. (Candidates that might qualify someday:
CLI tools with executable spec suites, simulations with scenario checkers.)
The test: could a QA agent find a real bug in the artifact without a human
describing where to look? If yes, that domain earns a pilot project.

---

## 5. Adopting an agent framework (LangGraph, CrewAI, Claude Agent SDK as the core loop)

**What it would look like:** `agent_runtime.py`'s bespoke tool loop replaced by
a framework's graph/executor; tool definitions migrated to its schema; the
`[TOOL_CALL]` protocol retired.

**Why not:** the bespoke loop *is* the research artifact. Loop stall detection,
context compaction thresholds, wrap-up injection, scout-phase write blocking,
vision-call caps, meta-investigation hooks, adaptive-flat completion gating —
these are the experiment variables of run4–run11. A framework would own
exactly the control points this project exists to study, and every framework
upgrade would be a confound in the experiment data. Frameworks also churn
faster than this codebase does.

**Tripwire:** provider-side standardization makes the bespoke layer a
liability rather than an asset — e.g. all target providers converge on a
server-side tool-loop/agents API where the raw completion loop stops being
offered at equivalent cost, or interop (MCP everywhere) means the custom
protocol blocks tools we need. Watch also for the maintenance signal: if >20%
of swarm-controller bug tasks in a quarter are tool-loop plumbing, buying
instead of building gets re-evaluated.

---

## 6. Second game engine (Unity, Unreal, Bevy, Love2D)

**What it would look like:** engine abstraction over StateServer (a C# port for
Unity, a Rust crate for Bevy), per-engine validation commands, per-engine
project templates and bootstrap scaffolds.

**Why not:** Godot is not an arbitrary choice — it is unusually automatable:
headless CLI validation, GDScript's forgiving iteration loop, a scene tree
that serializes cleanly into the a11y/state protocol, no license friction, no
multi-gigabyte editor for agents to fight. Unity's compile times and editor
coupling alone would halve agent throughput. Every hour spent porting
StateServer is an hour not spent deepening the Godot instrumentation that
makes QA agents actually good. One engine, mastered, is the moat.

**Tripwire:** the portfolio hits Godot's ceiling — a game concept the swarm
demonstrably cannot build *because of the engine* (not because of agent
capability), or Godot 5-era churn breaks the automation surface badly enough
that engine risk needs hedging. Alternatively: an external
contributor/community ports the protocol themselves, at which point accepting
a maintained port costs little.

---

## 7. Cloud deployment / containers / Kubernetes

**What it would look like:** Dockerfiles, a compose stack (controller + Gitea +
vector DB + vLLM), Helm charts, agents as ephemeral pods, S3-backed data dir.

**Why not:** the deployment target is one Mac on a desk, and several
load-bearing pieces are *local by nature*: mlx-vlm vision inference (Apple
Silicon), Godot GUI launches for screenshot QA, the screen-lock-safe input
injection work. Containerizing would break the QA stack, and orchestrating one
machine with Kubernetes is self-parody. `launchd` + the existing shell scripts
are the right amount of ops.

**Tripwire:** federation (roadmap #17) or a genuine second deployment (a
collaborator running their own swarm). Even then, start with a Dockerfile for
the *controller only* (headless projects), keeping vision QA as a
Mac-native capability tier that tasks route to.

---

## 8. Fine-tuning a custom model

**What it would look like:** curating the 12GB history into
successful-vs-failed trajectory pairs, LoRA-tuning a local model (mlx) for
scout/triage/plan phases, an eval harness comparing it against API models per
task type.

**Why not (yet):** the training data isn't curated — "task completed" is a
noisy success label until closure verification and the analytics layer can
distinguish *genuinely good* trajectories from lucky ones. Meanwhile frontier
model improvements keep arriving for free, and every fine-tune is frozen
against that tide. The prerequisite work (roadmap #7, #15) is on the roadmap;
the tuning itself is premature.

**Tripwire:** three conditions met together: (a) analytics can label
trajectories with high confidence, (b) a phase exists where the cheap-model
arm loses to the strong-model arm by a margin that routing can't close
(currently scout looks *fine* on cheap models — that's evidence against
urgency), and (c) a local model matures to within striking distance on that
phase. Then tune for that one phase, judged by the existing experiment
harness.

---

## 9. Inter-agent communication / cooperating agent teams

**What it would look like:** agents on the same project sharing a live message
bus, negotiating file ownership, pairing (one writes, one reviews in real
time), or a manager agent supervising workers mid-task.

**Why not:** the dependency graph *is* the coordination mechanism, and its
serialization is a feature — file-ownership analysis chains conflicting tasks
precisely so agents never need to negotiate. Live coordination reintroduces
the failure modes the DAG design eliminated (merge conflicts, lock contention,
two agents interleaving on one file) and multiplies token spend on
coordination chatter. The one-shot `delegate_helper` and broadcast knowledge
base already cover the async cases cheaply.

**Tripwire:** analytics shows serialization is the throughput bottleneck —
agents idle waiting on chains while `max_active_agents` slots sit empty, on
projects where the file-ownership partition says parallel work *should* be
safe. Or: validation-failure post-mortems show a class of bug a cheap
second-agent review pass would catch (then trial a review *phase*, which is
pipeline structure, not live communication).

---

## 10. Fully unattended operation (removing the human from the loop)

**What it would look like:** no review queue; exhausted branches self-resolve
via ever-deeper meta-agent escalation; the swarm merges design-doc changes it
authored itself; auto-mode never suspends for anything but quota.

**Why not:** the run5 record (7 manual interventions in two days) and the
emergent-behavior log (agents modifying the orchestrator unprompted) are
direct evidence that human judgment is currently load-bearing at the *edges* —
exactly the cases automation handles worst. The cheap, high-leverage move is
making interventions efficient (the review queue, roadmap #11), not
pretending they aren't needed. Removing the human before the closure loop is
trustworthy converts small failures into unbounded ones — quota drained on a
doomed recovery cascade, or a design doc quietly rewritten to match the bugs.

**Tripwire:** measured, sustained decline in interventions-per-run at
increasing autonomy — e.g. three consecutive experiment runs where the review
queue received zero items that actually required action. Autonomy should be
*earned per subsystem* (auto-replan first, self-modification last), with the
kill switch and catastrophic-action guards never removed.

---

## 11. SaaS / hosted product

**What it would look like:** multi-tenant controller, billing, org accounts,
hosted Gitea, BYO-API-key management, a landing page with a waitlist.

**Why not:** productizing now would freeze the architecture around its
current assumptions (single operator, trusted LAN, one workspace) while
simultaneously demanding the security/isolation work (#12) at its hardest
difficulty. The honest current shape of this project is a research system
with a compelling demo. Open-sourcing the studio (roadmap #13) captures the
upside — reputation, contributors, real-world usage data — without the
support burden that would consume the roadmap whole.

**Tripwire:** post-open-source signal: sustained inbound of "I'd pay for a
hosted version" from people who tried self-hosting and bounced off setup (not
idle enthusiasm — attempted installs). If the closed-loop demo (#14: design
doc in, playable game out) reliably lands, revisit as a *managed studio*
("we run the swarm, you get the game"), which monetizes the differentiated
loop rather than generic orchestration seats.

---

## 12. New modality frontiers (3D games, multiplayer, mobile export)

**What it would look like:** 3D scene QA (camera-controlled vision sweeps),
netcode tasks with multi-instance harness orchestration, iOS/Android export
pipelines with device-farm testing.

**Why not:** each multiplies QA difficulty enormously — 3D breaks the flat
a11y-tree grounding that makes clicking reliable; multiplayer breaks
deterministic checkpoints; mobile breaks the local launch-and-screenshot loop.
The current 2D portfolio is nowhere near exhausting what the existing
instrumentation can verify. Depth before breadth: a swarm that ships
*excellent* 2D games beats one that ships broken everything.

**Tripwire:** the closed loop (#14) works end-to-end for 2D — several games
shipped and playtested with closure green — and the experiment harness has
spare capacity. 3D first (it's still single-instance Godot; the StateServer
protocol extends with spatial queries), multiplayer last.

---

## How to use this document

- When one of these ideas resurfaces (it will — they're all *good* ideas),
  check the tripwire before relitigating. If it hasn't fired, the answer
  stands; append a dated note if the temptation was strong, so patterns show.
- When a tripwire fires, promote the entry to the roadmap with a scoped
  first step — never the full vision in one bite.
- Review annually alongside the roadmap. An anti-roadmap that never changes
  is dogma; one that changes monthly was never a decision.
