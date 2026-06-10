"""
Plan Phase

Uses a strong model to frame the task using local project context and a short
read-only tool loop: goal, constraints, success criteria, implementation steps,
files to inspect, and risk areas. Output is a structured plan dict written to
TaskState.

The plan is passed to subsequent phases as their primary context — they do not
receive the raw task description or conversation history.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from swarm.pipeline import Phase, TaskState, register_phase
from swarm.tool_dispatch import execute_tool, validate_tool_call
from swarm.llm_utils import parse_tool_calls


_PLAN_SYSTEM = """\
You are a senior software engineer producing a concrete task plan.
You will be given a task description and project context.
You may inspect files before planning. You are READ-ONLY: no writes, no commits,
no task creation.

To inspect a file or directory, output EXACTLY this format:
[TOOL_CALL]{"tool": "read_file", "args": {"path": "GAME_DESIGN.md"}}[/TOOL_CALL]

Available tools: read_file, read_file_range, list_files, search_code

When ready, output EXACTLY:
PLAN_COMPLETE
{...json...}

The JSON must have exactly these keys:
{
  "goal": "one sentence describing what success looks like",
  "constraints": ["list of hard constraints or invariants to preserve"],
  "success_criteria": ["specific, verifiable criteria for done"],
  "unknowns": ["open questions that need investigation before implementation"],
  "risk_areas": ["files, systems, or patterns most likely to cause problems"],
  "files_to_inspect_first": ["relative file paths the scout should read first"],
  "likely_files_to_change": ["relative file paths likely to need edits"],
  "implementation_steps": ["ordered concrete implementation steps"],
  "test_plan": ["specific validation or test commands/checks to run"],
  "stop_conditions": ["conditions that should stop work or force a smaller task"],
  "scope": "trivial | small | medium | large",
  "fast_path": true or false
}

Set fast_path=true if the task is trivially small (single obvious fix, <10 lines changed).
A fast_path task may skip the scout phase.
"""

_MAX_PLAN_LOOPS = 6
_ALLOWED_PLAN_TOOLS = {"read_file", "read_file_range", "list_files", "search_code"}

_DOC_CANDIDATES = (
    "GAME_DESIGN.md",
    "PROJECT_CLOSURE.md",
    "README.md",
    "CLAUDE.md",
    "AGENT_KNOWLEDGE.md",
    "VALIDATION_STATE.md",
    "project.godot",
)

_TREE_IGNORE_DIRS = {".git", ".godot", "addons", ".import", "__pycache__", "node_modules"}
_TREE_IGNORE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".wav", ".mp3", ".ogg", ".uid", ".import"}


def _read_context_file(project_path: Path, rel_path: str, max_chars: int = 6000) -> str:
    path = project_path / rel_path
    if not path.exists() or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if len(text) > max_chars:
        return text[:max_chars] + "\n[... truncated ...]"
    return text


def _project_tree(project_path: Path, max_entries: int = 120) -> list[str]:
    entries: list[str] = []
    if not project_path.exists():
        return entries
    for path in sorted(project_path.rglob("*")):
        try:
            rel = path.relative_to(project_path)
        except ValueError:
            continue
        parts = rel.parts
        if any(part in _TREE_IGNORE_DIRS or part.startswith(".wt-") for part in parts):
            continue
        if path.is_file() and path.suffix.lower() in _TREE_IGNORE_SUFFIXES:
            continue
        suffix = "/" if path.is_dir() else ""
        entries.append(f"{rel.as_posix()}{suffix}")
        if len(entries) >= max_entries:
            entries.append("[... tree truncated ...]")
            break
    return entries


def _build_context_packet(state: TaskState) -> str:
    project_path = Path(state.project_path)
    lines = [
        "PROJECT CONTEXT:",
        f"- Project: {state.project}",
        f"- Path: {state.project_path}",
        "",
        "PROJECT TREE:",
    ]
    tree = _project_tree(project_path)
    lines.extend(f"- {entry}" for entry in tree[:120])
    lines.append("")
    lines.append("KEY DOCUMENTS:")
    for rel in _DOC_CANDIDATES:
        content = _read_context_file(project_path, rel)
        if not content:
            continue
        lines.append(f"\n--- {rel} ---")
        lines.append(content)
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    """Extract JSON from LLM response, tolerating markdown fences."""
    text = text.strip()
    if "PLAN_COMPLETE" in text:
        text = text[text.index("PLAN_COMPLETE") + len("PLAN_COMPLETE"):].strip()
    # Strip ```json ... ``` fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _validate_plan(plan: dict) -> list[str]:
    """Return validation errors for plans that are too vague to hand to work."""
    required = {
        "goal": str,
        "constraints": list,
        "success_criteria": list,
        "unknowns": list,
        "risk_areas": list,
        "files_to_inspect_first": list,
        "likely_files_to_change": list,
        "implementation_steps": list,
        "test_plan": list,
        "stop_conditions": list,
        "scope": str,
        "fast_path": bool,
    }
    errors: list[str] = []
    for key, expected_type in required.items():
        if key not in plan:
            errors.append(f"missing {key}")
        elif not isinstance(plan[key], expected_type):
            errors.append(f"{key} must be {expected_type.__name__}")

    if errors:
        return errors

    goal = plan["goal"].strip()
    if not goal:
        errors.append("goal is empty")
    if not plan["success_criteria"]:
        errors.append("success_criteria is empty")

    concrete_targets = plan["files_to_inspect_first"] or plan["likely_files_to_change"]
    concrete_work = plan["implementation_steps"]
    concrete_tests = plan["test_plan"]

    if not plan["fast_path"]:
        if not concrete_targets:
            errors.append("non-fast plan needs files_to_inspect_first or likely_files_to_change")
        if not concrete_work:
            errors.append("non-fast plan needs implementation_steps")
        if not concrete_tests:
            errors.append("non-fast plan needs test_plan")
    elif not (concrete_targets or concrete_work or concrete_tests):
        errors.append("fast_path plan still needs at least one concrete file, step, or test")

    generic_criteria = [str(item).strip().lower() for item in plan["success_criteria"]]
    if generic_criteria == ["task completes without errors"]:
        errors.append("success_criteria is generic fallback text")

    return errors


@register_phase
class PlanPhase(Phase):
    name = "plan"

    def run(self, state: TaskState) -> TaskState:
        from swarm.llm_utils import call_llm
        import swarm.agent_runtime as rt

        provider = self.config.get("plan_provider") or rt.LLM_PROVIDER
        rt._ROUTING_PHASE = "plan"

        context_packet = _build_context_packet(state)
        user_msg = (
            f"{context_packet}\n\n"
            f"TASK TYPE: {state.task_type}\n\n"
            f"TASK DESCRIPTION:\n{state.description}\n\n"
            "Produce a concrete, ordered plan grounded in the project context above."
        )

        self.log(f"Calling {provider} to frame task...")
        messages = [{"role": "user", "content": user_msg}]
        plan = None
        text = ""
        last_plan_errors: list[str] = []

        for loop in range(1, _MAX_PLAN_LOOPS + 1):
            self.log(f"Plan loop {loop}/{_MAX_PLAN_LOOPS}")
            if loop == _MAX_PLAN_LOOPS - 1:
                messages.append({
                    "role": "user",
                    "content": "You have two planning loops left. Stop inspecting and output PLAN_COMPLETE with the JSON plan.",
                })
            text, _tokens, _thinking = call_llm(_PLAN_SYSTEM, messages, provider=provider)
            messages.append({"role": "assistant", "content": text})

            try:
                candidate = _extract_json(text)
                last_plan_errors = _validate_plan(candidate)
                if not last_plan_errors:
                    plan = candidate
                    break
                self.log(f"Rejected incomplete plan: {', '.join(last_plan_errors[:4])}")
                messages.append({
                    "role": "user",
                    "content": (
                        "The plan JSON was parsed but is not concrete enough: "
                        + "; ".join(last_plan_errors)
                        + ". Output PLAN_COMPLETE with a corrected JSON plan. "
                        "Do not use generic fallback criteria."
                    ),
                })
                continue
            except Exception as exc:
                plan = None
                last_plan_errors = [f"parse error: {exc}"]

            tool_calls = parse_tool_calls(text)
            if not tool_calls:
                messages.append({
                    "role": "user",
                    "content": "Either inspect one relevant file with a tool call or output PLAN_COMPLETE followed by the JSON plan.",
                })
                continue

            tool_results = []
            tool_names = [tc.get("tool", "") for tc in tool_calls]
            self.log(f"Tools: {', '.join(tool_names)}")
            for tc in tool_calls:
                tool_name = tc.get("tool", "")
                if tool_name not in _ALLOWED_PLAN_TOOLS:
                    tool_results.append(f"[{tool_name}] BLOCKED: plan phase is read-only; only file read/list/search tools are available")
                    continue
                err = validate_tool_call(tc)
                if err:
                    tool_results.append(f"[{tool_name}] Validation error: {err}")
                    continue
                result = execute_tool(tc)
                result_str = json.dumps(result) if isinstance(result, dict) else str(result)
                tool_results.append(f"[{tool_name}]\n{result_str[:4000]}")
            messages.append({"role": "user", "content": "\n\n".join(tool_results)})

        if plan is None:
            # WS2: instead of killing the pipeline, produce a minimal synthetic plan
            # from the task description so scout→work→validate can still proceed.
            detail = "; ".join(last_plan_errors) if last_plan_errors else "no valid PLAN_COMPLETE JSON"
            self.log(
                f"Plan phase could not produce a concrete plan after {_MAX_PLAN_LOOPS} loops "
                f"({detail}). Falling back to minimal synthetic plan."
            )
            plan = {
                "goal": state.description[:500],
                "constraints": [],
                "success_criteria": [f"Complete: {state.description[:100]}"],
                "unknowns": [],
                "risk_areas": [],
                "scope": "medium",
                "fast_path": False,
                "files_to_inspect_first": [],
                "likely_files_to_change": [],
                "implementation_steps": [],
                "test_plan": ["Run available project validation"],
                "stop_conditions": [],
                "_plan_parse_failed": True,
                "_plan_parse_errors": last_plan_errors[:5],
            }
            state.errors.append(f"plan: parse failure, using fallback plan — {detail}")

        self.log(f"Goal: {plan.get('goal', '?')[:120]}")
        self.log(f"Scope: {plan.get('scope')} | fast_path: {plan.get('fast_path')}")

        state.plan = plan
        return state
