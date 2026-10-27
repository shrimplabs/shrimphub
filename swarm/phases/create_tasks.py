"""
Create Tasks Phase

LLM tool loop (smart model, 15 loops max) that reads the synthesis output
and creates tasks via the swarm API tools.

The synthesize phase already did the hard reasoning. This phase gives the
LLM a focused loop to turn that into actual API calls — it can refine task
descriptions, adjust priorities, wire dependencies correctly, and verify
the DAG makes sense before committing.

Available tools: create_task, create_tasks, list_tasks (read-only search).
Write tools are blocked — the deliverable is tasks, not code.
"""

from __future__ import annotations

import json

from swarm.pipeline import Phase, TaskState, register_phase
from swarm.tool_dispatch import execute_tool, validate_tool_call
from swarm.llm_utils import call_llm, parse_tool_calls


from swarm.constants import CREATE_TASKS_MAX_LOOPS as _MAX_CREATE_LOOPS

# Only task-creation and read tools — no writes
_ALLOWED_TOOLS = frozenset({
    "create_task",
    "create_tasks",
    "create_tasks_file_aware",
    "list_tasks",
    "read_file",
    "list_files",
    "search_code",
    "list_dir",
    "search_files",
})

_CREATE_SYSTEM = """\
You are a project manager creating a task plan in a software development swarm.

You have been given a synthesis of research findings. Your job is to create
the appropriate tasks using the available tools, then output TASKS_CREATED.

To call a tool, output EXACTLY this format:
[TOOL_CALL]{"tool": "create_tasks", "args": {...}}[/TOOL_CALL]

Available tools:
- create_tasks(tasks, project) — create multiple tasks with DAG wiring in one call (preferred)
  tasks is a list of {type, description, priority, depends_on (integer indices), metadata}
- create_task(description, task_type, priority, dependencies, project) — create a single task
- list_tasks(project, status) — check what tasks already exist to avoid duplicates
- read_file(path) — read a file if you need more context
- list_files(path) — list directory contents
- search_code(query) — search code

Task types: bug (80), feature (50), refactor (100), polish (40)
DO NOT create research tasks — research pipelines must produce actionable implementation tasks only.
DO NOT create more than 8 tasks total. Consolidate micro-steps into broader actionable tasks.
Each task description must be executable by a single agent without further research.

Use create_tasks() (plural) for 2+ tasks — it handles dependency indices correctly.
Use depends_on with integer indices into the tasks array, not task IDs.

When done creating tasks, output: TASKS_CREATED
"""


def _build_create_prompt(state: TaskState) -> str:
    synthesis = state.synthesis or {}
    plan = state.plan or {}

    lines = [
        f"Project: {state.project}",
        f"Path: {state.project_path}",
        f"",
        f"ORIGINAL GOAL: {plan.get('goal', state.description)}",
        f"",
    ]

    if synthesis.get("summary"):
        lines.append(f"SYNTHESIS SUMMARY:")
        lines.append(f"  {synthesis['summary']}")
        lines.append("")

    if synthesis.get("key_conclusions"):
        lines.append("KEY CONCLUSIONS:")
        for c in synthesis.get("key_conclusions", []):
            lines.append(f"  - {c}")
        lines.append("")

    proposed = synthesis.get("proposed_tasks", [])
    if proposed:
        lines.append(f"PROPOSED TASKS (from synthesis — {len(proposed)} tasks):")
        for i, t in enumerate(proposed):
            dep_str = f", depends_on={t.get('depends_on', [])}" if t.get("depends_on") else ""
            lines.append(f"  [{i}] {t.get('type', 'feature')} (priority {t.get('priority', 50)}){dep_str}: {t.get('description', '')}")
            if t.get("rationale"):
                lines.append(f"       rationale: {t['rationale']}")
        lines.append("")

    lines.append(f"Create these tasks for project '{state.project}' using create_tasks().")
    lines.append("Check list_tasks() first if you want to avoid duplicating existing work.")
    lines.append("When done, output: TASKS_CREATED")
    return "\n".join(lines)


@register_phase
class CreateTasksPhase(Phase):
    name = "create_tasks"

    def run(self, state: TaskState) -> TaskState:
        import swarm.agent_runtime as rt

        synthesis = getattr(state, "synthesis", None) or {}
        if not synthesis.get("proposed_tasks"):
            self.log("No tasks proposed by synthesis — nothing to create")
            state.tasks_created = []
            state.work_report = {"completed": True, "loops_used": 0, "commit_sha": None, "patches_applied": False}
            state.record_phase_loops("create_tasks", 0)
            return state

        # Use the same smart model as synthesize — it knows the context
        provider = (
            self.config.get("synthesize_provider")
            or getattr(rt, "SYNTHESIZE_PROVIDER", "")
            or rt.LLM_PROVIDER
        )
        rt._ROUTING_PHASE = "synthesize"  # same phase backend as synthesize
        self.log(f"Using provider: {provider}")

        messages = [{"role": "user", "content": _build_create_prompt(state)}]
        completed = False
        all_created_ids: list[str] = []
        _prev_id_count = 0

        for loop in range(1, _MAX_CREATE_LOOPS + 1):
            self.log(f"Create-tasks loop {loop}/{_MAX_CREATE_LOOPS}")
            text, _tokens, _thinking = call_llm(_CREATE_SYSTEM, messages, provider=provider)
            messages.append({"role": "assistant", "content": text})

            if "TASKS_CREATED" in text:
                self.log(f"Tasks created at loop {loop}")
                completed = True
                break

            # Auto-stop if we've already created at least as many tasks as proposed
            proposed_count = len((state.synthesis or {}).get("proposed_tasks") or [])
            if proposed_count > 0 and len(all_created_ids) >= proposed_count:
                self.log(f"Auto-stop: created {len(all_created_ids)} tasks (proposed {proposed_count}) — model did not emit TASKS_CREATED")
                completed = True
                break

            tool_calls = parse_tool_calls(text)
            if not tool_calls:
                messages.append({
                    "role": "user",
                    "content": "Create the tasks using create_tasks() then output TASKS_CREATED.",
                })
                continue

            tool_results = []
            tool_names = [tc.get("tool", "") for tc in tool_calls]
            self.log(f"Tools: {', '.join(tool_names)}")

            for tc in tool_calls:
                tool_name = tc.get("tool", "")

                if tool_name not in _ALLOWED_TOOLS:
                    tool_results.append(f"[{tool_name}] BLOCKED: only task-creation and read tools available")
                    continue

                err = validate_tool_call(tc)
                if err:
                    tool_results.append(f"[{tool_name}] Validation error: {err}")
                    continue

                result = execute_tool(tc)

                # Collect created task IDs
                if tool_name in ("create_task", "create_tasks", "create_tasks_file_aware"):
                    if isinstance(result, dict):
                        if "task_id" in result:
                            all_created_ids.append(result["task_id"])
                        elif "ids" in result:
                            all_created_ids.extend(result["ids"])

                result_str = json.dumps(result) if isinstance(result, dict) else str(result)
                tool_results.append(f"[{tool_name}]\n{result_str[:3000]}")

            # If tasks were created this loop, append a nudge so the model knows to stop
            newly_created = len(all_created_ids) - _prev_id_count
            combined = "\n\n".join(tool_results)
            if newly_created > 0:
                combined += (
                    f"\n\n{newly_created} task(s) created successfully. "
                    "If all tasks have been created, output TASKS_CREATED now."
                )
            messages.append({"role": "user", "content": combined})
            _prev_id_count = len(all_created_ids)

        if not completed:
            self.log("Create-tasks hit loop limit without TASKS_CREATED")
            state.errors.append("create_tasks: hit loop limit without TASKS_CREATED")

        self.log(f"Total tasks created: {len(all_created_ids)} — {all_created_ids[:10]}")
        state.tasks_created = all_created_ids
        state.work_report = {
            "completed": completed,
            "loops_used": loop,
            "commit_sha": None,
            "patches_applied": False,
            "tasks_created": all_created_ids,
        }
        state.record_phase_loops("create_tasks", loop)
        return state
