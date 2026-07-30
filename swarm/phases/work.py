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
from swarm.agent_loop_helpers import compact_conversation
from swarm.runtime_config import _get_compaction_threshold


from swarm.constants import WORK_MAX_LOOPS as _MAX_WORK_LOOPS

_WORK_SYSTEM = """\
You are a software engineer implementing a specific change.
A plan and scout phase have already been completed. Their findings are in your conversation history.
The scout summary describes files but does not contain their full current text. Before patching, use
read_file_range to get the exact lines you are about to modify — but do NOT re-explore the codebase or
re-derive what scout already identified. Read only what you are about to change.
Implement the change, commit it, and output WORK_COMPLETE.

To call a tool, output EXACTLY this format (no markdown, no explanation before/after):
[TOOL_CALL]{"tool": "read_file", "args": {"path": "/absolute/path/to/file"}}[/TOOL_CALL]

Available tools:
- read_file(path) — read a file
- read_file_range(path, start_line, end_line) — read part of a file
- list_files(path) — list directory contents
- search_code(query) — search code
- write_file(path, content) — write/overwrite a file (max ~12KB; use append_file for larger files)
- patch_file(path, old, new) — replace a string in a file (preferred for editing existing files)
- append_file(path, content) — append to a file (use this for large new files: write_file a small header, then append_file the rest in chunks)
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

IMPORTANT: Every action must use the [TOOL_CALL] format above. Never describe what you would do — always output the tool call directly.
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


_SCOUT_HANDOFF_PROMPT = (
    "You are summarising a read-only recon phase (plan + scout) performed before implementation.\n"
    "Produce a concise but complete summary covering:\n"
    "- The task goal\n"
    "- Files read and their key contents relevant to the task\n"
    "- The root cause or problem location identified\n"
    "- Specific functions, classes, variables, and line numbers found\n"
    "- Any errors or patterns observed\n"
    "- A clear recommended implementation approach\n"
    "Be specific and actionable. This summary is the implementation agent's only memory of the scout's findings."
)

def _compact_scout_handoff(state: TaskState, provider: str, log_fn) -> list:
    """Summarise accumulated plan+scout messages into a single compact prefix.

    Without this, the work phase inherits the full raw transcript of every
    plan and scout loop (10+24 loops × 4k-char tool results) and re-sends
    it on every work call — burying plan context and burning quota.

    Returns a fresh 3-message list: [original_user_prompt, summary_block, ack].
    Falls back to returning state.messages unchanged if the LLM call fails.
    """
    if not state.messages:
        return state.messages

    n = len(state.messages)
    char_count = sum(
        len(m["content"]) if isinstance(m["content"], str) else len(str(m["content"]))
        for m in state.messages
    )
    # Only compact if there's meaningful history to collapse (>2 messages, >4k chars).
    if n <= 2 or char_count < 4_000:
        log_fn(f"Scout handoff: {n} messages, {char_count} chars — small enough to skip compaction")
        return state.messages

    history_text = "\n\n".join(
        f"[{m['role'].upper()}]: {m['content'] if isinstance(m['content'], str) else str(m['content'])}"
        for m in state.messages[1:]  # skip initial user prompt
    )
    try:
        summary, _, _ = call_llm(
            _SCOUT_HANDOFF_PROMPT,
            [{"role": "user", "content": history_text}],
            provider=provider,
        )
        compacted = [
            state.messages[0],  # original task prompt
            {"role": "user", "content": (
                f"[SCOUT RECON SUMMARY — {n} messages of read-only exploration]\n\n"
                f"{summary}\n\n"
                "You are now in the implementation phase. Proceed with the changes."
            )},
            {"role": "assistant", "content": "Understood. I have reviewed the plan and scout findings and will now implement the solution."},
        ]
        log_fn(
            f"Scout handoff: compacted {n} messages ({char_count} chars) → 3 messages "
            f"({len(summary)} char summary)"
        )
        return compacted
    except Exception as exc:
        log_fn(f"Scout handoff compaction failed ({exc}) — proceeding with full history")
        return state.messages


def _build_work_prompt_slim(state: TaskState) -> str:
    """Slim work directive used when continuous context is active.

    The model already has full file contents and findings in its message history
    from the plan and scout phases. We only inject genuinely new info:
    - Known failures from prior attempts (not in this conversation)
    - Synthesis implementation steps (if a synthesize phase ran)
    - Validation repair errors (if this is a repair loop)
    - Work profile (art_pass/polish contracts)
    """
    plan = state.plan
    lines = [
        f"GOAL: {plan.get('goal', state.description)}",
        "",
    ]

    # Inject success criteria and constraints as a quick goal anchor
    if plan.get("success_criteria"):
        lines.append("SUCCESS CRITERIA:")
        for sc in plan.get("success_criteria", []):
            lines.append(f"  - {sc}")
        lines.append("")

    if plan.get("constraints"):
        lines.append("CONSTRAINTS:")
        for c in plan.get("constraints", []):
            lines.append(f"  - {c}")
        lines.append("")

    # Scout recommended actions — the most valuable output of the scout phase.
    # Injected explicitly so work doesn't re-derive what scout already found.
    scout = state.scout_report or {}
    if scout.get("recommended_actions"):
        lines.append("SCOUT RECOMMENDED ACTIONS (implement these directly):")
        for a in scout.get("recommended_actions", []):
            lines.append(f"  - {a}")
        lines.append("")

    # Known failures — from prior attempts, not visible in this conversation
    handoff = state.handoff or {}
    if handoff.get("known_failures"):
        lines.append("KNOWN FAILURES FROM PRIOR RUNS:")
        for kf in handoff.get("known_failures", []):
            lines.append(f"  {kf[:300]}")
        lines.append("")

    # Synthesis steps — only produced if a synthesize phase ran
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

    lines.append(
        "The scout summary above describes files and root cause but does not contain their full text. "
        "Use read_file_range on specific lines you are about to patch, but do NOT re-explore the codebase "
        "or re-derive what scout already found. Implement, commit, then output WORK_COMPLETE."
    )
    return "\n".join(lines)


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

    # Known failures from prior attempts
    handoff = state.handoff or {}
    if handoff.get("known_failures"):
        lines.append("KNOWN FAILURES FROM PRIOR RUNS:")
        for kf in handoff.get("known_failures", []):
            lines.append(f"  {kf[:300]}")
        lines.append("")

    # Handoff hypotheses from prior phases (scout/synthesize/etc).
    # Rendered even when scout_report also has findings so downstream agents
    # see the most-upstream hypotheses alongside native findings.
    if handoff.get("hypotheses"):
        lines.append("HYPOTHESES (from scout):")
        for h in handoff.get("hypotheses", []):
            lines.append(f"  - {h}")
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

    # Art brief — injected by the art phase for art_pass tasks.
    art_brief = (scout or {}).get("_art_brief") or (handoff or {}).get("art_brief")
    if art_brief:
        lines.append("ART BRIEF (visual assessment from art phase):")
        lines.append(f"  Style: {art_brief.get('visual_style', '')}")
        lines.append("")
        work_order = art_brief.get("work_order", [])
        if work_order:
            lines.append(f"  Work order (implement in this order): {', '.join(work_order)}")
            lines.append("")
        elements_by_name = {e.get("name"): e for e in art_brief.get("elements", [])}
        for name in work_order:
            e = elements_by_name.get(name)
            if not e or e.get("priority") == "skip":
                continue
            lines.append(f"  ELEMENT: {name}")
            lines.append(f"    Current: {e.get('current_state', '')}")
            lines.append(f"    Target: {e.get('target', '')}")
            lines.append(f"    Method: {e.get('method', '')}")
            if e.get("prompt_hint"):
                w = e.get("width", 512)
                h = e.get("height", 512)
                lines.append(f"    Prompt: \"{e['prompt_hint']}\" ({w}x{h})")
            lines.append("")
        if art_brief.get("asset_library_notes"):
            lines.append(f"  Asset library: {art_brief['asset_library_notes']}")
            lines.append("")
        lines.append(
            "  generate_image and generate_3d_asset are BUILT-IN TOOL CALLS — call them directly. "
            "Do NOT search the project code for them. They work like run_command()."
        )
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

    lines.append(
        "The scout summary above describes files and root cause but does not contain their full text. "
        "Use read_file_range on specific lines you are about to patch, but do NOT re-explore the codebase "
        "or re-derive what scout already found. Implement, commit, then output WORK_COMPLETE."
    )
    return "\n".join(lines)


@register_phase
class WorkPhase(Phase):
    name = "work"

    def run(self, state: TaskState) -> TaskState:
        import swarm.agent_runtime as rt

        provider = self.config.get("work_provider") or rt.LLM_PROVIDER
        rt._ROUTING_PHASE = "work"
        self.log(f"Using provider: {provider}")

        if state.messages:
            # Compact the accumulated plan+scout conversation into a clean scout
            # handoff summary before handing off to the work phase. Without this,
            # work sends all raw plan/scout tool dumps (10+24 loops of 4k-char results)
            # on every call — burying the plan in noise and burning quota on stale context.
            state.messages = _compact_scout_handoff(state, provider, self.log)
            # Inject slim directive with genuinely new info (scout actions, repair context).
            state.messages.append({"role": "user", "content": _build_work_prompt_slim(state)})
            messages = state.messages
        else:
            # Isolated context (legacy): start fresh with full context summary.
            messages = [{"role": "user", "content": _build_work_prompt(state)}]
        commit_sha = None
        completed = False
        vision_calls_since_write = 0
        mutation_tool_calls = 0
        _consecutive_stalls = 0
        _premature_complete_count = 0
        phase_limits = self.config.get("phase_loop_limits") or {}
        max_work_loops = int(phase_limits.get("work") or self.config.get("work_max_loops") or _MAX_WORK_LOOPS)
        max_work_loops = max(1, max_work_loops)

        for loop in range(1, max_work_loops + 1):
            self.log(f"Work loop {loop}/{max_work_loops}")
            text, _tokens, _thinking = call_llm(_WORK_SYSTEM, messages, provider=provider)

            if text.startswith("Error:") or text.startswith("API error"):
                self.log(f"LLM error at loop {loop}: {text[:120]} — aborting work phase")
                state.errors.append(f"work: LLM error at loop {loop}: {text[:120]}")
                raise RuntimeError(f"LLM call failed: {text[:120]}")

            if "WORK_COMPLETE" in text and mutation_tool_calls > 0 and commit_sha is None:
                # Check if agent committed via run_command (bypassing git_commit tool)
                try:
                    from swarm.tools.shell import run as _run
                    _rc, _out, _err = _run("git status --porcelain")
                    if _rc == 0 and not _out.strip():
                        self.log("WORK_COMPLETE accepted: working tree clean (committed via run_command)")
                        commit_sha = "run_command"
                        messages.append({"role": "assistant", "content": text})
                        completed = True
                        break
                except Exception:
                    pass
                _premature_complete_count += 1
                self.log(
                    f"Work tried to complete at loop {loop} with {mutation_tool_calls} write(s) but no commit "
                    f"(attempt #{_premature_complete_count}) — requiring git_commit"
                )
                if _premature_complete_count >= 3:
                    # Model is ignoring nudges — check git status and auto-commit if needed
                    from swarm.tools.shell import run as _run2
                    _rc2, _status, _ = _run2("git status --porcelain")
                    if _rc2 == 0 and not _status.strip():
                        # Working tree already clean — treat as committed
                        self.log("Auto-accept: working tree already clean after repeated premature completions")
                        commit_sha = "run_command"
                        messages.append({"role": "assistant", "content": text})
                        completed = True
                        break
                    elif _rc2 == 0 and _status.strip():
                        # There are uncommitted changes — commit them
                        self.log("Auto-committing after repeated premature completions")
                        from swarm.tools.shell import git_commit as _git_commit_fn
                        auto_result = _git_commit_fn("feat: implement task changes")
                        if auto_result.get("ok"):
                            commit_sha = auto_result.get("sha") or "auto"
                            self.log(f"Auto-commit succeeded: {commit_sha}")
                            messages.append({"role": "assistant", "content": text})
                            completed = True
                            break
                        else:
                            self.log(f"Auto-commit failed: {auto_result.get('error')} — continuing")
                    else:
                        self.log(f"git status failed (rc={_rc2}) — continuing")
                # Strip the premature marker so it doesn't anchor the model's next response
                scrubbed = text.replace("WORK_COMPLETE", "[BLOCKED: must commit first]")
                messages.append({"role": "assistant", "content": scrubbed})
                messages.append({
                    "role": "user",
                    "content": (
                        "You wrote files but did not commit them. "
                        "Call [TOOL_CALL]{\"tool\": \"git_commit\", \"args\": {\"message\": \"feat: implement changes\", \"files\": []}}[/TOOL_CALL] "
                        "(replace the commit message with a meaningful description of your changes). "
                        "After git_commit succeeds, output WORK_COMPLETE."
                    ),
                })
                _consecutive_stalls = 0
                continue

            messages.append({"role": "assistant", "content": text})

            if "WORK_COMPLETE" in text:
                self.log(f"Work complete at loop {loop}")
                completed = True
                break

            tool_calls = parse_tool_calls(text)
            if not tool_calls:
                _consecutive_stalls += 1
                preview = text[:200].replace("\n", " ")
                self.log(f"No tool calls at loop {loop} (stall #{_consecutive_stalls}): {preview!r}")
                if _consecutive_stalls >= 4:
                    nudge = (
                        f"[STALL DETECTED — {_consecutive_stalls} loops without action] "
                        "Stop explaining. Pick one concrete next step and execute it NOW with a tool call. "
                        f"{'Commit your changes and output WORK_COMPLETE.' if mutation_tool_calls > 0 else 'Read a file, write a file, or run a command.'}"
                    )
                elif _consecutive_stalls >= 2:
                    nudge = (
                        "You have not called any tools. Use "
                        "[TOOL_CALL]{\"tool\": \"...\", \"args\": {...}}[/TOOL_CALL] "
                        "to take the next action. Commit and output WORK_COMPLETE when done."
                    )
                else:
                    nudge = (
                        "Continue. Use [TOOL_CALL]{\"tool\": \"...\", \"args\": {...}}[/TOOL_CALL] "
                        "for every tool call. Commit your changes and output WORK_COMPLETE when done."
                    )
                messages.append({"role": "user", "content": nudge})
                continue

            _consecutive_stalls = 0

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
                    mutation_tool_calls += 1
                elif tool_name == "run_command":
                    cmd = tc.get("args", {}).get("command", "")
                    if any(op in cmd for op in ("cp ", "mv ", "rsvg-convert", "inkscape", "ffmpeg", "convert ")):
                        vision_calls_since_write = 0
                # Track successful commit
                if tool_name == "git_commit" and isinstance(result, dict):
                    if result.get("ok"):
                        commit_sha = result.get("sha") or result.get("commit_sha") or "ok"
                    self.log(f"Committed: {commit_sha} (ok={result.get('ok')}, err={result.get('error')})")
                result_str = json.dumps(result) if isinstance(result, dict) else str(result)
                tool_results.append(f"[{tool_name}]\n{result_str[:4000]}")

            messages.append({"role": "user", "content": "\n\n".join(tool_results)})

            # Mid-loop compaction: keep the work conversation from growing unboundedly.
            # Use the same provider-aware threshold as the legacy agent loop.
            messages = compact_conversation(
                messages, _WORK_SYSTEM,
                compact_token_threshold=_get_compaction_threshold(),
                log_fn=self.log,
                compaction_provider=getattr(rt, "COMPACTION_PROVIDER", None) or None,
            )
            if messages is not state.messages:
                state.messages = messages

        no_op = completed and mutation_tool_calls == 0 and commit_sha is None
        if no_op:
            self.log(
                f"[NoOp] Work completed at loop {loop} with zero mutating tool calls and no commit — "
                "may be genuine (prior agent) or hallucinated completion; flagged for review"
            )
        elif completed and mutation_tool_calls > 0 and commit_sha is None:
            self.log(
                f"[UncommittedWrites] Work completed with {mutation_tool_calls} write(s) but no git_commit — "
                "changes may be lost if worktree is discarded"
            )

        state.work_report = {
            "completed": completed,
            "loops_used": loop,
            "commit_sha": commit_sha,
            "patches_applied": completed,
            "mutation_tool_calls": mutation_tool_calls,
            "no_op": no_op,
        }
        state.record_phase_loops("work", loop)

        if not completed:
            self.log("Work hit loop limit without completing")
            state.errors.append("work: hit loop limit without WORK_COMPLETE")
            state.failed = True

        return state
