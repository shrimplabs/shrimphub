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

---
## Scheduler Run: $(date -u +"%Y-%m-%dT%H:%M:%SZ") | scheduler-1780196478 (in_progress)

### Agent Utilization
- **Active**: 14/14 agents (100% capacity)
- **Quota**: 78.3% used (11746/15000)
- **Types**: bug(5), harness_qa(4), feature(3), meta_scheduler(1), art_pass(1)
- **Projects**: negative-space(3), signal-cartel(3), spawn-test-proj(2), echoes-of-exile(2), temporal-residue(1), ghost-circuit(1), the-memory-palace(1), swarm-controller(1)

### Task Breakdown
- **In-progress**: 14 | **Pending**: 5 | **Failed**: 11
- **Phantom deps**: 13 (self-referential + completed-task chains)

### Failed Backlog by Project
- temporal-residue: 3 (deep chain, parse errors in time_manager.gd/audio_manager.gd)
- echoes-of-the-unmade: 3
- negative-space: 3 (scene parse errors in crosshair.tscn, pillar_puzzle.tscn)
- signal-cartel: 1
- swarm-controller: 1 (scheduler self-dep phantom)

### Key Findings
1. **Agent capacity FULL** — 14/14 slots occupied. No ceiling increase possible (would worsen quota pressure at 78%).
2. **Deep bug chains** — 4 projects stuck in `bug-bug-...-recovery-...` chains 2-4 deep. These need archaeologist triage, not simple retry.
3. **Phantom deps**: 13 found. Scheduler task scheduler-1780196478 has phantom dep on completed scheduler-1780195021 (self-referential dep pattern from scheduler chain).
4. **Recovery tasks blocking** — `recovery-34cf5144` and `qa-bug-negative-space-9cf4258fa799` both blocked on scene parse errors from deep chain. Clear the chain or mark complete.
5. **QA reruns cycling** — 4 harness_qa tasks cycling (signal-cartel 2x, negative-space 2x, echoes-of-exile 1x). Harness connectivity issues (connection refused on 11050/11118).

### Decisions
- **No ceiling adjustment**: 100% utilization but quota 78% means ceiling is not the bottleneck. Adding more agents would spike quota past 100%.
- **No project pauses**: All projects have active work in progress. Pausing would waste in-progress agent cycles.
- **Recommend**: Archaeologist triage for 4 deep-chain projects (temporal-residue, echoes-of-the-unmade, negative-space, signal-cartel). Recovery chain too long — need root-cause fix, not more retries.
- **Clear scheduler phantom dep**: PATCH scheduler-1780196478 deps to remove completed parent reference.

### Phantom Dep Repair (pass 1)
- scheduler-1780196478: phantom dep on completed scheduler-1780195021 → CLEARED

### Status
- Phantom-blocked: 12 remaining (scheduler self-dep cleared, 12 cross-phantom remain)
- Agents: 14/14 active, 78.3% quota
- Archaeologist RECOMMENDED for 11 failed backlog across 5 projects
