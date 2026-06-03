"""
Work Phase

Uses the main model to implement changes. Receives the plan and scout report
from TaskState and has full tool access including writes, git commit, etc.

Runs a tool loop similar to the existing agent_runtime loop but scoped to
the implementation step only — no planning, no validation.
"""

from __future__ import annotations

import json

from swarm.pipeline import Phase, TaskState, register_phase
from swarm.tool_dispatch import execute_tool, validate_tool_call
from swarm.llm_utils import call_llm, parse_tool_calls


_MAX_WORK_LOOPS = 80

_WORK_SYSTEM = """\
You are a software engineer implementing a specific change.
You have been given a plan and scout findings. Implement the change, commit it, and output WORK_COMPLETE.

Use the tools available to read files, make changes, run tests, and commit.
When done, output: WORK_COMPLETE
"""


def _build_work_prompt(state: TaskState) -> str:
    plan = state.plan
    scout = state.scout_report
    lines = [
        f"Project: {state.project}",
        f"Path: {state.project_path}",
        f"",
        f"GOAL: {plan.get('goal', state.description)}",
        f"",
        f"SUCCESS CRITERIA:",
    ]
    for sc in plan.get("success_criteria", []):
        lines.append(f"  - {sc}")
    lines.append("")
    lines.append("CONSTRAINTS:")
    for c in plan.get("constraints", []):
        lines.append(f"  - {c}")
    lines.append("")

    if scout.get("findings"):
        lines.append("SCOUT FINDINGS:")
        for f in scout.get("findings", []):
            lines.append(f"  - {f}")
        lines.append("")

    if scout.get("recommended_actions"):
        lines.append("RECOMMENDED ACTIONS (from scout):")
        for a in scout.get("recommended_actions", []):
            lines.append(f"  - {a}")
        lines.append("")

    if scout.get("files_inspected"):
        lines.append("FILES ALREADY INSPECTED BY SCOUT:")
        for f in scout.get("files_inspected", [])[:20]:
            lines.append(f"  {f}")
        lines.append("")

    lines.append("Implement the change, commit, then output WORK_COMPLETE.")
    return "\n".join(lines)


@register_phase
class WorkPhase(Phase):
    name = "work"

    def run(self, state: TaskState) -> TaskState:
        import swarm.agent_runtime as rt

        provider = self.config.get("work_provider") or rt.LLM_PROVIDER
        self.log(f"Using provider: {provider}")

        messages = [{"role": "user", "content": _build_work_prompt(state)}]
        commit_sha = None
        completed = False

        for loop in range(1, _MAX_WORK_LOOPS + 1):
            self.log(f"Work loop {loop}/{_MAX_WORK_LOOPS}")
            text, _tokens = call_llm(_WORK_SYSTEM, messages, provider=provider)
            messages.append({"role": "assistant", "content": text})

            if "WORK_COMPLETE" in text:
                self.log(f"Work complete at loop {loop}")
                completed = True
                break

            tool_calls = parse_tool_calls(text)
            if not tool_calls:
                messages.append({
                    "role": "user",
                    "content": "Continue. Commit your changes and output WORK_COMPLETE when done.",
                })
                continue

            tool_results = []
            for tc in tool_calls:
                tool_name = tc.get("tool", "")
                err = validate_tool_call(tc)
                if err:
                    tool_results.append(f"[{tool_name}] Validation error: {err}")
                    continue
                result = execute_tool(tc)
                # Track commit sha if git_commit ran
                if tool_name == "git_commit" and isinstance(result, dict):
                    commit_sha = result.get("sha") or result.get("commit_sha")
                result_str = json.dumps(result) if isinstance(result, dict) else str(result)
                tool_results.append(f"[{tool_name}]\n{result_str[:4000]}")

            messages.append({"role": "user", "content": "\n\n".join(tool_results)})

        state.work_report = {
            "completed": completed,
            "loops_used": loop,
            "commit_sha": commit_sha,
            "patches_applied": completed,
        }

        if not completed:
            self.log("Work hit loop limit without completing")
            state.errors.append("work: hit loop limit without WORK_COMPLETE")
            state.failed = True

        return state
