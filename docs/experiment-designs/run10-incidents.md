# Run 10 Incidents & Observations

Experiment: `void-patrol-pipeline-ab-run10-20260618`

Arms:
- `void-patrol-variant-f-tail-run10`
- `void-patrol-variant-f-tail-quality-run10`
- `void-patrol-variant-f-tail-quality-parallel-run10`
- `void-patrol-adaptive-flat-run10`

---

## Incident: `_is_infrastructure_failure` false positives caused infinite retry loops

**Date:** 2026-06-20  
**Affected arms:** quality-run10, quality-parallel-run10  
**Unaffected arms:** variant-f-tail-run10 (completed clean), adaptive-flat-run10 (completed clean)

### What happened

Tasks in the quality arms accumulated extremely high attempt counts (att=23, att=39, att=47) and high infrastructure-failure counts (ifc=40–74) while never exhausting `max_attempts=3` to spawn research feeders or surface the real error.

Root cause: `_is_infrastructure_failure()` in `swarm/agent_recovery.py` scanned the **entire** agent log for provider-error markers (`"all backends failed"`, `"gateway error 502"`, etc.). When an agent hit a transient 502 mid-run — which the agent's retry logic recovered from — and then failed later for a real reason (Godot parse errors, validation failures), the full log contained both signals. The infra check found the 502 line and returned `True`, resetting the task to pending without consuming an attempt.

The quality arms were hit harder because they run longer pipelines (plan → scout → work → validate → repair phases), increasing the probability of encountering at least one transient 502 during a run.

### Fix

Rewrote `_is_infrastructure_failure()` with a 3-stage check (committed 2026-06-20):

1. **Authoritative:** `failure_kind=infrastructure_exception` in output → always infra
2. **Real-failure veto:** Pipeline-completion or validation-failure markers anywhere in output (`[pipeline] done. failed`, `script error:`, `validation failed`, etc.) → always NOT infra, regardless of earlier 502s
3. **Tail-only check:** Provider error markers only count when present in the last 2000 chars of output — meaning the run actually terminated on a provider error

Conservative by design: when ambiguous, return False (consume the attempt). Looping forever is worse than burning one retry.

All 101 tests passed after the fix.

### Recovery

15 tasks across dragon-mmo, project-bastion, quality-run10, and quality-parallel-run10 were manually reset to `pending` with `attempts=0` after the fix was deployed. The `ifc` counter was also cleared to avoid confusing future diagnostics.

### Impact on run-10 results

- `variant-f-tail-run10` and `adaptive-flat-run10`: **not affected**, completed cleanly before the bug accumulated enough failures to be visible
- `quality-run10`: 2 tasks stuck (boss fight feature att=39, power-ups feature att=23) — these were the primary quality-arm bottlenecks. Results for this arm should be interpreted with the caveat that these tasks ran many more iterations than intended before the real failure was surfaced.
- `quality-parallel-run10`: 1 task stuck (att=3, borderline)

The stuck tasks' actual failures were Godot validation errors (parse errors, type resolution failures) unrelated to the experiment design — real implementation bugs that would have been resolved normally via research feeders if the infra check had worked correctly.
