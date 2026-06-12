"""
Scout Phase

Uses a cheap/local model to gather evidence: files to change, relevant code
patterns, project structure. Write tools are blocked — scout is read-only.

Reads the plan from TaskState and produces a scout_report with findings,
hypotheses, and recommended actions for the Work phase.

Design: scout runs for up to _MAX_SCOUT_LOOPS loops. If it outputs
SCOUT_COMPLETE + JSON, that is used directly. Otherwise, findings are
extracted from the conversation history — the model's prose observations
are already the report, we just collect them.
"""

from __future__ import annotations

import json
import re

from swarm.pipeline import Phase, TaskState, register_phase
from swarm.tool_dispatch import execute_tool, validate_tool_call
from swarm.llm_utils import call_llm, parse_tool_calls


# Tools the scout may NOT use
_BLOCKED_TOOLS = frozenset({
    "write_file", "patch_file", "append_file",
    "run_python", "run_command",
    "git_commit", "git_push",
    "create_task", "create_tasks", "create_subtask",
    "create_tasks_file_aware", "delegate_task_batch",
})
_ALLOWED_SCOUT_TOOLS = {"read_file", "read_file_range", "list_files", "search_code", "list_dir", "search_files"}

from swarm.constants import SCOUT_MAX_LOOPS as _MAX_SCOUT_LOOPS

_SCOUT_SYSTEM = """\
You are a read-only code scout. Your job is to gather evidence to help an
implementation agent understand what needs to change.

You have access to file reading and search tools ONLY. You may NOT write files,
commit, or create tasks.

To call a tool, output EXACTLY this format:
[TOOL_CALL]{"tool": "read_file", "args": {"path": "relative/path/to/file"}}[/TOOL_CALL]

Available tools: read_file, read_file_range, list_files, search_code

You have a limited number of investigation loops. After 8 loops, you MUST
output your report regardless of whether you feel done.

When ready to report (or when instructed), output EXACTLY this format —
the word SCOUT_COMPLETE on its own line, then the JSON immediately after:

SCOUT_COMPLETE
{
  "files_inspected": ["list of file paths you read"],
  "findings": ["concrete facts you discovered"],
  "hypotheses": ["your best theories about root cause or approach"],
  "recommended_actions": ["specific changes the implementation agent should make"],
  "confidence": 0.8
}
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
    lines.append("Success criteria to preserve:")
    for sc in plan.get("success_criteria", []):
        lines.append(f"  - {sc}")
    lines.append(f"")
    lines.append("Hard constraints:")
    for c in plan.get("constraints", []):
        lines.append(f"  - {c}")
    lines.append(f"")
    lines.append("Files to inspect first:")
    for f in plan.get("files_to_inspect_first", []):
        lines.append(f"  - {f}")
    lines.append(f"")
    lines.append("Likely files to change:")
    for f in plan.get("likely_files_to_change", []):
        lines.append(f"  - {f}")
    lines.append(f"")
    lines.append("Implementation steps to validate:")
    for step in plan.get("implementation_steps", []):
        lines.append(f"  - {step}")
    lines.append(f"")
    lines.append("Use only these canonical tool names: read_file, read_file_range, list_files, search_code.")
    lines.append(f"")
    lines.append("Investigate, then output SCOUT_COMPLETE + JSON report. You have 12 loops max — report by loop 8.")
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


def _extract_paths_from_tool_calls(text: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(r"\[TOOL_CALL\](.*?)\[/TOOL_CALL\]", text, flags=re.DOTALL):
        try:
            call = json.loads(match.group(1))
        except Exception:
            continue
        args = call.get("args") or {}
        path = args.get("path")
        if isinstance(path, str) and path and path not in paths:
            paths.append(path)
    return paths


def _extract_findings_from_history(messages: list[dict]) -> dict:
    """Build a scout report from conversation history when SCOUT_COMPLETE was never output.

    Collects all assistant messages and treats them as running observations.
    """
    observations = []
    files_seen: list[str] = []

    for msg in messages:
        if msg["role"] != "assistant":
            continue
        text = msg.get("content", "")
        for path in _extract_paths_from_tool_calls(text):
            if path not in files_seen:
                files_seen.append(path)
        # Strip tool call blocks — we want the prose
        text = re.sub(r"\[TOOL_CALL\].*?\[/TOOL_CALL\]", "", text, flags=re.DOTALL).strip()
        if text:
            observations.append(text)

    # Extract file paths from tool result messages
    for msg in messages:
        if msg["role"] != "user":
            continue
        for match in re.finditer(r'"path":\s*"([^"]+)"', msg.get("content", "")):
            p = match.group(1)
            if p not in files_seen:
                files_seen.append(p)

    # Combine observations into findings list (each observation = one finding, truncated)
    findings = []
    for obs in observations:
        # Split on newlines, take non-empty lines as separate findings
        for line in obs.splitlines():
            line = line.strip()
            if len(line) > 20:
                findings.append(line[:300])
        if len(findings) >= 20:
            break

    return {
        "files_inspected": files_seen[:30],
        "findings": findings[:20] if findings else ["Scout did not produce structured findings — see work phase logs"],
        "hypotheses": [],
        "recommended_actions": [],
        "confidence": 0.4,
    }


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

        state.messages.append({"role": "user", "content": _build_scout_prompt(state)})
        messages = state.messages
        report = None
        _consecutive_stalls = 0

        for loop in range(1, _MAX_SCOUT_LOOPS + 1):
            self.log(f"Scout loop {loop}/{_MAX_SCOUT_LOOPS}")

            # Nudge toward completion in the final few loops
            if loop == _MAX_SCOUT_LOOPS - 2:
                messages.append({
                    "role": "user",
                    "content": (
                        f"You have {_MAX_SCOUT_LOOPS - loop + 1} loops remaining. "
                        "Wrap up your investigation and output SCOUT_COMPLETE + JSON now."
                    ),
                })

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
                _consecutive_stalls += 1
                self.log(f"No tool calls and no SCOUT_COMPLETE — nudging scout (stall {_consecutive_stalls})")
                if _consecutive_stalls >= 2:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Output SCOUT_COMPLETE followed immediately by the JSON object. No other text.\n"
                            "Use what you have found so far — partial findings are fine."
                        ),
                    })
                else:
                    messages.append({
                        "role": "user",
                        "content": "Continue investigating or output SCOUT_COMPLETE + JSON report.",
                    })
                continue

            _consecutive_stalls = 0
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
                if tool_name not in _ALLOWED_SCOUT_TOOLS:
                    tool_results.append(
                        f"[{tool_name}] BLOCKED: scout phase only allows read_file, read_file_range, list_files, and search_code"
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
            self.log("Scout hit loop limit — extracting findings from conversation history")
            report = _extract_findings_from_history(messages)
        elif not report.get("files_inspected"):
            history_report = _extract_findings_from_history(messages)
            report["files_inspected"] = history_report.get("files_inspected", [])

        files_n = len(report.get("files_inspected", []))
        findings_n = len(report.get("findings", []))
        self.log(f"Report: {files_n} files, {findings_n} findings, confidence={report.get('confidence', '?')}")
        state.scout_report = report
        # Populate normalized handoff (WS5)
        plan = state.plan
        state.handoff = {
            "goal": plan.get("goal") or state.description,
            "facts": list(report.get("findings", [])),
            "files_inspected": list(report.get("files_inspected", [])),
            "files_to_modify": list(plan.get("likely_files_to_change", [])),
            "known_failures": list(state.handoff.get("known_failures", [])),  # preserve from earlier phases
            "constraints": list(plan.get("constraints", [])),
            "next_actions": list(report.get("recommended_actions", [])),
            "unknowns": list(plan.get("unknowns", [])),
            "hypotheses": list(report.get("hypotheses", [])),
        }
        return state
