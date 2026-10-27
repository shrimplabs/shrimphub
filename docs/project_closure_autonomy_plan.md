# Project Closure Autonomy Plan

This document defines the implementation plan for turning `swarm-controller`
from an expansion-oriented executor into a two-loop autonomous system:

- `expansion loop`: create and advance new feature/refactor work
- `closure loop`: verify runnable state, generate repairs, prioritize repairs,
  and converge projects toward stable end-to-end flows

The goal is not more human review. The goal is for the controller to own
closure work automatically and only escalate when progress stalls.

## Goals

- Add machine-readable per-project finish contracts.
- Persist verification evidence instead of relying on transient logs.
- Convert failed verification into structured regressions and repair tasks.
- Reprioritize auto mode toward repair when a project is unhealthy.
- Detect repeated failure loops and escalate only when the controller is stuck.
- Support web, Python, and Godot projects with one shared closure model.

## Non-Goals

- Full release-management automation in the first pass.
- Universal test orchestration for every framework.
- Replacing the existing integrity model. Closure state should complement
  dependency/integrity state, not collapse into it.

## Architecture Summary

Add a project-level closure state machine backed by three new durable concepts:

1. `ProjectSpec`
   - machine-readable project finish contract
   - defines boot command, verification commands, smoke checks, gates, and
     autonomy policy

2. `VerificationRun`
   - durable record of checks executed after work
   - stores commands, artifacts, structured results, and normalized failure
     fingerprints

3. `Regression`
   - normalized recurring failure record
   - tracks first/last seen timestamps, occurrence count, linked repair tasks,
     and resolution state

This drives a computed `ClosureStatus` for each project:

- `green`: required gates satisfied
- `yellow`: runnable but incomplete or regressed
- `red`: boot or primary flow broken
- `stalled`: repeated repair loops are not improving the state
- `frozen`: feature work blocked while repairs take priority

## Recommended Module Layout

The current repo already separates integrity, reconciliation, and operator
surfaces. Closure should follow the same pattern instead of scattering logic.

### New canonical modules

- `swarm/closure/specs.py`
  - load, validate, and normalize `ProjectSpec`
  - provide defaults by project profile (`godot`, `python`, `typescript`)

- `swarm/closure/runs.py`
  - create and persist `VerificationRun`
  - normalize result payloads and artifacts

- `swarm/closure/regressions.py`
  - fingerprint failures
  - upsert/open/resolve regressions
  - detect repeated-failure patterns

- `swarm/closure/verification.py`
  - execute the project verification contract
  - route commands and probes by project type

- `swarm/closure/status.py`
  - derive `ClosureStatus` and gate summaries from specs, runs, and regressions

- `swarm/closure/repair_planning.py`
  - convert failed verification into bug/triage/smoke follow-up tasks
  - enforce repair-first policy

### Existing modules to extend

- `swarm/db.py`
  - schema and persistence for project closure state, verification runs,
    regressions

- `swarm/api_projects.py`
  - project-scoped closure endpoints

- `swarm/api_config.py`
  - optional global defaults for closure policy

- `swarm/orchestrator.py`
  - post-task verification trigger
  - scheduler behavior when projects are `yellow`, `red`, `stalled`, `frozen`

- `dashboard.js`
  - closure status summary cards and project-level views

- `README.md`
  - document closure mode and verification-driven auto repair

## Data Model Changes

### `projects` table additions

Add these nullable columns:

- `closure_mode TEXT DEFAULT 'build'`
- `closure_status TEXT DEFAULT 'yellow'`
- `closure_spec TEXT DEFAULT '{}'`
- `last_verification_at TEXT`
- `last_verification_status TEXT`
- `open_regression_count INTEGER DEFAULT 0`
- `stall_count INTEGER DEFAULT 0`

### New `verification_runs` table

Suggested schema:

- `id TEXT PRIMARY KEY`
- `project TEXT NOT NULL`
- `trigger_task_id TEXT`
- `run_type TEXT NOT NULL`
- `status TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- `started_at TEXT`
- `completed_at TEXT`
- `results_json TEXT DEFAULT '{}'`
- `artifacts_json TEXT DEFAULT '{}'`
- `fingerprints_json TEXT DEFAULT '[]'`
- `metadata_json TEXT DEFAULT '{}'`

Indexes:

- `idx_verification_runs_project`
- `idx_verification_runs_created_at`
- `idx_verification_runs_status`

### New `regressions` table

Suggested schema:

- `id TEXT PRIMARY KEY`
- `project TEXT NOT NULL`
- `fingerprint TEXT NOT NULL`
- `status TEXT NOT NULL DEFAULT 'open'`
- `severity TEXT DEFAULT 'medium'`
- `first_seen_at TEXT NOT NULL`
- `last_seen_at TEXT NOT NULL`
- `occurrences INTEGER DEFAULT 1`
- `source_run_id TEXT`
- `linked_task_id TEXT`
- `details_json TEXT DEFAULT '{}'`

Indexes:

- `idx_regressions_project`
- `idx_regressions_project_status`
- `idx_regressions_fingerprint`
- unique constraint on `(project, fingerprint, status='open')` should be
  enforced in application logic if SQLite partial uniqueness is not used

## ProjectSpec Shape

Recommended normalized shape:

```json
{
  "mode": "build",
  "boot": {
    "command": "npm run dev",
    "ready_check": {
      "type": "http",
      "url": "http://127.0.0.1:5173"
    }
  },
  "verification": {
    "unit_test_command": "npm test",
    "integration_test_command": "npm run test:e2e",
    "smoke_checks": [
      {
        "id": "main-flow",
        "type": "command",
        "command": "npm run test:e2e -- --grep main-flow"
      }
    ]
  },
  "critical_flows": [
    {
      "id": "main-flow",
      "description": "generate tool, mount tool, use tool"
    }
  ],
  "gates": {
    "require_boot_ok": true,
    "require_unit_tests_ok": true,
    "required_critical_flows": ["main-flow"],
    "max_open_regressions": 0
  },
  "autonomy": {
    "feature_freeze_on_red": true,
    "repair_budget": 8,
    "stall_threshold": 3
  }
}
```

### Initial project-profile defaults

- `typescript`
  - boot command
  - unit tests
  - optional Playwright smoke

- `python`
  - boot command or import-level smoke
  - pytest suite
  - optional HTTP health probe

- `godot`
  - script validation
  - harness or headless smoke scene
  - log scan and one primary flow smoke

Profile defaults should make the feature usable before every project has a
hand-authored spec.

## VerificationRun Semantics

Every verification run should produce structured booleans and evidence, not just
stdout blobs.

Suggested normalized result fields:

- `boot_ok`
- `unit_tests_ok`
- `integration_tests_ok`
- `smoke_ok`
- `critical_flows_ok`
- `errors_detected`
- `timed_out`

Artifacts should be stored by reference where possible:

- command stdout/stderr snippets
- generated report paths
- screenshots or traces
- server logs
- QA report paths

## Failure Fingerprinting

The closure loop only becomes autonomous if it can recognize repeated failures.

Examples:

- `boot:http:5001:connection_refused`
- `playwright:tool-generation:generationPill_failed`
- `godot:main-menu:new-game:missing_initialize`
- `pytest:test_scenarios:unboundlocal_job`

Fingerprint generation rules:

- include subsystem and failing check
- exclude timestamps and unstable values
- normalize exception names and primary assertion targets
- preserve enough detail to route repair tasks correctly

## Closure Status Rules

Recommended first-pass computation:

- `red`
  - boot fails
  - required smoke/critical flow fails
  - open regression count exceeds gate with boot or flow impact

- `yellow`
  - project boots
  - some verification or gates still failing
  - project is workable but not converged

- `green`
  - all required gates pass
  - open regressions are below threshold

- `frozen`
  - same as `red`, plus policy says feature work is blocked

- `stalled`
  - repeated repair attempts have not reduced the same regression set after
    `stall_threshold` cycles

## Scheduler Integration

Extend auto mode so scheduling obeys closure state.

### Rules

- `green`
  - normal feature/refactor scheduling

- `yellow`
  - allow feature work, but prefer bug/integration/qa work when open regressions
    exist

- `red`
  - stop spawning new feature tasks for that project
  - prefer repair tasks, triage, and smoke runs

- `frozen`
  - same as `red`, but explicit project policy state visible in UI/API

- `stalled`
  - stop automatic expansion and emit one escalation artifact instead of
    continuing the loop blindly

### Trigger points in orchestrator

- after successful agent completion
- after failure recovery creates a replacement/continuation task
- on periodic idle-health cycle for managed projects

## Repair Planning Rules

Map verification failure classes into bounded task types.

- boot failure
  - create `bug` task with startup evidence and exact command

- unit test failure
  - create `bug` task when test names are specific
  - create `triage` task if failures are too broad or unstable

- smoke failure
  - create `bug` or `integration_bug` style task tied to the critical flow

- recurring identical failure
  - if the same fingerprint repeats beyond threshold, create or switch to
    `triage` and increment stall counter

- no improvement after repeated repair loops
  - mark `stalled`
  - create one operator-facing escalation bundle

Repair tasks should include:

- source verification run id
- fingerprint
- exact failing command/check
- artifacts/log snippets
- recommended scope

## API Additions

Add project-scoped closure endpoints:

- `GET /api/projects/<project>/closure`
  - return spec, closure status, gate summary, last verification, regression
    summary

- `POST /api/projects/<project>/closure/spec`
  - create or update `ProjectSpec`

- `POST /api/projects/<project>/closure/mode`
  - switch `build`, `stabilize`, `ship`

- `POST /api/projects/<project>/closure/verify`
  - run verification immediately

- `POST /api/projects/<project>/closure/repair`
  - generate repair tasks from latest failed verification

- `GET /api/projects/<project>/regressions`
  - list normalized open and resolved regressions

## Dashboard Changes

Add project-closure operator surfaces without collapsing them into dependency
integrity.

### New dashboard data

- closure status pill
- current mode
- last verification summary
- gate checklist
- open regression count
- top recurring fingerprints

### New actions

- run verification now
- switch to stabilize mode
- freeze/unfreeze feature expansion
- generate repair tasks from latest failure
- acknowledge stalled project

## Rollout Plan

### Phase 1: persistence and spec foundation

- extend `swarm/db.py` schema
- add `swarm/closure/specs.py`
- add `swarm/closure/status.py`
- expose `closure_mode` and `closure_status`
- keep status computation simple and non-blocking

### Phase 2: verification execution

- add `swarm/closure/runs.py`
- add `swarm/closure/verification.py`
- support command and HTTP readiness checks
- integrate with existing post-task validation flow

### Phase 3: regression model and repair generation

- add `swarm/closure/regressions.py`
- add `swarm/closure/repair_planning.py`
- create repair tasks automatically from failed verification
- upsert recurring regressions instead of creating duplicate bug tasks

### Phase 4: scheduler and auto-mode behavior

- update `swarm/orchestrator.py`
- prefer repair-first scheduling on `yellow`/`red`
- freeze features on `red` when policy says so
- mark projects `stalled` after repeated non-improving runs

Cross-cutting safety addition:

- add duplicate-run suppression, throttling, and verification run-budget guards
  so closure verification cannot create runaway load under overlapping lifecycle
  triggers

### Phase 5: API and dashboard

- expose closure endpoints in `swarm/api_projects.py`
- add dashboard views and actions
- keep closure UX distinct from dependency integrity UX

Structural safeguard:

- if closure UI grows beyond a thin surface addition, extract it behind a
  dedicated dashboard module boundary rather than re-expanding `dashboard.js`

### Phase 6: seeded project specs and rollout verification

- seed or document specs for:
  - one web/typescript project
  - one python/service project
  - one Godot project
- add integration tests covering:
  - spec persistence
  - verification run persistence
  - regression upsert behavior
  - repair-first scheduling
  - stall detection

Escalation completion:

- define the bounded stalled-project escalation artifact / operator handoff
  bundle that is emitted when the controller cannot converge autonomously

## Test Gates

The overhaul should promote through explicit validation checkpoints rather than
defer most testing until the end.

### Gate 1: foundation verification

Run after Phase 1.

- DB migration coverage on existing-style data
- closure spec normalization tests
- closure status derivation tests
- compatibility checks for existing project retrieval behavior

Tracked by:

- `swarm-controller-yyi`

### Gate 2: verification runtime checkpoint

Run after Phase 2.

- VerificationRun persistence behavior
- command and HTTP readiness checks
- post-task trigger integration
- structured failure recording without controller crashes

Tracked by:

- `swarm-controller-udu`

### Gate 3: autonomy checkpoint

Run after Phase 3.

- regression fingerprinting and upsert behavior
- bounded repair task generation
- recurrence/stall input correctness

Tracked by:

- `swarm-controller-w4e`

### Gate 4: scheduler policy checkpoint

Run after Phase 4.

- repair-first scheduling
- feature-freeze and stalled-state transitions
- duplicate-run suppression and throttling under repeated triggers
- orchestrator-level closure-policy regressions

Tracked by:

- `swarm-controller-109`

### Gate 5: operator surface checkpoint

Run after Phase 5.

- closure API response correctness
- dashboard summary rendering
- verify-now and repair action wiring
- UI/backend state consistency

Tracked by:

- `swarm-controller-62h`

### Gate 6: representative project rollout checkpoint

Run after Phase 6.

- seeded spec behavior on one web/typescript project
- seeded spec behavior on one python/service project
- seeded spec behavior on one Godot project
- stalled escalation artifact behavior where applicable

Tracked by:

- `swarm-controller-8it`

### Final gate: full closure overhaul regression pass

Run after all prior gates succeed.

- full controller regression coverage relevant to closure changes
- closure-specific unit/API/orchestrator coverage
- at least one realistic web, Python, and Godot flow

Tracked by:

- `swarm-controller-lva`

## Test Plan

### Unit tests

- spec normalization and defaulting
- closure status derivation
- failure fingerprint normalization
- regression upsert and recurrence counting
- repair-task generation from verification results

### API tests

- closure spec CRUD
- verify-now endpoint
- regressions listing
- mode transitions

### Orchestrator tests

- successful completion triggers verification
- failed verification creates repair tasks
- `red` projects stop spawning new feature work
- recurring failures eventually mark `stalled`

### Dashboard smoke

- closure status renders correctly
- verification action calls API
- stabilize/freeze controls reflect state transitions

## Migration and Backward Compatibility

- existing projects should default to `closure_mode='build'`
- if no spec exists, use project-profile defaults and mark project `yellow`
- closure data must not affect dependency runnability directly
- existing integrity/repair endpoints continue to operate independently

## Recommended Initial Slice

Implement the smallest end-to-end autonomous closure loop first:

1. persist `ProjectSpec`
2. persist `VerificationRun`
3. run verification after task completion
4. create structured regressions from failures
5. auto-create repair tasks from those regressions
6. stop new feature work for `red` projects

That slice is enough to move the controller from “builds code” to “detects and
repairs closure failures” without requiring the full dashboard overhaul first.
