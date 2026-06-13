"""
Diagnose Phase

Read-only phase used by research feeder tasks. Receives the scout report and
full failure context from TaskState, then produces a structured root-cause
diagnosis to be injected into the original (failing) task's research_context.

Write tools, commit, and task-creation tools are all blocked — diagnose is
strictly observational. The output is a structured JSON report under the
DIAGNOSE_COMPLETE marker.
"""

from __future__ import annotations

import json
import re

from swarm.pipeline import Phase, TaskState, register_phase
from swarm.tool_dispatch import execute_tool, validate_tool_call
from swarm.llm_utils import call_llm, parse_tool_calls


# Tools the diagnose phase may NOT use
_BLOCKED_TOOLS = frozenset({
    "write_file", "patch_file", "append_file",
    "run_python", "run_command",
    "git_commit", "git_push",
    "create_task", "create_tasks", "create_subtask",
    "create_tasks_file_aware", "delegate_task_batch",
    "launch_game", "kill_game",
})
_ALLOWED_DIAGNOSE_TOOLS = {"read_file", "read_file_range", "list_files", "search_code", "list_dir", "search_files"}

from swarm.constants import DIAGNOSE_MAX_LOOPS as _MAX_DIAGNOSE_LOOPS

_DIAGNOSE_SYSTEM = """\
You are a root-cause investigator. A software agent has repeatedly failed to
complete a task. Your job is to diagnose WHY — and produce a short, actionable
report that a fix agent can act on immediately.

You have access to file reading and search tools ONLY. You may NOT write files,
commit, run commands, or create tasks.

To call a tool, output EXACTLY this format:
[TOOL_CALL]{"tool": "read_file", "args": {"path": "relative/path/to/file"}}[/TOOL_CALL]

Available tools: read_file, read_file_range, list_files, search_code

Investigate the failure evidence provided. Inspect the relevant files. Then
output your diagnosis starting with DIAGNOSE_COMPLETE on its own line,
followed immediately by a JSON object with this exact schema:

DIAGNOSE_COMPLETE
{
  "root_cause": "one-paragraph description of the exact root cause",
  "files_inspected": ["list of file paths you read"],
  "exact_failure": "the specific error message, line number, or behaviour that caused the failure",
  "recommended_fix": "precise, actionable description of what the fix agent should change",
  "confidence": 0.8
}

You have at most 10 loops. Output DIAGNOSE_COMPLETE by loop 8 — partial
findings are acceptable.
"""


def _build_diagnose_prompt(state: TaskState) -> str:
    """Build the initial prompt for the diagnose phase.

    Includes: task description, plan goal, previous scout report, and any
    failure context captured from the original failing task.
    """
    plan = state.plan
    scout = state.scout_report

    lines = [
        f"Project: {state.project}",
        f"Path: {state.project_path}",
        "",
        f"TASK THAT KEEPS FAILING: {plan.get('goal', state.description)}",
        "",
    ]

    # Failure context from original task (injected by _spawn_research_feeder)
    failure_context = plan.get("failure_context") or ""
    if failure_context:
        lines.append("FAILURE HISTORY:")
        lines.append(failure_context[:3000])
        lines.append("")

    # Scout findings
    if scout.get("findings"):
        lines.append("SCOUT FINDINGS (what was already observed):")
        for f in scout.get("findings", []):
            lines.append(f"  - {f}")
        lines.append("")

    if scout.get("hypotheses"):
        lines.append("SCOUT HYPOTHESES:")
        for h in scout.get("hypotheses", []):
            lines.append(f"  - {h}")
        lines.append("")

    if scout.get("files_inspected"):
        lines.append("FILES ALREADY READ BY SCOUT:")
        for f in scout.get("files_inspected", [])[:15]:
            lines.append(f"  {f}")
        lines.append("(You may re-read these for deeper inspection.)")
        lines.append("")

    # Risk areas from plan
    if plan.get("risk_areas"):
        lines.append("KNOWN RISK AREAS:")
        for r in plan.get("risk_areas", []):
            lines.append(f"  - {r}")
        lines.append("")

    lines.append("Use only these canonical tool names: read_file, read_file_range, list_files, search_code.")
    lines.append("")
    lines.append("Investigate the failure, identify the root cause, then output DIAGNOSE_COMPLETE + JSON.")
    lines.append("You have 10 loops max — output your diagnosis by loop 8.")
    return "\n".join(lines)


def _extract_diagnose_report(text: str) -> dict | None:
    """Extract JSON report from diagnose output."""
    if "DIAGNOSE_COMPLETE" not in text:
        return None
    after = text[text.index("DIAGNOSE_COMPLETE") + len("DIAGNOSE_COMPLETE"):].strip()
    after = re.sub(r"^```(?:json)?\s*", "", after)
    after = re.sub(r"\s*```$", "", after)
    try:
        data = json.loads(after)
    except Exception:
        return None
    # Ensure required fields exist with sensible defaults
    data.setdefault("root_cause", "")
    data.setdefault("files_inspected", [])
    data.setdefault("exact_failure", "")
    data.setdefault("recommended_fix", "")
    data.setdefault("confidence", 0.5)
    return data


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


def _fallback_report_from_history(messages: list[dict]) -> dict:
    """Build a minimal diagnose report from conversation history when the
    model never output DIAGNOSE_COMPLETE."""
    files_seen: list[str] = []
    observations: list[str] = []

    for msg in messages:
        if msg["role"] == "assistant":
            text = msg.get("content", "")
            for path in _extract_paths_from_tool_calls(text):
                if path not in files_seen:
                    files_seen.append(path)
            clean = re.sub(r"\[TOOL_CALL\].*?\[/TOOL_CALL\]", "", text, flags=re.DOTALL).strip()
            if clean:
                observations.append(clean[:500])

    combined = " ".join(observations)[:1000]
    return {
        "root_cause": combined or "Diagnosis incomplete — model did not produce a structured report",
        "files_inspected": files_seen[:20],
        "exact_failure": "",
        "recommended_fix": "Review scout findings and failure history manually",
        "confidence": 0.2,
    }


@register_phase
class DiagnosePhase(Phase):
    name = "diagnose"

    def run(self, state: TaskState) -> TaskState:
        import swarm.agent_runtime as rt

        provider = self.config.get("scout_provider") or rt.SCOUT_PROVIDER or rt.LLM_PROVIDER
        rt._ROUTING_PHASE = "scout"  # route as scout (cheap model) — diagnose is also read-only
        self.log(f"Using provider: {provider}")

        messages = [{"role": "user", "content": _build_diagnose_prompt(state)}]
        report = None
        _consecutive_stalls = 0

        for loop in range(1, _MAX_DIAGNOSE_LOOPS + 1):
            self.log(f"Diagnose loop {loop}/{_MAX_DIAGNOSE_LOOPS}")

            if loop == _MAX_DIAGNOSE_LOOPS - 2:
                messages.append({
                    "role": "user",
                    "content": (
                        f"You have {_MAX_DIAGNOSE_LOOPS - loop + 1} loops remaining. "
                        "Finalize your analysis and output DIAGNOSE_COMPLETE + JSON now."
                    ),
                })

            text, _tokens, _thinking = call_llm(_DIAGNOSE_SYSTEM, messages, provider=provider)
            messages.append({"role": "assistant", "content": text})

            report = _extract_diagnose_report(text)
            if report is not None:
                self.log(f"Diagnose complete at loop {loop}")
                break

            tool_calls = parse_tool_calls(text)
            if not tool_calls:
                _consecutive_stalls += 1
                self.log(f"No tool calls and no DIAGNOSE_COMPLETE — nudging (stall {_consecutive_stalls})")
                if _consecutive_stalls >= 2:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Output DIAGNOSE_COMPLETE followed immediately by the JSON object. No other text.\n"
                            "Use what you have found so far — partial findings are fine."
                        ),
                    })
                else:
                    messages.append({
                        "role": "user",
                        "content": "Continue investigating or output DIAGNOSE_COMPLETE + JSON report.",
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
                        f"[{tool_name}] BLOCKED: write/commit/task tools are not available in diagnose phase"
                    )
                    continue
                if tool_name not in _ALLOWED_DIAGNOSE_TOOLS:
                    tool_results.append(
                        f"[{tool_name}] BLOCKED: diagnose phase only allows read_file, read_file_range, list_files, and search_code"
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

        state.record_phase_loops("diagnose", loop)

        if report is None:
            self.log("Diagnose hit loop limit — building fallback report from history")
            report = _fallback_report_from_history(messages)

        self.log(
            f"Report: {len(report.get('files_inspected', []))} files inspected, "
            f"confidence={report.get('confidence', '?')}"
        )
        self.log(f"Root cause: {report.get('root_cause', '')[:200]}")

        # Store in synthesis slot — the pipeline's create_tasks/work phases read from synthesis
        # but for research feeder we store in a dedicated field via the synthesis dict
        state.synthesis = {
            "root_cause": report.get("root_cause", ""),
            "files_inspected": report.get("files_inspected", []),
            "exact_failure": report.get("exact_failure", ""),
            "recommended_fix": report.get("recommended_fix", ""),
            "confidence": report.get("confidence", 0.5),
            # Keep raw report for _apply_research_feeder_result
            "_diagnose_report": report,
        }
        return state
