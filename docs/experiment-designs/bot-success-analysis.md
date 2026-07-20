# Playthrough Bot Success Analysis

**Date:** 2026-07-13
**Question:** What predicts whether a deterministic playthrough bot can autonomously complete a game — and what does bot success actually measure?

**Dataset:** 8 projects with playthrough bot tasks. 7 with results (manatee-tides pending). 63 bot tasks total: 57 completed, 6 cancelled.

---

## 0. The data, derived

| Project | Tasks | Failed | V/R | Bots | Completed | Cancelled | Success rate | Attempts | Attempts / completed bot |
|---|---|---|---|---|---|---|---|---|---|
| star-sovereigns | 685 | 94 | 0.17 | 1 | 1 | 0 | 100% | 4 | 4.0 |
| tetris-neon | 44 | 0 | 0.41 | 4 | 3 | 1 | 75% | 12 | 4.0 |
| fusion-foundry-td | 53 | 2 | 0.47 | 17 | 15 | 2 | 88% | 15 | 1.0 |
| marble-mania | 108 | 0 | 0.68 | 22 | 20 | 2 | 91% | 10 | 0.5 |
| void-patrol-bot-proof-run12 | 59 | 0 | 0.71 | 4 | 3 | 1 | 75% | 6 | 2.0 |
| void-patrol-playthrough-bot-test | 103 | 1 | 1.26 | 14 | 14 | 0 | 100% | 3 | 0.2 |
| word-wizard | 6 | 0 | 2.00 | 1 | 1 | 0 | 100% | 1 | 1.0 |
| manatee-tides | 95 | 0 | 7.43 | 1 | 0 (pending) | 0 | — | 0 | — |

Aggregate (excluding manatee-tides): **57/63 = 90.5% bot completion rate.** Median per-project attempts-per-completed-bot ≈ **1.0**.

**Caveats on the attempts field before anything else:**

- We only have *total* attempts per project, not per-bot-task distributions. "Attempts/completed bot" above is an average that mixes first-try successes with grinders. Median attempts-to-success per bot task is not computable from this data.
- **The attempts numbers are internally inconsistent with the cancellation semantics.** If "cancelled" means "exhausted max_attempts=20," fusion-foundry-td's 2 cancelled bots alone should contribute ~40 attempts — but the project total is 15. Either (a) cancelled bots were cancelled manually/early, (b) the attempts field only accumulates for completed bots, or (c) max_attempts was lower for these tasks. Until this is resolved, treat attempts as a *relative* difficulty signal, not an absolute count. This needs to be fixed before run 12 uses attempts as a metric (see §7).

---

## 1. Bot success rate by project

Completion rate is high almost everywhere: 5 of 7 projects are at 88–100%; the two at 75% (tetris-neon, void-patrol-bot-proof-run12) each have exactly one cancelled bot out of four, so a single failure moves the rate by 25 points. **At this n, per-project success rate is nearly useless as a discriminator — everything clusters near the ceiling.**

The interesting variance is in the 6 cancellations. Given the attempts inconsistency above, we cannot tell from this data whether they represent:

- **Uncompletable games** — the game genuinely has no path to completion (a real, valuable signal), or
- **Agent gave up on a solvable problem** — bot-authoring difficulty (flaky harness, real-time timing, physics nondeterminism), or
- **Early manual cancellation** — operational noise, not a signal at all.

Circumstantial evidence points away from "uncompletable game": every project with cancellations *also* has multiple completed bots (fusion-foundry-td 15, marble-mania 20, tetris-neon 3, void-proof 3). If the game were uncompletable, later bots on the same project should also fail. The most likely reading is that cancellations reflect **bot-authoring difficulty against a specific game state or interface**, or operational cleanup — not game uncompletability. But this is inference; the data doesn't record *why* a bot was cancelled.

Also note what bot *count* means: fusion-foundry-td (17) and marble-mania (22) have many bot tasks because bots were re-run repeatedly as the game evolved — bot count measures how long a project ran with bot verification enabled, not difficulty. Don't aggregate across bots of different game vintages as if they were repeated trials of the same thing.

## 2. Does V/R ratio predict bot success?

**No — not in this data, in either direction.**

Ordering projects by V/R against their bot outcomes:

- V/R 0.17 (star-sovereigns): completed, 4 attempts
- V/R 0.41 (tetris-neon): 75%, ~4.0 attempts/completion — worst attempts profile
- V/R 0.47 (fusion-foundry-td): 88%, ~1.0
- V/R 0.68 (marble-mania): 91%, ~0.5 — best attempts profile among production runs
- V/R 0.71 (void-proof-run12): 75%, ~2.0
- V/R 1.26 (void-patrol test harness): 100%, ~0.2 — confounded (this is where the bot tooling was developed; excluded from inference)
- V/R 2.00 (word-wizard): 100%, 1 attempt — n=1, 6-task micro-project

There is a *faint* positive association if you squint (the two 100%/low-attempt projects have the highest V/R), but it's carried entirely by the confounded test harness and a 6-task project. Within the production band (V/R 0.17–0.71), the ordering is non-monotonic: the lowest-V/R project (star-sovereigns) completed, and the second-lowest (tetris-neon) has the same attempts profile despite 15× fewer tasks.

**The strong claim the data supports: a high-churn pipeline does NOT produce an uncompletable game.** star-sovereigns is the existence proof — 474 repair tasks, V/R=0.17, and the bot still passed in 4 attempts. Churn during development and end-state completability are, at minimum, decoupled; possibly the churn is *why* the end state is completable (474 repair tasks means 474 rounds of something being detected and fixed).

## 3. What predicts bot difficulty (attempts needed)?

Testing each candidate against attempts-per-completed-bot:

- **Total task count?** No. star-sovereigns (685 tasks) → 4 attempts; tetris-neon (44 tasks) → ~4 attempts avg; marble-mania (108 tasks) → 0.5. If anything the relationship is inverted-U or absent. More accumulated work does not make a game harder to bot.
- **Repair ratio?** No. Highest repair share (star-sovereigns, 69% of tasks are repair) completed in 4; marble-mania (31% repair) is the easiest production project at 0.5 attempts/bot. tetris-neon (50% repair) is hard, fusion-foundry (28%) is easy — no pattern.
- **Failed tasks?** No. star-sovereigns' 94 failed tasks did not block the bot. Meanwhile all 6 bot cancellations occurred on projects with 0–2 failed tasks. Failed task count measures pipeline friction during development, not end-state playability.
- **Something else?** The best candidate visible in this data is **game genre / interface type**. The projects with cancellations or high attempts are real-time or physics-driven: tetris-neon (real-time falling blocks), marble-mania (physics), fusion-foundry-td (tower defense with wave timing), void-patrol. The clean first-try successes are turn-based or discrete-input (word-wizard). A deterministic bot's difficulty is plausibly dominated by how *legible and deterministic the game's interface* is — StateServer coverage, checkpoint availability, timing sensitivity — not by anything in the task ledger. This is a hypothesis, not a finding; genre isn't a recorded field (see §7).

Bottom line: **nothing in the task-breakdown data predicts bot difficulty.** That itself is the finding — the bot is measuring an axis orthogonal to the pipeline metrics we track.

## 4. The star-sovereigns anomaly

685 tasks, 94 failures, 474 repair tasks, V/R=0.17 — and the bot passed on attempt 4, which is unremarkable (tetris-neon needed the same on a 44-task project).

Three readings, not mutually exclusive:

1. **V/R measures pipeline efficiency, not product quality.** V/R tells you what fraction of agent effort went to forward progress vs. rework. A V/R of 0.17 means the pipeline was wasteful — it does not mean the output is broken. star-sovereigns paid a 474-repair-task cost to reach a completable state; a healthier pipeline would have reached it cheaper, not "more completable."
2. **Repair tasks converge the game.** Each repair task is evidence a defect was *found and fixed*. A project that survives 474 repair cycles has been error-corrected 474 times. High churn may actually *raise* end-state completability while wrecking cost efficiency. (Counterpoint we can't test: we don't know if a zero-churn star-sovereigns would also have passed.)
3. **The bot tests a narrow slice.** The bot verifies one golden path to completion. star-sovereigns could simultaneously be full of off-path bugs and have a clean critical path — the bot can't distinguish these.

The anomaly's real lesson: **V/R and bot success are answering different questions** (how efficiently was it built vs. does the built thing work end-to-end), and run 11's use of V/R as the headline metric should not be conflated with a quality claim about the games. Both metrics are needed; neither substitutes for the other.

## 5. What the bot is actually measuring

Based on the completion patterns, bot success is best understood as a **conjunction of three things**:

1. **Completability** — a path from start to win-state exists and is reachable. This is the intended signal, and it's real: it's a full end-to-end integration test that no amount of GUT tests or headless validation replicates.
2. **Interface legibility** — StateServer exposes enough state, checkpoints exist, inputs are injectable, and the game is deterministic enough for a scripted player. A cancellation may mean "illegible interface," not "broken game."
3. **Bot-authoring tractability for the agent** — attempts partly measure how hard it was for the *agent* to write a correct bot (understanding game rules from code, timing loops, state polling), which is a property of the codebase's clarity as much as the game's correctness.

What it does **not** tell you:

- **Fun, polish, or visual quality** — a gray-box game with one button can pass; run 11's art-arm value is invisible to the bot.
- **Off-path correctness** — bugs on branches the bot doesn't take.
- **Difficulty balance** — a bot with perfect information and reflexes completing the game says nothing about human playability.
- **Robustness** — the bot exercises one deterministic trace; it won't find race conditions or state corruption from unusual input orderings.
- **Regression over time** — unless bots are re-run per commit, a pass is a statement about one snapshot.

Given the 90.5% aggregate pass rate, the bot as currently deployed is close to a **smoke test with a high pass ceiling**: it reliably distinguishes "structurally broken or illegible" from "completable," and little else. Its discriminating power lives in attempts and cancellations, not in pass/fail.

## 6. Implications for run 12 measurement

1. **Don't use pass/fail as the arm-comparison metric.** At a 90% base rate with a handful of bots per arm, pass/fail has near-zero statistical power. An arm difference would need to be enormous to show up.
2. **Use attempts-to-first-passing-bot as the primary bot metric** — but only after fixing the attempts-field semantics (§0, §7). It's the only bot output that varies meaningfully across projects (0.2–4.0 range here). Treat cancellations as right-censored observations at max_attempts, not as missing data — a cancelled bot is the *worst* score, not no score.
3. **Standardize the protocol so bot results are comparable.** Current bot counts (1 vs 22 per project) reflect run history, not design. For run 12: one bot task per arm at each fixed gate (e.g., mid-run and final), same max_attempts, same prompt. Compare arms on (final-gate attempts, cancellation indicator) pairs.
4. **Keep V/R and bot metrics as separate axes; report both.** §2/§4 show they're decoupled. The run-12 headline should be something like: value/repair ratio (pipeline efficiency) × bot attempts at final gate (end-state completability) — an arm that wins on one and loses on the other is a genuinely different tradeoff, not a wash.
5. **Log the failure reason on every bot attempt** (game bug found / harness gap / bot logic error / timing). This converts the bot from a pass/fail smoke test into a bug-finding instrument — bot attempts that fail because of *game* bugs should spawn bug tasks and be counted as detection value, not just as difficulty.
6. **Expect the bot to be insensitive to the art arm.** If run 12 makes art-pass the baseline (per run 11), bot metrics will not detect art regressions or improvements at all. Don't interpret "bot metrics flat across arms" as "arms equivalent."

## 7. What data is missing

Ordered by leverage:

1. **Per-attempt failure reason** (enum: game_bug / harness_gap / bot_logic / timing / env). The single highest-value addition — it resolves the §1 ambiguity about what cancellation means and turns attempts into an interpretable signal.
2. **Fix the attempts accounting.** Per-bot-task attempt counts (not project totals), with cancelled bots' attempts included, and a recorded cancellation reason (exhausted vs. manual). The current numbers are provably inconsistent (§0).
3. **Progress depth on failure** — furthest checkpoint / scene / level / score the bot reached before each failed attempt. Distinguishes "died at the menu" (interface problem) from "died at level 9/10" (near-complete game). This is the difference between a binary and a gradient metric.
4. **Wall-clock and cost per bot task** — time-to-bot-success, agent loops, tokens. Attempts is a proxy for effort; measure effort directly.
5. **Game genre / interface class** (turn-based, real-time, physics) — needed to test the §3 hypothesis that genre, not pipeline history, drives bot difficulty. Cheap to backfill from GAME_DESIGN.md.
6. **Game commit SHA per bot run** — ties each bot result to a game snapshot, enabling "did the game get more completable over the run" trajectories instead of one aggregate per project.
7. **Bot playtime / actions executed on success** — a passing 10-second bot and a passing 10-minute bot say very different things about game depth.
8. **Bug tasks spawned by bot failures** — links the bot into the detection-value accounting used in run 11 analysis.

## Limits

n=7 projects with results, and only ~5 are clean production runs (void-patrol-playthrough-bot-test is the tool-development harness; word-wizard is a 6-task micro-project). One project (star-sovereigns) carries most of the argument in §4 with a single bot task. No correlation here survives removing one project. The two robust statements are the negative ones: **task-ledger metrics (count, V/R, failures) do not predict bot outcomes in this sample**, and **extreme churn did not prevent completability in the one extreme-churn case we have**. Everything else is hypothesis material for run 12's instrumentation to confirm or kill.
