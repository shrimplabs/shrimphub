# Windows Compatibility Findings

Audit date: 2026-05-06

This document tracks the current Windows blockers and the work completed so far.

## Current posture

- `swarm-controller` is **macOS-first**
- core controller/runtime/setup flows can be made Windows-friendly without a major rewrite
- full end-to-end Windows validation still requires a real Windows machine or CI runner

## Main blocker categories

### 1. Hardcoded tool paths

Previously:
- `/opt/homebrew/bin/godot` in validation and closure checks

Status:
- replaced with shared runtime resolution via `swarm/platform.py`
- Godot now prefers:
  - `GODOT_PATH`
  - executables on `PATH`
  - common macOS/Linux/Windows install locations

### 2. Unix-only virtualenv command examples

Previously:
- docs/prompts used `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/pip`

Status:
- public docs and prompts are being shifted toward:
  - create `.venv`
  - activate `.venv`
  - then use `python -m ...`
- this keeps `.venv` as the environment while avoiding `bin/` vs `Scripts/` drift

### 3. macOS-only GUI QA automation

Affected tooling:
- `osascript`
- `System Events`
- `screencapture`

Status:
- these paths now need explicit capability checks
- unsupported non-macOS fallback paths should fail clearly instead of silently assuming macOS
- no promise of Windows GUI QA parity yet

### 4. Process-group handling

Previously:
- subprocess lifecycle relied on `os.setsid` and `os.killpg`

Status:
- shared platform helpers now provide:
  - session/process-group launch kwargs
  - platform-aware process-tree termination

## Files touched by the first portability pass

- `swarm/platform.py`
- `swarm/validation.py`
- `swarm/closure/verification.py`
- `swarm/qa_tools.py`
- `swarm/tools/core.py`
- `swarm/api_agents.py`
- `swarm/mcp_client.py`
- `README.md`
- `docs/new_project_setup.md`
- `docs/release_checklist.md`
- Godot/Python prompt files

## Remaining gaps before calling Windows supported

- run controller setup/startup on a real Windows machine
- verify `.venv\\Scripts\\Activate.ps1` workflow from a clean checkout
- verify Godot discovery through `PATH` and `GODOT_PATH`
- verify closure validation against a real Windows Godot project
- confirm macOS-only QA features degrade cleanly without confusing operators

## Recommendation

Do the code/doc portability pass on macOS first, then perform a short Windows smoke pass covering:

1. install dependencies
2. start API/dashboard
3. create or import one project
4. run one validation path
5. confirm unsupported QA paths fail clearly
