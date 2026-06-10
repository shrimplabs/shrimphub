"""
Work Phase

Uses the main model to implement changes. Receives the plan and scout report
from TaskState and has full tool access including writes, git commit, etc.

Runs a tool loop similar to the existing agent_runtime loop but scoped to
the implementation step only — no planning, no validation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from swarm.pipeline import Phase, TaskState, register_phase
from swarm.tool_dispatch import execute_tool, validate_tool_call
from swarm.llm_utils import call_llm, parse_tool_calls


_MAX_WORK_LOOPS = 80

_WORK_SYSTEM = """\
You are a software engineer implementing a specific change.
You have been given a plan and scout findings. Implement the change, commit it, and output WORK_COMPLETE.

To call a tool, output EXACTLY this format (no markdown, no explanation before/after):
[TOOL_CALL]{"tool": "read_file", "args": {"path": "/absolute/path/to/file"}}[/TOOL_CALL]

Available tools:
- read_file(path) — read a file
- read_file_range(path, start_line, end_line) — read part of a file
- list_files(path) — list directory contents
- search_code(query) — search code
- write_file(path, content) — write/overwrite a file
- patch_file(path, old, new) — replace a string in a file
- append_file(path, content) — append to a file
- run_command(command, timeout) — run a shell command
- run_python(code, timeout) — run a Python snippet (use this instead of trying to invoke python/perl via shell)
- git_commit(message, files) — commit changes
- launch_game(project_path) — launch a Godot game for live verification
- get_game_state(command) — read live state from the running game; use command="screenshot_b64" for renderer screenshots
- take_screenshot(filename) — save a screenshot after launch_game
- screenshot_burst(filename_prefix, count, interval) — capture multiple screenshots
- vision_query(image_path, question, model) — analyze screenshots with the configured vision model
- kill_game() — stop the launched game

When done, output: WORK_COMPLETE
"""


_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
_WORK_PROFILE_TASK_TYPES = {"art_pass", "polish"}
_INCLUDE_RE = re.compile(r"<%\s*include\s+'([^']+)'\s*%>")
_WORK_PROFILE_CACHE: dict[str, str] = {}


_VISUAL_TASK_FALLBACK_GUIDANCE = """\
VISUAL VERIFICATION REQUIRED:
- Before visual/UI changes, launch the game and capture a baseline screenshot.
- Use vision_query(..., model="powerful") to assess the current visual state.
- After each meaningful art/polish change, run validation, launch the game again, capture an after screenshot, and use vision_query to verify the result.
- Do not claim WORK_COMPLETE for art_pass or polish unless you have either verified screenshots with vision_query or documented why visual capture was impossible.
- For art_pass, make a concrete file change after at most three screenshot/vision calls.
"""


def _work_profile_context(state: TaskState) -> dict[str, str]:
    project_path_arg = state.project_path
    return {
        "project": state.project,
        "project_name": state.project,
        "project_path": state.project_path,
        "project_path_arg": project_path_arg,
        "description": state.description,
        "task_description": state.description,
        "godot_command": "godot",
        "godot_status": (
            "Godot status: pipeline mode. If Godot is unavailable, document the "
            "configuration blocker and use the configured validation phase result."
        ),
        "validation_block": (
            "Run the project's available validation before committing. For Godot "
            "projects, prefer a headless Godot check when available."
        ),
    }


def _render_profile_text(template: str, context: dict[str, str]) -> str:
    result = template
    for key, value in context.items():
        result = result.replace(f"<< {key} >>", str(value))
        result = result.replace(f"<<{key}>>", str(value))
        result = result.replace(f"{{{{{key}}}}}", str(value))
    return result.replace("TASK_COMPLETE", "WORK_COMPLETE")


def _expand_profile_includes(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        include_path = match.group(1)
        path = (_PROMPTS_DIR / include_path).resolve()
        try:
            path.relative_to(_PROMPTS_DIR.resolve())
        except ValueError:
            return f"[include skipped: {include_path}]"
        if not path.exists():
            return f"[include missing: {include_path}]"
        return path.read_text(encoding="utf-8").strip()

    return _INCLUDE_RE.sub(replace, text)


def _load_work_profile(task_type: str) -> str:
    if task_type not in _WORK_PROFILE_TASK_TYPES:
        return ""
    if task_type in _WORK_PROFILE_CACHE:
        return _WORK_PROFILE_CACHE[task_type]

    path = _PROMPTS_DIR / f"{task_type}.yaml"
    if not path.exists():
        return ""

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    system = data.get("system") or data.get("system_prompt") or ""
    user = data.get("user") or data.get("user_template") or ""
    parts = []
    if system:
        parts.append("SYSTEM CONTRACT:\n" + _expand_profile_includes(str(system)).strip())
    if user:
        parts.append("TASK BRIEF:\n" + _expand_profile_includes(str(user)).strip())
    profile = "\n\n".join(parts).strip()
    _WORK_PROFILE_CACHE[task_type] = profile
    return profile


def _build_work_profile_section(state: TaskState) -> str:
    profile = _load_work_profile(state.task_type)
    if profile:
        rendered = _render_profile_text(profile, _work_profile_context(state))
        return (
            "TASK-TYPE WORK PROFILE:\n"
            "Follow this task-type contract inside the pipeline work phase. "
            "Where it refers to WORK_COMPLETE, that is the completion signal for this phase.\n\n"
            f"{rendered}"
        )
    if state.task_type in {"art_pass", "polish"}:
        return _VISUAL_TASK_FALLBACK_GUIDANCE
    return ""


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

    if plan.get("implementation_steps"):
        lines.append("IMPLEMENTATION PLAN:")
        for step in plan.get("implementation_steps", []):
            lines.append(f"  - {step}")
        lines.append("")

    if plan.get("likely_files_to_change"):
        lines.append("LIKELY FILES TO CHANGE:")
        for f in plan.get("likely_files_to_change", []):
            lines.append(f"  {f}")
        lines.append("")

    if plan.get("test_plan"):
        lines.append("TEST PLAN:")
        for t in plan.get("test_plan", []):
            lines.append(f"  - {t}")
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

    # Normalized handoff (WS5): additional structured context from prior phases
    handoff = state.handoff or {}
    if handoff.get("hypotheses"):
        lines.append("HYPOTHESES (from scout):")
        for h in handoff.get("hypotheses", []):
            lines.append(f"  - {h}")
        lines.append("")
    if handoff.get("known_failures"):
        lines.append("KNOWN FAILURES FROM PRIOR RUNS:")
        for kf in handoff.get("known_failures", []):
            lines.append(f"  {kf[:300]}")
        lines.append("")

    synthesis = state.synthesis
    if synthesis and synthesis.get("implementation_steps"):
        lines.append("IMPLEMENTATION BRIEF (from synthesize phase):")
        lines.append(f"  {synthesis.get('summary', '')}")
        lines.append("")
        lines.append("STEPS:")
        for step in synthesis.get("implementation_steps", []):
            lines.append(f"  [{step.get('action','modify').upper()}] {step.get('file','')}")
            lines.append(f"    {step.get('description','')}")
            if step.get("code_hint"):
                lines.append(f"    Hint: {step['code_hint'][:200]}")
        if synthesis.get("risks"):
            lines.append("")
            lines.append("RISKS:")
            for r in synthesis.get("risks", []):
                lines.append(f"  ⚠ {r}")
        lines.append("")

    work_profile = _build_work_profile_section(state)
    if work_profile:
        lines.append(work_profile)
        lines.append("")

    # Repair context: injected when this is a re-work after a validation failure
    validation = state.validation or {}
    if state.repair_attempts > 0 and not validation.get("passed"):
        val_errors = validation.get("errors", [])
        error_text = "\n".join(val_errors)[:2000]
        lines.append(
            f"VALIDATION REPAIR (attempt {state.repair_attempts}):\n"
            f"Your previous implementation failed validation. Fix these errors before "
            f"committing again:\n\n{error_text}"
        )
        lines.append("")

    lines.append("Implement the change, commit, then output WORK_COMPLETE.")
    return "\n".join(lines)


@register_phase
class WorkPhase(Phase):
    name = "work"

    def run(self, state: TaskState) -> TaskState:
        import swarm.agent_runtime as rt

        provider = self.config.get("work_provider") or rt.LLM_PROVIDER
        rt._ROUTING_PHASE = "work"
        self.log(f"Using provider: {provider}")

        messages = [{"role": "user", "content": _build_work_prompt(state)}]
        commit_sha = None
        completed = False
        vision_calls_since_write = 0

        for loop in range(1, _MAX_WORK_LOOPS + 1):
            self.log(f"Work loop {loop}/{_MAX_WORK_LOOPS}")
            text, _tokens, _thinking = call_llm(_WORK_SYSTEM, messages, provider=provider)
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
            tool_names = [tc.get("tool", "") for tc in tool_calls]
            self.log(f"Tools: {', '.join(tool_names)}")
            for tc in tool_calls:
                tool_name = tc.get("tool", "")
                err = validate_tool_call(tc)
                if err:
                    tool_results.append(f"[{tool_name}] Validation error: {err}")
                    continue
                if state.task_type == "art_pass" and tool_name in {"vision_query", "take_screenshot", "screenshot_burst"}:
                    if vision_calls_since_write >= 3:
                        tool_results.append(
                            f"[{tool_name}]\n"
                            "[VISION CAP] You have used three visual assessment calls without a file change. "
                            "Make a concrete asset/UI file change with write_file, patch_file, append_file, "
                            "run_command, or git_commit before taking more screenshots or vision queries."
                        )
                        continue
                    vision_calls_since_write += 1
                result = execute_tool(tc)
                if tool_name in {"write_file", "patch_file", "append_file", "git_commit"}:
                    vision_calls_since_write = 0
                elif tool_name == "run_command":
                    cmd = tc.get("args", {}).get("command", "")
                    if any(op in cmd for op in ("cp ", "mv ", "rsvg-convert", "inkscape", "ffmpeg", "convert ")):
                        vision_calls_since_write = 0
                # Track commit sha if git_commit ran
                if tool_name == "git_commit" and isinstance(result, dict):
                    commit_sha = result.get("sha") or result.get("commit_sha")
                    self.log(f"Committed: {commit_sha}")
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
