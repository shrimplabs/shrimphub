# New Godot Project Setup — Swarm Compatibility Checklist

Everything a new Godot project needs before the swarm can build, validate, and QA it correctly.
Missing any of these will cause systematic failures (validation always fails, QA can't run, auto-replan never fires).

For projects created through the swarm controller's new-project flow, the
canonical bootstrap files below are installed automatically. This guide is most
useful when:

- importing an existing Godot repo into the controller
- repairing a project whose bootstrap files were removed or drifted
- creating a project manually outside the dashboard/chat flow

For imported managed Godot projects, the controller may also auto-create a
`setup-gut-<project>` task if `addons/gut/` is missing. Treat that as a
recovery convenience, not a substitute for understanding the required bootstrap
state below.

## 1. `autoload/state_server.gd`

Copy from `templates/godot/autoload/state_server.gd`.

Provides a TCP endpoint (port 11009) the QA agent uses to read live game state, inject input, and take screenshots. Register it in `project.godot`:

```
[autoload]
StateServer="res://autoload/state_server.gd"
```

The QA agent will fail silently or fall back to screenshot-only mode without this.

## 2. `check_scripts.gd` (project root)

Copy from `templates/godot/check_scripts.gd`.

The orchestrator runs this after every task completion to validate the codebase:

```bash
godot --headless --path <project_path> --script res://check_scripts.gd --quit
```

It scans all `.gd` files for parse/load errors, excluding `addons/`, `test/`, and `tests/`. Exit code 1 = validation failed → bug task spawned.

## 3. `addons/gut/` — GUT addon (cached external dependency)

**Must be committed inside each game project. Do NOT gitignore it there.**

GUT is run as part of post-task validation for all task types. If the addon is missing or the folder is empty, every single validation will fail with:
```
ERROR: Failed loading resource: res://addons/gut/gut_cmdln.gd
```
This will cause every completed task to immediately spawn a bug task, creating an infinite failure loop.

Source: https://github.com/bitwes/Gut (pinned to v9.6.0, MIT license)

The swarm-controller repo does not vendor GUT. Instead, project bootstrap uses a
local cache:

- first use downloads the pinned GUT version into the controller cache
- later projects copy from that cache
- tests can override the source via `SWARM_GUT_SOURCE_DIR`
- on Windows, ensure `godot` is on `PATH` or set `GODOT_PATH`

Enable in `project.godot` under `[editor_plugins]`:
```
[editor_plugins]
enabled=PackedStringArray("res://addons/gut/plugin.cfg")
```

## 4. `test/` directory with at least one GUT test file

The validator looks for GUT test directories (`test/` or `tests/`). An empty test suite passes cleanly. A missing suite causes GUT to error.

Minimum viable test file (`test/unit/test_placeholder.gd`):
```gdscript
extends GutTest

func test_placeholder():
    pass
```

## 5. `GAME_DESIGN.md` (project root)

Required by:
- **QA agent** — reads this to derive a test plan and know what to verify
- **Auto-replan** — planner reads this to generate a full dependency-ordered task set when the queue empties
- **Art pass agent** — reads this for visual direction and asset requirements

Should describe: core gameplay loop, scenes, systems, visual style, win/lose conditions.

## 6. `project.godot` checklist

- `StateServer` registered as autoload (see §1)
- GUT plugin enabled (see §3)
- Main scene set (`run/main_scene`)

## 7. Swarm controller config (`config.json`)

```json
{
  "managed_projects": ["your-project-name"],
  "workspace": "/path/to/projects/parent"
}
```

The project directory must be at `<workspace>/<project-name>` and must contain `project.godot`.

## Template files

All boilerplate is pre-staged in `swarm-controller/templates/godot/`:

```
templates/godot/
  addons/gut/              — not bundled here; installed from the local cache
  autoload/state_server.gd — TCP state endpoint for QA agent
  check_scripts.gd         — post-task validation script
  icon.svg                 — portable default project icon
  test/unit/test_placeholder.gd — minimal passing GUT test
```

To bootstrap an existing/manual project:
```bash
cp -f swarm-controller/templates/godot/check_scripts.gd <project>/
cp -f swarm-controller/templates/godot/icon.svg <project>/
cp -f swarm-controller/templates/godot/autoload/state_server.gd <project>/autoload/
cp -rf swarm-controller/templates/godot/test <project>/
cd /path/to/swarm-controller
python - <<'PY'
from pathlib import Path
from swarm.godot_bootstrap import install_gut_into_project
install_gut_into_project(Path("/path/to/project"))
PY
```

Then register `StateServer` as an autoload and enable the GUT plugin in `project.godot`.

## Windows notes

- Use `python -m venv .venv` and activate with `.venv\\Scripts\\Activate.ps1`
- Put the Godot executable on `PATH` or set `GODOT_PATH`
- The controller can bootstrap imported Godot projects on Windows, but macOS-specific QA fallbacks such as `osascript` and `screencapture` are not available there

## Quick copy checklist

```
[ ] autoload/state_server.gd  — copied + registered in project.godot
[ ] check_scripts.gd          — copied to project root
[ ] addons/gut/               — full addon committed (not gitignored)
[ ] test/unit/test_placeholder.gd  — at minimum a passing stub
[ ] GAME_DESIGN.md            — core design doc in project root
[ ] project.godot             — StateServer autoload + GUT plugin enabled + main scene set
[ ] config.json               — project in managed_projects
```
