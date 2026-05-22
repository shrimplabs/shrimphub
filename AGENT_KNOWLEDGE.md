# Agent Knowledge

Current, high-signal notes for agent work in this repository.

## Canonical Runtime Path

1. `swarm/api.py` bootstraps config, SQLite, route modules, and the monitor thread.
2. `swarm/orchestrator.py` decides what task can run next.
3. `swarm/agent_lifecycle.py` spawns and reaps subprocess agents.
4. `swarm_runner.py` generates the thin wrapper script for each task.
5. `swarm/agent_runtime.py` runs the LLM/tool loop inside the subprocess.
6. `swarm/validation.py` and `swarm/closure/*` decide whether finished work is actually healthy.

## Canonical State Ownership

- Live task graph: `swarm/db.py` `tasks` table
- Project head / branch continuity: `swarm/task_chains.py` and `swarm/maintenance/project_heads.py`
- Runtime agent records: `swarm/db.py` `agents` table
- Live process handles: `swarm/agent_lifecycle.py`
- Verification and regressions: `swarm/db.py` `verification_runs` and `regressions`

See:
- `docs/controller_module_boundaries.md`
- `docs/controller_state_ownership_map.md`

## Current Defaults That Matter

- Scheduler default strategy: `priority`
- Agent loop limit: `200`
- Auto-QA threshold: `8` completed tasks
- Auto-audit threshold: `20` completed tasks
- QA requeue cap: `3`

## Documentation Authority

- Human-facing overview: `README.md`
- OSS work items: `docs/open_source_checklist.md`
- Release hygiene: `docs/release_checklist.md`
- Agent helper docs: `docs/agent-ops/`

If implementation changes any of the defaults or flows above, update the matching docs in the same change.

---
swarm/tools/shell.py _safe_cwd(): _project_root() returns str, not Path — must call Path(root).is_dir() not root.is_dir(). _core._safe_cwd() does not exist in core.py — removed dead call. Both bugs were introduced during tools-split refactor.

---
ARCH GOTCHA: never do `import swarm.tools.core as _core` inside a function in swarm/tools/shell.py, or any other file that core.py re-exports from. core.py re-exports from shell.py at module level — any call-time import of core inside shell creates a module-load circular import. If shell.py needs config vars (WORKSPACE, etc.), import them from swarm.tools._shared instead (same source as _project_root). _shared is safe because neither core nor shell imports _shared at module level — they only reference the functions it defines.
