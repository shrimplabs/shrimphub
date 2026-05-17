# Godot Project Templates

Canonical support files for swarm-managed Godot 4 projects. The `project_create` agent copies these into every new project. **Update these files here first**, then sync to existing game projects.

## Files

### `autoload/state_server.gd`

TCP server that gives QA agents a live window into the running game. Listens on port **11009**.

**Register in `project.godot`:**
```ini
[autoload]
StateServer="*res://autoload/state_server.gd"
```

**Implement in your main scene for domain-specific state:**
```gdscript
func get_game_state() -> Dictionary:
    return {
        "score": score,
        "lives": lives,
        # ... whatever your game needs
    }
```

**Supported commands (send JSON over TCP, read newline-terminated response):**

| Command | Response |
|---------|----------|
| `{"command":"state"}` | Full game state snapshot |
| `{"command":"screenshot_b64"}` | `{"image_base64":"<png base64>"}` |
| `{"command":"input","type":"click","x":N,"y":N}` | `{"ok":true}` |
| `{"command":"input","type":"action","action":"ui_accept"}` | `{"ok":true}` |
| `{"command":"press_button","id":"ButtonName"}` | `{"ok":true}` |

**Click injection design:**
Clicks are injected via `Input.parse_input_event` / `viewport.push_input` only — no `DisplayServer.window_move_to_foreground()` or `Window.grab_focus()`. Those focus calls require macOS window focus and silently no-op when the screen is locked, causing QA agents to falsely report buttons as unresponsive. Action/keyboard events are unaffected by screen lock and always work.

---

### `autoload/test_harness.gd`

Automated test harness for `harness_qa` agents. Provides structured game flow navigation (menus, level select, in-game actions) so the agent doesn't rely solely on coordinate-based vision clicks.

Register as an autoload alongside StateServer:
```ini
[autoload]
StateServer="*res://autoload/state_server.gd"
TestHarness="*res://autoload/test_harness.gd"
```

The orchestrator checks for `autoload/test_harness.gd` to decide which QA agent type to spawn: if present → `harness_qa`; otherwise → `qa` (vision-only).

**Two phases** (current implementation):
- **Navigation phase**: before `start_loop()` / `checkpoint()` is called, the harness accepts `goto_scene`, `press_button`, `input_action` commands in `_process()`. The agent uses these to get from the main menu to gameplay.
- **Game phase**: once the game calls `checkpoint()` at a stable state, structured game actions are exchanged.

**Known limitations (future work):**
- `goto_scene` bypasses game initialization that happens during menu flow (save loading, level config, game manager setup). Games that rely on this will crash or misbehave if jumped to directly.
- The harness has no way to know when a scene transition is complete and the next input is safe to send.
- Proper solution: games should implement a `test_entry_point()` convention — a function called by the harness when enabled, which performs all required setup and calls `checkpoint()` once the game is in a testable state. This requires deliberate per-game wiring and a defined protocol.

---

### `check_scripts.gd`

Headless GDScript validator. Run after every agent change to catch parse/type errors:

```bash
godot --headless --path /path/to/project --script res://check_scripts.gd --quit 2>&1
```

The swarm's post-task validation step runs this automatically. Any `ERROR:` or `SCRIPT ERROR:` in the output triggers an auto-spawned bug task.

---

### `icon.svg`

Portable default Godot project icon copied into new projects as `res://icon.svg`.
Generated `project.godot` files reference this path through `config/icon`, so
the template must keep this file present.

---

### `addons/gut/`

GUT (Godot Unit Testing) framework. This repo does not vendor the addon tree.
`project_create` installs a pinned GUT version into the local controller cache
and then copies it into new projects. Tests live in `tests/` and are run headlessly:

```bash
godot --headless --path /path/to/project --script res://addons/gut/gut_cmdln.gd -gdir=res://tests -gexit 2>&1
```

**Test files are read-only for bug agents.** Agents must fix game code to make tests pass — never modify the tests themselves.

---

### `test/unit/`

Example GUT test structure for reference when creating new test files.

---

## Syncing fixes to existing projects

When you fix a template file, apply the same fix to all existing game projects:

```bash
# Find projects that still have the old pattern
grep -rl "window_move_to_foreground" ~/path/to/workspace --include="*.gd"

# Copy the fixed template to each project
cp templates/godot/autoload/state_server.gd /path/to/project/autoload/state_server.gd
cd /path/to/project && git add autoload/state_server.gd && git commit -m "Sync StateServer from template"
```
