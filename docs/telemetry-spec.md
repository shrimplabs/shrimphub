# Telemetry System Spec

## Goal

Close the feedback loop: games ship, players play, the swarm learns what works.
Without this, the system optimizes for "passes QA" not "players enjoy it."

## Privacy Design

All data is anonymous by default. No consent gate required because no personal
data is collected — the GDPR test is identifiability, not data type.

**Guarantees:**
- Ephemeral session ID: random UUID generated at game launch, discarded on close,
  never written to disk on the device, never linked to a user account
- No IP logging: telemetry endpoint strips IP before any write hits SQLite;
  nginx/Flask access logs disabled on `/api/telemetry/*` routes specifically
- No device fingerprinting: no hardware ID, OS version, screen resolution, or
  any field that could be combined to identify a device
- No third-party processors: self-hosted endpoint only, no analytics SaaS
- Opt-out toggle: game options menu exposes "Send anonymous usage data" (default on);
  sets a session flag, no data sent if off. No consent UI required — opt-out
  is a good-faith UX gesture, not a legal requirement given the above

**What this means legally:**
Aggregated behavioral data from ephemeral sessions with no IP and no persistent
identifier is not personal data under GDPR Article 4(1). No DPA, no consent
mechanism, no retention limits required. The opt-out toggle is belt-and-suspenders.

---

## Data Model

### Tier 1 — Scene & outcome events (always collected)

Emitted at discrete moments. Lightweight, no sampling.

```json
{
  "session_id": "a7f3...",       // ephemeral UUID, discarded at session end
  "game": "raccoon-city",
  "platform": "macos",           // "macos" | "windows" | "linux" | "web"
  "event": "scene_entered",      // see event types below
  "scene": "Level_2",
  "t": 142.3,                    // seconds since session start (float)
  "data": {}                     // event-specific payload (see below)
}
```

**Event types:**

| Event | `data` payload | Notes |
|-------|---------------|-------|
| `session_start` | `{"game_version": "1.0.3"}` | First event, starts the clock |
| `scene_entered` | `{"from_scene": "MainMenu"}` | Every scene change |
| `scene_exited` | `{"duration_s": 34.2}` | Paired with scene_entered |
| `player_died` | `{"cause": "enemy", "scene": "Level_2"}` | Cause is a game-defined string |
| `player_retry` | `{"attempt": 3}` | Retry after death |
| `level_complete` | `{"level": "Level_2", "duration_s": 87.1, "attempts": 2}` | |
| `victory` | `{"total_duration_s": 412.0, "deaths": 7}` | Game completed |
| `quit` | `{"scene": "Level_3", "playtime_s": 210.5}` | Window closed / quit to menu |
| `custom` | `{"key": "...", "value": ...}` | Game-specific events |

### Tier 2 — Positional & input stream (opt-in per game, same session ID)

Sampled continuously. Higher volume, richer signal.

```json
{
  "session_id": "a7f3...",
  "game": "raccoon-city",
  "event": "position_sample",
  "scene": "Level_2",
  "t": 142.3,
  "data": {
    "x": 384,
    "y": 210,
    "action": "jump"             // last input action in this frame, optional
  }
}
```

**Additional Tier 2 events:**

| Event | `data` payload | Notes |
|-------|---------------|-------|
| `position_sample` | `{"x", "y", "action?"}` | Every 2s by default, configurable |
| `input_action` | `{"action": "attack", "held_ms": 120}` | Every discrete input |
| `camera_focus` | `{"x", "y", "zoom"}` | For games with camera control |

Tier 2 is enabled per-game via a flag in the autoload config, not per-player.
When enabled, the same no-IP, ephemeral-ID guarantees apply.

---

## Godot Autoload: `analytics_reporter.gd`

Lives at `autoload/analytics_reporter.gd` in the template alongside `state_server.gd`.
Registered in `project.godot` as `AnalyticsReporter`.

### Design principles

- **Passive**: listens to signals already emitted by conventional Godot patterns;
  zero changes to existing game logic required
- **Fire-and-forget**: HTTP POST via `HTTPRequest` node, non-blocking, failures
  silently dropped (no retry, no queue drain on quit)
- **Session-local**: session ID generated in `_ready()`, stored only in memory
- **Opt-out aware**: checks a `user://telemetry_optout` flag; if present, no-ops silently

### Configuration (top of file)

```gdscript
const TELEMETRY_URL = "http://telemetry.example.com/api/telemetry"
const TIER2_ENABLED = false          # set true per-game to enable position sampling
const POSITION_SAMPLE_INTERVAL = 2.0 # seconds between position samples
const GAME_NAME = "raccoon-city"     # set by project_create scaffold
```

### API surface (for game code to call)

```gdscript
AnalyticsReporter.scene_entered(scene_name: String, from_scene: String = "")
AnalyticsReporter.scene_exited(scene_name: String, duration_s: float)
AnalyticsReporter.player_died(cause: String = "unknown")
AnalyticsReporter.player_retry(attempt: int)
AnalyticsReporter.level_complete(level: String, duration_s: float, attempts: int)
AnalyticsReporter.victory(total_duration_s: float, deaths: int)
AnalyticsReporter.quit_game(scene: String, playtime_s: float)
AnalyticsReporter.custom_event(key: String, value)
```

### Auto-wiring (zero game-code changes for Tier 1)

`AnalyticsReporter` connects to the SceneTree's `tree_changed` signal and tracks
`get_tree().current_scene` to detect scene transitions automatically. Games that
emit the standard signals get Tier 1 coverage with no manual calls.

Games can optionally call the API directly for richer cause strings on death,
level labels, etc.

---

## Swarm API

### Ingest endpoint

```
POST /api/telemetry
```

Body: single event object or array of events (batched flush on quit).

**IP scrubbing**: the route handler reads `request.remote_addr` and immediately
discards it — it is never passed to any storage layer. Flask/werkzeug access
logging is disabled for this blueprint via `app.logger` filter.

**Rate limiting**: 1000 events/session/minute (drop silently above threshold).
Protects against malformed clients without returning errors that would surface in
game logs.

**Storage**: `data/telemetry.db` — separate SQLite from `swarm.db` to avoid
write contention. WAL mode. Schema:

```sql
CREATE TABLE events (
  id        INTEGER PRIMARY KEY,
  game      TEXT NOT NULL,
  session   TEXT NOT NULL,   -- ephemeral, not a user ID
  platform  TEXT,
  event     TEXT NOT NULL,
  scene     TEXT,
  t         REAL,            -- seconds since session start
  data      TEXT,            -- JSON blob
  received  INTEGER NOT NULL -- unix timestamp (server clock)
);

CREATE INDEX idx_events_game ON events(game);
CREATE INDEX idx_events_session ON events(session);
CREATE INDEX idx_events_event ON events(event, game);
```

### Query endpoints

```
GET /api/telemetry/<game>/summary
```

Returns aggregate stats for the swarm analytics layer:

```json
{
  "game": "raccoon-city",
  "sessions_total": 142,
  "sessions_last_7d": 38,
  "median_playtime_s": 312,
  "victory_rate": 0.31,
  "median_deaths": 4.2,
  "top_quit_scenes": [
    {"scene": "Level_3", "quit_count": 29, "pct": 0.20}
  ],
  "top_death_causes": [
    {"cause": "enemy", "count": 412}
  ],
  "funnel": [
    {"scene": "MainMenu",  "entered": 142, "exited": 142},
    {"scene": "Level_1",   "entered": 138, "exited": 121},
    {"scene": "Level_2",   "entered": 119, "exited":  87},
    {"scene": "Level_3",   "entered":  87, "exited":  44}
  ]
}
```

```
GET /api/telemetry/<game>/heatmap?scene=Level_2
```

Returns Tier 2 position samples aggregated into a grid (bucketed to 32px cells,
min 5 samples per cell to appear) — safe to render in dashboard without exposing
raw session data.

```
GET /api/telemetry/<game>/sessions?limit=100
```

Returns per-session summaries (no raw event stream exposed via API):
playtime, deaths, victory, quit scene. Session ID is included but it's
ephemeral and not linked to any user.

### Dashboard panel

New "Telemetry" tab in the dashboard (per-project view):
- Session count (7d / 30d / all time)
- Victory rate + median playtime
- Scene funnel (drop-off waterfall)
- Top quit points + top death causes
- Heatmap viewer (Tier 2, shown if data exists)

---

## Feed into Swarm Prompts

### project_plan.yaml

Add a `## Player Data` section injected when telemetry summary is available:

```
## Player Data (last 30 days)
- Sessions: 142 | Victory rate: 31% | Median playtime: 5m12s
- Top quit point: Level_3 (20% of sessions quit here)
- Top death cause: enemy (412 deaths)

Prioritize tasks that address the Level_3 drop-off and enemy difficulty.
```

### bug.yaml / feature.yaml

When a bug or feature task is created for a game with telemetry data, the task
description generation can optionally append the quit-point and death-cause
summary so agents have player context without needing to query the API themselves.

---

## Build Order

1. **`templates/godot/autoload/analytics_reporter.gd`** — the Godot autoload
2. **`swarm/api_telemetry.py`** — Flask blueprint (ingest + query endpoints)
3. **`swarm/telemetry_db.py`** — SQLite layer for `telemetry.db`
4. **Register blueprint** in `swarm/api.py`
5. **Dashboard panel** in `dashboard.html` / `dashboard.js`
6. **Prompt injection** in `swarm/api_chat.py` `_build_state_snapshot()` and task description builder
7. **project_plan.yaml** telemetry context section
8. **`sync_templates.py`** — add `analytics_reporter.gd` to the sync list

Each step is independent enough to be a separate swarm task. Steps 1-4 are the
MVP (data flowing); 5-7 are the feedback loop closing.

---

## What We Don't Build (yet)

- Consent UI — not legally required given the privacy design above
- Data export / right-to-erasure flow — not needed, no personal data
- Real-time streaming — batch flush on quit is sufficient for planning signals
- Cross-game aggregate analytics — interesting but not the first goal
- A/B feature flags driven by telemetry — that's post-#9 territory
