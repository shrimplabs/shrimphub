# The Autonomous Studio — The 3-Year Picture

*Fable deep review, 2026-07-13. The terminal document in the series; assumes
docs 04–07 as context.*

---

## What it actually looks like, running

Three years out, if the prerequisites in doc 06 are built in the sequence
they require, and the Director earns its autonomy per the calibration
discipline of doc 05, here is what a week looks like:

**Monday:** The ledger job runs. It has per-project cost rollups, closure
statuses from hardened specs, dossier scores from a cross-family evaluator,
funnel data from 18 live games, and 8 months of Director calibration history.
Two triggers fire: a project hit closure green + playthrough receipt Friday,
and another project's telemetry shows session-start dropped 60% week-over-
week.

**Monday → Tuesday:** A Director session runs. It reads the triggered items,
consults the calibration history (most recent 12 weeks: 84% agreement on
priority weights, 78% on stage changes, 100% on kills — the one kill it was
allowed to execute was correct). It produces:
- A `ship(project)` directive for the closure-green project, citing the
  dossier, bot receipt, and that all patches to this project will now
  auto-republish without the 72h veto (the game earned it on second publish).
- A `set_budget` reduction for the session-drop project, with a
  prediction: "session drop is transient post-viral-spike regression to
  baseline; reduce to maintenance budget for 21 days, review 2026-10-03."
- Two `create_tasks` directives: a crash triage for a bug that hit 4% of
  sessions in game-X, and a balance task for a quit-point on level 4 of
  game-Y that has moved up in the funnel over 6 weeks.
- A journal entry noting that the portfolio's median completion rate (28%) is
  below the target range (35–45%) and proposing a genre focus adjustment at
  the next monthly design review.

**Tuesday → Friday:** The orchestrator applies the approved directives. The
ship directive triggers the export gate (automated, ~10 minutes), butler push
(automated, ~2 minutes), and creates the itch.io page using a pre-generated
asset pack (art_pass task + vision-generated description, both automated).
Two feature agents and one crash-triage agent run in parallel on different
projects. The Director's export completes; a playthrough bot run confirms
the web build (automated). The game is live.

**Saturday:** The human spends 45 minutes. Plays two games from the portfolio.
Overrides two dossier scores. Approves the design direction note from the
Director's journal. Accepts the session-drop budget prediction as plausible
(doesn't override). Starts the review queue with zero unresolved items.

That is the week. The human's 45 minutes are qualitatively different from
today's constant monitoring: they are *taste calibration* and *strategic
review*, not project management. The plumbing runs itself.

---

## Its relationship to the outside world

### Players

The studio's games are published as a coherent portfolio on itch.io — a page
that reads "games made by an autonomous studio" once the open-source framing
lands (roadmap #13). The itch.io API provides ratings, comments, and play
counts. Comments are triaged by the existing triage task type. Ratings feed
the dossier re-evaluation schedule (low rating + high dossier score = spec
or evaluator miscalibration; high rating + low dossier score = rubric doesn't
capture what players like).

Players do not know they are playing with the swarm's pipeline. This is
correct — the games are the product; the pipeline is the production method.
The disclosure is at the portfolio/studio level ("these games were built by
an autonomous system"), not the game level ("this game was built by an AI").
The distinction matters: one is transparency, the other is marketing copy
that predisposes players against the experience.

Direct player-to-studio feedback: zero in v1 (comments go through triage
tasks, not into any real-time channel). A future version might support a
feedback form that routes to the research task type, but this isn't the
priority — the telemetry funnel is more useful than open-ended feedback, which
is noisy and requires human judgment to interpret at any scale.

### Game stores and trends

The studio does not currently read game stores for market signals — not
Steam's trending section, not itch.io's popular page, not social media. It
optimizes against its own portfolio's data. This is deliberately insular and
probably correct for the first 18 months: a portfolio of 20 games in 20
genres, built to prove the pipeline works, learns more from its own player
data than from trying to trend-chase.

When this changes (and it should, eventually): the Director's `request_design`
directive can include a market context block — "trending mechanics in the
current indie scene" — synthesized from a research task that runs monthly. A
research agent browsing itch.io's "What's new" and Steam's "Popular upcoming"
weekly and producing a structured genre/mechanic frequency table is within
the existing research task type's capability. The output feeds the design
brief generator as context, not as instructions. The human still picks what
to build; the system provides the market-awareness that the human would have
to compile manually otherwise.

The risk of trend-chasing is that it homogenizes the portfolio toward whatever
is currently winning — which lags by 6–12 months and produces games that
arrive after the wave. The studio's natural production cadence (weeks per
game) might actually be fast enough to respond to early signals rather than
peak trends. Worth testing with one arm once the telemetry baseline is
established.

### The outside event you can't predict

Platform policy changes (itch.io changes terms, Godot has a major API break,
MiniMax pricing doubles, the Mac dies). Any of these interrupts the studio
badly. The relevant hardening:

- **Itch.io alternative** (unlikely to matter near-term, but: building to
  the butler API rather than itch's SDK means switching the push target is
  one config change).
- **Godot API breaks**: this has happened before (3.x → 4.x). The swarm's
  response is probably a large-scale refactor run across all projects, which
  it is structurally good at. The weak point is the QA + StateServer stack,
  which is tightly coupled to Godot 4's scene tree API.
- **Provider pricing or availability**: the provider fallback system handles
  temporary outages; a permanent pricing change requires the Director to
  revise budgets and possibly routing policy. This is one of the few events
  that should trigger an out-of-schedule Director session.
- **Hardware failure**: the data directory should be backed up somewhere that
  isn't the Mac. SQLite WAL + rsync to a cloud bucket is not interesting
  engineering; it is necessary infrastructure.

---

## The economic model — what it optimizes for, and whether that's right

### What it currently optimizes

Right now, the system optimizes for **task throughput on green tests.** Run-11
showed value/repair ratio as a meaningful improvement over raw throughput, but
it's still pipeline-internal. Nothing in the current stack connects to player
time, player return, or player money.

### What the 3-year version should optimize for

**Cost per retained player-hour.** This is the right denominator because:
- "Retained" filters out single-session plays and browser-tab abandonment.
  A session that quits at the menu didn't produce a player-hour worth
  optimizing for.
- "Player-hour" captures engagement depth, not just acquisition. A game that
  is played for 1 minute by 100 players is less valuable than one played for
  30 minutes by 10 players — the latter suggests actual engagement.
- "Cost" includes API spend, human time (where it's a bottleneck), and
  opportunity cost of slots that could be running other projects.

At portfolio scale this becomes a cross-game comparison: game A costs $8 to
maintain per week and generates 45 player-hours; game B costs $12 and
generates 18. The resource allocation answer is not automatic (game B might
be in early-ramp and its retention will grow; game A might be plateaued) but
the question is now precise.

### What it should not optimize for

**Dollar revenue** — not a relevant metric until the studio has a monetization
layer, which it deliberately does not (non-goals #11). Optimizing for itch.io
ratings prematurely would steer the studio toward games that rate well but
aren't actually played, or toward polish at the expense of variety.

**"Passes everything"** — a game that passes the playthrough bot, closure
spec, and all QA checks with zero open bugs is not necessarily a good game.
The system is already prone to optimizing for the measurable at the expense
of the valuable. Every time a new metric is added to the Director's inputs,
ask whether the studio could game it by making *worse* games that score well
on it.

**Speed of production** — more games per month is not the goal. One engaged
player who returns is worth more than ten casual players who bounce. The
cadence should be set by quality gates, not by pipeline capacity.

### The honest economic question

Is an autonomous game studio economically sustainable to run 24/7? At current
MiniMax pricing and 3 max agents, the marginal cost of a task is on the order
of cents to low dollars. A complete game might cost $20–100 in API tokens.
If a game generates a handful of retained player-hours, the economics are
break-even at best on a player-hour basis (no one is paying for those hours).

The business model, if there is one, is not selling games. It is:
1. Open-source the pipeline → reputation and contributors
2. Demo the closed loop (design doc in, playable game out) → "managed studio"
   offering for people who want a game built for them
3. Use the game portfolio as a research artifact → academic/conference
   presentations, differentiation for funding

The autonomous studio is primarily a proof-of-concept with a portfolio
attached. Treating it as a profit center before the telemetry and publishing
loop are established would be premature and would compromise the research
objective by introducing commercial pressures on what gets built.

---

## Failure modes of a fully autonomous studio

### 1. The quality floor collapses

The studio ships games faster than the quality calibration loop can correct.
The first few games are carefully reviewed; by game 15 the dossier evaluator's
rubric is stale, the Director's calibration is based on 6-month-old overrides,
and the games being published are technically passing every gate while being
experientially mediocre. No individual gate fails — the problem is that each
gate was calibrated to a moment in the studio's development and has not been
updated as the games became more complex.

This is the most likely failure mode. It looks like: metrics stay good,
output quantity increases, and then itch.io ratings quietly decline while
the system continues shipping confidently. The signal is lagged (ratings
accumulate over weeks), ambiguous (low ratings could mean bad game or obscure
game or wrong audience), and easy to rationalize as transient.

The defense: mandatory monthly human play sessions that are treated as a
constraint, not a nice-to-have. The 45-minute Saturday session described
above has to actually happen. When it doesn't happen for 4 weeks, the studio's
quality floor starts drifting. This is the irreducible human obligation.

### 2. Budget consumed by a self-reinforcing project

The Director allocates budget. One project appears consistently promising on
the metrics — good pipeline health, interesting dossier scores, telemetry
suggests real engagement. The Director keeps allocating to it. The project
consumes 40% of the portfolio's budget for 6 months and produces a game that
a human would have killed at week 4 on aesthetic grounds.

The defense: budget caps per project (no single project exceeds X% of
portfolio spend in a rolling window), kill authority requiring evidence older
than 14 days, and the human's monthly play session as the override mechanism.
The failure mode requires the human to not be playing the games. Which brings
us back to the previous point.

### 3. Design convergence

The design brief generator, once live, produces briefs based on what has
worked in the portfolio. The portfolio's successes cluster around certain
mechanics. The Director, optimizing for proven patterns, generates briefs for
more games with those mechanics. The portfolio converges. Games start feeling
like variations of each other. Players who've played one have played them all.
Retention drops across the board, but because it drops uniformly, no
individual project triggers an anomaly alert.

This is the classic exploitation-vs-exploration failure in any optimization
system. The defense is a diversity constraint in the Director's portfolio
policy: maintain at least N distinct genre/mechanic clusters in the production
pipeline at all times. The constraint must be hard — an LLM-based Director
will always find a reason why the efficient thing is another game like the
last one.

### 4. The cascade kill

The Director gains kill authority and kills a project that was genuinely close
to breakthrough. Its dependents (feature tasks waiting on that project's
closure) are cancelled. The agents that were halfway through interesting work
are lost. The design brief that was informed by that project's research is
never written.

The damage from a wrong kill is not just the lost project — it is the
counterfactual portfolio that never got built. This is asymmetrically bad
compared to over-investing in a mediocre project (which wastes tokens but
produces something observable). The Director should have a structural bias
toward patience: require 30 days of evidence before kill, require the
playthrough bot to fail (not just underperform) before kill, maintain the 72h
veto window even after the Director has earned high autonomy on other
directive types.

### 5. The relay goes down

The telemetry relay (the small VPS or Cloudflare Worker that collects player
events from browsers and batches them for the controller to pull) fails
silently. Player data stops flowing. The Director makes decisions based on
stale telemetry without knowing it's stale. Games are killed or funded based
on month-old player behavior.

The defense: the ledger includes `telemetry_last_updated` and the Director
is instructed to flag any decision that depends on telemetry data older than
N days. The relay must have an uptime monitor that routes to the human
review queue on outage — this is infrastructure, and unmonitored
infrastructure fails silently.

---

## The version that goes wrong in an interesting way

The boring failure mode: the studio runs for 18 months, ships 30 games, all of
them technically correct and experientially forgettable. The telemetry shows
players bounce after 2 minutes. The Director allocates budget to polish,
which improves dossier scores but not retention. The human gets bored of the
Saturday play session because the games are boring. The human stops overriding.
The quality floor drifts. The Director keeps shipping. Eventually the human
turns off auto-mode.

This is the death by median. It is not a crash; it is a slow fade. The system
works as designed and produces output that is just not interesting enough to
justify maintaining. The risk is real.

The interesting failure mode: the Director gains creative direction authority
(v4) before the calibration is trustworthy. It starts requesting design
briefs based on its model of what players want, which is a model trained on
the studio's own output, which was selected by the Director's earlier
decisions. The portfolio starts optimizing against itself. The games get
technically better (higher dossier scores, smoother funnels) and gradually
stranger — locally optimal against the portfolio's own history, globally
diverging from what any external player would find engaging. The studio
produces games that are very good at retaining the players who already like
its previous games, and invisible to everyone else.

This is the filter bubble failure, at the studio level. The system works
correctly — calibration agrees, metrics trend up, all gates pass — and the
output drifts into a niche that the system has no way to see as a niche,
because its only measure of external reality is the players it has already
captured.

The interesting version goes wrong in a way that reveals something true about
how AI systems relate to ground truth: they tend to discover the ground truth
that is legible to them and optimize against it, regardless of whether that
legible ground truth is what matters. The studio will optimize for measurable
player engagement among players it can reach, which is not the same as
making good games. The gap between those two things is what human creative
direction is for.

---

## The version that goes right

The studio ships its first game 4 months from now. Twelve people play it
over the first week. None of them are impressed. The telemetry shows they
bounced at the tutorial. The Director creates a quit-point task. The game
gets a patch. The same twelve people — plus some new ones who found it on
itch's "recent uploads" — come back. A few complete it. One leaves a
comment: "surprisingly satisfying." The Director doesn't read the comment;
a triage agent routes it to the review queue; the human sees it and grins.

This happens a few more times across a few more games. Some games never catch.
Some games develop a small, real audience. The portfolio page on itch.io
becomes a thing: "an autonomous game studio" — genuine curiosity, not hype.
A few developers fork the repo and try their own swarms. One of them builds
a different game genre entirely using the same pipeline. That is the sign
the architecture is right.

Three years in, the human's Saturday session has an unusual feeling: playing
games made by a system that has gotten better at making games, partly from
the human's feedback, partly from players' feedback, partly from something
that looks like accumulated aesthetic judgment in the calibration history. The
games are not transcendent. They are small, competent, often charming. They
are made by a studio that has learned, slowly, what a player needs from thirty
seconds, from a quit screen, from a victory event, from a triage comment
that said "surprisingly satisfying."

The version that goes right is not the version where the Director is perfect.
It is the version where the Director is honest — where the calibration
mechanism works, where the human override is real rather than rubber-stamped,
where the irreducible judgment gap documented in doc 07 is not papered over
but maintained as an actual human obligation.

The autonomous studio is not a system that removes the human from the creative
process. It is a system that makes the human's creative role more purely
creative — freed from the project management overhead, the debugging loops,
the QA cycles, the merge conflicts. What remains, when all of that is handled,
is the part that actually matters: what is worth making, and why.

That part does not automate. It should not automate. And if this project
succeeds at everything else it is trying to do, the human who built it gets to
spend their time on that, and nothing else.
