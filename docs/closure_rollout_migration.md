# Closure Loop Rollout and Migration

This document is the operational handoff for enabling the closure loop on
existing projects.

## Default Behavior

Projects without a custom closure spec are still closure-aware.

- profile defaults come from `swarm/closure/specs.py`
- missing specs normalize to `mode=build`
- missing verification evidence leaves a project at `closure_status=yellow`
- closure state does not mutate dependency truth
- closure state only affects verification, repair generation, and scheduling

That means enabling the controller changes observation and prioritization first,
not the dependency graph itself.

## Existing Project Migration

Recommended order:

1. confirm the project has a correct `profile`
2. inspect the current runnable surface
3. apply a narrow closure spec through `/api/projects/<project>/closure/spec`
4. run `/api/projects/<project>/closure/verify`
5. review regressions and generated repair tasks
6. switch from `build` to `stabilize` only after the representative flow is credible

Do not start with exhaustive specs. Closure rollout should begin with one
representative flow per project.

## Representative Seeds

The first-pass seed set is codified in:

- `swarm/closure/project_seeds.py`
- `docs/closure_representative_project_seeds.md`

Current seeded project classes:

- one web/typescript project
- one python project
- one Godot project

These seeds are starter contracts, not automatically applied policy.

## Closure Modes

`build`

- feature expansion is allowed
- verification still runs
- regressions and repair tasks are still generated

`stabilize`

- repair-first scheduling applies when closure health is poor
- red/frozen/stalled states are meaningful operator signals

`ship`

- use when the project should converge on closure gates rather than expand scope
- keep specs narrow and high-signal

## Scheduler Expectations

The scheduler now uses closure state generically.

- unhealthy projects with open regressions prefer repair and triage work
- `frozen` and `stalled` projects block expansion work
- dependency readiness still wins over closure preference
- idle controller cycles can trigger periodic closure verification
- duplicate or over-frequent verification runs are guarded

## Freeze and Stalled Semantics

`frozen`

- means the project is red and policy blocks expansion
- repair paths remain allowed

`stalled`

- means repeated regressions have not improved enough to clear the stall threshold
- expansion work is blocked
- triage and repair remain allowed

Neither state should be treated as a dependency error. They are controller policy
states.

## Operator Workflow

Supported operator surfaces now include:

- closure summary view per project
- verify-now action
- repair generation action
- closure mode transitions
- regression listing

Recommended operator loop:

1. inspect closure summary
2. trigger verify-now if state is stale
3. inspect regressions
4. generate repair tasks if the latest failed run has not already done so
5. move to `stabilize` when the representative flow matters more than expansion

## Rollout Guardrails

To avoid overfitting:

- use one representative flow, not every flow
- encode assumptions beside the spec seed or rollout docs
- do not add project-name conditionals to scheduler or route logic
- prefer project data and normalized closure state over bespoke exceptions

## Known Limits

- representative seeds are manually reviewed starter specs
- stalled escalation artifacts and handoff bundles land later in Phase 6
- seeded projects prove the loop across project types, not complete production readiness
