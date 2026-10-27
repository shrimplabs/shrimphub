"""
Validate Phase

Deterministic post-work validation. No LLM call — runs the existing project
validation logic (Godot headless, pytest, etc.) and records pass/fail in
TaskState.

On failure, sets state.failed = True. The caller (agent_runtime) is responsible
for spawning a bug task if needed, just as the existing monitor thread does.
"""

from __future__ import annotations

from swarm.pipeline import Phase, TaskState, register_phase


@register_phase
class ValidatePhase(Phase):
    name = "validate"

    def run(self, state: TaskState) -> TaskState:
        state.record_phase_loops("validate", 0)
        from swarm.validation import _post_task_validation_in_worktree, filter_new_errors
        from swarm import db
        from pathlib import Path

        # Validation uses the DB — init it if not already done
        import swarm.agent_runtime as rt
        data_dir = self.config.get("data_dir") or rt.DATA_DIR
        try:
            db.init(Path(data_dir) / "swarm.db")
        except Exception:
            pass  # Already initialised in this process

        project_path = Path(state.project_path) if state.project_path else None

        self.log(f"Running validation for {state.project} at {project_path}")

        try:
            failed, error_output = _post_task_validation_in_worktree(
                project=state.project,
                task_id=state.task_id,
                worktree_path=project_path,
                timeout=self.config.get("validation_timeout", 120),
            )
        except Exception as exc:
            self.log(f"Validation raised exception: {exc}")
            state.validation = {"passed": False, "errors": [str(exc)], "inherited_errors": []}
            state.errors.append(f"validate: exception: {exc}")
            state.failed = True
            return state

        if failed:
            # Pre-flight baseline diffing: capture_validation_baseline() runs at
            # agent spawn time (swarm/agent_lifecycle.py) and stores pre-existing
            # error signatures on task.metadata.validation_baseline. Without this
            # diff, a project with even one already-documented, tracked test
            # failure (see CLAUDE.md "Known Test Failures") can never pass
            # validation through this phase, regardless of what the agent does --
            # every run reports the same pre-existing failure as "new" and loops
            # into an unwinnable repair cycle. Empty/missing baseline (task types
            # that skip capture, or a race where the background capture thread
            # hasn't finished yet) is conservative: with nothing to subtract,
            # every current error is still treated as new, so behavior is
            # unchanged when no baseline exists.
            baseline_sigs: list = []
            project_type = "unknown"
            try:
                task = db.task_get(state.task_id)
                if task:
                    baseline_sigs = list((task.get("metadata") or {}).get("validation_baseline") or [])
                if (project_path / "project.godot").exists() if project_path else False:
                    project_type = "godot"
                elif project_path and ((project_path / "requirements.txt").exists() or (project_path / "pyproject.toml").exists()):
                    project_type = "python"
            except Exception as _base_err:
                self.log(f"[Baseline] lookup failed, treating all errors as new: {_base_err}")

            new_errors, inherited_errors = filter_new_errors(error_output, baseline_sigs, project_type)

            if not new_errors and inherited_errors:
                self.log(
                    f"Validation reported {len(inherited_errors)} pre-existing (inherited) "
                    f"error(s), 0 new -- treating as PASS: {inherited_errors[:3]}"
                )
                state.validation = {"passed": True, "errors": [], "inherited_errors": inherited_errors}
                state.handoff["known_failures"] = []
                return state

            self.log(f"Validation FAILED ({len(new_errors)} new error(s)):\n{error_output[:500]}")
            state.validation = {
                "passed": False,
                "errors": [error_output],
                "inherited_errors": inherited_errors,
            }
            state.errors.append("validate: validation failed")
            state.failed = True
            # Update normalized handoff with known failures (WS5)
            state.handoff["known_failures"] = [error_output[:500]]
        else:
            self.log("Validation PASSED")
            state.validation = {"passed": True, "errors": [], "inherited_errors": []}
            state.handoff["known_failures"] = []

        return state
