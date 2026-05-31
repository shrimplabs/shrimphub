# Scheduler Log

## scheduler-1780195021 -- 2026-05-30 23:05 UTC

### Agent Distribution
- 11 active / 11 total agents
- Meta mode active (auto_mode_enabled=true, auto_scale=true)
- meta_scheduler: 1 active
- harness_qa: 4 active (echoes-of-exile x1, negative-space x1, signal-cartel x2)
- bug: 3 active (echoes-of-exile x1, signal-cartel x1, negative-space x1)
- art_pass: 1 active (ghost-circuit)
- feature: 1 active (the-memory-palace)

### Quota
- 75.8% used, 24.2% remaining -- NO ceiling/throttle change needed
- 10864 tasks completed, 1625 failed (13% historical fail rate)

### Task Breakdown
- In-Progress: 11 (well-distributed across 6 projects)
- Pending: 8 (blocked by 1 phantom, cleared -- now unblocked)
- Failed (zombie): 10 (5 projects)

### Phantom Deps
- 1 phantom cleared in pass 1 (bug-bug-recovery-75d63d01)
- No further phantom-blocked tasks

### Decision
- NO ceiling change (11 active, meta mode allows override, 75.8% quota fine)
- NO throttle change needed (24.2% headroom)
- **Archaeologist recommended** for 10-failed backlog (temporal-residue 3, echoes-of-the-unmade 3, negative-space 2, signal-cartel 1, swarm-controller 1)
- 8 pending tasks now unblocked after phantom clear -- scheduler will spawn agents naturally

### Failed Backlog (needs archaeologist)
- temporal-residue: bug-bug-bug-recovery-b91fa077, bug-bug-bug-recovery-9bde3a3d, bug-bug-bug-recovery-15bc32f5
- echoes-of-the-unmade: bug-bug-bug-recovery-bede5a7e, recovery-3a43f166, recovery-31ac69af
- negative-space: bug-bug-bug-pol-auto-negative-space-1780187671, bug-bug-bug-recovery-77cb87be
- signal-cartel: qa-signal-cartel-rerun-d130511d4efe
- swarm-controller: archaeologist-stone-garden-1780186394

### Next Run
- Monitor phantom dep regeneration (passes may be needed)
- archaeologist-1780195616 should be spawned to triage failed backlog
