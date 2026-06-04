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
        from swarm.validation import _post_task_validation_in_worktree
        from swarm import db
        from pathlib import Path

        # Validation uses the DB — init it if not already done
        import swarm.agent_runtime as rt
        data_dir = self.config.get("data_dir") or rt.DATA_DIR
        try:
            db.init(data_dir)
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
            self.log(f"Validation FAILED:\n{error_output[:500]}")
            state.validation = {
                "passed": False,
                "errors": [error_output],
                "inherited_errors": [],
            }
            state.errors.append("validate: validation failed")
            state.failed = True
        else:
            self.log("Validation PASSED")
            state.validation = {"passed": True, "errors": [], "inherited_errors": []}

        return state
