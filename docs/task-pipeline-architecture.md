# Task Pipeline Architecture

## Overview

The swarm orchestrates tasks across projects. This document describes the architecture
for what happens *inside* a task — a pipeline of phases that mirrors the swarm at a
smaller scope.

Each task is a mini-swarm. The swarm assigns work; the pipeline executes it.

---

## The Core Problem with the Current Approach

Today a task is a single agent in a loop:

```
LLM call 1 → full conversation → LLM call 2 → full conversation → LLM call 3...
```

Context accumulates across every call. Every model sees everything. A strong expensive
model reads raw file contents. A weak cheap model is asked to make architectural
decisions it isn't equipped for. Token usage grows linearly and model selection is
all-or-nothing.

The scout phase introduced in v1 is the first crack in this — a cheaper model handles
recon, a stronger model handles implementation. But it is still a single conversation
with a model swap partway through. The context is still shared.

---

## The Knowledge Pipeline

The pipeline replaces shared context with structured handoffs.

```
Task Description
      ↓
  [ Plan Phase ]        ← strong model
      ↓
  {goal, constraints, unknowns, success criteria, risk areas}
      ↓
  [ Scout Phase ]       ← weak / local models (can run concurrently)
      ↓
  {files inspected, findings, hypotheses, recommended actions}
      ↓
  [ Synthesis Phase ]   ← strong model
      ↓
  {work packets — concrete instructions per change}
      ↓
  [ Work Phase ]        ← medium / weak models
      ↓
  {patches, test results, patch reports}
      ↓
  [ Review Phase ]      ← strong model
      ↓
  {approved / rejected / needs revision}
      ↓
  [ Validate Phase ]    ← deterministic (Godot headless, pytest, etc.)
      ↓
  {pass / fail, new bug tasks if needed}
      ↓
  [ Compact Phase ]     ← strong model
      ↓
  {project memory updated, findings written to knowledge base}
```

Each boundary is a deliberate lossy compression. Signal is preserved; noise is dropped.
The strong model never reads raw file contents — it reads the scout's distilled findings.
The worker never sees planning reasoning — it receives instructions.

This is information compression at each handoff, not accumulation.

---

## Phases Are Plugins

A phase is a modular unit with a defined interface:

```python
class Phase:
    def run(task_state: TaskState) -> PhaseResult:
        ...
```

Phases can be:
- Reordered
- Removed
- Duplicated
- Replaced with a different implementation
- Run concurrently (where the phase supports it)

This means workflows become configuration, not code. Experimentation does not require
rewriting the orchestration system.

Example pipeline variants:

```
# Fast path for simple tasks
Plan → Scout → Work → Validate

# Standard path
Plan → Scout → Synthesis → Work → Review → Validate → Compact

# High-confidence path with parallel scouts
Plan → Scout → Scout → Synthesis → Work → Review → Validate → Compact

# Parallel work graph
Plan → Scout → Synthesis → [Work A, Work B, Work C] → Merge → Review → Validate
```

---

## Structured Handoffs

Phases do not pass raw conversation history. They pass structured documents.

### Plan Output
```json
{
  "goal": "...",
  "constraints": [...],
  "success_criteria": [...],
  "unknowns": [...],
  "risk_areas": [...],
  "scope": "..."
}
```

### Scout Output (per agent)
```json
{
  "files_inspected": [...],
  "findings": [...],
  "hypotheses": [...],
  "confidence": 0.0,
  "recommended_actions": [...]
}
```

### Synthesis Output
```json
{
  "work_packets": [
    {
      "id": "...",
      "description": "...",
      "files_to_change": [...],
      "instructions": "...",
      "depends_on": [...]
    }
  ]
}
```

### Review Output
```json
{
  "verdict": "approved | rejected | needs_revision",
  "issues": [...],
  "quality_notes": "..."
}
```

The handoff objects are the unit of communication between phases. A phase receives
only what it needs to do its job. This keeps token usage bounded and model selection
meaningful.

---

## Memory Hierarchy

The handoff objects map directly onto the memory hierarchy:

| Layer | Content | Source |
|-------|---------|--------|
| 1 — Evidence | Raw findings, logs, file contents | Scout output |
| 2 — Task State | Active plan, work graph, current packets | Synthesis output |
| 3 — Project Memory | Architecture, conventions, decisions | Compact phase writes |
| 4 — System Intelligence | Model performance, routing heuristics | Swarm-level metrics |

Each phase draws from the layer appropriate to its role. Workers see Layer 2.
The synthesis model sees Layers 1 and 2. The compact phase writes to Layer 3.
No phase sees more than it needs.

---

## Model Assignment

### v1 — Explicit Assignment

Phases are assigned model tiers explicitly:

| Phase | Model Tier |
|-------|-----------|
| Plan | Strong (M3, Claude Opus) |
| Scout | Weak / Local (M2.7, Qwen-VL, local mlx) |
| Synthesis | Strong |
| Work | Medium / Weak |
| Review | Strong |
| Validate | Deterministic (no LLM) |
| Compact | Strong |

This is what the current scout_provider / LLM_PROVIDER split implements,
extended across all phases.

### v2 — Capability Profiles + Router Phase

Rather than naming a model, phases declare capability requirements:

```yaml
synthesis:
  requires:
    reasoning: 8       # 1-10
    context_window: 7
    cost_sensitivity: low

scout:
  requires:
    reasoning: 3
    cost_sensitivity: high
    concurrency: high
```

A Router phase sits before each phase and selects the best available model
that satisfies the requirements:

```
Phase
  ↓
Router → Model Registry lookup → best available model
  ↓
Phase executes on selected model
```

This survives provider churn. When a new model is available, it is registered
with capability scores and immediately eligible for routing. No pipeline changes
required.

---

## Relationship to the Current System

The current system has embryonic versions of several of these concepts:

| Current | Pipeline equivalent |
|---------|-------------------|
| `task.metadata` blob | Structured handoff objects (partially) |
| Scout phase (loop-gated) | Scout phase (phase-gated) |
| Context compaction mid-loop | Compact phase (end-of-task) |
| Post-task validation | Validate phase |
| `research_context` in metadata | Plan/Synthesis output injected into task state |
| `last_failure` + `attempt_history` | Review phase rejection feeding back into Work |

The migration path is evolutionary, not a rewrite. Each current mechanism can
be lifted into a phase one at a time.

---

## Scout Concurrency

The current scout is sequential — one agent, one loop, model swap at threshold.

The pipeline model allows concurrent scouts:

```
Plan output
    ↓
Scout A (files 1-50) ──┐
Scout B (files 51-100) ─┤→ Synthesis
Scout C (logs, tests) ──┘
```

Each scout is a separate lightweight agent. Results are collected and merged
by the Synthesis phase. This is the same pattern the swarm uses across tasks,
applied within a single task.

This is a significant implementation jump from v1. It requires:
- Scout agents that can be spawned and awaited within a task
- A merge/dedup step in synthesis
- Concurrent agent management at the task level (not just swarm level)

---

## Implementation

### Phase Registry + Config-Driven Pipelines

Phases are registered by name and pipelines are defined in config per task type:

```json
"pipelines": {
  "bug":     ["plan", "scout", "work", "validate"],
  "feature": ["plan", "scout", "work", "validate"],
  "refactor": ["scout", "plan", "work", "validate"]
}
```

Each phase is a class registered in a central registry:

```python
PHASE_REGISTRY = {
    "plan":     PlanPhase,
    "scout":    ScoutPhase,
    "work":     WorkPhase,
    "validate": ValidatePhase,
}
```

The runner iterates the list, instantiates each phase, and passes the output
of the previous phase as input to the next:

```python
state = TaskState(task=task)
for phase_name in pipeline:
    phase = PHASE_REGISTRY[phase_name](config)
    state = phase.run(state)
```

`TaskState` is the accumulating handoff object. Each phase reads what it needs
from it and writes its output back. Reordering phases is a one-line config change.
Adding a new phase is writing the class and registering it — no changes to the runner.

### MVP Pipeline

The first implementation uses four phases, in-process, sequential:

```
Plan → Scout → Work → Validate
```

This is the minimum that proves the architecture. Synthesis, Review, and Compact
are added once the basic pipeline is proven.

### Rollout Strategy

The pipeline is additive — existing tasks are unaffected. Tasks with a matching
entry in `pipelines` config use the pipeline; tasks without fall back to the
existing agent loop. This allows opt-in per task type with no risk to current
behaviour.

### In-Process vs Subprocess

MVP phases run in-process. This is simpler and sufficient for sequential pipelines.
Concurrent scouts (v2) will require subprocess isolation — extracting to separate
agents at that point is the natural path, reusing the existing agent spawning
infrastructure.

---

## Open Questions

These are noted for completeness but are expected to answer themselves through
experimentation rather than upfront design:

1. **State persistence across phases** — if a phase fails mid-pipeline, should it
   resume from the last successful handoff or restart the full task? Current
   intuition: restart is fine for MVP, per-phase checkpointing if retry waste
   proves expensive in practice.

2. **Phase timeout policy** — each phase needs its own timeout. Current single
   `AGENT_TIMEOUT` doesn't map cleanly to a multi-phase task.

3. **Work graph execution** — parallel work packets inside a task requires the
   same dep-graph logic the swarm uses. Defer to v2 concurrent scout work.

4. **Handoff schema versioning** — defer until two versions of a schema actually
   differ in production.

5. **Fast-path detection** — simple tasks should skip expensive phases. The Plan
   phase output is the natural place to signal this — if Plan determines the
   change is trivial, it can set a flag that causes Synthesis to be skipped.

---

## Current State (v1 Summary)

What exists today:
- Single-agent loop per task
- Scout phase: weak model for first N loops, strong model after threshold
- Compaction: mid-loop context compression (window management, not knowledge distillation)
- Post-task validation: deterministic, synchronous in monitor thread
- Research feeder: strong model diagnosis injected into task metadata on retry

What this document proposes for MVP:
- `swarm/pipeline.py` — `TaskState`, `Phase` base class, `PHASE_REGISTRY`, runner
- `swarm/phases/plan.py`, `scout.py`, `work.py`, `validate.py` — four MVP phases
- Wire into `agent_runtime.py` with config-driven opt-in per task type
- Distinguish mid-loop compaction (window management) from end-of-task compaction (knowledge)
- Evolve task metadata into typed handoff objects over time

What is deferred to v2:
- Router phase for dynamic model selection via capability profiles
- Concurrent scouts within a single task
- Synthesis and Compact phases
- Subprocess isolation per phase
