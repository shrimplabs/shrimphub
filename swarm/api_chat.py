"""Chat and project-creation route handlers for the Swarm API.

Routes: POST /api/chat, POST /api/project-chat, POST /api/create-project-tasks
"""

import json
import os
import requests
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
import re as _re

from flask import jsonify, request

from swarm import project_graph_policy as _graph_policy
from swarm.closure.documents import write_project_closure_doc
from swarm.closure.proposals import propose_project_closure
from swarm.godot_bootstrap import install_gut_into_project
from swarm.task_chains import anchor_project_batch_roots, chain_to_project_head, ensure_project_head, set_project_head


def _copy_tree(src: Path, dst: Path):
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _ensure_project_godot_file(project_name: str, project_path: Path):
    project_file = project_path / "project.godot"
    minimal = f"""; Engine configuration file.
; It's best edited using the editor and not directly.

config_version=5

[application]
config/name="{project_name}"
run/main_scene="res://scenes/main.tscn"
config/icon="res://icon.svg"

[autoload]
StateServer="*res://autoload/state_server.gd"
TestHarness="*res://autoload/test_harness.gd"

[editor_plugins]
enabled=PackedStringArray("res://addons/gut/plugin.cfg")
"""
    if not project_file.exists():
        project_file.write_text(minimal)
        return

    text = project_file.read_text()

    def _ensure_section_block(section: str, lines: list[str]):
        nonlocal text
        marker = f"[{section}]"
        if marker not in text:
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"\n{marker}\n" + "\n".join(lines) + "\n"
            return
        section_re = _re.compile(rf"(?ms)^\[{_re.escape(section)}\]\n(.*?)(?=^\[|\Z)")
        match = section_re.search(text)
        if not match:
            return
        body = match.group(1)
        updated_body = body
        for line in lines:
            key = line.split("=", 1)[0].strip()
            if _re.search(rf"(?m)^{_re.escape(key)}\s*=", updated_body):
                updated_body = _re.sub(rf"(?m)^{_re.escape(key)}\s*=.*$", line, updated_body, count=1)
            else:
                if updated_body and not updated_body.endswith("\n"):
                    updated_body += "\n"
                updated_body += line + "\n"
        text = text[:match.start(1)] + updated_body + text[match.end(1):]

    _ensure_section_block("application", [
        f'config/name="{project_name}"',
        'run/main_scene="res://scenes/main.tscn"',
        'config/icon="res://icon.svg"',
    ])
    _ensure_section_block("autoload", [
        'StateServer="*res://autoload/state_server.gd"',
        'TestHarness="*res://autoload/test_harness.gd"',
    ])
    _ensure_section_block("editor_plugins", [
        'enabled=PackedStringArray("res://addons/gut/plugin.cfg")',
    ])
    project_file.write_text(text)


def _bootstrap_godot_project_support(project_name: str, project_path: Path):
    templates_root = Path(__file__).resolve().parent.parent / "templates" / "godot"
    install_gut_into_project(project_path)
    _copy_tree(templates_root / "autoload", project_path / "autoload")
    _copy_tree(templates_root / "test", project_path / "test")
    check_src = templates_root / "check_scripts.gd"
    if check_src.exists():
        shutil.copy2(check_src, project_path / "check_scripts.gd")
    icon_src = templates_root / "icon.svg"
    if icon_src.exists() and not (project_path / "icon.svg").exists():
        shutil.copy2(icon_src, project_path / "icon.svg")

    scenes_dir = project_path / "scenes"
    scripts_dir = project_path / "scripts"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    main_scene = scenes_dir / "main.tscn"
    if not main_scene.exists():
        main_scene.write_text(
            '[gd_scene load_steps=2 format=3]\n\n'
            '[ext_resource type="Script" path="res://scripts/main.gd" id="1_main"]\n\n'
            '[node name="Main" type="Node"]\n'
            'script = ExtResource("1_main")\n'
        )
    main_script = scripts_dir / "main.gd"
    if not main_script.exists():
        main_script.write_text(
            "extends Node\n\n"
            "func get_game_state() -> Dictionary:\n"
            '    return {"scene": get_tree().current_scene.name if get_tree().current_scene else "Main"}\n'
        )

    _ensure_project_godot_file(project_name, project_path)


def _normalize_project_type(project_type: str | None) -> str:
    return _graph_policy.normalize_project_type(project_type)


def _project_file_extensions(project_type: str) -> list[str]:
    if _normalize_project_type(project_type) == "python":
        return [".py", ".md", ".toml", ".json", ".yaml", ".yml"]
    return [".gd"]


def _project_brief_filename(project_type: str) -> str:
    if _normalize_project_type(project_type) == "python":
        return "PROJECT_BRIEF.md"
    return "GAME_DESIGN.md"


def _python_project_chat_prompt(existing_projects: list[str]) -> str:
    return f"""You are a senior software project designer. Your job is to gather requirements through iterative questions, then produce a structured PRD that swarm agents will use to build a Python software project.

EXISTING PROJECTS (avoid name conflicts): {', '.join(existing_projects) or 'none yet'}

YOUR PROCESS — THREE PHASES:

PHASE 1 — QUESTIONS: Ask 3-5 clarifying questions with lettered options, one set at a time.
PHASE 2 — PLAN PREVIEW: Once you have enough context, write a short plan preview with:
- project name and concept
- planned tasks
- an explicit dependency map per task
End with: "Want to change anything, or shall I generate the tasks?"
PHASE 3 — PRD GENERATION: Only when the user explicitly says to proceed (e.g. "yes", "go ahead", "create", "looks good"), emit the full [PRD] block.

FORMAT QUESTIONS LIKE THIS (always use lettered options so users can reply "1A, 2C"):
1. What kind of software project is this?
   A. CLI/tooling
   B. Web service/API
   C. Data processing/automation
   D. Developer utility/library
   E. Other: [specify]

ALWAYS ASK ABOUT QUALITY GATES (in Phase 1):
What quality checks should pass for each user story in the NEW project?
   A. `python -m py_compile` on changed modules
   B. `pytest` test suite passes
   C. Lint/type checks if configured
   D. Other: [specify]

PHASE 2 PLAN SUMMARY EXAMPLE:
Here's the plan for **log-triage-cli**:
- Project: log-triage-cli (Python CLI)
- US-001: CLI argument parser and config model
- US-002: Log file reader and normalization
- US-003: Error pattern classifier
- US-004: Summary report renderer
- US-005: Tests and packaging polish

Dependency map:
- US-001 CLI argument parser and config model -> []
- US-002 Log file reader and normalization -> [US-001]
- US-003 Error pattern classifier -> [US-002]
- US-004 Summary report renderer -> [US-002, US-003]
- US-005 Tests and packaging polish -> [US-001, US-003, US-004]

Quality gate: pytest passes.

Want to change anything, or shall I generate the tasks?

DEPENDENCY RULES (CRITICAL — apply inside the PRD):
- Use a DAG, NOT a chain. Independent systems should run in parallel.
- Foundation stories (configuration, data model, core interfaces) come first; everything that builds on them fans out in parallel.
- Do NOT create stories for generic repository setup, virtualenv setup, or placeholder tests. The project scaffold handles repository basics automatically.
- WRONG: US-001 → US-002 → US-003 → US-004 (pure chain)
- RIGHT: US-001 → [US-002, US-003, US-004 in parallel] → US-005
- Add a `depends-on: US-NNN` line inside each story block that needs a predecessor. Use `depends-on: US-NNN, US-MMM` for multiple.
- Stories with no dependency get no depends-on line.
- The dependency map in Phase 2 and the `depends-on` lines in Phase 3 must describe the same graph.

PHASE 3 — ONLY when user confirms, emit this EXACT format (dependencies go INSIDE each story block):

[PRD]
# PRD: [Project Title]
project-name: kebab-case-slug

## Overview
[2-3 sentence description]

## Quality Gates
These must pass for every user story:
- [quality gate command]

## User Stories

### US-001: [Foundation title — no deps]
**Description:** As a user, I want [feature] so that [benefit].
**Acceptance Criteria:**
- [ ] [verifiable criterion]

### US-002: [Title that needs US-001]
depends-on: US-001
**Description:** As a user, I want [feature] so that [benefit].
**Acceptance Criteria:**
- [ ] [verifiable criterion]

### US-003: [Title that also needs US-001, parallel with US-002]
depends-on: US-001
**Description:** As a user, I want [feature] so that [benefit].
**Acceptance Criteria:**
- [ ] [verifiable criterion]

### US-004: [Title that needs US-002 and US-003]
depends-on: US-002, US-003
**Description:** As a user, I want [feature] so that [benefit].
**Acceptance Criteria:**
- [ ] [verifiable criterion]

## Non-Goals
- [what this will NOT include]

## Technical Notes
- Python 3
- Use pytest when tests are requested or already present
[/PRD]

OTHER RULES:
- Never emit [PRD] before the user explicitly confirms in Phase 3
- In Phase 2, write plain text only — no [PRD] tags
- Stories should be small (one agent session each), 5-10 total
- [PRD] block must be the last thing in your message when emitted
- ONLY create tasks for the new project being designed. NEVER create tasks for other existing projects."""


def _chat_call_llm(system_prompt, messages, config, fmt_override=None):
    """Shared LLM call for chat endpoints. Returns reply string."""
    try:
        import swarm_runner as _runner
        provider_name = _runner.LLM_PROVIDER
        providers     = _runner.LLM_PROVIDERS
    except Exception:
        provider_name = config.get("llm_provider", "minimax")
        providers     = {}

    provider = providers.get(provider_name, {})
    base_url    = provider.get("base_url", "https://api.minimax.io/anthropic/v1")
    model       = provider.get("model", "MiniMax-M2.7")
    api_key_env = provider.get("api_key_env", "MINIMAX_API_KEY")
    fmt         = fmt_override or provider.get("format", "anthropic")
    project_chat_cap = int(config.get("project_chat_max_tokens", 16384))
    max_tok     = min(int(provider.get("max_tokens", project_chat_cap)), project_chat_cap)
    api_key     = os.environ.get(api_key_env, "")

    if fmt == "anthropic_native":
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": model, "max_tokens": max_tok,
            "system": system_prompt, "messages": messages,
        }
        url = base_url.rstrip("/") + "/messages"
    elif fmt == "openai":
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        body = {
            "model": model, "max_tokens": max_tok,
            "messages": [{"role": "system", "content": system_prompt}] + messages,
        }
        url = base_url.rstrip("/") + "/chat/completions"
    else:  # anthropic (Bearer)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        body = {
            "model": model, "max_tokens": max_tok,
            "system": system_prompt, "messages": messages,
        }
        url = base_url.rstrip("/") + "/messages"
    use_stream = fmt in ("anthropic", "anthropic_native")
    if use_stream:
        body["stream"] = True

    def _parse_sse_stream(resp):
        text_parts = []
        stream_complete = False
        for raw_line in resp.iter_lines(chunk_size=8192):
            if not raw_line:
                continue
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode("utf-8", errors="replace")
            if not raw_line.startswith("data: "):
                continue
            data_str = raw_line[6:]
            if data_str == "[DONE]":
                stream_complete = True
                break
            try:
                ev = json.loads(data_str)
            except Exception:
                continue
            ev_type = ev.get("type", "")
            if ev_type == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta":
                    text_parts.append(delta.get("text", ""))
            elif ev_type == "message_stop":
                stream_complete = True
        if not stream_complete:
            raise RuntimeError("stream truncated before completion")
        return "".join(text_parts)

    def _parse_json_response(result):
        if fmt == "openai":
            return result["choices"][0]["message"]["content"]
        content_blocks = result.get("content", [])
        return next((b["text"] for b in content_blocks if b.get("type") == "text"), "(no text response)")

    backoff = [5, 10, 20]
    last_err = None
    for attempt in range(len(backoff) + 1):
        try:
            if use_stream:
                resp = requests.post(url, headers=headers, json=body, stream=True, timeout=(10, 300))
                resp.raise_for_status()
                return _parse_sse_stream(resp)
            resp = requests.post(url, headers=headers, json=body, timeout=(10, 180))
            resp.raise_for_status()
            return _parse_json_response(resp.json())
        except Exception as e:
            last_err = e
            if attempt >= len(backoff):
                break
            time.sleep(backoff[attempt])
    raise last_err


def _task_title(task_or_desc):
    return _graph_policy.task_title(task_or_desc)


def _task_graph_quality_errors(tasks, allowed_external_roots=None, project_type: str = "godot"):
    return _graph_policy.task_graph_quality_errors(
        tasks,
        allowed_external_roots=allowed_external_roots,
        project_type=project_type,
    )


def _task_graph_semantic_warnings(tasks) -> list[str]:
    return _graph_policy.task_graph_semantic_warnings(tasks)


def _extract_first_json_array(text: str):
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "[":
            continue
        try:
            obj, _ = decoder.raw_decode(text[idx:])
        except Exception:
            continue
        if isinstance(obj, list):
            return obj
    raise ValueError("No JSON array found in model response.")


def _parse_prd_tasks_preview(prd_content: str, project_name: str, id_sequence: list[str] | None = None):
    tasks_preview = []
    ts = int(time.time()) % 10000
    id_map = {}
    story_blocks = _re.split(r'###\s+(US-\d+):', prd_content)
    pairs = list(zip(story_blocks[1::2], story_blocks[2::2]))
    for i, (us_num, _block) in enumerate(pairs, 1):
        if id_sequence and i - 1 < len(id_sequence) and id_sequence[i - 1]:
            task_id = id_sequence[i - 1]
        else:
            task_id = f"{project_name}-t{i:02d}-{ts + i}"
        id_map[us_num.strip()] = task_id

    for i, (us_num, block) in enumerate(pairs, 1):
        task_id = id_map[us_num.strip()]
        lines = block.strip().splitlines()
        title = lines[0].strip() if lines else f"Story {i}"
        deps = []
        desc_lines = []
        for line in lines[1:]:
            dep_match = _re.match(r'\s*depends-on:\s*(.+)', line, _re.IGNORECASE)
            if dep_match:
                for ref in _re.findall(r'US-\d+', dep_match.group(1)):
                    if ref in id_map and id_map[ref] not in deps:
                        deps.append(id_map[ref])
            else:
                desc_lines.append(line.strip())
        description = f"{title}\n\n" + "\n".join(l for l in desc_lines if l)
        tasks_preview.append({
            "id": task_id,
            "project": project_name,
            "type": "feature",
            "priority": 50,
            "description": description.strip(),
            "dependencies": deps,
        })
    return tasks_preview


def _prd_dependency_annotation_stats(prd_content: str) -> dict[str, int]:
    story_blocks = _re.split(r'###\s+(US-\d+):', prd_content)
    pairs = list(zip(story_blocks[1::2], story_blocks[2::2]))
    stories = len(pairs)
    stories_with_dep_lines = 0
    total_dep_refs = 0
    for _us_num, block in pairs:
        found_line = False
        for line in block.strip().splitlines()[1:]:
            dep_match = _re.match(r'\s*depends-on:\s*(.+)', line, _re.IGNORECASE)
            if not dep_match:
                continue
            found_line = True
            total_dep_refs += len(_re.findall(r'US-\d+', dep_match.group(1)))
        if found_line:
            stories_with_dep_lines += 1
    return {
        "stories": stories,
        "stories_with_dep_lines": stories_with_dep_lines,
        "total_dep_refs": total_dep_refs,
    }


def _extract_prd_quality_gates(prd_content: str) -> list[str]:
    match = _re.search(r'##\s+Quality Gates\s*\n(.*?)(?=\n##|\Z)', prd_content, _re.DOTALL)
    if not match:
        return []
    gates: list[str] = []
    for line in match.group(1).splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        cleaned = _re.sub(r'^\s*[-*]\s*', '', cleaned).strip()
        if cleaned:
            gates.append(cleaned)
    return gates


def _render_project_preview_from_tasks(project_name: str, overview: str, tasks_preview, note: str = ""):
    lines = [f"## Plan Preview: {project_name}", ""]
    if overview:
        lines.extend([overview, ""])
    if note:
        lines.extend([note, ""])
    lines.append("### Dependency Map")
    for task in tasks_preview or []:
        title = _task_title(task)
        deps = list(task.get("dependencies") or [])
        dep_text = ", ".join(deps) if deps else "(none)"
        lines.append(f"- {title}")
        lines.append(f"  - id: {task.get('id')}")
        lines.append(f"  - dependencies: {dep_text}")
    lines.extend(["", "Want to change anything, or shall I generate the tasks?"])
    return "\n".join(lines)


def _render_project_design_doc(
    project_name: str,
    overview: str,
    tasks,
    project_type: str = "godot",
    quality_gates: str | list[str] | None = None,
    source_doc: str | None = None,
) -> str:
    """Build the durable in-repo project brief from the accepted task plan."""
    source_text = (source_doc or "").strip()
    if source_text:
        if not source_text.startswith("#"):
            return f"# {project_name}\n\n{source_text}\n"
        return source_text + "\n"

    lines: list[str] = [f"# {project_name}", ""]
    if overview:
        overview_body = overview.strip()
        if overview_body.startswith(f"# {project_name}"):
            lines.append(overview_body)
        else:
            lines.extend(["## Overview", "", overview_body])
        lines.append("")

    if quality_gates:
        lines.extend(["## Quality Gates", ""])
        if isinstance(quality_gates, str):
            for gate in quality_gates.splitlines():
                gate = gate.strip()
                if gate:
                    lines.append(gate if gate.startswith("-") else f"- {gate}")
        else:
            for gate in quality_gates:
                lines.append(f"- {gate}")
        lines.append("")
    elif project_type == "godot":
        lines.extend([
            "## Quality Gates",
            "",
            "- No Godot script errors on project load.",
            "- GUT tests pass in headless mode.",
            "",
        ])

    lines.extend(["## User Stories", ""])
    for task in tasks or []:
        task_id = task.get("id", "task")
        title = _task_title(task)
        lines.append(f"### {task_id}: {title}")
        lines.append("")
        description = str(task.get("description") or "").strip()
        if description:
            lines.append(description)
            lines.append("")
        deps = list(task.get("dependencies") or [])
        dep_text = ", ".join(deps) if deps else "(none)"
        lines.append(f"depends-on: {dep_text}")
        lines.append("")

    lines.extend(["## Dependency Map", ""])
    for task in tasks or []:
        deps = list(task.get("dependencies") or [])
        dep_text = ", ".join(deps) if deps else "(none)"
        lines.append(f"- {task.get('id')}: {dep_text}")
    lines.append("")
    return "\n".join(lines)


def _write_project_design_doc(
    project_path: Path,
    project_name: str,
    overview: str,
    tasks,
    project_type: str,
    quality_gates: str | list[str] | None = None,
    source_doc: str | None = None,
) -> None:
    brief_file = _project_brief_filename(project_type)
    content = _render_project_design_doc(
        project_name,
        overview,
        tasks,
        project_type=project_type,
        quality_gates=quality_gates,
        source_doc=source_doc,
    )
    (project_path / brief_file).write_text(content)


def _append_project_design_phase(
    project_path: Path,
    project_name: str,
    phase_name: str,
    overview: str,
    tasks,
    project_type: str,
    anchor_task_id: str | None,
) -> None:
    brief_file = _project_brief_filename(project_type)
    brief_path = project_path / brief_file
    if brief_path.exists():
        content = brief_path.read_text()
        if content and not content.endswith("\n"):
            content += "\n"
    else:
        content = f"# {project_name}\n\n"

    lines: list[str] = [
        "",
        f"## {phase_name}",
        "",
    ]
    if overview:
        lines.extend([overview.strip(), ""])
    if anchor_task_id:
        lines.extend([f"Anchored after: `{anchor_task_id}`", ""])

    lines.extend(["### User Stories", ""])
    for task in tasks or []:
        task_id = task.get("id", "task")
        title = _task_title(task)
        lines.append(f"#### {task_id}: {title}")
        lines.append("")
        description = str(task.get("description") or "").strip()
        if description:
            lines.append(description)
            lines.append("")
        deps = list(task.get("dependencies") or [])
        lines.append(f"depends-on: {', '.join(deps) if deps else '(none)'}")
        lines.append("")

    lines.extend(["### Dependency Map", ""])
    for task in tasks or []:
        deps = list(task.get("dependencies") or [])
        lines.append(f"- {task.get('id')}: {', '.join(deps) if deps else '(none)'}")
    lines.append("")
    brief_path.write_text(content + "\n".join(lines))


def _extract_prd_content(reply: str) -> str | None:
    text = (reply or "").strip()
    if not text:
        return None
    tagged = _re.search(r'\[PRD\](.*?)\[/PRD\]', text, _re.DOTALL)
    if tagged:
        return tagged.group(1).strip()
    looks_like_prd = (
        _re.search(r'^\s*#\s*PRD\s*:', text, _re.IGNORECASE | _re.MULTILINE)
        and _re.search(r'^\s*project-name\s*:\s*[a-z0-9-]+', text, _re.IGNORECASE | _re.MULTILINE)
        and _re.search(r'^\s*##\s+User Stories\b', text, _re.IGNORECASE | _re.MULTILINE)
    )
    if looks_like_prd:
        return text
    return None


def _project_chat_user_confirmed_generation(history, user_message: str) -> bool:
    text = (user_message or "").strip().lower()
    if not text:
        return False
    confirmation_markers = (
        "generate the tasks",
        "generate tasks",
        "generate the task",
        "generate it",
        "go ahead",
        "looks good",
        "create the tasks",
        "create tasks",
        "proceed",
        "yes",
    )
    if not any(marker in text for marker in confirmation_markers):
        return False
    assistant_messages = [m.get("content", "") for m in (history or []) if m.get("role") == "assistant"]
    if not assistant_messages:
        return False
    last_assistant = assistant_messages[-1]
    return "Want to change anything, or shall I generate the tasks?" in last_assistant


def _task_graph_shape_summary(tasks, allowed_external_roots=None) -> dict[str, object]:
    return _graph_policy.task_graph_shape_summary(
        tasks,
        allowed_external_roots=allowed_external_roots,
    )


def _render_graph_repair_context(tasks, validation_errors, allowed_external_roots=None) -> str:
    return _graph_policy.render_graph_repair_context(
        tasks,
        validation_errors,
        allowed_external_roots=allowed_external_roots,
    )


def _graph_diagnostics_payload(tasks, validation_errors=None, allowed_external_roots=None) -> dict[str, object]:
    return _graph_policy.graph_diagnostics_payload(
        tasks or [],
        validation_errors=validation_errors,
        allowed_external_roots=allowed_external_roots,
    )


def _repair_task_graph_with_llm(project_name, overview_text, tasks, validation_errors, config,
                                allowed_external_roots=None, max_rounds=2, project_type: str = "godot"):
    project_type = _normalize_project_type(project_type)
    allowed_external_roots = [x for x in (allowed_external_roots or []) if isinstance(x, str) and x]
    current_tasks = [dict(t) for t in tasks]
    last_errors = list(validation_errors or [])

    for round_idx in range(max_rounds):
        repair_prompt = _graph_policy.build_graph_repair_prompt(
            project_name,
            overview_text,
            current_tasks,
            last_errors,
            allowed_external_roots=allowed_external_roots,
        )
        try:
            reply = _chat_call_llm("", [{"role": "user", "content": repair_prompt}], config)
            try:
                repaired = _extract_first_json_array(reply)
            except Exception:
                repaired_prd = _extract_prd_content(reply)
                if not repaired_prd:
                    raise
                repaired = _parse_prd_tasks_preview(
                    repaired_prd,
                    project_name,
                    id_sequence=[t.get("id") for t in current_tasks],
                )
        except Exception as e:
            last_errors = [f"Automatic graph repair round {round_idx + 1} failed: {e}"]
            continue

        normalized = _graph_policy.normalize_repaired_tasks(current_tasks, repaired)

        quality_errors = _task_graph_quality_errors(
            normalized,
            allowed_external_roots=allowed_external_roots,
            project_type=project_type,
        )
        if not quality_errors:
            return normalized, round_idx + 1, []
        current_tasks = normalized
        last_errors = quality_errors

    return None, max_rounds, last_errors


def _project_creation_validation_errors(project_path: Path, created_tasks, allowed_external_roots=None, project_type: str = "godot"):
    return _graph_policy.project_creation_validation_errors(
        project_path,
        created_tasks,
        allowed_external_roots=allowed_external_roots,
        project_type=project_type,
    )


def _scaffold_project_repo(project_name, project_path: Path, project_type: str, overview_text: str, config):
    project_type = _normalize_project_type(project_type)
    git_log = []
    gitea_host = config.get("gitea_host", "")
    gitea_user = config.get("gitea_user", "")
    gitea_pass = config.get("gitea_pass", "")
    gitea_org = config.get("gitea_org", "example-org")

    def _run(*cmd, timeout=20):
        try:
            r = subprocess.run(
                cmd,
                cwd=str(project_path),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            git_log.append(f"$ {' '.join(cmd)} → {r.returncode}")
            return r
        except subprocess.TimeoutExpired:
            git_log.append(f"$ {' '.join(cmd)} → timeout")
            return None

    project_path.mkdir(parents=True, exist_ok=True)
    (project_path / "README.md").write_text(f"# {project_name}\n\n{project_type.capitalize()} project.\n")
    if project_type == "godot":
        _bootstrap_godot_project_support(project_name, project_path)
    elif project_type == "python":
        (project_path / "pyproject.toml").write_text(
            "[project]\n"
            f'name = "{project_name}"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
        )
    _run("git", "init")
    _run("git", "checkout", "-b", "main")

    if project_type == "python":
        gitignore_content = (
            "# Python\n__pycache__/\n*.py[cod]\n*.egg-info/\n"
            ".venv/\nvenv/\ndist/\nbuild/\n*.egg\n\n"
            "# Env\n.env\n*.env\n\n# macOS\n.DS_Store\n\n# Logs\n*.log\n"
        )
    else:
        gitignore_content = (
            "# Godot editor cache and generated files\n.godot/\n*.uid\n\n"
            "# Imported assets (regenerated from source)\n.import/\n\n"
            "# Export artifacts\nexport.cfg\nexport_presets.cfg\n"
            "*.apk\n*.aab\n*.ipa\n*.exe\n*.pck\n*.zip\n\n"
            "# macOS\n.DS_Store\n\n# Logs\n*.log\n"
        )
    (project_path / ".gitignore").write_text(gitignore_content)

    add_files = ["README.md", ".gitignore"]
    if project_type == "python":
        add_files.append("pyproject.toml")
    if overview_text:
        brief_file = _project_brief_filename(project_type)
        (project_path / brief_file).write_text(f"# {project_name}\n\n{overview_text}\n")
        add_files.append(brief_file)
    if project_type == "godot":
        add_files.extend(["addons", "autoload", "check_scripts.gd", "project.godot", "scenes", "scripts", "test"])

    _run("git", "add", *add_files)
    _run("git", "commit", "-m", "Initial commit")
    git_log.append("Local repo initialised")

    gitea_url = None
    if config.get("disable_remote_repo", False) or not (gitea_host and gitea_user and gitea_pass and gitea_org):
        git_log.append("Remote repo creation disabled or incomplete in config")
        return git_log, None

    try:
        import urllib.request as _ur
        import base64 as _b64
        creds = _b64.b64encode(f"{gitea_user}:{gitea_pass}".encode()).decode()
        body = json.dumps({"name": project_name, "private": False, "auto_init": False}).encode()
        req = _ur.Request(
            f"http://{gitea_host}/api/v1/orgs/{gitea_org}/repos",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Basic {creds}"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=10) as resp:
            repo_data = json.loads(resp.read())
            gitea_url = repo_data.get("clone_url") or f"http://{gitea_host}/{gitea_org}/{project_name}.git"
        git_log.append(f"Gitea repo created: {gitea_url}")
    except Exception as e:
        git_log.append(f"Gitea repo creation: {e}")
        gitea_url = f"http://{gitea_host}/{gitea_org}/{project_name}.git"

    try:
        remote_url = f"http://{gitea_user}:{gitea_pass}@{gitea_host}/{gitea_org}/{project_name}.git"
        _run("git", "remote", "add", "origin", remote_url)
        push_result = _run("git", "push", "-u", "origin", "main", timeout=15)
        if push_result is not None and push_result.returncode == 0:
            git_log.append("Pushed to Gitea")
        else:
            git_log.append("Push to Gitea skipped or failed")
    except Exception as e:
        git_log.append(f"Push error: {e}")

    return git_log, gitea_url


def _cleanup_failed_project_creation(db, project_name, project_path: Path, config, created_task_ids=None,
                                     config_file: Path | None = None, config_write_lock=None):
    created_task_ids = created_task_ids or []
    for tid in created_task_ids:
        try:
            db.task_delete(tid)
        except Exception:
            pass
    try:
        conn = db._connect()
        conn.execute("DELETE FROM projects WHERE name=?", (project_name,))
        conn.commit()
    except Exception:
        pass
    managed = list(config.get("managed_projects", []))
    if project_name in managed:
        managed.remove(project_name)
        config["managed_projects"] = managed
        if config_file is not None:
            try:
                if config_write_lock:
                    with config_write_lock:
                        existing_cfg = json.loads(config_file.read_text()) if config_file.exists() else {}
                        existing_cfg["managed_projects"] = managed
                        config_file.write_text(json.dumps(existing_cfg, indent=2) + "\n")
                else:
                    existing_cfg = json.loads(config_file.read_text()) if config_file.exists() else {}
                    existing_cfg["managed_projects"] = managed
                    config_file.write_text(json.dumps(existing_cfg, indent=2) + "\n")
            except Exception:
                pass
    if project_path.exists():
        shutil.rmtree(project_path, ignore_errors=True)


def _build_state_snapshot(db, all_tasks=None, all_agents=None, projects=None):
    """Build a current swarm state string for chat system prompts."""
    if all_tasks is None:
        all_tasks = db.task_get_all()
    if all_agents is None:
        all_agents = db.agent_get_all()
    if projects is None:
        projects = db.project_get_all()

    pending_tasks = [t for t in all_tasks if t["status"] == "pending"]
    in_prog_tasks = [t for t in all_tasks if t["status"] == "in_progress"]
    failed_tasks  = [t for t in all_tasks if t["status"] == "failed"]
    active_agents = [a for a in all_agents if a["status"] == "active"]

    proj_summary = {}
    for t in all_tasks:
        p = t["project"]
        if p not in proj_summary:
            proj_summary[p] = {"pending": 0, "in_progress": 0, "failed": 0, "completed": 0}
        proj_summary[p][t["status"]] = proj_summary[p].get(t["status"], 0) + 1

    recent_failed = [a for a in all_agents if a.get("exit_code") not in (0, None)][-10:]
    failure_snippets = [
        f"  agent {a.get('id','')[:8]} ({a.get('project','')}/{a.get('task_id','')[:20]}): {(a.get('output') or '')[-200:].strip()}"
        for a in recent_failed
    ]

    return f"""CURRENT SWARM STATE ({datetime.now().strftime('%H:%M:%S')}):

Active agents ({len(active_agents)}):
{chr(10).join(f'  id={a.get("id","")[:12]} project={a.get("project","")} task={a.get("task_id","")[:30]}' for a in active_agents) or '  (none)'}

Pending tasks: {len(pending_tasks)}  In-progress: {len(in_prog_tasks)}  Failed: {len(failed_tasks)}

Per-project summary:
{chr(10).join(f'  {p}: pending={v["pending"]} in_progress={v["in_progress"]} failed={v["failed"]}' for p, v in sorted(proj_summary.items()) if p != '_swarm')}

Failed tasks (up to 10):
{chr(10).join(f'  {t["id"]} ({t["project"]}) att:{t.get("attempts",0)}/{t.get("max_attempts",3)}: {t["description"][:80]}' for t in failed_tasks[-10:]) or '  (none)'}

Recent agent failures:
{chr(10).join(failure_snippets) or '  (none)'}
"""


def _execute_chat_actions(action_blocks, db, config, config_file, _config_write_lock,
                          orchestrator, auto_mode_state):
    """Execute [ACTION] blocks and return a results summary string."""
    import swarm.orchestrator as _orch
    results = []
    for raw in action_blocks:
        try:
            act = json.loads(raw)
        except Exception as e:
            results.append(f"[ACTION parse error: {e}]")
            continue

        atype = act.get("type", "")

        if atype == "kill_agent":
            agent_id = act.get("agent_id", "")
            agent = db.agent_get(agent_id)
            if not agent:
                results.append(f"kill_agent: agent {agent_id!r} not found")
                continue
            killed = False
            handle = _orch._active_handles.get(agent_id)
            if handle and handle.get("process"):
                try:
                    handle["process"].kill()
                    killed = True
                except Exception:
                    pass
            if not killed:
                pid = agent.get("pid")
                if pid:
                    try:
                        os.kill(int(pid), 9)
                        killed = True
                    except Exception:
                        pass
            if killed:
                db.agent_update_status(agent_id, "killed")
                task_id = agent.get("task_id")
                if task_id:
                    task = db.task_get(task_id)
                    if task and task.get("status") == "in_progress":
                        db.task_update(task_id, {"status": "pending"})
                results.append(f"kill_agent: killed {agent_id[:12]} (task reset to pending)")
            else:
                results.append(f"kill_agent: could not kill {agent_id[:12]}")

        elif atype == "kill_all_agents":
            all_agents = db.agent_get_all()
            active = [a for a in all_agents if a.get("status") == "active"]
            killed_count = 0
            for agent in active:
                agent_id = agent["id"]
                killed = False
                handle = _orch._active_handles.get(agent_id)
                if handle and handle.get("process"):
                    try:
                        handle["process"].kill()
                        killed = True
                    except Exception:
                        pass
                if not killed:
                    pid = agent.get("pid")
                    if pid:
                        try:
                            os.kill(int(pid), 9)
                            killed = True
                        except Exception:
                            pass
                if killed:
                    db.agent_update_status(agent_id, "killed")
                    task_id = agent.get("task_id")
                    if task_id:
                        task = db.task_get(task_id)
                        if task and task.get("status") == "in_progress":
                            db.task_update(task_id, {"status": "pending"})
                    killed_count += 1
            results.append(f"kill_all_agents: killed {killed_count}/{len(active)} agents")

        elif atype == "create_task":
            project  = act.get("project", "")
            ttype    = act.get("task_type", "bug")
            desc     = act.get("description", "")
            priority = int(act.get("priority", 80 if ttype == "bug" else 50))
            deps     = act.get("dependencies", [])
            if not project or not desc:
                results.append("create_task: project and description required")
                continue
            task_id = f"{ttype}-{project}-chat-{uuid.uuid4().hex[:12]}"
            try:
                db.task_upsert({
                    "id": task_id, "project": project, "type": ttype,
                    "description": desc, "priority": priority,
                    "status": "pending", "attempts": 0, "max_attempts": 3,
                    "dependencies": chain_to_project_head(db, project, deps, task_id=task_id, ensure_head=True),
                    "metadata": {"created_by": "chat"},
                })
            except ValueError as e:
                results.append(f"create_task: invalid task payload ({e})")
                continue
            existing_project = db.project_get(project)
            if existing_project:
                db.project_upsert({**existing_project, "managed": True})
            # Auto-add project to managed_projects so it will be picked up
            if project not in orchestrator.MANAGED_PROJECTS:
                orchestrator.MANAGED_PROJECTS.append(project)
                config["managed_projects"] = orchestrator.MANAGED_PROJECTS
                try:
                    with _config_write_lock:
                        cfg = json.loads(config_file.read_text()) if config_file.exists() else {}
                        cfg["managed_projects"] = orchestrator.MANAGED_PROJECTS
                        config_file.write_text(json.dumps(cfg, indent=2) + "\n")
                except Exception as e:
                    print(f"[Chat] Warning: could not persist managed_projects: {e}")
            results.append(f"create_task: created {task_id}")

        elif atype == "set_auto_mode":
            enabled = bool(act.get("enabled", True))
            _orch.AUTO_MODE = enabled  # kept for backward compat (some tests check this)
            with auto_mode_state["lock"]:
                auto_mode_state["enabled"] = enabled
                if not enabled:
                    auto_mode_state["suspended_for_quota"] = False
            results.append(f"set_auto_mode: auto mode {'enabled' if enabled else 'disabled'}")

        elif atype == "restart_server":
            results.append("restart_server: restarting in 1s...")
            import threading as _t
            import sys as _sys
            def _do_restart():
                import time as _time
                _time.sleep(1)
                os.execv(_sys.executable, [_sys.executable] + _sys.argv)
            _t.Thread(target=_do_restart, daemon=True).start()

        else:
            results.append(f"unknown action type: {atype!r}")

    return "\n".join(results) if results else ""


def register_routes(app, config, config_file, _config_write_lock, orchestrator, workspace,
                    db, auto_mode_state, generate_task_script):
    """Register chat and project-creation routes on the Flask app."""

    import re as _re

    @app.route("/api/chat", methods=["POST"])
    def chat():
        """Conversational swarm manager — can observe state and execute actions.

        Request JSON:
          { "message": "...", "history": [{"role": "user"|"assistant", "content": "..."}] }

        Response JSON:
          { "response": "...", "history": [...], "actions_taken": [...] }

        The LLM may emit [ACTION]{...}[/ACTION] blocks to take actions.
        Supported action types:
          kill_agent        {"type":"kill_agent","agent_id":"<full-id>"}
          kill_all_agents   {"type":"kill_all_agents"}
          create_task       {"type":"create_task","project":"...","task_type":"bug","description":"...","priority":80,"dependencies":[]}
          set_auto_mode     {"type":"set_auto_mode","enabled":true|false}
          restart_server    {"type":"restart_server"}
        """
        data = request.get_json(force=True) or {}
        user_message = (data.get("message") or "").strip()
        history = data.get("history") or []
        if not user_message:
            return jsonify({"error": "message required"}), 400

        # Build live state snapshot
        try:
            state_snapshot = _build_state_snapshot(db)
        except Exception as e:
            state_snapshot = f"(Could not load swarm state: {e})"

        system_prompt = f"""You are the Swarm Controller manager — you can both observe the swarm and take actions on it.

CAPABILITIES:
- Answer questions about projects, tasks, agents, failures
- Kill specific agents or all agents
- Create bug tasks or feature tasks for any project
- Enable or disable auto mode
- Restart the server

HOW TO TAKE ACTIONS:
When you want to take an action, emit one or more [ACTION] blocks in your response (anywhere in the text). The server will execute them and inject results back.

Examples:
  [ACTION]{{"type":"kill_agent","agent_id":"full-agent-uuid-here"}}[/ACTION]
  [ACTION]{{"type":"kill_all_agents"}}[/ACTION]
  [ACTION]{{"type":"create_task","project":"chrome-rebellion","task_type":"bug","description":"Fix parse error in lab.gd","priority":80}}[/ACTION]
  [ACTION]{{"type":"set_auto_mode","enabled":false}}[/ACTION]
  [ACTION]{{"type":"restart_server"}}[/ACTION]

IMPORTANT RULES:
- Always confirm with the user before taking destructive actions (kill, restart). Ask "Shall I go ahead?" and only emit [ACTION] blocks after they confirm.
- Use the full agent ID from the state snapshot (not truncated).
- Be concise. Use plain text, minimal bullet points.
- For non-destructive actions (creating a bug task) you can act immediately without asking.
- After emitting actions, describe what you did in plain text after the [ACTION] block.

{state_snapshot}"""

        messages = list(history) + [{"role": "user", "content": user_message}]

        try:
            reply = _chat_call_llm(system_prompt, messages, config)
        except Exception as e:
            reply = f"(LLM error: {e})"

        # Parse and execute [ACTION] blocks
        action_pattern = _re.compile(r'\[ACTION\](.*?)\[/ACTION\]', _re.DOTALL)
        action_blocks = action_pattern.findall(reply)
        actions_taken = []
        if action_blocks:
            try:
                action_results = _execute_chat_actions(
                    action_blocks, db, config, config_file, _config_write_lock,
                    orchestrator, auto_mode_state,
                )
                actions_taken = action_blocks
                # If any task was created and auto mode is on, fill slots immediately
                has_create = any(
                    '"create_task"' in b or "'create_task'" in b for b in action_blocks
                )
                if has_create and auto_mode_state["enabled"]:
                    try:
                        orchestrator.fill_slots(generate_task_script)
                    except Exception:
                        pass
                # Strip the [ACTION] tags from the visible reply, append results
                clean_reply = action_pattern.sub('', reply).strip()
                if action_results:
                    reply = clean_reply + f"\n\n**Actions executed:**\n{action_results}"
                else:
                    reply = clean_reply
            except Exception as e:
                reply = reply + f"\n\n(Action execution error: {e})"

        updated_history = messages + [{"role": "assistant", "content": reply}]
        if len(updated_history) > 20:
            updated_history = updated_history[-20:]

        return jsonify({"response": reply, "history": updated_history, "actions_taken": actions_taken})

    @app.route("/api/project-chat", methods=["POST"])
    def project_chat():
        """Conversational project creation assistant.

        The LLM asks clarifying questions, builds the concept, then emits
        [CREATE_PROJECT]{"name": "...", "description": "..."}[/CREATE_PROJECT]
        when it has enough information. The server intercepts the tool call,
        queues the project_create task, and returns confirmation.
        """
        data = request.get_json(force=True) or {}
        user_message = (data.get("message") or "").strip()
        history = data.get("history") or []
        project_type = _normalize_project_type(data.get("project_type", "godot"))
        if not user_message:
            return jsonify({"error": "message required"}), 400

        existing_projects = list(db.project_get_all().keys())
        force_prd_generation = _project_chat_user_confirmed_generation(history, user_message)

        system_prompt = f"""You are a Godot game project designer. Your job is to gather requirements through iterative questions, then produce a structured PRD that the swarm agents will use to build the game.

EXISTING PROJECTS (avoid name conflicts): {', '.join(existing_projects) or 'none yet'}

YOUR PROCESS — THREE PHASES:

PHASE 1 — QUESTIONS: Ask 3-5 clarifying questions with lettered options, one set at a time.
PHASE 2 — PLAN PREVIEW: Once you have enough context, write a short plan preview with:
- project name and concept
- planned tasks
- an explicit dependency map per task
End with: "Want to change anything, or shall I generate the tasks?"
PHASE 3 — PRD GENERATION: Only when the user explicitly says to proceed (e.g. "yes", "go ahead", "create", "looks good"), emit the full [PRD] block.

FORMAT QUESTIONS LIKE THIS (always use lettered options so users can reply "1A, 2C"):
1. What is the core mechanic?
   A. Arcade action (fast reflexes, score attack)
   B. Puzzle / strategy (think before you act)
   C. Exploration / adventure
   D. Other: [specify]

ALWAYS ASK ABOUT QUALITY GATES (in Phase 1):
What quality checks should pass for each user story in the NEW project?
   A. No script errors on load (simplest — no test framework needed)
   B. GUT tests pass (headless Godot run) — the project bootstrap installs GUT automatically
   C. Other: [specify]

PHASE 2 PLAN SUMMARY EXAMPLE:
Here's the plan for **snake-game**:
- Project: snake-game (Godot 4, pixel art, top-down)
- US-001: Player movement (grid-based, 4 directions)
- US-002: Food spawning and snake growth
- US-003: Collision detection and game over
- US-004: Score display and high score
- US-005: Main menu and restart

Dependency map:
- US-001 Player movement -> []
- US-002 Food spawning and snake growth -> [US-001]
- US-003 Collision detection and game over -> [US-001, US-002]
- US-004 Score display and high score -> [US-002, US-003]
- US-005 Main menu and restart -> [US-003, US-004]

Quality gate: GUT tests pass.

Want to change anything, or shall I generate the tasks?

DEPENDENCY RULES (CRITICAL — apply inside the PRD):
- Use a DAG, NOT a chain. Most stories should run in parallel.
- Foundation stories (core scene, data model, foundational gameplay systems) come first; everything that builds on them fans out in parallel.
- Do NOT create stories for generic project bootstrap, GUT installation, harness installation, StateServer installation, or placeholder tests. New Godot projects get those automatically during project creation.
- WRONG: US-001 → US-002 → US-003 → US-004 (pure chain)
- RIGHT: US-001 → [US-002, US-003, US-004 in parallel] → US-005
- Add a `depends-on: US-NNN` line inside each story block that needs a predecessor. Use `depends-on: US-NNN, US-MMM` for multiple.
- Stories with no dependency get no depends-on line.
- The dependency map in Phase 2 and the `depends-on` lines in Phase 3 must describe the same graph.

PHASE 3 — ONLY when user confirms, emit this EXACT format (dependencies go INSIDE each story block):

[PRD]
# PRD: [Game Title]
project-name: kebab-case-slug

## Overview
[2-3 sentence description]

## Quality Gates
These must pass for every user story:
- [quality gate command]

## User Stories

### US-001: [Foundation title — no deps]
**Description:** As a player, I want [feature] so that [benefit].
**Acceptance Criteria:**
- [ ] [verifiable criterion]

### US-002: [Title that needs US-001]
depends-on: US-001
**Description:** As a player, I want [feature] so that [benefit].
**Acceptance Criteria:**
- [ ] [verifiable criterion]

### US-003: [Title that also needs US-001, parallel with US-002]
depends-on: US-001
**Description:** As a player, I want [feature] so that [benefit].
**Acceptance Criteria:**
- [ ] [verifiable criterion]

### US-004: [Title that needs US-002 and US-003]
depends-on: US-002, US-003
**Description:** As a player, I want [feature] so that [benefit].
**Acceptance Criteria:**
- [ ] [verifiable criterion]

## Non-Goals
- [what this will NOT include]

## Technical Notes
- Godot 4, GDScript
- GUT for testing (addons/gut/) is already installed for new Godot projects when applicable
[/PRD]

OTHER RULES:
- Never emit [PRD] before the user explicitly confirms in Phase 3
- In Phase 2, write plain text only — no [PRD] tags
- Stories should be small (one agent session each), 5-10 total
- [PRD] block must be the last thing in your message when emitted
- ONLY create tasks for the new project being designed. NEVER create tasks for other existing projects."""
        if project_type == "python":
            system_prompt = _python_project_chat_prompt(existing_projects)

        if force_prd_generation:
            system_prompt += """

PHASE OVERRIDE:
- The user has explicitly approved the current plan preview.
- Do NOT ask more questions.
- Do NOT restate the preview.
- Emit the final [PRD] block now as the last thing in your response.
"""

        messages = list(history) + [{"role": "user", "content": user_message}]

        # Call LLM
        try:
            reply = _chat_call_llm(system_prompt, messages, config)
        except Exception as e:
            return jsonify({"error": f"LLM error: {e}"}), 500

        # Check for PRD output — parse into task preview instead of treating it as plain chat.
        tasks_preview = None
        project_name = None
        overview = ""
        quality_gates: list[str] = []
        prd_content = _extract_prd_content(reply)
        if prd_content:
            try:
                preview_note = ""
                prd_stats = _prd_dependency_annotation_stats(prd_content)
                # Extract project-name
                name_match = _re.search(r'^project-name:\s*([a-z0-9-]+)', prd_content, _re.MULTILINE)
                project_name = name_match.group(1).strip() if name_match else f"project-{int(time.time())}"
                # Extract overview section
                ov_match = _re.search(r'##\s+Overview\s*\n(.*?)(?=\n##|\Z)', prd_content, _re.DOTALL)
                if ov_match:
                    overview = ov_match.group(1).strip()
                quality_gates = _extract_prd_quality_gates(prd_content)
                tasks_preview = _parse_prd_tasks_preview(prd_content, project_name)

                quality_errors = _task_graph_quality_errors(tasks_preview, project_type=project_type)
                if quality_errors:
                    repaired_preview, repair_rounds, repair_errors = _repair_task_graph_with_llm(
                        project_name,
                        overview,
                        tasks_preview,
                        quality_errors,
                        config,
                        project_type=project_type,
                    )
                    if repaired_preview:
                        tasks_preview = repaired_preview
                        preview_note = f"Refined the dependency graph automatically after {repair_rounds} validation round(s)."
                    else:
                        reply += (
                            "\n\nThe draft task graph is not valid enough to create yet:\n- "
                            + "\n- ".join(repair_errors[:6])
                            + "\n\nPlease refine the plan and regenerate dependencies."
                        )
                        tasks_preview = None
                        project_name = None

                if tasks_preview and project_name and prd_stats["stories"] >= 8 and prd_stats["stories_with_dep_lines"] < max(2, prd_stats["stories"] // 4):
                    sparse_note = (
                        f"Warning: the PRD only included explicit depends-on lines for "
                        f"{prd_stats['stories_with_dep_lines']} of {prd_stats['stories']} stories."
                    )
                    preview_note = f"{preview_note}\n{sparse_note}".strip()

                # Replace the model-authored PRD display with the authoritative
                # parsed preview so the visible graph matches tasks_preview exactly.
                if tasks_preview and project_name:
                    reply = _render_project_preview_from_tasks(project_name, overview, tasks_preview, note=preview_note)
                else:
                    reply = reply.replace('[PRD]', '').replace('[/PRD]', '').strip()
            except Exception as e:
                reply += f"\n\n(Error parsing PRD: {e})"

        updated_history = messages + [{"role": "assistant", "content": reply}]
        if len(updated_history) > 30:
            updated_history = updated_history[-30:]

        # Build overview string to pass through to create-project-tasks
        resp_overview = ""
        preview_diagnostics = None
        if tasks_preview and project_name:
            try:
                task_lines = "\n".join(
                    f"- {t['description'].split(chr(10))[0]}" for t in tasks_preview
                )
                resp_overview = (overview or "") + ("\n\n## Features\n" + task_lines if task_lines else "")
            except Exception:
                resp_overview = overview or ""
            preview_diagnostics = _graph_diagnostics_payload(tasks_preview)

        return jsonify({
            "response": reply,
            "history": updated_history,
            "tasks_preview": tasks_preview,
            "project_name": project_name,
            "project_type": project_type,
            "overview": resp_overview,
            "quality_gates": quality_gates,
            "design_doc": prd_content or "",
            "preview_diagnostics": preview_diagnostics,
        })

    @app.route("/api/project-ideas", methods=["POST"])
    def project_ideas():
        """Generate lightweight project ideas for the new-project chat."""
        data = request.get_json(force=True) or {}
        raw_kind = (data.get("kind") or "mixed").strip().lower()
        kind = raw_kind if raw_kind in {"mixed", "game", "programming"} else "mixed"
        history = data.get("history") or []
        existing_projects = list(db.project_get_all().keys())
        kind_instruction = {
            "game": "Generate game ideas only. Every idea must be a game concept.",
            "programming": (
                "Generate programming/software project ideas only. Every idea must be a software project, "
                "developer tool, automation, data tool, web/API service, or library. Do not include games, "
                "Godot projects, game mechanics, levels, enemies, or game vertical slices."
            ),
            "mixed": "Generate a mix: mostly games, plus a few programming/software project ideas.",
        }[kind]
        type_label = "Programming" if kind == "programming" else ("Game" if kind == "game" else "Game/Programming")
        system_prompt = f"""You generate concise project ideas for a human using a swarm project builder.

EXISTING PROJECTS (avoid name conflicts): {', '.join(existing_projects) or 'none yet'}

RULES:
- {kind_instruction}
- Return 8 ideas.
- Do not ask clarifying questions.
- Do not generate a PRD.
- Do not emit [PRD] tags or task lists.
- Keep each idea small enough for a first vertical slice.
- Include project name, type, one-sentence concept, and a tiny first slice.
- Prefer ideas that can later become a healthy dependency DAG with parallel branches.

FORMAT:
## Project Ideas
1. **kebab-case-name** — {type_label}
   Concept: ...
   First slice: ...
"""
        # Do not pass stale project-chat history here: a previous game greeting can
        # overpower the selected "programming" modality and make the idea list drift.
        messages = [{
            "role": "user",
            "content": f"Generate {kind} project ideas I can choose from.",
        }]
        try:
            reply = _chat_call_llm(system_prompt, messages, config)
        except Exception as e:
            return jsonify({"error": f"LLM error: {e}"}), 500

        updated_history = list(history) + [
            {"role": "user", "content": f"Generate {kind} project ideas."},
            {"role": "assistant", "content": reply},
        ]
        if len(updated_history) > 30:
            updated_history = updated_history[-30:]
        return jsonify({"ideas": reply, "history": updated_history})

    @app.route("/api/create-project-tasks", methods=["POST"])
    def create_project_tasks():
        """Create a project and its tasks from a confirmed PRD task list."""
        data = request.get_json(force=True) or {}
        project_name = data.get("project_name", "").strip()
        project_type = _normalize_project_type(data.get("project_type", "godot"))
        tasks = data.get("tasks", [])
        if not project_name:
            return jsonify({"error": "project_name required"}), 400
        if not tasks:
            return jsonify({"error": "tasks list is empty"}), 400

        project_path = Path(workspace) / project_name
        existing = db.project_get(project_name)
        if existing:
            return jsonify({
                "error": f"Project '{project_name}' already exists. New project chat cannot append a fresh graph onto an existing project.",
                "retryable": True,
                "chat_recovery_assistant": (
                    f"The project name `{project_name}` is already in use. "
                    "Choose a new project name or start a fresh new-project chat if this draft resumed an older project by mistake."
                ),
            }), 409
        overview_text = data.get("overview", "").strip()
        source_design_doc = (
            data.get("design_doc")
            or data.get("prd")
            or data.get("prd_text")
            or ""
        )
        quality_gates = data.get("quality_gates")
        created_tasks = []

        try:
            git_log, gitea_url = _scaffold_project_repo(
                project_name,
                project_path,
                project_type,
                overview_text,
                config,
            )
        except Exception as e:
            return jsonify({"error": f"Project scaffold failed: {e}"}), 400

        closure_proposal = propose_project_closure(
            project_name,
            project_path,
            project_type,
            context={
                "overview": overview_text,
                "quality_gates": quality_gates,
                "tasks": tasks,
                "design_doc": source_design_doc,
            },
        )

        # 4. Register project in DB and add to managed_projects
        cfg_path = Path(__file__).parent.parent / "config.json"
        if not existing:
            db.project_upsert({
                "name": project_name,
                "path": str(project_path),
                "file_extensions": _project_file_extensions(project_type),
                "files": {},
                "managed": True,
                "profile": closure_proposal["profile"],
                "closure_mode": closure_proposal["closure_spec"].get("mode", "build"),
                "closure_spec": closure_proposal["closure_spec"],
            })
        else:
            db.project_upsert({
                **existing,
                "managed": True,
                "file_extensions": _project_file_extensions(project_type),
                "profile": closure_proposal["profile"],
                "closure_mode": closure_proposal["closure_spec"].get("mode", "build"),
                "closure_spec": closure_proposal["closure_spec"],
            })
        head_id = ensure_project_head(db, project_name)
        if project_name not in config.get("managed_projects", []):
            config.setdefault("managed_projects", []).append(project_name)
            import swarm.orchestrator as _orch
            _orch.MANAGED_PROJECTS = config["managed_projects"]
            # Persist to config.json
            try:
                with _config_write_lock:
                    existing_cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
                    existing_cfg["managed_projects"] = config["managed_projects"]
                    cfg_path.write_text(json.dumps(existing_cfg, indent=2))
            except Exception as e:
                print(f"[Warning] Could not persist managed_projects: {e}")

        # Auto-scan so file sizes are populated immediately
        try:
            exts = tuple(_project_file_extensions(project_type))
            orchestrator.update_project_registry(exts)
        except Exception as e:
            print(f"[Warning] Auto-scan after project creation failed: {e}")

        def _delete_attempt_tasks(task_ids):
            for tid in task_ids:
                try:
                    db.task_delete(tid)
                except Exception:
                    pass

        current_tasks = [dict(t) for t in tasks]
        correction_rounds = 0
        creation_validation_errors = []
        max_correction_rounds = int(
            app.config.get(
                "PROJECT_CREATION_RETRY_ROUNDS_OVERRIDE",
                config.get("project_creation_retry_rounds", 2),
            )
        )

        for attempt in range(max_correction_rounds + 1):
            created_tasks = []
            pending_batch = []
            for t in current_tasks:
                task_id = t.get("id", f"{project_name}-{uuid.uuid4().hex[:12]}")
                pending_batch.append({
                    "id": task_id,
                    "project": project_name,
                    "type": t.get("type", "feature"),
                    "priority": t.get("priority", 50),
                    "max_attempts": t.get("max_attempts", 3),
                    "description": t.get("description", ""),
                    "dependencies": list(t.get("dependencies", []) or []),
                    "metadata": {"prd_generated": True, "project_head_id": head_id},
                })
            for task in anchor_project_batch_roots(pending_batch, head_id):
                try:
                    db.task_upsert(task)
                except ValueError as e:
                    cycle_errors = [f"Invalid dependency graph in task list: {e}"]
                    repair_context = _render_graph_repair_context(
                        current_tasks,
                        cycle_errors,
                        allowed_external_roots=[head_id],
                    )
                    _delete_attempt_tasks([x["id"] for x in created_tasks])
                    _cleanup_failed_project_creation(
                        db,
                        project_name,
                        project_path,
                        config,
                        config_file=cfg_path,
                        config_write_lock=_config_write_lock,
                    )
                    return jsonify({
                        "error": "Project creation validation failed",
                        "details": cycle_errors,
                        "retryable": True,
                        "graph_diagnostics": _graph_diagnostics_payload(
                            current_tasks,
                            cycle_errors,
                            allowed_external_roots=[head_id],
                        ),
                        "chat_recovery_assistant": (
                            "The generated dependency graph contains a cycle, so the project was not created. "
                            "Tell me which task should come first or which stories should run in parallel, and I will regenerate the plan."
                            "\n\nLatest validation issues:\n- "
                            + "\n- ".join(cycle_errors[:8])
                            + "\n\nGraph diagnostics:\n"
                            + repair_context
                        ),
                        "task_id": task["id"],
                    }), 400
                created_tasks.append(task)

            creation_validation_errors = _project_creation_validation_errors(
                project_path,
                created_tasks,
                allowed_external_roots=[head_id],
                project_type=project_type,
            )
            # Defensive check: re-read exactly what was persisted so star/fan
            # regressions cannot slip through if preview-time state diverges.
            persisted_tasks = [
                db.task_get(task["id"]) or task
                for task in created_tasks
            ]
            persisted_validation_errors = _project_creation_validation_errors(
                project_path,
                persisted_tasks,
                allowed_external_roots=[head_id],
                project_type=project_type,
            )
            if persisted_validation_errors:
                creation_validation_errors = list(dict.fromkeys(
                    list(creation_validation_errors or []) + list(persisted_validation_errors)
                ))
            if not creation_validation_errors:
                break

            _delete_attempt_tasks([t["id"] for t in created_tasks])
            created_tasks = []
            if attempt >= max_correction_rounds:
                break

            repaired_tasks, _, repair_errors = _repair_task_graph_with_llm(
                project_name,
                overview_text,
                current_tasks,
                creation_validation_errors,
                config,
                project_type=project_type,
            )
            if not repaired_tasks:
                creation_validation_errors = repair_errors or creation_validation_errors
                break
            current_tasks = repaired_tasks
            correction_rounds += 1

        if creation_validation_errors:
            repair_context = _render_graph_repair_context(
                current_tasks,
                creation_validation_errors,
                allowed_external_roots=[head_id],
            )
            retry_note = (
                "Automatic project-graph repair could not produce a valid dependency DAG. "
                "Tell me which systems should unlock others or which tasks should converge, and I will regenerate the plan."
            )
            _cleanup_failed_project_creation(
                db,
                project_name,
                project_path,
                config,
                config_file=cfg_path,
                config_write_lock=_config_write_lock,
            )
            return jsonify({
                "error": "Project creation validation failed",
                "details": creation_validation_errors,
                "retryable": True,
                "graph_diagnostics": _graph_diagnostics_payload(
                    current_tasks,
                    creation_validation_errors,
                    allowed_external_roots=[head_id],
                ),
                "chat_recovery_assistant": (
                    f"{retry_note}\n\nLatest validation issues:\n- "
                    + "\n- ".join(creation_validation_errors[:8])
                    + "\n\nGraph diagnostics:\n"
                    + repair_context
                ),
            }), 400

        try:
            _write_project_design_doc(
                project_path,
                project_name,
                overview_text,
                created_tasks,
                project_type,
                quality_gates=quality_gates,
                source_doc=source_design_doc,
            )
        except Exception as e:
            _cleanup_failed_project_creation(
                db,
                project_name,
                project_path,
                config,
                created_task_ids=[t["id"] for t in created_tasks],
                config_file=cfg_path,
                config_write_lock=_config_write_lock,
            )
            return jsonify({"error": f"Project design doc generation failed: {e}"}), 400

        try:
            write_project_closure_doc(project_path, project_name, closure_proposal)
        except Exception as e:
            _cleanup_failed_project_creation(
                db,
                project_name,
                project_path,
                config,
                created_task_ids=[t["id"] for t in created_tasks],
                config_file=cfg_path,
                config_write_lock=_config_write_lock,
            )
            return jsonify({"error": f"Project closure doc generation failed: {e}"}), 400

        return jsonify({
            "created": len(created_tasks),
            "project": project_name,
            "project_type": project_type,
            "closure_proposal": closure_proposal,
            "task_ids": [t["id"] for t in created_tasks],
            "git_log": git_log,
            "gitea_url": gitea_url,
            "correction_rounds": correction_rounds,
        })

    @app.route("/api/projects/<project_name>/append-phase", methods=["POST"])
    def append_project_phase(project_name):
        """Append a validated task DAG phase to an existing project."""
        data = request.get_json(force=True) or {}
        phase_name = (data.get("phase_name") or data.get("name") or "Appended Phase").strip()
        overview_text = (data.get("overview") or "").strip()
        project_type = _normalize_project_type(data.get("project_type") or "")
        tasks = data.get("tasks", [])

        project = db.project_get(project_name)
        if not project:
            return jsonify({"error": f"Project '{project_name}' not found"}), 404
        if not project_type:
            project_type = _normalize_project_type(project.get("profile") or "godot")

        project_path = Path(workspace) / project_name
        if not project_path.exists():
            return jsonify({"error": f"Project path not found: {project_path}"}), 404

        anchor_task_id = (data.get("anchor_task_id") or "").strip() or ensure_project_head(db, project_name)
        if not anchor_task_id:
            return jsonify({"error": "Could not determine project phase anchor"}), 400
        anchor_task = db.task_get(anchor_task_id) or db.task_get_completed_record(anchor_task_id)
        if not anchor_task or anchor_task.get("project") != project_name:
            return jsonify({"error": f"Invalid anchor_task_id '{anchor_task_id}' for project '{project_name}'"}), 400

        pending_batch = []
        batch_ids: set[str] = set()
        boundary_anchor_id = anchor_task_id
        use_phase_qa = bool(data.get("qa_before_phase") or data.get("phase_qa"))
        use_phase_gate = bool(data.get("phase_gate") or data.get("pause_before_phase"))
        if not tasks and not (use_phase_qa or use_phase_gate):
            return jsonify({"error": "tasks list is empty"}), 400
        phase_qa_id = (data.get("phase_qa_id") or f"{project_name}-{_re.sub(r'[^a-z0-9]+', '-', phase_name.lower()).strip('-')}-qa").strip()
        if use_phase_qa:
            if db.task_get(phase_qa_id) or db.task_get_completed_record(phase_qa_id):
                return jsonify({"error": f"Phase QA id already exists: {phase_qa_id}"}), 400
            qa_type = data.get("phase_qa_type") or data.get("qa_task_type") or "harness_qa"
            batch_ids.add(phase_qa_id)
            pending_batch.append({
                "id": phase_qa_id,
                "project": project_name,
                "type": qa_type,
                "priority": 90,
                "max_attempts": 3,
                "description": (
                    f"Phase QA checkpoint: {phase_name}\n\n"
                    "Run QA/signoff against the current project state before the next phase unlocks. "
                    "File bugs for failures and complete only when the phase boundary is acceptable."
                ),
                "dependencies": [anchor_task_id],
                "metadata": {
                    "phase_name": phase_name,
                    "phase_anchor_task_id": anchor_task_id,
                    "phase_boundary": True,
                },
            })
            boundary_anchor_id = phase_qa_id

        phase_gate_id = (data.get("phase_gate_id") or f"{project_name}-{_re.sub(r'[^a-z0-9]+', '-', phase_name.lower()).strip('-')}-gate").strip()
        if use_phase_gate:
            if db.task_get(phase_gate_id) or db.task_get_completed_record(phase_gate_id):
                return jsonify({"error": f"Phase gate id already exists: {phase_gate_id}"}), 400
            batch_ids.add(phase_gate_id)
            pending_batch.append({
                "id": phase_gate_id,
                "project": project_name,
                "type": "phase_gate",
                "priority": 100,
                "max_attempts": 1,
                "description": (
                    f"Phase gate: {phase_name}\n\n"
                    "This task intentionally pauses the project graph. "
                    "Release it explicitly when testing/signoff is complete."
                ),
                "dependencies": [boundary_anchor_id],
                "metadata": {
                    "phase_name": phase_name,
                    "phase_anchor_task_id": anchor_task_id,
                    "phase_boundary_dep": boundary_anchor_id,
                    "release_endpoint": f"/api/projects/{project_name}/phase-gates/{phase_gate_id}/release",
                },
            })
            boundary_anchor_id = phase_gate_id

        for t in tasks:
            if not isinstance(t, dict):
                return jsonify({"error": "Each task must be an object"}), 400
            task_id = t.get("id") or f"{project_name}-phase-{uuid.uuid4().hex[:12]}"
            if db.task_get(task_id) or db.task_get_completed_record(task_id) or task_id in batch_ids:
                return jsonify({"error": f"Task id already exists or is duplicated: {task_id}"}), 400
            batch_ids.add(task_id)
            pending_batch.append({
                "id": task_id,
                "project": project_name,
                "type": t.get("type", "feature"),
                "priority": t.get("priority", 50),
                "max_attempts": t.get("max_attempts", 3),
                "description": t.get("description", ""),
                "dependencies": list(t.get("dependencies", []) or []),
                "metadata": {
                    **(t.get("metadata") or {}),
                    "prd_generated": True,
                    "phase_name": phase_name,
                    "phase_anchor_task_id": anchor_task_id,
                },
            })

        for task in pending_batch:
            for dep in list(task.get("dependencies") or []):
                if dep in batch_ids or dep == anchor_task_id:
                    continue
                existing = db.task_get(dep) or db.task_get_completed_record(dep)
                if not existing:
                    return jsonify({"error": f"Unknown dependency '{dep}' for task '{task['id']}'"}), 400
                if existing.get("project") != project_name:
                    return jsonify({"error": f"Dependency '{dep}' belongs to another project"}), 400

        created_tasks = anchor_project_batch_roots(pending_batch, boundary_anchor_id)
        for task in created_tasks:
            if task["id"] == phase_qa_id and use_phase_qa:
                task["dependencies"] = [anchor_task_id]
            elif task["id"] == phase_gate_id and use_phase_gate:
                task["dependencies"] = [phase_qa_id if use_phase_qa else anchor_task_id]
        validation_errors = _project_creation_validation_errors(
            project_path,
            created_tasks,
            allowed_external_roots=[anchor_task_id],
            project_type=project_type,
        )
        if validation_errors:
            return jsonify({
                "error": "Project phase validation failed",
                "details": validation_errors,
                "graph_diagnostics": _graph_diagnostics_payload(
                    created_tasks,
                    validation_errors,
                    allowed_external_roots=[anchor_task_id],
                ),
            }), 400

        added: list[str] = []
        try:
            for task in created_tasks:
                db.task_upsert(task)
                added.append(task["id"])

            depended_on = {
                dep
                for task in created_tasks
                for dep in (task.get("dependencies") or [])
                if dep in batch_ids
            }
            leaves = [task for task in created_tasks if task["id"] not in depended_on]
            requested_head = (data.get("head_task_id") or "").strip()
            if requested_head:
                new_head = requested_head
            elif leaves:
                new_head = leaves[-1]["id"]
            else:
                new_head = created_tasks[-1]["id"]
            if new_head not in batch_ids:
                return jsonify({"error": "head_task_id must be one of the appended tasks"}), 400
            set_project_head(db, project_name, new_head)

            _append_project_design_phase(
                project_path,
                project_name,
                phase_name,
                overview_text,
                created_tasks,
                project_type,
                anchor_task_id,
            )
        except Exception as e:
            for task_id in reversed(added):
                try:
                    db.task_delete(task_id)
                except Exception:
                    pass
            return jsonify({"error": f"Append phase failed: {e}"}), 400

        return jsonify({
            "created": len(created_tasks),
            "project": project_name,
            "project_type": project_type,
            "phase_name": phase_name,
            "anchor_task_id": anchor_task_id,
            "phase_qa_id": phase_qa_id if use_phase_qa else None,
            "phase_gate_id": phase_gate_id if use_phase_gate else None,
            "head_task_id": new_head,
            "task_ids": [t["id"] for t in created_tasks],
        })

    @app.route("/api/projects/<project_name>/phase-gates/<gate_id>/release", methods=["POST"])
    def release_project_phase_gate(project_name, gate_id):
        """Release a phase gate so downstream appended-phase tasks can run."""
        task = db.task_get(gate_id)
        if not task:
            return jsonify({"error": f"Phase gate '{gate_id}' not found"}), 404
        if task.get("project") != project_name:
            return jsonify({"error": f"Phase gate '{gate_id}' does not belong to project '{project_name}'"}), 400
        if task.get("type") != "phase_gate":
            return jsonify({"error": f"Task '{gate_id}' is not a phase_gate"}), 400
        if task.get("status") == "completed":
            return jsonify({"released": True, "task": task})
        if task.get("status") != "pending":
            return jsonify({"error": f"Phase gate '{gate_id}' is not pending", "status": task.get("status")}), 409

        db.task_update_status(gate_id, "completed", completed=datetime.now().isoformat())
        released = db.task_get(gate_id) or {**task, "status": "completed"}
        return jsonify({"released": True, "task": released})
