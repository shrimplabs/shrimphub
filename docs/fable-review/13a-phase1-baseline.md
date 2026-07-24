# Phase 1 Baseline — Pre-event-bus latency metrics

Captured 2026-07-24 before enabling `event_bus_enabled`.

## Inter-task gap (same project, agent completes → next agent starts)

Measured from `tasks` table, last 778 samples where gap is 0–300s.

| Percentile | Gap |
|------------|-----|
| p50 | 6.6s |
| p90 | 111.9s |
| mean | 34.2s |
| min | 0.4s |
| max | 297.5s |

**p50 of 6.6s** reflects the 5s sweep cadence (agent exits → sweep detects
on next tick → finish thread starts → fill_slots runs).  The long tail
(p90=112s) is monitor idle-mode (30s cadence) plus slow `_finish_agent`
(Godot validation can take minutes).

## Task turnaround (started → completed)

Last 494 completed tasks:

| Percentile | Duration |
|------------|----------|
| p50 | 693s |
| p90 | 2090s |
| p99 | 6637s |
| mean | 1013s |

## Expected improvement after event bus enabled

With `event_bus_enabled: true`:
- Waiter thread fires immediately on proc exit → `_finish_agent` starts in
  ~milliseconds (was: ≤5s active, ≤30s idle)
- `AGENT_FINISHED` wakes monitor Event → `fill_slots` runs immediately
  after teardown (was: remainder of 5s sleep)
- **Expected p50 inter-task gap: ~1–3s** (teardown time, not sweep cadence)
- p90 stays high (long Godot validation is the bottleneck, not the bus)

## Re-measure after soak

Run the same query after 48h with `event_bus_enabled: true` and compare.
Target: p50 ≤ 3s, mean ≤ 10s.
