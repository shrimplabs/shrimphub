"""
Scout Phase

Uses a cheap/local model to gather evidence: files to change, relevant code
patterns, project structure. Write tools are blocked — scout is read-only.

Reads the plan from TaskState and produces a scout_report with findings,
hypotheses, and recommended actions for the Work phase.
"""

from __future__ import annotations

import json
import re

from swarm.pipeline import Phase, TaskState, register_phase
from swarm.tool_dispatch import execute_tool, validate_tool_call
from swarm.llm_utils import call_llm, parse_tool_calls


# Tools the scout may NOT use — same set as _SCOUT_BLOCKED_TOOLS in agent_runtime
_BLOCKED_TOOLS = frozenset({
    "write_file", "patch_file", "append_file",
    "git_commit", "git_push",
    "create_task", "create_tasks", "create_subtask",
    "create_tasks_file_aware", "delegate_task_batch",
})

_MAX_SCOUT_LOOPS = 30

_SCOUT_SYSTEM = """\
You are a read-only code scout. Your job is to gather evidence to help an
implementation agent understand what needs to change.

You have access to file reading and search tools ONLY. You may NOT write files,
commit, or create tasks.

When you have enough information, output a JSON report (no markdown fences):
{
  "files_inspected": ["list of file paths you read"],
  "findings": ["concrete facts you discovered"],
  "hypotheses": ["your best theories about root cause or approach"],
  "recommended_actions": ["specific changes the implementation agent should make"],
  "confidence": 0.0-1.0
}

Output SCOUT_COMPLETE followed by the JSON when done.
"""


def _build_scout_prompt(state: TaskState) -> str:
    plan = state.plan
    lines = [
        f"Project: {state.project}",
        f"Path: {state.project_path}",
        f"",
        f"Task goal: {plan.get('goal', state.description)}",
        f"",
        f"Unknowns to investigate:",
    ]
    for u in plan.get("unknowns", []):
        lines.append(f"  - {u}")
    lines.append(f"")
    lines.append(f"Risk areas to check:")
    for r in plan.get("risk_areas", []):
        lines.append(f"  - {r}")
    lines.append(f"")
    lines.append("Gather evidence and output SCOUT_COMPLETE + JSON report when done.")
    return "\n".join(lines)


def _extract_scout_report(text: str) -> dict | None:
    """Extract JSON report from scout output."""
    if "SCOUT_COMPLETE" not in text:
        return None
    after = text[text.index("SCOUT_COMPLETE") + len("SCOUT_COMPLETE"):].strip()
    after = re.sub(r"^```(?:json)?\s*", "", after)
    after = re.sub(r"\s*```$", "", after)
    try:
        return json.loads(after)
    except Exception:
        return None


@register_phase
class ScoutPhase(Phase):
    name = "scout"

    def run(self, state: TaskState) -> TaskState:
        import swarm.agent_runtime as rt

        # Fast-path: skip scout if plan says trivial
        if state.plan.get("fast_path"):
            self.log("Skipping scout (fast_path=true)")
            state.scout_report = {
                "files_inspected": [],
                "findings": ["fast_path: task deemed trivial by plan phase"],
                "hypotheses": [],
                "recommended_actions": [],
                "confidence": 1.0,
            }
            return state

        provider = self.config.get("scout_provider") or rt.SCOUT_PROVIDER or rt.LLM_PROVIDER
        rt._ROUTING_PHASE = "scout"
        self.log(f"Using provider: {provider}")

        messages = [{"role": "user", "content": _build_scout_prompt(state)}]
        report = None

        for loop in range(1, _MAX_SCOUT_LOOPS + 1):
            self.log(f"Scout loop {loop}/{_MAX_SCOUT_LOOPS}")
            text, _tokens, _thinking = call_llm(_SCOUT_SYSTEM, messages, provider=provider)
            messages.append({"role": "assistant", "content": text})

            # Check for completion
            report = _extract_scout_report(text)
            if report is not None:
                self.log(f"Scout complete at loop {loop}")
                break

            # Execute tool calls
            tool_calls = parse_tool_calls(text)
            if not tool_calls:
                self.log("No tool calls and no SCOUT_COMPLETE — nudging scout")
                messages.append({
                    "role": "user",
                    "content": "Continue investigating. Output SCOUT_COMPLETE + JSON report when done.",
                })
                continue

            tool_results = []
            tool_names = [tc.get("tool", "") for tc in tool_calls]
            self.log(f"Tools: {', '.join(tool_names)}")
            for tc in tool_calls:
                tool_name = tc.get("tool", "")
                if tool_name in _BLOCKED_TOOLS:
                    tool_results.append(
                        f"[{tool_name}] BLOCKED: write tools are not available in scout phase"
                    )
                    continue
                err = validate_tool_call(tc)
                if err:
                    tool_results.append(f"[{tool_name}] Validation error: {err}")
                    continue
                result = execute_tool(tc)
                result_str = json.dumps(result) if isinstance(result, dict) else str(result)
                tool_results.append(f"[{tool_name}]\n{result_str[:4000]}")

            messages.append({"role": "user", "content": "\n\n".join(tool_results)})

        if report is None:
            self.log("Scout hit loop limit without completing — using partial findings")
            # Extract any useful content from last assistant message
            last = next(
                (m["content"] for m in reversed(messages) if m["role"] == "assistant"), ""
            )
            report = {
                "files_inspected": [],
                "findings": [f"Scout loop limit reached. Last output: {last[:500]}"],
                "hypotheses": [],
                "recommended_actions": [],
                "confidence": 0.2,
            }

        files_n = len(report.get("files_inspected", []))
        findings_n = len(report.get("findings", []))
        self.log(f"Report: {files_n} files, {findings_n} findings, confidence={report.get('confidence', '?')}")
        state.scout_report = report
        return state
