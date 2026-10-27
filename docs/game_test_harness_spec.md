# Game Test Harness — Protocol Specification

Generic protocol for synchronous AI-driven testing of real-time Godot games.
Games implement the checkpoint interface; the swarm uses it uniformly.

Two complementary servers run in the game process:

- **Port 11009 — StateServer**: continuous polling, screenshots, input injection
- **Port 11010 — TestHarness**: synchronous checkpoint handshake for deterministic QA

---

## StateServer (port 11009)

Used by `qa` and `art_pass` agents. Provides live access to the running game
without pausing it.

### Setup

1. Copy `templates/godot/autoload/state_server.gd` into your project — never write from scratch
2. Register it in `project.godot`:
   ```ini
   [autoload]
   StateServer="res://autoload/state_server.gd"
   ```
3. The server starts automatically on port 11009 when the game runs

### Commands (send JSON over TCP, receive JSON response)

| Command | Response |
|---------|----------|
| `{"command":"state"}` | Full scene tree + `game_state` dict |
| `{"command":"screenshot_b64"}` | `{"image_base64":"<png>"}` |
| `{"command":"input","type":"click","x":N,"y":N}` | Inject mouse click |
| `{"command":"input","type":"action","action":"ui_accept"}` | Inject Godot action |
| `{"command":"press_button","id":"start"}` | Fire button by `qa_label` or node name |
| `{"command":"a11y_tree"}` | Flat list of all visible interactive elements |

### `state` response shape

```json
{
  "timestamp": 1712345678.0,
  "scene_tree": {
    "name": "Main", "type": "Node2D", "path": "/root/Main",
    "visible": true, "position": [0, 0],
    "children": [
      {
        "name": "StartButton", "type": "Button", "qa_label": "start",
        "visible": true, "position": [400, 300],
        "bounds": [375, 285, 150, 50]
      }
    ]
  },
  "game_state": {}
}
```

`game_state` is populated only if the root scene implements
`get_game_state() -> Dictionary`. Implement this for domain-specific assertions
(score, lives, level, player state, etc.).

### `a11y_tree` response shape

```json
{
  "a11y_tree": [
    {"role": "button", "label": "Start Game", "path": "/root/Main/StartButton", "bounds": [375, 285, 150, 50], "visible": true},
    {"role": "label",  "label": "Score: 0",   "path": "/root/Main/ScoreLabel",  "bounds": [10, 10, 100, 20],  "visible": true}
  ]
}
```

Roles: `button`, `label`, `input`, `progressbar`, `slider`, `listbox`, `widget`.
Label priority: `qa_label` metadata → `node.text` → `node.name`.

### Tagging nodes for reliable button targeting

```gdscript
# In your scene _ready():
$PlayButton.set_meta("qa_label", "play")
$MenuButton.set_meta("qa_label", "menu")
```

Then agents can call `press_button{"id":"play"}` regardless of internal node name.

### Click injection note

Clicks are injected via `Input.parse_input_event` — **not** via window focus
calls. This works regardless of screen lock. Never add `grab_focus()` or
`window_move_to_foreground()` to the click path.

---

## TestHarness (port 11010)

Used by `harness_qa` agents. Pauses the game at discrete checkpoints and
waits for the agent to send an action before resuming. Fully deterministic —
no vision model needed.

### Setup

1. Copy `templates/godot/autoload/test_harness.gd` into your project
2. Register it in `project.godot`:
   ```ini
   [autoload]
   TestHarness="res://autoload/test_harness.gd"
   ```
3. In your game code, call `TestHarness.checkpoint(state_dict)` at each
   stable, testable state

### Game-side contract

```gdscript
# In your game scene:
func _ready():
    await TestHarness.checkpoint({"scene": "main_menu", "buttons_visible": true})
    # game pauses here until harness_step() is called

func _on_play_pressed():
    await TestHarness.checkpoint({"scene": "gameplay", "score": 0, "lives": 3})
    # pauses again
```

`checkpoint()` blocks until the external controller sends an action. The
state dict is sent to the agent as the checkpoint payload.

### Agent-side tools

| Tool | Description |
|------|-------------|
| `harness_step(action)` | Send action to resume from current checkpoint; returns next checkpoint state |
| `harness_poll_state()` | Poll current game state via StateServer (port 11009) |
| `harness_inject(command)` | Send a StateServer command (click, action, press_button) |
| `launch_game(project_path, args=["--test-harness"])` | Launch game in headless test mode |

### Typical harness_qa flow

```python
# Agent receives task: "verify main menu loads and game starts"
launch_game(project_path, args=["--test-harness"])

# Wait for first checkpoint
state = harness_step({"action": "none"})
# state == {"scene": "main_menu", "buttons_visible": true}
assert state["scene"] == "main_menu"

# Click Play via StateServer
harness_inject({"command": "press_button", "id": "play"})

# Advance to next checkpoint
state = harness_step({"action": "none"})
# state == {"scene": "gameplay", "score": 0, "lives": 3}
assert state["lives"] == 3
```

### Checkpoint protocol (TCP, port 11010)

The TestHarness server listens on port 11010. The handshake per checkpoint:

1. Game calls `checkpoint(state_dict)` → server sends `state_dict` as JSON
2. Controller (agent) receives JSON, inspects state
3. Controller sends JSON action: `{"action": "continue"}` or `{"action": "fail", "reason": "..."}`
4. Game resumes (or marks test failed and halts)

---

## Python client layer

The wire protocol above is consumed through one canonical Python client, not
duplicated per-tool (roadmap #10, converged 2026-09-11 — this used to be a
second, hand-rolled TCP client in `qa_tools.py` plus a duplicated a11y-label
matcher in `playthrough_kit.py`; both now delegate to the same code):

- **`swarm.tools.scenario_qa.StateServerClient`** — the TCP client. Every
  consumer (`qa_tools.py`, `scenario_qa.py`'s own scenario executor,
  `playthrough_kit.py`) either instantiates this directly or delegates to it.
  Methods: `send(command, timeout=None)`, `ping()`, `wait_ready()`,
  `get_state()`, `screenshot_b64()`, `press_button(id)`, `play_macro(actions)`,
  `a11y_tree()`.
- **`swarm.tools.observe_common`** — pure-function helpers with no TCP
  dependency:
  - `screen_to_viewport(x, y, window)` / `viewport_to_screen(vx, vy, window)`
    — OS-screen ↔ game-viewport coordinate conversion, parameterized on a
    window-geometry dict (`{x, y, pixel_ratio}` or `{x, y, viewport_w, w}`).
  - `resolve_image_coords_from_model(raw_x, raw_y, img_w, img_h, provider_name, raw_text)`
    — maps a vision model's reported coordinates into image pixels, handling
    the 0–1000-normalized-vs-raw-pixel ambiguity different providers exhibit.
  - `match_a11y_element(a11y_tree, description, mode, require_visible)` —
    finds the best-matching element in an `a11y_tree` response.
    `mode="scored"` is a weighted match (exact/substring/token-overlap plus a
    role bonus) used by `qa_tools.py`'s LLM-driven `click_element`, which has
    a vision fallback if grounding misses. `mode="substring"` is a simpler
    first-match-wins search used by `playthrough_kit.py`'s deterministic
    bots, which have **no** LLM fallback — do not switch it to `"scored"`
    without re-validating bot behavior, since tightening or loosening the
    match changes what a zero-LLM-in-loop bot can click.

Each caller owns its own *click strategy* on top of a match
(`press_button` by node path vs. clicking the bounds centroid) — only the
*matching* logic is shared.

---

## QA agent types

| Type | Port | Vision model | Deterministic | Use when |
|------|------|-------------|---------------|----------|
| `qa` | 11009 | Yes | No | Free-play exploration, visual validation |
| `harness_qa` | 11009 + 11010 | No | Yes | Scripted flows, regression testing |
| `hybrid_qa` | 11009 + 11010 | Yes | Partial | Both structured checkpoints and visual checks |
| `scenario_qa` | 11009 | No | Yes | Replay compiled JSON scenario files |

---

## Implementation status

| Component | File | Port | Status |
|-----------|------|------|--------|
| StateServer | `templates/godot/autoload/state_server.gd` | 11009 | Implemented |
| TestHarness | `templates/godot/autoload/test_harness.gd` | 11010 | Implemented |
| TCP client | `swarm/tools/scenario_qa.py::StateServerClient` | — | Implemented (canonical) |
| Coordinate/a11y helpers | `swarm/tools/observe_common.py` | — | Implemented (canonical) |
| Agent tools | `swarm/qa_tools.py` | — | Implemented |
| Scenario replayer | `swarm/tools/scenario_qa.py` | — | Implemented |
| Playthrough bots | `swarm/tools/playthrough_kit.py` | — | Implemented |
| `qa` prompt | `prompts/qa.yaml` | — | Implemented |
| `harness_qa` prompt | `prompts/harness_qa.yaml` | — | Implemented |

The `game-harness` research project (separate repo,
`game-harness/`) consumes this
same canonical protocol and client layer directly — its tooling
(`tools/qa_probe.py`, `tools/game_harness.py`) lives in this repo's `tools/`
directory and imports `swarm.qa_tools`. It no longer carries its own
Godot-side StateServer implementation (an early, diverged fork was removed
2026-09-11 once confirmed unused).

---

## Minimum game requirements for QA to work

**Required (any structured state reads):**
- Register `StateServer` autoload in `project.godot`

**Recommended (meaningful state assertions):**
- Implement `get_game_state() -> Dictionary` on the root scene

**Optional (label-based button targeting without coordinates):**
- Tag interactive nodes with `set_meta("qa_label", "...")`

**For `harness_qa`:**
- Register `TestHarness` autoload in `project.godot`
- Call `await TestHarness.checkpoint(state_dict)` at each stable state
