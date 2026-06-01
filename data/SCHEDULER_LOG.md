# Scheduler Log

## scheduler-1780279316 -- 2026-06-01 14:55 UTC

### Agent Utilization
- **Active**: 17 agents reported, 2 in-progress tasks (loop=None display lag)
- **Quota**: 32.0% used, 68.0% remaining
- **Over limit**: false

### Task Breakdown
- In-Progress: 2 (star-sovereigns x1 bug, echoes-of-the-unmade x1 recovery)
- Pending: 30 (many high-priority: 95, 90, 85)
- Failed (zombie): 7 (echoes-of-the-unmade x3, swarm-controller x3, star-sovereigns x1)

### Decision
- **NO ceiling change** (2 in-progress / 8 ceiling = 25% util, well under 75% threshold)
- **NO throttle change needed** (68% quota remaining, far under 75%)
- **NO project pauses** (all 82 managed projects active)
- **NO run_after assignments** (quota headroom sufficient)
- **Archaeologist recommended** for 7-failed backlog (echoes-of-the-unmade x3, swarm-controller x3, star-sovereigns x1)

### High-Priority Pending Tasks (require agents)
- [95] bug-qa-bug-the-memory-palace-ec5220be148d
- [95] bug-qa-bug-echoes-of-the-unmade-6a8ebe4b5b7c
- [90] bug-bug-qa-bug-ghost-circuit-b6ead162f50a
- [90] recovery-eb4e9000 (echoes-of-the-unmade)
- [90] bug-bug-bug-recovery-33aa2771 (negative-space)
- [90] recovery-653fd42d (temporal-residue)
- [85] integration-the-memory-palace-1780248058

### Phantom Deps
- 2 phantom-blocked cleared by scheduler_check.py (qa-bug-star-sovereigns, qa-star-sovereigns-rerun)
- No further phantom-blocked tasks

### Failed Backlog (needs archaeologist)
- echoes-of-the-unmade: 3 failed (phantom/recovery chains cycling)
- swarm-controller: 3 failed (archaeologist tasks, likely no-ops)
- star-sovereigns: 1 failed (integration or bug)

### Next Run
- Monitor for phantom dep regeneration
- archaeologist should be spawned to triage 7-failed backlog
- Pending queue has 30 tasks with many high-priority items -- scheduler will spawn agents naturally once in-progress tasks complete
