# Fable Review 14 — Run-12: Art-Pass Scheduling

**Reviewer:** Fable
**Date:** 2026-07-14
**Subject:** `docs/experiment-designs/run12-analysis.md`, cross-referenced against run-11 and the run-12 pre-registration (`run12-proposal.md`)
**Prior context:** reviews 07 (taste/judgment), and the V/R discussions in runs 10–11.

---

## TL;DR

Run-12 is the most valuable run you've done, but not for the reason the analysis foregrounds. Its value is that it **falsified the run-11 story** and **surfaced a new, more important failure class (design drift)** — not that it crowned batched art as the winner. The analysis quietly launders a null result into a "batched wins" conclusion, and in doing so it contradicts your own pre-registered readout matrix. That matters more than the art scheduling question itself, because the whole point of pre-registering was to stop exactly this kind of post-hoc narrative fitting.

Read this run as: **the art-pass V/R effect from run-11 did not replicate; the real finding is a control-integrity failure that no automated gate caught.** Everything else is secondary.

The four questions, up front:

1. **Design drift** is a general property of any warm-context feedback loop where the artifact being fed back is *unconstrained in scope*. It is not art-specific. A QA design-conformance check is necessary but **not sufficient** — you also need an upstream scope constraint. Do both.
2. **Run-13 (mid vs tail) is the wrong next variable.** You are tuning a knob on an effect you have not established exists. Higher leverage: (a) build the design-conformance gate and (b) do the second-genre replication. Both were already flagged as more important in your own proposal.
3. **More features with wrong design is worse, decisively**, for a "game that ships" — but the interesting sub-answer is that per-task art is *implicated in the drift*, so this reframes A's feature count as a liability, not a mixed blessing.
4. **V/R is a throughput metric wearing a quality costume.** Fourth appearance of the anti-correlation is not noise — it's a structural signal that V/R measures *motion*, and motion at high frequency without a design anchor produces confident divergence. Stop treating V/R as a quality proxy. It's a cost/effort meter.

---

## 1. The conclusion the analysis should have led with

Line the pre-registration up against the result.

The proposal (`run12-proposal.md`) pre-committed a readout matrix. The relevant rows:

| Observed | Pre-registered meaning | Pre-registered action |
|---|---|---|
| A > B > C | Warm context is real and additive | Keep interleaving; per-task polish |
| **A ≈ B > C** | **Art quota is the mechanism; timing is free** | **Let scheduler batch art freely; timing doesn't matter** |
| A > B ≈ C | Timing is everything; batching wasted | Never batch |
| A ≈ B ≈ C | Run-11 was noise | Stop, re-examine metrics |

Run-12's V/R came in at **A=1.0, B=1.07, C=0.64**. B's edge over A (1.07 vs 1.0) is explicitly called "marginal and within noise" in the analysis itself (line 49). So on the *pre-registered primary metric*, the result is **A ≈ B > C**.

That is the "art quota is the mechanism, timing is free" cell. You wrote that mapping down *before* you saw the data. The honest readout is: **timing does not matter; the quantity of art passes is what separates the art arms from control.**

Instead, the analysis pivots to the human playtest — where B ranked 1st — and concludes "shift toward batched art as new baseline" (line 166), justified by a qualitative coherence argument about batched art letting the agent "respond to a complete system rather than an evolving one."

That may even be *true*. But notice what happened: you pre-registered a metric, the metric said "timing is free," and when the metric disagreed with the eyeball test, you followed the eyeball test and rewrote the mechanism story to match. That is the precise move pre-registration exists to prevent. **You cannot have it both ways** — either V/R is your primary metric (in which case the finding is "timing free") or the human playtest is (in which case *why is V/R still in the results table as the headline row?*).

I don't think you're being dishonest. I think the human playtest is genuinely more informative than V/R here, and your instinct to weight it is correct. But then **the disciplined conclusion is: "V/R declared the arms equivalent; the human playtest broke the tie toward B; therefore V/R failed to capture the thing we care about, and the batched-vs-per-task question is now downstream of a metric we no longer trust."** That's a much stronger and more durable statement than "batched art wins." It also directly sets up question 4.

**Pushback, sharpened:** the analysis's stated Run-13 direction ("mid vs tail timing") is built on the batched-wins conclusion, which is built on n=1 human judgment overriding your primary metric. You are about to spend a full run refining the timing of an effect whose *existence* rests on a single playtest session where "the 3-HP arcade system felt deliberate." That is not a foundation. See §3.

---

## 2. Design drift — the actual headline

This is the finding worth the session. Let me be precise about what it is and isn't.

### It is not art-specific. It is a property of unconstrained warm-context feedback.

The mechanism, stated generally: an agent makes a decision (art pass) conditioned on the current state; that decision alters the state; the next feature decision is conditioned on the altered state; repeat. If each step is locally reasonable and *nothing external re-anchors to the spec*, the trajectory integrates. Over 14 iterations, small conditioning nudges compound into a genre shift. No individual step is an error. There is no gradient pointing "back toward the spec" because nothing in the loop holds the spec.

Art passes are just the highest-frequency, most-visually-load-bearing decision in your pipeline, so they're where drift showed up first. But the same dynamic will appear in **any** warm-context loop where the fed-back artifact can influence design-level choices: per-task polish, per-task "juice," refactors that rename core mechanics, even QA bug-fixes that "helpfully" adjust controls to make a level passable. The common factor is **scope**: the feedback artifact was allowed to touch things that define the game's identity (control scheme, core mechanic, genre), not just its surface.

So the correct framing is not "art-before-stabilization is dangerous." It's: **warm-context loops need a fixed point.** Right now your loop has no fixed point — the only anchor (GAME_DESIGN.md) is read at plan time and never re-consulted as a conformance target. Everything after plan-time is free-running.

The tell is in your own data: Arm A *interleaved* art (per-task, warm), and it drifted. Arm B *batched* art (after stabilization, colder context), and it stayed on-spec. That's consistent with "warm context amplifies drift because each pass conditions the next feature." But it's *also* consistent with "batching gave fewer opportunities for the design-influencing decision to fire." You cannot distinguish these two from this run. Which is another reason mid-vs-tail timing is premature — you haven't isolated whether it's *warmth* or *frequency* driving the drift.

### Is a QA design-conformance check sufficient? No — necessary but insufficient.

The analysis proposes (lines 171–173) adding a design-conformance check to the QA gate: compare live control scheme and core mechanic against the design doc. Do it — it's the detection layer you're missing, and it's the only thing that would have caught Arm A. Pipeline was green throughout precisely because "runs + QA passes + bot completes" says nothing about "is this still the game we specified."

But a gate is **detection, not prevention**, and detection at the QA gate has three problems:

1. **It fires late.** By the time QA runs, 14 features and 7 art passes have compounded. A conformance failure at t=40 tells you the game drifted; it does not tell you *which* of the 21 decisions to revert, and reverting is expensive and itself lossy. Drift is a ratchet — cheap to accumulate, costly to undo.
2. **It's a binary trip on a continuous phenomenon.** "Control scheme == spec?" catches the Star Control flip. It does not catch the *approach* to the flip — the run of decisions that were each 5% off. You'll get a gate that's green until it's suddenly, expensively red.
3. **It requires the checker to have taste and a ground truth.** "Core mechanic matches doc" is an LLM-judge call. Per review 07, your judge infrastructure is not yet trustworthy for taste-level calls. A conformance judge comparing "vertical scroller w/ 8-dir movement" vs "rotation-thrust combat" is an *easy* call — those are categorically different. But the interesting drift is the near-miss, and there the judge is as unreliable as the agent that drifted.

So pair the gate with an **upstream scope constraint**, which the analysis already gestures at (lines 160–161: "cosmetic-only passes cannot modify physics or control scripts"). That's the right instinct and it's the higher-leverage half. Make it concrete:

- **Art/polish passes get a write-scope allowlist.** They may touch sprite assets, shaders, particle configs, UI theme, audio. They may **not** edit files matching control/physics/state-machine patterns (input handling, `move_and_slide`, velocity/acceleration constants, game-state transitions). Enforce at the tool layer — reject the write, don't rely on the prompt. You already have file-lock and write-scope machinery in the agent runtime; this is the same shape.
- **The design doc gets promoted from a plan-time input to a persistent conformance contract.** Extract the load-bearing invariants (genre, control scheme, core loop, win/lose condition) into a small structured `DESIGN_INVARIANTS` block. Inject it into *every* feature and art prompt, not just the planner. Reference it in the conformance gate. This is your fixed point.

The scope constraint prevents the drift class that killed Arm A (art touching controls). The conformance gate catches the residual drift that leaks through feature tasks. Neither alone is enough; the gate without the constraint is a smoke alarm with no sprinklers, and the constraint without the gate blocks the obvious path while leaving the subtle one open.

One caution: **do not over-constrain into blandness.** The reason C was the worst game is that it never took a visual/design swing at all. If your scope constraints are so tight that no agent can make an interesting choice, you'll manufacture more Arm Cs. The constraint should fence *identity-defining* files (controls, core mechanic), not creative surface. Drift is a failure of anchoring, not of ambition — don't fix it by amputating ambition.

---

## 3. Run-13 direction — mid-vs-tail is the wrong next variable

The analysis and proposal both flag two questions as *more* important than timing, then the analysis picks timing anyway. Trust your earlier self.

Reasons mid-vs-tail is premature:

1. **You'd be tuning a non-established effect.** Batched-wins rests on one playtest overriding your primary metric (§1). Refining *when* to batch, before confirming batching beats per-task on anything measurable and repeatable, is polishing a hypothesis you haven't earned.
2. **n=1 per cell, again.** The proposal itself argues (proposal §3) that between-arm noise at ~50 tasks is large, and that a 2×2 gives "four anecdotes instead of three." Mid-only vs tail-only vs both is the same trap — three timing anecdotes on a metric you've just shown doesn't track quality.
3. **The genre question dominates external validity.** Twelve runs, one game. Every art-pass finding is potentially "Void Patrol is sprite-dense and visually forgiving." The proposal explicitly says (proposal line 130) "one replication on a different genre is worth more than a fourth arm on void-patrol." That was true then and the drift finding makes it *more* true — you now need to know whether drift is a Void-Patrol artifact or a general property. A puzzle game or a text-forward game would stress the drift mechanism completely differently (less visual surface for art to hijack).

**What Run-13 should actually be:**

- **Primary:** ship the two mitigations from §2 (design-invariants injection + art write-scope constraint + conformance gate) and re-run the *drift-prone* configuration (Arm A, per-task warm art) **with the mitigations on** vs **off**. The question that matters is not "when should I batch art" — it's **"can I get Arm A's feature velocity without Arm A's drift?"** If yes, per-task art is rehabilitated and you keep the throughput. If no, *then* batching wins for a real reason (it structurally avoids drift), and you can adopt it with justification instead of vibes.
- **Secondary, same run or next:** the second-genre replication. Even one arm.

This reframes Run-13 from "optimize a timing knob" to "validate a control mechanism." The first is a rounding-error study; the second decides whether your autonomous pipeline can be trusted to stay on-brief — which is the actual blocker to everything in reviews 05–08 about autonomy and shipping.

If you insist on keeping the art-scheduling thread alive, fold it in cheaply: run mitigated-A and batched-B in the *same* Run-13, and if mitigated-A matches B on the conformance gate and beats it on features, per-task art with guardrails is your answer and the timing question dissolves.

---

## 4. More features, wrong design — worse, and it indicts per-task art

From a "game that ships" standpoint this is not close. **A shipped game is a coherent game.** 14 features of a game that isn't the game you specified is 14 features of a prototype you now have to either accept (you shipped something you didn't design — fine only if it's *good*, which requires taste you're not yet automating) or repair back toward spec (which means A's feature count is partly *negative work* — it built distance from the target). Arm A's own analysis concedes this (lines 184–186): "design drift may have created more repair surface," and its 24 bug fixes vs B's 14 is consistent with A spending extra budget fighting the consequences of its own divergence.

So the reframe is stronger than "more features but wrong design is a wash." It's: **A's high feature count is partly an artifact of the drift, and partly a liability created by it.** Rotation-thrust combat opens a different, larger design space than a constrained vertical scroller — more surface, more emergent interactions, more features to add and more bugs to chase. A wasn't more productive; it was building a *bigger, wronger* thing. Feature count went up because the target got vaguer, not because the pipeline got better.

**Does this change the calculus on per-task art for certain task types?** Yes, and usefully:

- **Per-task art is fine — even good — for task types that cannot influence design.** A pure sprite-swap or particle-polish pass, *scope-constrained to cosmetic files*, gets the warm-context benefit (agent knows what it just built, art fits the feature) with no drift risk, because it structurally cannot touch controls or mechanics. That's the win from §2: the value of per-task art was real (assets on disk, features that look finished), the danger was only the *scope*. Constrain the scope and per-task art becomes the best of both.
- **Per-task art is dangerous for task types with wide write scope** (anything that can edit control/state code). That's where the feedback loop finds a path to design.

So the answer isn't "batch vs per-task." It's **"per-task art, scope-locked to cosmetics."** That preserves A's velocity, B's coherence, and dissolves the false dichotomy the run set up. This is the actual product of Run-12 and it isn't stated in the analysis.

---

## 5. V/R as a metric — the fourth appearance is the finding

You flagged this yourself: V/R anti-correlated with playability for the third or fourth time. B > A on play, A ≈ B on V/R; and in run-11 the art arm's 2.8x V/R crowned it while this run's coherent winner (B) barely edged A on V/R. When a metric disagrees with the outcome you care about *four times*, the metric is not noisy — **the metric is measuring a different thing, reliably.**

What V/R actually measures: **the ratio of forward-motion tasks to cleanup tasks.** It's a throughput/efficiency meter — "how much of the budget went to building vs fixing." That's a genuinely useful operational signal. A run at V/R 0.64 (Arm C) is *stuck* — burning budget on repair, not building — and V/R correctly flagged C as the worst-performing *process*. Where V/R is silent is on **whether the motion was toward the right target.** It's a speedometer with no compass. Arm A had high V/R *and* drove off the map; the speedometer read fine the whole way.

This is why it anti-correlates with playability specifically at the top end: the two arms that are *building fast* (A and B) both have healthy V/R, and among fast-building arms, the differentiator is *direction* (coherence, taste, design conformance) — which V/R cannot see. So the moment you're comparing two productive arms, V/R goes flat and quality decides the winner. The anti-correlation isn't V/R being wrong; it's V/R being **orthogonal**, and orthogonal metrics look anti-correlated whenever the thing they *don't* measure is what varies.

Concretely, use V/R as:
- **A floor detector.** V/R < ~0.7 means the process is stuck in repair (Arm C, run-11 control). That's real and actionable — it says "this pipeline configuration is spinning, intervene." Keep it for that.
- **Not a quality signal, and not a tiebreaker between healthy arms.** When two arms are both above the floor, V/R has no more to say. Stop putting it in the headline row of the results table; stop letting a 1.0-vs-1.07 gap imply anything. The analysis already knows this (it calls the gap "within noise") but then structures the whole comparison around it anyway.

**Is it a lagging indicator of something else?** Partly. Low V/R lags a *design or scoping* problem — Arm C's repair spiral and run-11 control's fix/refactor loop both reflect a plan that generated more problems than progress. So a *sustained* low V/R is a lagging signal that the upstream plan is bad. But high V/R is not a leading indicator of anything good — it's just "moving," and §2/§4 established that fast motion without an anchor is how you get Star Control when you asked for Galaga.

**What to measure instead, going into Run-13:** you need a *direction* metric to sit next to the *motion* metric. The design-conformance gate from §2 is exactly that — a binary or graded "still on-spec?" signal. Pair `V/R` (are we moving, and toward build or repair?) with `conformance` (are we moving toward the target?). Those two together would have flagged Arm A immediately: high V/R, failing conformance = "productive and lost." Neither metric alone catches it. That pairing is the durable instrumentation upgrade this run argues for, and it's worth more than any art-scheduling result.

---

## 6. Confounders and integrity notes (don't let these slide)

The analysis is admirably honest about these; I'm reinforcing, not correcting.

- **The StateServer headless bug hit all arms.** Fine for *between-arm* comparison (it's a common-mode error), but note it means *every* arm needed research-feeder recovery to get a green bot, so the "bot completed" signal is not evidence of quality for any arm — it's evidence the recovery path works. Don't cite bot-completion as a per-arm quality point (the analysis mostly avoids this — keep avoiding it).
- **Human playtest is n=1, no rubric.** The analysis says (lines 187–189) the doc-07 rubric was not formally applied. Given that the entire "batched wins" conclusion rests on this one session, this is not a minor caveat — it's load-bearing. **Before Run-13, apply the doc-07 rubric formally, ideally blind to arm labels.** If B still wins under a structured rubric, the conclusion firms up. If the ranking wobbles, you've dodged building a baseline on a hunch.
- **Arm A's 58 completed tasks are inflated by its own drift** (more surface → more bugs). Do not read A's task count as productivity. The analysis flags this (lines 184–186); make sure it propagates into any Run-13 planning that uses A's structure as a velocity reference.

---

## 7. What to actually take from Run-12

1. **The run-11 art-pass V/R story did not replicate.** A≈B on V/R. The 2.8x from run-11 was, as the run-11 doc itself warned, contaminated by the interruption and by the integration arm's symlink failure. Treat run-11's headline number as retired. This is a *good* outcome — the experiment did its job.
2. **The real finding is design drift**, a general failure of unconstrained warm-context loops, detectable only by playing the game. It is the most important thing Run-12 produced and should be the title of the analysis, not a subsection.
3. **The fix is two-layered:** upstream write-scope constraints on art/polish (prevent) + a design-conformance gate with injected invariants (detect). The gate alone is insufficient.
4. **The dichotomy the run set up (per-task vs batched) is false.** The answer is per-task art *scope-locked to cosmetics* — it keeps A's velocity and B's coherence. That's the product; the analysis doesn't state it.
5. **V/R is a motion meter, not a quality meter.** Fourth confirmation. Demote it to a floor detector; pair it with a conformance signal.
6. **Run-13 should validate the drift mitigation and add a second genre — not tune art timing.** Timing is a rounding-error study on an effect you haven't established.

The single sentence I'd want you to carry forward: **you built a pipeline that can be productively, confidently wrong, and nothing in it noticed — so the next thing to build is not a better art schedule, it's a compass.**
