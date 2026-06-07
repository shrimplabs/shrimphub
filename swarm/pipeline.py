"""
Task Pipeline

Orchestrates a sequence of phases within a single task. Each phase receives
a TaskState, does its work, and returns an updated TaskState. Phases communicate
via structured handoff objects on TaskState rather than shared conversation history.

Usage in agent wrappers (set by swarm_runner.py):
    rt.PIPELINE = ["plan", "scout", "work", "validate"]

If PIPELINE is empty or the task type has no pipeline config, agent_runtime
falls back to the existing single-loop behaviour.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# TaskState — the accumulating handoff object passed between phases
# ---------------------------------------------------------------------------

@dataclass
class TaskState:
    """Structured state passed between pipeline phases.

    Each phase reads what it needs and writes its output back.
    Phases must not depend on fields produced by later phases.
    """

    # --- Input (set before pipeline runs) ---
    task_id: str = ""
    task_type: str = ""
    project: str = ""
    description: str = ""
    project_path: str = ""
    workspace: str = ""

    # --- Plan phase output ---
    plan: dict = field(default_factory=dict)
    # {goal, constraints, success_criteria, unknowns, risk_areas, scope}

    # --- Scout phase output ---
    scout_report: dict = field(default_factory=dict)
    # {files_inspected, findings, hypotheses, recommended_actions, confidence}

    # --- Synthesize phase output (research pipeline) ---
    synthesis: dict = field(default_factory=dict)
    # {summary, key_conclusions, proposed_tasks, confidence}

    # --- Create tasks phase output (research pipeline) ---
    tasks_created: list = field(default_factory=list)
    # list of task IDs created

    # --- Work phase output ---
    work_report: dict = field(default_factory=dict)
    # {patches_applied, test_output, commit_sha}

    # --- Validate phase output ---
    validation: dict = field(default_factory=dict)
    # {passed, errors, inherited_errors}

    # --- Execution metadata ---
    phases_completed: list = field(default_factory=list)
    phase_timings: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    failed: bool = False

    def mark_phase_done(self, phase_name: str, elapsed: float) -> None:
        self.phases_completed.append(phase_name)
        self.phase_timings[phase_name] = round(elapsed, 2)

    def to_summary(self) -> str:
        """Human-readable summary for logging."""
        lines = [f"Pipeline state for task {self.task_id} ({self.task_type}):"]
        lines.append(f"  Phases completed: {', '.join(self.phases_completed) or 'none'}")
        if self.plan:
            lines.append(f"  Plan goal: {self.plan.get('goal', '?')[:120]}")
        if self.scout_report:
            n = len(self.scout_report.get('files_inspected', []))
            lines.append(f"  Scout: {n} files inspected, {len(self.scout_report.get('findings', []))} findings")
        if self.synthesis:
            n = len(self.synthesis.get('proposed_tasks', []))
            lines.append(f"  Synthesis: {n} tasks proposed, confidence={self.synthesis.get('confidence', '?')}")
        if self.tasks_created:
            lines.append(f"  Tasks created: {len(self.tasks_created)} ({self.tasks_created[:5]})")
        if self.work_report:
            lines.append(f"  Work: commit={self.work_report.get('commit_sha', 'none')}")
        if self.validation:
            lines.append(f"  Validation: {'PASS' if self.validation.get('passed') else 'FAIL'}")
        if self.errors:
            lines.append(f"  Errors: {self.errors}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase base class
# ---------------------------------------------------------------------------

class Phase:
    """Base class for all pipeline phases.

    Subclasses implement run(state) -> TaskState.
    The runner calls phases in order, passing the state through each.
    """

    name: str = "base"

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def run(self, state: TaskState) -> TaskState:
        raise NotImplementedError(f"Phase {self.name} must implement run()")

    def log(self, msg: str) -> None:
        print(f"[Pipeline:{self.name}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Phase registry
# ---------------------------------------------------------------------------

# Populated by importing swarm.phases.*
PHASE_REGISTRY: dict[str, type[Phase]] = {}


def register_phase(cls: type[Phase]) -> type[Phase]:
    """Decorator to register a phase class by its name."""
    PHASE_REGISTRY[cls.name] = cls
    return cls


def _ensure_phases_loaded() -> None:
    """Import phase modules so they self-register."""
    from swarm.phases import plan, scout, work, validate, synthesize, create_tasks  # noqa: F401


def _write_phase_artifact(state: TaskState, phase_name: str, config: dict | None, log_fn=print) -> None:
    """Persist completed phase output immediately for live observability."""
    if not state.task_id:
        return
    data_dir = Path((config or {}).get("data_dir") or "data")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "task_id": state.task_id,
            "project": state.project,
            "task_type": state.task_type,
            "phase": phase_name,
            "phases_completed": list(state.phases_completed),
            "phase_timings": dict(state.phase_timings),
            "errors": list(state.errors),
            "failed": bool(state.failed),
        }
        if phase_name == "plan":
            payload["plan"] = state.plan
        elif phase_name == "scout":
            payload["plan"] = state.plan
            payload["scout_report"] = state.scout_report
        elif phase_name == "synthesize":
            payload["plan"] = state.plan
            payload["scout_report"] = state.scout_report
            payload["synthesis"] = state.synthesis
        elif phase_name == "work":
            payload["work_report"] = state.work_report
        elif phase_name == "validate":
            payload["validation"] = state.validation
        elif phase_name == "create_tasks":
            payload["tasks_created"] = state.tasks_created
            payload["work_report"] = state.work_report
        path = data_dir / f"agent_{state.task_id}_{phase_name}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        log_fn(f"[Pipeline] WARNING: failed to write {phase_name} artifact: {exc}")


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

def run_pipeline(
    pipeline: list[str],
    state: TaskState,
    config: dict | None = None,
    log_fn=print,
) -> TaskState:
    """Run a sequence of phases, threading TaskState through each.

    Args:
        pipeline: ordered list of phase names, e.g. ["plan", "scout", "work", "validate"]
        state: initial TaskState (task_id, description, etc. pre-populated)
        config: optional config dict passed to each phase
        log_fn: logging callable

    Returns:
        Final TaskState after all phases complete (or after first failure).
    """
    _ensure_phases_loaded()

    log_fn(f"[Pipeline] Starting: {' → '.join(pipeline)}")

    for phase_name in pipeline:
        if phase_name not in PHASE_REGISTRY:
            log_fn(f"[Pipeline] ERROR: unknown phase '{phase_name}' — skipping")
            state.errors.append(f"Unknown phase: {phase_name}")
            continue

        phase_cls = PHASE_REGISTRY[phase_name]
        phase = phase_cls(config=config)

        log_fn(f"")
        log_fn(f"{'='*60}")
        log_fn(f"  PHASE: {phase_name.upper()}  ({pipeline.index(phase_name)+1}/{len(pipeline)})")
        log_fn(f"{'='*60}")
        t_start = time.monotonic()

        try:
            state = phase.run(state)
        except Exception as exc:
            elapsed = time.monotonic() - t_start
            log_fn(f"[Pipeline] FAILED {phase_name} after {elapsed:.1f}s: {exc}")
            state.errors.append(f"{phase_name}: {exc}")
            state.failed = True
            break

        elapsed = time.monotonic() - t_start
        state.mark_phase_done(phase_name, elapsed)
        _write_phase_artifact(state, phase_name, config, log_fn=log_fn)
        log_fn(f"  ✓ {phase_name.upper()} complete ({elapsed:.1f}s)")
        log_fn(f"{'='*60}")

        if state.failed:
            log_fn(f"[Pipeline] Stopping — phase {phase_name} signalled failure")
            break

    log_fn(f"[Pipeline] Done. {'FAILED' if state.failed else 'OK'}")
    log_fn(state.to_summary())
    return state


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_pipeline_for_task_type(task_type: str, config: dict) -> list[str]:
    """Return the pipeline phase list for a task type, or [] if not configured."""
    pipelines = config.get("pipelines", {})
    return pipelines.get(task_type, [])
