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
| Agent tools | `swarm/qa_tools.py` | — | Implemented |
| `qa` prompt | `prompts/qa.yaml` | — | Implemented |
| `harness_qa` prompt | `prompts/harness_qa.yaml` | — | Implemented |

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
