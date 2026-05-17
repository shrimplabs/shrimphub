# Game Test Harness — Protocol Specification

Generic protocol for synchronous AI-driven testing of real-time Godot games.
Games implement the checkpoint interface; the swarm uses it uniformly.

## Protocol

- **Port 11009**: `StateServer` — TCP server that gives QA agents a live window into the running game (live state polling)
- **Port 11010**: `TestHarness` — game serves JSON state snapshot when paused at a checkpoint; external controller sends JSON action to resume

## Game-side contract

### StateServer (port 11009)
1. Include `templates/godot/autoload/state_server.gd` in your project
2. Launch with `--test-harness` flag to activate
3. Agents can poll game state by sending `{"command":"state"}` over TCP

### TestHarness (port 11010)
1. Include `templates/godot/autoload/test_harness.gd` in your project
2. At each stable state, call `TestHarness.checkpoint(state_dict)`
3. Launch with `--test-harness` flag to activate

## Swarm-side tools

- `harness_step(action)` — send action, receive next checkpoint state (via port 11010)
- `harness_poll_state()` — get current game state (via port 11009)
- `harness_inject(command)` — send a StateServer command such as click/action/press_button (via port 11009)
- `launch_game(project_path, args=["--test-harness"])` — headless test launch

## Implementation

| Component | File | Port | Status |
|-----------|------|------|--------|
| StateServer | `templates/godot/autoload/state_server.gd` | 11009 | ✅ Implemented |
| TestHarness | `templates/godot/autoload/test_harness.gd` | 11010 | ✅ Implemented |
| Agent tools | `swarm/agent_runtime.py` | — | ✅ Implemented |

## Source Files

- `templates/godot/autoload/state_server.gd` — StateServer autoload
- `templates/godot/autoload/test_harness.gd` — TestHarness autoload
