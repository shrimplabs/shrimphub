# spawn-test-proj Closure Contract

## Summary

- source: heuristic
- profile: python
- mode: build

## Boot

- ready_check.type: `command`
- ready_check.command: `cd spawn-test-proj && python3 service.py 18080 127.0.0.1 &
sleep 2
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18080/health`

## Verification

- smoke_checks:
  - `service-smoke` (command)
  - service-smoke.command: `curl -s http://127.0.0.1:18080/ping`

## Critical Flows

- `main-flow`: Start the service and exercise the primary request path.

## Gates

- boot_ok: `True`
- tests_ok: `True`
- critical_flow_count: `1`
- max_open_regressions: `0`

## Autonomy

- repair_budget: `8`
- stall_threshold: `3`
- feature_freeze_on_red: `True`

## Assumptions

- none
