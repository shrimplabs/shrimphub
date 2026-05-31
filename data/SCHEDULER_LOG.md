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

---
## scheduler-1780197379 -- 2026-05-31 00:00 UTC

### Agent Distribution
- 13/13 agents active (100% capacity)
- meta_scheduler: 1 | bug: 6 | feature: 3 | art_pass: 1 | harness_qa: 1
- Projects: negative-space(3), echoes-of-exile(2), spawn-test-proj(2), signal-cartel(1), temporal-residue(1), the-memory-palace(1), ghost-circuit(1), swarm-controller(1)

### Quota
- **83.6% used, 16.4% remaining** -- NO ceiling/throttle change needed
- 10878 tasks completed, 1626 failed (13% historical fail rate)

### Task Breakdown
- In-Progress: 12 | Pending: 4 (unblocked) | Failed: 11
- **Phantom deps: 0** -- clean

### Decision
- **No ceiling change** — 13 active is at capacity, but quota at 83.6% has headroom. Adding more agents would spike quota past threshold.
- **No throttle** — 16.4% headroom, pending tasks will naturally drain.
- **No project pauses** — all projects have active in-progress agents.
- **4 pending unblocked** — scheduler will spawn naturally as agents complete.
- **Archaeologist RECOMMENDED** for 11 failed backlog across 5 projects (temporal-residue 3, echoes-of-the-unmade 3, negative-space 3, signal-cartel 1, swarm-controller 1). Deep recovery chains need root-cause triage, not simple retry.

### Failed Backlog
- temporal-residue: 3 (deep bug-bug-... chains)
- echoes-of-the-unmade: 3
- negative-space: 3 (scene parse errors)
- signal-cartel: 1
- swarm-controller: 1

### Next Run
- System healthy: 0 phantom-blocked, 4 pending ready to fill
- Monitor quota threshold as agents complete and new ones spawn
### Scheduler Run $(date)
- 11/11 agents active, all progressing (loops 3-151)
- Quota: 86.4% used, 13.6% remaining — NO ceiling/throttle change needed
- Phantom deps: 0, 0 phantom-blocked
- In-Progress: 11 | Pending: 5 (legitimately blocked on in-progress deps) | Failed: 1 (archived)
- No agent kills, no project pauses needed
- Pending task deps verified: all dep targets are in-progress (not completed/stuck)
- Archaeologist RECOMMENDED for 1 archived failed (bug-bug-bug-pol-auto-negative-space-1780187671, negative-space)

### Scheduler Run $(date)
- **Time**: $(date)
- **Agents**: 3/3 active (loop=None display lag, all running based on log output)
- **Quota**: 11.3% used, 88.7% remaining — NO ceiling/throttle change needed
- **Phantom deps**: 4 repaired (auto by scheduler_check.py), 0 remaining, 0 phantom-blocked
- **In-Progress**: 3 | **Pending**: 10 | **Failed**: 3

### Actions Taken
1. **Zombie agent recovery**: 12 zombie agents (loop=None) detected — all had spawned subprocesses but orchestrator's `_active_handles` was empty. Monitor auto-cleaned. Restarted 4 agents via `/api/spawn`: task-c0bbf0d018fb (the-memory-palace), bug-qa-bug-negative-space-53cee9473b7b (negative-space), qa-bug-negative-space-9cf4258fa799 (negative-space), task-ae8616647f06 (ghost-circuit).
2. **Phantom dep repair**: 4 phantom deps auto-cleared by scheduler_check.py:
   - bug-bug-bug-recovery-75d63d01 (phantom dep)
   - bug-bug-qa-bug-negative-space-53cee9473b7b (phantom dep)
   - pol-auto-echoes-of-the-unmade-1780203746 (phantom dep)
   - scheduler-1780204581 (phantom dep on completed scheduler)
3. **Stale orchestrator state**: orchestrator.get_active_count() showed 4-12 stale count but _active_handles was empty. Used `/api/spawn` direct spawn to bypass monitor's fill_slots (which was blocked by stale count). Monitor now correctly shows 3.
4. **Agent loop=None display**: The `loop` field in API responses shows None even for actively-running agents. This is a display/refresh lag — agent logs show loops 1-6+ across all 3 active agents. NOT zombies.

### Failed Backlog
- **bug-bug-bug-pol-auto-negative-space-1780187671**: archived, scene parse errors (phantom dep, cleared)
- **qa-signal-cartel-rerun-a61ff5f8763b**: needs archaeologist triage
- **scheduler-1780198279**: blocked on phantom dep (cleared), needs re-run

### No Changes Made
- **max_active_agents ceiling**: 8 (current max 8, only 3 active) — NO change needed
- **Project pauses**: NONE — projects are healthy
- **run_after**: NONE needed

### Archaeologist RECOMMENDED for 2 failed tasks
- qa-signal-cartel-rerun-a61ff5f8763b: signal-cartel wall collision fix was committed but QA rerun still failing
- scheduler-1780198279: phantom dep cleared, scheduler needs manual restart or monitor re-trigger

