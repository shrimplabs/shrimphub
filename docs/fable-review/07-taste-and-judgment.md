# Taste and Judgment — The Hard Problem

*Fable deep review, 2026-07-13. Companion to doc 05 (Director architecture)
and doc 06 (prerequisites). This document is about the thing those documents
defer.*

---

## The gap, stated plainly

The Director architecture in doc 05 is honest about its constraint: the
quality verdicts the Director consumes must come from somewhere. The
calibration loop grounds the Director against human overrides and,
eventually, player telemetry. But neither source eliminates the prior
question: **what is a good game?**

This is not a database query. It is not an aggregate. It is not a benchmark.
"Is this game fun?" is a judgment that requires:
- Playing or watching the game, not reading about it
- Knowing what the game is *trying to be* (genre conventions, the design doc's
  intent, the target player)
- Recognizing when something technically correct is experientially wrong
- Tolerating ambiguity — a game can be flawed and compelling, polished and
  dull

None of these are things the current swarm does or approximates. The QA agent
checks *whether the game works*, not *whether it is good*. The playthrough bot
checks *whether a bot can complete it*, not *whether a human would want to*.
Value/repair ratio measures *pipeline efficiency*, not *game quality*.

The question this document tries to answer is: can you get close enough to
useful without closing the gap all the way? And what closes the gap most
efficiently?

---

## What telemetry can substitute for

Telemetry is the best available approximation of a quality signal that an LLM
can reason about. Here is what it genuinely measures vs. what it does not:

### What telemetry measures reliably

**Session completion rate (funnel).** If 40% of players who start a game
complete it and the design intent is a 10-minute experience, that's a signal
about the difficulty curve, pacing, and on-ramp — all designable, all
fixable. This is quality-adjacent and highly actionable.

**Quit-point distribution.** Players quitting at scene X means something
specific is wrong at scene X. The specificity makes this more useful than
aggregate ratings. Combined with game_state snapshots at quit, an agent fixing
a quit-point bug has the exact evidence it needs.

**Death cause distribution.** If 60% of deaths are from a single enemy type
on level 2 and the design says this should be a tutorial level, that's a
balance bug. Machine-diagnosable.

**Return rate.** Do players open the game twice? A game that is completed once
and never returned to is different from one that has repeat sessions. For
small indie web games this signal is noisy, but over a portfolio it aggregates
to something real.

**Time-in-scene.** Players spending 30 seconds on a menu that should take 5
seconds indicates discoverability or usability failure. Players spending 3
minutes on a "fast-paced wave" that the design intends to last 90 seconds
indicates a difficulty problem.

### What telemetry cannot measure

**Aesthetic coherence.** Does the game have a consistent visual identity? Do
the sound effects match the feel of movement? Is the color palette reading
correctly as "cozy puzzle" vs "action arcade"? These are perceptual properties
that don't emit events. A player who bounces at 30 seconds might be bouncing
for any of these reasons, but telemetry shows you *that* they bounced, not
*why*.

**Emotional resonance.** Whether a game is surprising, satisfying, delightful,
or creatively interesting is not in the event stream. A competent, technically
solid game that is fundamentally boring generates the same event types as a
good game, just with different values.

**Design intent fidelity.** A game that performs well on telemetry metrics may
be doing so by accident — a difficulty curve that retains players because a
bug made one early section unexpectedly forgiving, or a quit-point that moved
because players found an exploit. Telemetry shows outcomes; it does not show
whether those outcomes match what the design intended.

**Genre competence.** Is this a good tower defense, judged against the genre?
Players who have never played a tower defense will generate the same telemetry
as players who are genre-experts, but their judgments differ. Aggregate
sessions don't capture "this feels wrong for the genre."

**The floor.** Telemetry rewards games that are played. A game can be played
a lot because it is good, or because players can't figure out how to quit, or
because it is running in a browser tab they forgot about. Itch.io web games
have this problem — session length is confounded by tab-leaving behavior.

The honest framing: telemetry closes the feedback loop between the studio and
its output, but it is a funnel-shaped view of quality — it sees the big
obvious failures (nobody finishes level 3, quit rate 80% at menu) and is
blind to the subtle failures that separate good from mediocre.

---

## Can telemetry substitute for taste?

No. But it can substitute for enough of taste to make autonomous operation
viable at a specific quality floor.

Here's the partition:

| Quality dimension | Telemetry coverage | LLM rubric coverage | Human taste required |
|---|---|---|---|
| Game is completable | Playthrough bot + victory events | — | — |
| On-ramp is functional | Funnel (quit-point at scene 1/2) | — | — |
| Balance is calibrated to design doc | Death/retry patterns vs. intent | Weak (doc reference) | Judgment calls |
| Game is visually coherent | — | Moderate (screenshot + rubric) | Strong |
| Game is genre-competent | Weak (session metrics vs. genre baseline) | Moderate | Strong |
| Game is emotionally engaging | — | Very weak | Irreducible |
| Game deserves more investment | Weak (return rate, rating trend) | Weak | Strong |

The viable strategy: **guarantee the top two rows mechanically, approximate
the middle rows with a rubric + cross-family critique, accept human-in-the-
loop for the bottom two rows.** This is exactly the existing architecture —
playthrough bot for the top, the quality dossier (below) for the middle, the
review queue for the bottom. The honest label for what the autonomous studio
produces without human taste involvement: *functional games of unknown
quality.* The honest label for what it produces with telemetry + periodic
human calibration: *functional games with bounded quality variance.* That is
actually commercially viable for the itch.io indie market.

---

## What a "game quality" signal looks like that an LLM can reason about

The quality dossier is the Director's taste approximation. Here is how to
design one that doesn't immediately collapse into vibes:

### The rubric

A frozen, versioned rubric, human-authored, with exactly these properties:
- Each criterion is *observable from a screenshot or a recorded session*, or
  *decidable from the design doc + game state*.
- Each criterion has a 1–5 scale with specific anchors at 1, 3, and 5.
- The rubric version is stamped on every verdict. When the rubric changes, old
  verdicts are not retroactively comparable.

Draft rubric (v1, to be human-reviewed before use):

**R1 — On-ramp clarity.** Can a first-time player understand what to do within
30 seconds without reading instructions?
1: No indication of controls or goals; 3: Controls clear, goal implied;
5: Controls and goal obvious from first screen, no dead time.

**R2 — Feedback responsiveness.** Do player actions have immediate, readable
consequences?
1: Significant lag or no visual/audio response to inputs; 3: Most actions have
clear feedback; 5: Every meaningful action has immediate, readable response
within 100ms.

**R3 — Visual identity consistency.** Do art style, palette, and UI language
cohere into a single visual register?
1: Mixed art styles that conflict; 3: Consistent enough to not be jarring;
5: Intentional aesthetic with all elements reading from the same register.

**R4 — Difficulty progression.** Does the challenge ramp in a way that matches
the design doc's intent for the target player?
1: Sudden difficulty spikes or no progression; 3: Generally increasing, one
problematic section; 5: Smooth ramp consistent with design intent.

**R5 — Design doc fidelity.** Does the live game match the stated mechanics,
setting, and feel of GAME_DESIGN.md?
1: Major mechanics from the doc are absent or contradicted;
3: Core mechanics present, secondary elements missing;
5: All stated mechanics present and coherent, atmosphere matches description.

**R6 — Completion satisfaction.** Does completing the game (victory event)
feel like a conclusion rather than an arbitrary stop?
1: No win state or win state is abrupt with no feedback;
3: Recognizable win state, minimal ceremony;
5: Clear win state with contextually appropriate feedback.

That is six criteria, not twenty. Six criteria that a reviewer can actually
apply, from screenshots + design doc + a 2-minute recording of a play session.
More criteria produce the false impression of rigor while actually producing
noise.

### The cross-family critique mechanism

The human builds a game with MiniMax (primary). A quality verdict for that
game should not be produced by MiniMax. The evaluator must be:
- A different model family (Claude or GPT-class)
- Given only: the rubric, GAME_DESIGN.md, 3–5 screenshots, and a session
  recording if available
- Forbidden from accessing the development history, agent logs, or task
  descriptions — it should see the artifact, not the process

Why cross-family: different training distributes different aesthetic priors.
A MiniMax-trained evaluator has more in common with the MiniMax builder than
a Claude-trained one does. The disagreement rate between families on rubric
scores is itself a signal — high disagreement means the criterion is
ambiguous; low disagreement means the criterion is legible. Over time, the
rubric tightens to where multi-family agreement is high, which is also the
regime where the evaluation is most trustworthy.

Director sessions consume the dossier entry: average rubric scores per
criterion, inter-model agreement, and the raw verdicts as attachments. The
Director reasons about the dossier, not about "is this game fun."

### Updating the dossier

A dossier entry is created:
- When a project reaches `closure_green + playthrough_receipt` (pre-ship
  checkpoint, always)
- After a major art pass or polish sprint (periodic quality snapshots)
- When telemetry shows an anomaly (new playthrough data contradicts rubric
  scores — investigate)

Human override: the human can mark any rubric score as overridden, with a
note, which feeds Director calibration. Over time, the override frequency
per criterion reveals where the rubric is wrong — a criterion with high
override frequency needs its anchors rewritten.

---

## Preventing proxy metric optimization

The Director has access to value/repair ratio, closure status, playthrough
receipt, dossier scores, and (eventually) telemetry. Each is a proxy. The
Director will optimize the measurable ones. Here are the failure modes and
their mitigations:

**Value/repair optimization:** favor projects that have few bugs, not projects
that are good. A game can have an excellent value/repair ratio because it is
simple enough to never generate QA flags — not because it is polished.
Mitigation: cap project investment at a quality floor (dossier R1 < 2 ⇒ no
new features regardless of pipeline metrics).

**QA passing as quality:** projects that pass QA are projects whose QA tests
are easy to pass. Mitigation: the playthrough bot is harder to game than QA
(it requires demonstrating agency through real game states, not just script
checks), and the dossier evaluator does not see QA status.

**Closure green as done:** a green closure spec is a function of how demanding
the spec was written. Mitigation: spec provenance (human-authored, locked) and
periodic human spot-checks of green projects (sample 10% of green checkmarks
against the dossier — if green doesn't correlate with dossier scores, the
specs are underspecified).

**Dossier score optimization:** agents may, over time, learn to produce games
that score well on the rubric without being good games. (This is Goodhart's
law applied to any rubric.) Mitigation: rubric versions are frozen, human-
changed, and the cross-family evaluator's training was not conditioned on this
rubric (models don't know what your rubric says until they see it). The
rubric can be updated but updates trigger a re-evaluation wave; version
mismatch is visible in the ledger.

**Telemetry funnel optimization:** when telemetry lands, the swarm could
learn to make games with very low funnel drop-off by making them trivially
easy. Mitigation: design doc is the referee (the design doc specifies target
difficulty; agents tune toward the doc, not toward the metric), and a
difficulty floor in the rubric (R4 anchored so "no challenge" scores 1, not 5).

No defense is perfect. The failure mode for any of these is detectable in the
calibration loop — if the Director's predictions about player retention
consistently miss despite the dossier looking good, the dossier is being
gamed. The calibration mechanism detects drift that single-metric analysis
misses.

---

## Human taste as periodic calibration

The design in doc 05 already encodes this implicitly, but it deserves explicit
statement:

**Human taste should be a calibration signal, not a continuous gate.**

A human in the loop at every decision is not an autonomous studio; it is a
human-led studio with an expensive assistant. The goal is human taste as
*error correction*, not *approval gate*.

What this looks like concretely:
- Once per month, the human plays 2–3 games from the portfolio — not to
  approve them, but to produce override verdicts on the dossier and
  calibration grades on Director decisions. This is the session that cannot
  be automated: someone has to pick up the controller.
- Override verdicts are fed back into rubric refinement. If the human
  consistently overrides R3 scores, either the criterion is wrong or the
  evaluator is systematically off.
- The frequency of human taste sessions can decrease as the calibration
  scorecard shows high agreement — but should never reach zero while the
  studio is producing new games. A studio that never has a human play its
  games is a studio that has no idea whether it is making games.

The target state: **the human overrides fewer than 15% of dossier scores and
fewer than 10% of Director decisions over a 90-day window.** Below those
thresholds, the automation is tracking human judgment well enough to operate
with weekly sessions rather than continuous involvement. Above those
thresholds, either the rubric is wrong, the Director is miscalibrated, or the
human's taste has shifted — all three are diagnosable from the override
pattern.

---

## The irreducible judgment gap

There is a version of this that cannot be closed. Some questions about games
are genuinely not answerable from evidence:

- Is this game worth making? (Not: will players play it. Whether it should
  exist at all.)
- Is this the right game for this portfolio, at this time, in this genre
  climate?
- Does this idea have creative potential that a technically rough v1 obscures?
- Should the design be changed, or the execution improved?

These are portfolio-level creative direction questions. The Director in v4
(creative authority, doc 05) can generate design briefs informed by portfolio
analytics — but "informed by analytics" is not the same as "good judgment."
The analytics tell you what has worked; they are systematically silent about
what could work but doesn't fit the current data.

The honest ceiling of this system — even with all prerequisites met, all
telemetry live, all rubrics tuned, all calibration clean — is a studio that
optimizes well within a genre/mechanic space the human has already selected.
The creative direction that decides "we should make a game in a category we've
never tried" or "this game has something special even though the metrics say
kill it" remains human. Not because LLMs can't generate that content, but
because those decisions have consequences that extend beyond the data, and the
entity that bears the consequences of those decisions should make them.

That is not a failure of the system. It is the correct placement of the
human in the loop.

---

## Addendum: market research as an external taste signal (2026-07-13)

The above assumes taste inputs are internal (telemetry from our games, human
overrides, cross-family critique). But there is a second source: **what the
market is already rewarding**, which is prior information we don't have to earn.

Market research tells you what players are paying for and why in genres you
haven't built yet — it solves the cold-start problem that telemetry cannot.
Before a single game ships, market signal is the only taste input available.

**Sources the Director can query:**
- itch.io trending / top-rated (downloads + comment sentiment, no auth needed)
- Steam reviews for genre peers (revealed preference + language players use for fun vs. frustration)
- Reddit/Discord genre communities (what players say is missing from the market)
- App store top charts (revealed preference at scale)

The web search + fetch tools already exist in the swarm. A lightweight
**Market Research meta-agent** running weekly could scrape these sources and
write a `MARKET_SIGNAL.md` per genre into `data/market/`. The Director reads
this alongside the portfolio ledger and telemetry summary.

**Two-source taste model:**
- Internal: telemetry from our games (what our players do)
- External: market signal (what the market rewards elsewhere)

Cross-referencing both catches the failure mode where we optimize well within
a category no one wants — the market signal acts as a prior that prevents
the Director from doubling down on a dead genre just because our game in it
performs well relative to our other games.

**Build order implication:** the market research agent has no dependencies on
telemetry or the publish pipeline — it works against public web data today.
This makes it the correct first taste signal to build, before anything else
in the Director stack.

*Fable to think through: market research agent design, source selection,
signal schema, how it feeds into Director directives — see queued questions.*
