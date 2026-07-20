# 11 — Publishing Pipeline: From Green Checkmarks to Shipped Games

*Fable review, 2026-07-13. Companion to [04-two-year-vision.md](04-two-year-vision.md) and
[05-director-agent-architecture.md](05-director-agent-architecture.md). Implements roadmap #14.*

## The gap, stated plainly

The pipeline today ends at a database column. A game reaches `closure_status: green`,
`ship_candidates()` ranks it first, the playthrough bot proves it's completable — and then
nothing happens. The signal exists; there is no actuator. "Shipped" is undefined, so the
system's terminal state is "sits in a Gitea repo forever."

This doc defines shipped, designs the export → publish → post-publish loop, and names the
four real blockers up front so nobody discovers them in week three:

1. **itch.io page metadata has no write API.** butler pushes builds; it cannot set
   description, tags, cover art, or flip a page from draft to public. Full autonomy to
   itch.io is not possible with supported interfaces. The design works around this with a
   two-tier target (self-hosted web = autonomous; itch.io = one 5-minute human step per game).
2. **The shipped artifact is invisible to the entire existing QA stack.** StateServer is
   raw TCP (port 11009); web exports run in WASM, which has no TCP sockets. Every QA tool —
   `get_game_state`, `click_element`, `a11y_tree`, harness checkpoints — works only against
   the editor build. A green closure status certifies a build nobody will ever play.
   Worse: StateServer has **no debug gating** (`templates/godot/autoload/state_server.gd`
   listens unconditionally), so a shipped desktop build ships an open TCP control port.
3. **`export_presets.cfg` is gitignored in the templates** (`templates/godot/.gitignore:10`,
   `swarm/api_wizard.py:1482`). No project in the fleet has export configuration, and the
   scaffolding actively prevents committing it. Every game needs a one-time export-setup
   task, and the gitignore rule must be reversed first.
4. **Cover art.** The art pass agent produces in-game assets, not marketing assets. The
   minimum viable answer (composited screenshot + title) is achievable and defined below,
   but it is jam-tier, and pretending otherwise would be papering over it.

None of these are fatal. Two (#2, #3) are fixable in code this month; #1 is a bounded
human step; #4 has a floor that's acceptable for free web games.

---

## 1. What "shipped" means

### The definition

**Shipped = a stranger can reach a URL, the game loads, and the core loop described in
`GAME_DESIGN.md` is playable to completion, without anyone from the studio in the loop.**

Everything below decomposes into gates that certify that sentence.

### The ship bar (gates, in dependency order)

The closure spec already has `mode: build | stabilize | ship` (`swarm/closure/specs.py`).
Today `ship` mode changes nothing. Give it teeth — a project in `ship` mode must pass
all of the following before a publish task can be created:

| Gate | Already exists? | Source of truth |
|------|----------------|-----------------|
| G1. Closure green | ✅ | `closure_status == "green"` (boot_ok, tests_ok, critical flows, zero open regressions) |
| G2. Playthrough bot completed | ✅ | `playthrough_bot` task completed (already the top signal in `ship_candidates()`) |
| G3. QA cycle exhausted cleanly | ✅ partially | Final QA cycle produced zero new P0/P1 bug tasks; `QA_REPORT.md` known-issues list contains nothing tagged `blocker` |
| G4. Export succeeds | ❌ new | `export` task completed; artifact exists and is non-trivially sized |
| G5. **Exported-build smoke test passes** | ❌ new | Playwright loads the web build: canvas renders, no JS console errors, first-scene screenshot is non-black (see §2) |
| G6. Store page assets exist | ❌ new | `STORE_PAGE.md` + cover + ≥3 screenshots present (see §6) |
| G7. Release hygiene | ❌ new | QA autoloads stripped from release build; git tag created; version recorded |

G5 is the load-bearing new gate. G1–G3 certify the editor build; G4–G5 certify the thing
players actually get. These are different artifacts and the difference is exactly where
web exports break (threading, audio worklets, GDExtension, texture compression).

### Is there a quality floor below which the system refuses to publish?

Yes, and it should be explicit rather than emergent. All gates above are *correctness*
gates — a game can pass every one and still be 40 seconds of joyless clicking. Add a
small **ship-floor checklist** to the closure spec's ship mode, verified by the final QA
cycle (these are vision-QA-verifiable, not vibes):

- Core loop is completable AND repeatable (game-over → restart works; the playthrough
  bot proves completable, QA must prove restartable).
- A player who has never seen `GAME_DESIGN.md` can discover the controls (title screen
  or in-game hint exists — a11y tree check).
- Win and lose states both reachable and visually distinct.
- No placeholder assets in the first 60 seconds of play (vision_query check — the run-11
  art-arm result says per-task art passes make this achievable).
- Audio: at least silence-by-intent, not missing-file errors.

This floor is deliberately low. These are free web games; the floor's job is to prevent
*embarrassment*, not to demand quality. The Director raises the bar over time using
telemetry (roadmap #15/#18) — the floor is the constant, taste is the variable.

**Anti-gaming note:** the floor must be verified by a QA agent that did not implement the
game features (it already is — separate task, separate agent), and the checklist result
goes in `metadata.ship_floor_report`, not a self-attested `TASK_COMPLETE`.

### Who makes the publish call

Three-phase answer, matching the autonomy trajectory in doc 06:

**Phase 1 (now): human-released ship gate.** Reuse the existing `phase_gate` machinery —
gates are skipped by the scheduler (`orchestrator.py` ready-task loop) and released via
`POST /api/projects/<name>/phase-gates/<id>/release`. Define a `ship_gate` as a
`phase_gate` task with `metadata.gate_kind: "ship"`. When G1–G7 all pass, the gate
surfaces in the human review queue (roadmap #11) with a one-click release. The human
call is cheap here because it coincides with the one manual step itch.io forces anyway
(page setup, §3) — batch them into a single 5-minute session.

**Phase 2: Director proposes, human confirms.** The Director (doc 05) watches
`ship_candidates()` and creates the ship-gate chain (export → store page → publish)
proactively, with the release click still human.

**Phase 3: auto-release for the self-hosted tier only.** Publishing to your own static
host is reversible and unembarrassing at fleet scale; itch.io publishes stay
human-confirmed indefinitely (it's a community with norms, and it's also the tier where
the human step is mandatory anyway).

Do **not** make publish automatic-on-criteria-met in phase 1. The gates are new and
their false-positive rate is unknown. Automatic publish is earned by a track record the
analytics layer (#7) can show, same rule as everything else on the roadmap.

---

## 2. Export pipeline

### Export is not an LLM task

Key design decision: `godot --headless --export-release "Web" <path>` is deterministic.
Running an LLM agent whose job is to execute one known shell command is the wrong shape —
it burns quota, adds nondeterminism, and the failure modes (missing template, preset
error) need diagnosis only *sometimes*.

Split it:

- **`export_setup`** — an LLM task (one-time per project, feature-prompt family). Writes
  `export_presets.cfg` with a Web preset, fixes export-blocking issues (GDExtension refs,
  thread settings, viewport/stretch config for browser), commits. This is genuinely agent
  work: presets interact with project settings and the errors are project-specific.
- **`export`** — a **system task**: a task row in the DAG (so dependencies chain through
  it normally) whose `metadata.executor == "system"`. The orchestrator, on picking it,
  spawns a subprocess running a deterministic pipeline script instead of the LLM wrapper —
  same handle machinery, same PID tracking, same `data/agent_<id>.log`, same timeout,
  zero LLM calls. On nonzero exit, normal retry applies; on exhaustion, the research-feeder
  escalation fires exactly as it does for any task — and *that* agent diagnoses the export
  log. This reuses the whole recovery stack while keeping the happy path free.

This "system executor" concept is small (one branch in `generate_task_script()` /
`fill_slots`) and immediately reusable — the publish task (§3) and future deterministic
steps (asset optimization, tag-and-release) use the same mechanism. It is also a gentle
first step toward the roadmap-#9 event-driven core: jobs that aren't agents.

### What the export system task runs

```
1. git -C <project> rev-parse HEAD                 # record exact commit
2. Strip/verify QA autoloads (see below)
3. godot --headless --path <project> --export-release "Web" \
       <workspace>/_builds/<project>/<version>/web/index.html
4. Verify artifact: index.html + .wasm + .pck exist, .wasm > 1MB
5. Playwright smoke: serve dir with COOP/COEP headers, load page,
   wait for canvas, assert no console errors, screenshot, assert non-black
6. Write manifest.json {project, version, commit, godot_version, files, sha256s}
7. git tag v<version> && git push --tags
8. (optional) POST artifact zip to Gitea release API
```

Step 5 is the G5 gate and it's the piece that makes export more than a checkbox. It needs
a tiny static server with `Cross-Origin-Opener-Policy: same-origin` and
`Cross-Origin-Embedder-Policy: require-corp` headers — Godot 4 web builds require
cross-origin isolation for SharedArrayBuffer. (Alternative: export with
`variant/thread_support=false` for maximum compatibility — slower but works without the
headers and on more hosts. **Recommend thread_support=false as the default preset**; these
are 2D games, they don't need worker threads, and it removes the single most common
web-export failure class. itch.io's SharedArrayBuffer support is still flagged
experimental.)

Playwright is already a dev dependency (dashboard tests) — no new infrastructure.

### QA autoload stripping (do this regardless of publishing)

`state_server.gd` listens on TCP 11009 unconditionally. Two fixes, both cheap:

1. Guard the template: in `_ready()`, `if not OS.is_debug_build(): return`. Release
   exports are non-debug by definition, so shipped builds no-op. Sync via
   `sync_templates.py` to all projects (the established mechanism).
2. Belt-and-braces: the export step greps the artifact's `project.godot` export config
   or uses preset `exclude_filter` for `autoload/state_server.gd, autoload/test_harness.gd`.
   (Prefer the code guard — exclude_filter breaks if anything else references the script.)

This is a security fix independent of shipping; do it first.

### Platforms: web first, desktop never (for now)

**Web (HTML5) is the only target in v1.** Reasons:
- itch.io plays web builds in-browser — zero install friction, which for unknown free
  games is the difference between 30 players and 0.
- Desktop builds multiply everything: per-platform export templates, macOS
  codesigning/notarization (genuinely painful, requires Apple Developer account and
  human-held credentials), Windows SmartScreen reputation, per-platform smoke testing
  the swarm has no harness for.
- Telemetry (roadmap #15) works identically from a web build via HTTP POST.

Desktop is a tripwire item for ROADMAP-NON-GOALS: reopen when a specific game has
demonstrated web-tier traction, not before.

### Artifacts: local builds dir + Gitea releases, never in the repo

- **Primary:** `<workspace>/_builds/<project>/<version>/` — outside every git repo.
  Build artifacts in git bloat every clone and every worktree (`use_worktrees: true`
  means each validation run would copy them).
- **Durable:** Gitea release per version with the zipped web build as an asset. The
  Gitea API (creds already in config: `gitea_host/user/pass/org`) supports
  `POST /repos/{owner}/{repo}/releases` and asset upload — fully automatable, gives
  immutable versioned storage, and makes the release visible in the existing repo UI.
- The `releases` DB table (§7) records the canonical path + checksums; the builds dir is
  reconstructible from Gitea if the disk entry is lost.

Retention: keep the latest 3 versions locally, all versions in Gitea. Wire into the
existing log-rotation machinery (`log_retention_days` precedent).

### Export templates in a headless environment

Templates are a ~1 GB `.tpz` per Godot version and **must exactly match the binary
version** (including `.stable`/`.rc` suffix). There is no reliable headless
auto-install in 4.x; treat it as environment bootstrap, not per-task work:

- `tools/install-export-templates.sh`: reads the version from
  `"$GODOT_PATH" --version`, downloads
  `https://github.com/godotengine/godot/releases/download/<ver>/Godot_v<ver>_export_templates.tpz`,
  unzips into `~/Library/Application Support/Godot/export_templates/<ver.stable>/`
  (the `.tpz` is a zip with a `templates/` root — rename accordingly).
- The export system task's step 0 checks the directory exists and fails fast with a
  message naming the script if not. **Do not** have agents download 1 GB inside task
  loops; one machine, one install per Godot upgrade.
- Config key `godot_export_templates_dir` for non-default locations; validated at
  startup alongside `godot_path`.

### Fix the gitignore

Remove `export_presets.cfg` from `templates/godot/.gitignore` and from the wizard
scaffold gitignore (`swarm/api_wizard.py:1482`). The historical reason to ignore it is
embedded signing credentials — web presets have none. Without this, `export_setup`'s
work evaporates on the next fresh worktree.

---

## 3. Publish targets

### Two tiers, because of blocker #1

**Tier 1 — self-hosted static web (fully autonomous).** A directory of static files is
a complete distribution channel for a Godot web build. Rsync/copy
`_builds/<project>/<version>/web/` to a static host (a `games/` vhost on the same box,
a $4 VPS, or GitHub/Gitea Pages) with the COOP/COEP headers (or thread-free builds that
don't need them), plus an auto-generated index page from `STORE_PAGE.md`. This is the
tier where "a stranger can reach a URL" becomes true with zero human involvement, and
it's where telemetry (#15) points first. It is also the honest MVP: **build Tier 1
first**, because it exercises the whole pipeline without blocking on itch.io's manual
step.

**Tier 2 — itch.io via butler (one human step per game, then autonomous updates).**

### The itch.io publish task

Also a **system task** (`publish`, `metadata.executor: "system"`):

```
1. butler validate <build_dir>                         # sanity (butler has no real validate; use dir checks)
2. butler push <build_dir> <itch_user>/<game-slug>:html5 \
       --userversion <version> --if-changed
3. butler status <itch_user>/<game-slug>:html5         # confirm build processed
4. Poll until the pushed build leaves "processing"; record butler build id
5. Update releases row: status=live, channel=itch, url=https://<user>.itch.io/<slug>
6. HTTP GET the page URL → 200 check (only meaningful after human page-publish)
```

butler details that matter:
- Auth: `BUTLER_API_KEY` env var (from itch.io account settings) — goes in `.env`
  alongside the other keys, never in config.json.
- Pushing to a nonexistent target **auto-creates the project as a draft page**. That's
  the hook for the human step: the swarm pushes first, the human then spends 5 minutes
  on the draft (title confirm, description paste from STORE_PAGE.md, cover upload,
  tags, "This file will be played in the browser" checkbox, visibility → public).
- butler uploads are diffed (bsdiff) — repeat pushes upload deltas, so updates are cheap.
- `--if-changed` makes publish idempotent; safe to retry.

### Metadata itch.io needs that the swarm doesn't generate

| Field | Source | Automatable? |
|-------|--------|-------------|
| Title | project name / GAME_DESIGN.md | ✅ |
| Short tagline (≤120 chars) | `store_page` task | ✅ |
| Description (HTML/markdown) | `store_page` task from GAME_DESIGN.md + QA_REPORT known issues | ✅ generate; ❌ upload (no API) |
| Tags/genre (up to 10) | `store_page` task | ✅ generate; ❌ set |
| Cover image (630×500 min) | §6 pipeline | ✅ generate; ❌ upload |
| Screenshots (3–5) | QA agent already takes them | ✅ generate; ❌ upload |
| Classification, pricing (free), kind (HTML) | constant | ❌ set (one-time human) |
| The build itself | butler | ✅ fully |

So: **generation is 100% automatable, page population is 0% automatable.** The
`store_page` task's deliverable is a `store/` directory in the repo —
`STORE_PAGE.md` (paste-ready description + tagline + tag list), `cover.png`,
`screenshots/01..05.png` — so the human step is pure copy-paste, no judgment or
authoring. Measure it: if the step takes >10 minutes, the store_page task is failing
its job.

### Who generates the metadata

A **dedicated `store_page` task**, not the planner and not the Director:

- Not the planner: it runs at project start, before the game exists; descriptions
  written from a design doc read like design docs. The store page must describe the
  game *as shipped* (post-QA, post-art-pass, known issues acknowledged).
- Not the Director: the Director decides *whether/when* to ship (portfolio call); an
  agent with vision tools and repo access produces the assets. Same separation as
  everywhere else in the system.
- The `store_page` agent is an `art_pass`-family agent (vision-capable, write access):
  launches the game, plays briefly, captures screenshots at visually strong moments
  (vision_query to rank candidates), composites the cover (§6), writes the copy from
  GAME_DESIGN.md + its own observation of the actual game.

DAG position: `ship_gate → export_setup → export → store_page → publish(tier1) →
publish(itch, after human page setup)`. `store_page` depends on export only so its
screenshots reflect the shipping build.

### Failure modes: butler fails or itch.io is down

- The system-task retry machinery handles it for free: nonzero exit → `pending` with
  `last_failure` → retries with backoff. Set `max_attempts: 4` and put a
  `run_after` +2h on the final retry (the field already exists on Task) so an itch.io
  outage doesn't burn attempts in minutes.
- `--if-changed` + content-addressed butler uploads make retries safe (no double-publish).
- On exhaustion: escalation policy `on_exhaust: "cancel"` + flag for human review
  (this is a network/credential problem, not a code problem — a research feeder
  cannot fix itch.io being down). Add `publish` to `_DEFAULT_ESCALATION_POLICY`
  accordingly.
- Crucially, **Tier 1 is unaffected** — the game is live on the self-hosted URL
  regardless; itch.io is additive distribution, not the definition of shipped.

---

## 4. Post-publish loop

### "Published" state in the DB

New table (not columns on `projects` — a project can have many releases across channels):

```sql
CREATE TABLE releases (
    id            TEXT PRIMARY KEY,        -- release-<project>-<version>
    project       TEXT NOT NULL,
    version       TEXT NOT NULL,           -- e.g. 2026.07.1 (date-based; no semver theater for games)
    commit_sha    TEXT NOT NULL,
    channel       TEXT NOT NULL,           -- selfhost | itch
    artifact_path TEXT,                    -- _builds/<project>/<version>/
    url           TEXT,                    -- live URL once known
    status        TEXT NOT NULL,           -- built | pushed | live | superseded | delisted
    butler_build_id TEXT,
    created       TEXT, published_at TEXT,
    metadata      TEXT                     -- JSON: smoke result, checksums, notes
);
```

Plus one denormalized column on `projects` for cheap dashboard/scheduler reads:
`live_release_id TEXT` (null = unpublished). `_evolve_schema()` handles both, per the
established pattern.

"How does the system know it's live": `status` transitions are written by the pipeline
itself (`built` by export, `pushed` by publish, `live` when the URL returns 200 and —
for itch — `butler status` shows the build processed). For itch, `live` additionally
requires the page to be public, which only happens after the human step; a lightweight
poll (the live-watch below) flips `pushed → live` when the public URL stops 404ing.
That poll is also how the system *observes* the human completing their step, closing
the loop without a manual "mark as published" action.

### How bug reports reach the system

Three inlets, in order of build priority:

1. **Telemetry (roadmap #15) — the primary channel.** The spec already exists
   (`docs/telemetry-spec.md`); add one event type to it: `client_error {message,
   stack_hint, scene, version}` fired from a global error hook in the telemetry
   autoload. This catches script errors in the wild — the crash-report channel — with
   no consent complexity (no PII). Publishing is what makes telemetry worth building;
   build them together, this event type first.
2. **itch.io comment scraping.** No comments API; the page HTML is scrapable. A
   `live_watch` poll (below) diffs comment blocks and files anything that parses as a
   bug into triage. Fragile (markup changes break it silently) — treat as best-effort,
   never as the primary channel, and alert when the scraper starts returning zero
   comments for pages that previously had them (silence-vs-breakage ambiguity).
3. **In-game feedback button** (later): a "report a problem" UI posting to the telemetry
   ingest endpoint. Highest signal, needs a template addition; defer until telemetry v1
   is proven.

All three converge on the existing `triage` prompt type: raw report → triage agent →
bug task (or discard). Don't create bug tasks directly from scraped text — comments are
noisy and occasionally adversarial, and triage is exactly the existing tool for that.

### Post-publish bug lifecycle

A dev-phase bug ends at "merged to main." A post-publish bug ends at "players have the
fix." The difference is the tail of the chain, and the batch-task machinery already
expresses it:

```
bug (fix on main, normal validation)          priority 80 (or 100 if crash-class)
 └─► export   (system, new version)           auto-chained via depends_on
      └─► publish selfhost (system)
      └─► publish itch     (system)           parallel; both depend on export
```

Rules:
- **No release branches.** Main is truth, exports are tagged snapshots of main. Release
  branches would double the merge surface for LLM agents — the single-main model is a
  large part of why the current system works. Cost: a post-publish fix ships whatever
  else has landed on main since the last release. Acceptable at this scale; the ship
  gates re-run in the export step anyway (smoke test), so a broken main can't ship.
- The export→publish tail is appended automatically: when `_finish_agent()` completes a
  bug task whose project has `live_release_id` set, it auto-chains the export+publish
  pair — same mechanism as auto-QA/auto-audit in `agent_auto_tasks.py`, and it should
  live there (`auto_spawn_release_tasks()`).
- **Debounce:** don't cut a release per bug fix. Auto-spawn checks for an existing
  pending export task for the project (the auto-QA dedupe pattern) and, if present, does
  nothing — the pending export will pick up all fixes merged before it runs. Optionally
  gate on `run_after` +6h to batch a day's fixes into one release.

### How a published game gets updated

Same path, no special machinery: new version = new `releases` row, previous row flips to
`superseded`. butler's diff-based push makes itch updates cheap; selfhost is a directory
swap (write to `<version>/`, flip a `current` symlink — atomic, instant rollback by
re-pointing the symlink at the prior version, which the retention policy keeps).
Rollback is a publish-task variant that re-pushes a superseded release; on itch,
`butler push` of the old build dir does exactly this.

---

## 5. Closure gate integration

### Publish in the closure spec

Extend the spec's existing vocabulary rather than inventing beside it:

- `mode: ship` (already parses, currently inert) gains meaning: when a project enters
  ship mode, the ship gates G4–G7 become **required gates** alongside the existing
  `boot_ok`/`tests_ok`/`max_open_regressions`. Concretely, `gates` gains
  `export_ok: bool`, `web_smoke_ok: bool`, `store_assets_ok: bool`, and
  `closure/verification.py` learns to check them (artifact exists + smoke manifest for
  the current HEAD-adjacent commit; store/ directory populated).
- The **ship gate is a `phase_gate` task** (`metadata.gate_kind: "ship"`) sitting between
  the last dev-phase task and the export chain. The scheduler already skips phase_gates;
  the release endpoint already exists. New behavior: the closure system may *auto-release*
  the gate when all ship-mode gates are green **iff** `publish_auto_release: true` in
  config (default false — Phase 1 keeps the human click).

### A new closure epoch, yes

Post-ship acceptance criteria are different in kind, not just degree: pre-ship the
question is "does it match the design doc"; post-ship it's "is the live artifact healthy
and current." Add `mode: live` to the spec vocabulary (`build → stabilize → ship → live`),
entered automatically when a release reaches `status: live`. In live mode:

- Gates become: `live_url_ok` (page loads, checked by live-watch), `no_open_p0` (crash-class
  telemetry signatures have no open untriaged reports), `release_current` (main isn't
  >N commits ahead of the published commit — configurable drift threshold, default 25 —
  which turns "we fixed ten bugs but never re-exported" into a visible yellow).
- `closure_status` semantics carry over: red in live mode (site down, open P0) triggers
  the same feature-freeze machinery (`feature_freeze_on_red` already in
  `DEFAULT_AUTONOMY`) — a broken live game outranks new features, which is exactly the
  behavior you want and it falls out of existing code.
- Regression tracking continues across the epoch boundary; a post-publish bug that
  reoccurs is the same regression row, keeping the history unified.

### Director portfolio use

Published/unpublished becomes the Director's primary portfolio axis (doc 05):

- **Unpublished + closure green** = shelf inventory; the ranked `ship_candidates()` list
  is the Director's ship queue. Cost-to-ship (pending tasks × observed cost/task from
  analytics #7) prices each candidate.
- **Published** projects are judged on telemetry (plays, completion rate, quit-points)
  instead of internal proxies — the first externally-grounded signal the system has ever
  had, and the input roadmap #18 (cost per shipped feature) needs for its denominator.
- Portfolio rule the data will eventually support: maintenance effort flows to published
  games with players; unpublished games compete for ship slots; published games with
  zero players after N weeks get `delisted`-or-archive decisions. Until telemetry
  exists, the Director only uses the binary published flag + ship queue.

---

## 6. The cover art problem

Straight answer: **there is no fully-automatic path to *good* cover art. There is a
fully-automatic path to *acceptable* cover art, and a cheap human path to good.**

### Minimum viable pipeline (automatic, ship-blocking bar)

The QA and art-pass agents already capture screenshots through StateServer. The
`store_page` agent:

1. Launches the game, plays to a visually rich moment (mid-gameplay, not menus —
   vision_query ranks candidate frames: "which of these looks most like an exciting
   game?").
2. Captures at high resolution; crops to 630×500 (itch minimum; use 1260×1000 @2x).
3. Composites the title: game name in a bundled display font (ship 3–4 open-font
   choices in `templates/store/fonts/`), dark scrim band or drop shadow for
   legibility, small "Paraxenia" studio mark. Pure Pillow, ~40 lines, deterministic.
4. vision_query self-check: "is the title text legible against this background?" —
   retry with a different frame if not.

This is honest programmer-marketing. It looks like what it is: a small free web game.
For free browser games on itch, that's within genre norms — the floor is "not broken,
not blank, title readable," and this clears it.

### The upgrade paths (don't build yet, name them)

- **Human-in-loop (recommended, near-zero cost):** cover approval happens inside the
  same 5-minute itch page-setup step the human already owes. The store_page task
  generates 3 cover candidates instead of 1; human picks/rejects. Rejection with a
  note becomes a retry with feedback — the progressive-refinement pattern applied to
  marketing.
- **Image-generation model:** a local or API image model producing key art from the
  game's palette + description. Real quality ceiling, but a new dependency class and a
  new failure class (off-model art that misrepresents the game reads worse than an
  honest screenshot). ROADMAP-NON-GOALS material with tripwire: "a published game shows
  traction and its cover is measurably the bottleneck (itch page views high, plays low)."

### Minimum viable metadata to not look like a bot submission

The tells for bot submissions on itch are: no screenshots, one-line generic description,
default theme, zero tags, broken build. The floor that clears human-made-small-game:

- Cover per above (title legible, actual gameplay).
- 3–5 real gameplay screenshots (not the title screen ×5 — vision-rank for variety of
  scenes).
- Description: 2–3 short paragraphs — what you do, one distinctive mechanic, controls
  list, honest known-issues line from QA_REPORT. Written from playing the game, not from
  the design doc. No superlatives; LLM marketing voice ("Embark on an unforgettable
  journey!") is itself a bot tell, and the store_page prompt must say so explicitly.
- 5–8 accurate tags including engine tag (godot) and genre.
- A short devlog post on publish ("v2026.07.1 — initial release, built by an autonomous
  agent pipeline") — which, honestly, is the interesting story and worth telling
  truthfully. Radical transparency here is both ethical and differentiating: these
  *are* AI-built games and the itch community will figure that out; being upfront in the
  description ("an experiment in autonomous game development — report bugs, a robot will
  fix them") converts the liability into the hook.

---

## 7. Build spec

### New task types

| Type | Executor | Prompt | Purpose |
|------|----------|--------|---------|
| `export_setup` | LLM | feature-family, new `export_setup.yaml` | One-time: write export_presets.cfg (web, thread_support=false), fix export blockers, commit |
| `export` | **system** | none | Headless export + artifact checks + Playwright smoke + tag + manifest |
| `store_page` | LLM | art_pass-family, new `store_page.yaml` | STORE_PAGE.md + cover + screenshots into `store/` |
| `publish` | **system** | none | butler push / selfhost rsync; `metadata.channel` selects target |
| ship gate | existing `phase_gate` | — | `metadata.gate_kind: "ship"`; human-released (Phase 1) |

Explicitly **not** a task type: `post_publish_monitor`. Watching live games is a
scheduler concern, not a unit of work with a completion state. It follows the
meta-agent route-module pattern:

- `swarm/api_livewatch.py` (~250 lines, matching `api_cartographer.py`'s shape):
  interval loop over `releases WHERE status IN ('pushed','live')` → URL 200 check,
  itch comment scrape diff, telemetry error-signature summary → flips
  `pushed→live`, files triage tasks, sets `live_url_ok` gate input.
- The **monitor thread knows a game needs watching** via `projects.live_release_id IS
  NOT NULL` — one indexed column, no config list to maintain. Dev games have it null
  and cost nothing.

The system-executor change: `fill_slots`/`generate_task_script` branch on
`task.metadata.executor == "system"` → generate a wrapper that runs
`swarm/release_pipeline.py:main(task)` (new module, ~300 lines: export, smoke, butler,
selfhost steps) instead of `agent_runtime.main()`. Everything else — handles, logs,
timeouts, retry, escalation — is untouched and inherited.

### New API endpoints

```
GET  /api/releases                          all releases; ?project= filter
GET  /api/projects/<name>/releases          release history for a project
POST /api/projects/<name>/ship              create the ship chain:
                                            ship_gate → export_setup? → export → store_page → publish(s)
                                            (idempotent; skips export_setup if presets committed)
POST /api/releases/<id>/rollback            re-publish a superseded release
GET  /api/livewatch                         live-watch status per published project
(existing) POST /api/projects/<n>/phase-gates/<id>/release   human ship approval
```

`POST /ship` is the single entry point the Director/dashboard/human all use — it builds
the chain via the batch endpoint with `depends_on` indices, respecting every existing
dep-graph rule (chained to project HEAD, no floating chains).

### Config keys

```jsonc
{
  "publish_enabled": false,              // master gate; nothing exports/publishes when false
  "publish_targets": ["selfhost"],       // add "itch" when butler is set up
  "publish_auto_release": false,         // Phase 3 only: auto-release ship gates
  "builds_dir": "",                      // default <workspace>/_builds
  "godot_export_templates_dir": "",      // default OS-standard path; validated at startup
  "butler_path": "",                     // default: `butler` on PATH
  "itch_user": "",                       // itch.io account for push targets
  "selfhost_dir": "",                    // rsync/copy target for tier-1 hosting
  "selfhost_url_base": "",               // e.g. https://games.example.com — for live checks
  "release_drift_threshold": 25,         // commits ahead of published before closure yellow
  "livewatch_interval_minutes": 60
}
```

Secrets in `.env`, matching the existing pattern: `BUTLER_API_KEY`. Never in config.json.

### Prompt/template changes

- `prompts/export_setup.yaml`, `prompts/store_page.yaml` (+ CLAUDE.md prompt table rows).
- `templates/godot/autoload/state_server.gd` + `test_harness.gd`: `OS.is_debug_build()`
  guard; sync fleet-wide via `sync_templates.py`.
- `templates/godot/.gitignore`: **remove** `export_presets.cfg`; same in the wizard
  scaffold string (`api_wizard.py:1482`).
- `templates/store/fonts/` + `tools/compose_cover.py` (Pillow compositor, used by
  store_page agents so every project doesn't reinvent it).
- `tools/install-export-templates.sh` (one-time environment bootstrap).
- `triage.yaml`: add a section for player-sourced reports (comment scrapes, telemetry
  signatures) — different trust level than internal QA findings.

### DB changes

- `releases` table (§4) via `_evolve_schema()`.
- `projects.live_release_id` column.
- Closure spec: `mode: live` accepted; `gates` accepts `export_ok`, `web_smoke_ok`,
  `store_assets_ok`, `live_url_ok`, `release_current`.

### Build order (each step independently shippable)

1. **StateServer debug guard + template sync** — security fix, zero publish dependency. *(hours)*
2. **Gitignore fix + export templates bootstrap script.** *(hours)*
3. **System-task executor + `export` pipeline + Playwright smoke + `releases` table** —
   after this, every green game can produce a verified playable artifact, which is
   already a meaningful new capability even with zero publishing. *(the core week of work)*
4. **Tier-1 selfhost `publish` + `/ship` endpoint + ship gate wiring** — first
   stranger-reachable URL, fully autonomous. *(days)*
5. **`store_page` task + cover compositor.** *(days)*
6. **butler + itch tier + the documented human step.** *(days)*
7. **Live-watch module + `mode: live` closure epoch + post-publish bug auto-chaining.** *(week)*
8. **Telemetry `client_error` event** — build with telemetry #15, not before it.

Pick one guinea-pig project for steps 3–6 — `void-patrol-bot-proof-run12` is the obvious
candidate (already designated the playthrough-bot-gated baseline). Ship one game
end-to-end manually-supervised before turning any auto-chaining on; the run-experiment
discipline applies to the pipeline itself.

### What this deliberately does not include

Desktop exports, codesigning, Steam, monetization, mobile, multiple itch accounts,
CDN/hosting infrastructure beyond a static directory, and automated page-metadata
upload via unofficial itch endpoints (using undocumented APIs against a community
platform's ToS is a reputational risk the 5-minute human step doesn't justify). Each
gets a tripwire in ROADMAP-NON-GOALS if it isn't there already.

---

## Summary of the honest blockers

| # | Blocker | Severity | Resolution |
|---|---------|----------|-----------|
| 1 | No itch.io metadata/publish API | Structural | Two-tier design; 5-min human step per game, batched with ship approval; selfhost tier is the autonomous path |
| 2 | QA stack (StateServer/TCP) can't see exported builds; StateServer ships an open port | High, fixable | Debug-build guard now; Playwright web-smoke gate (G5) as the exported-build verifier |
| 3 | `export_presets.cfg` gitignored fleet-wide; no project has export config | Medium, fixable | Un-ignore in templates + wizard; `export_setup` task per project |
| 4 | Cover art quality ceiling | Permanent at MVP | Screenshot+title compositor clears the floor; human pick-of-3 during page setup; image-gen deferred with tripwire |
| 5 | Export templates: 1 GB, exact-version-matched, no headless installer | Low | One-time bootstrap script per Godot upgrade; export task fails fast with instructions |
| 6 | Post-publish feedback is blind until telemetry (#15) ships | Medium | Comment scraping as fragile stopgap; build telemetry's `client_error` event with the pipeline, not after |

The through-line: the system already knows when a game is done — `ship` mode exists in
the closure spec, unwired; `ship_candidates()` already ranks the queue; `phase_gate` and
its release endpoint already model human approval. The publishing pipeline is less a new
subsystem than the missing actuator on signals the codebase has been accumulating for
months. Build order step 3 (verified playable artifact) is where "shipped" stops being
a definition and starts being a fact.
