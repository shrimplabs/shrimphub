"""Project wizard route handlers for the Swarm API.

Routes: POST /api/wizard/plan, POST /api/wizard/create
"""

import json
import os
from pathlib import Path

from flask import jsonify, request

from swarm.task_chains import anchor_project_batch_roots, chain_to_project_head, ensure_project_head


def _llm_call(prompt: str, system: str, config: dict) -> str:
    """Minimal LLM call using the currently configured provider."""
    import requests as _req
    provider_name = config.get("llm_provider", "minimax")
    from swarm_runner import LLM_PROVIDERS as _providers
    cfg = dict(_providers.get(provider_name, _providers.get("minimax", {})))
    api_key_env = cfg.get("api_key_env", "MINIMAX_API_KEY")
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise ValueError(f"{api_key_env} not set")
    base_url = cfg.get("base_url", "https://api.minimax.io/anthropic/v1").rstrip("/")
    model = cfg.get("model", "MiniMax-M2.7")
    fmt = cfg.get("format", "anthropic")
    max_tok = cfg.get("max_tokens", 8096)
    messages = [{"role": "user", "content": prompt}]
    if fmt == "anthropic_native":
        url = f"{base_url}/messages"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        body = {"model": model, "max_tokens": max_tok, "system": system, "messages": messages}
    elif fmt == "openai":
        url = f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {"model": model, "max_tokens": max_tok,
                "messages": [{"role": "system", "content": system}] + messages}
    else:
        url = f"{base_url}/messages"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {"model": model, "max_tokens": max_tok, "system": system, "messages": messages}
        group_id = os.environ.get("MINIMAX_GROUP_ID", "")
        if group_id:
            body["group_id"] = group_id
    resp = _req.post(url, headers=headers, json=body, timeout=180)
    resp.raise_for_status()
    data = resp.json()
    if "content" in data:
        return "".join(item.get("text", "") for item in data["content"] if item.get("type") == "text")
    return data.get("choices", [{}])[0].get("message", {}).get("content", "")


def register_routes(app, config, config_file, _config_write_lock, orchestrator, db):
    """Register project wizard routes on the Flask app."""

    from datetime import datetime

    @app.route("/api/wizard/imagine", methods=["POST"])
    def wizard_imagine():
        """Ask the LLM to invent a project concept from scratch."""
        data = request.json or {}
        project_type = _normalize_project_type(data.get("project_type", "godot"))
        hint = data.get("hint", "").strip()  # optional creative nudge, e.g. "something weird"

        type_hints = {
            "godot":      "a Godot 4 game (GDScript)",
            "python":     "a Python application or tool",
            "typescript": "a TypeScript/Node application",
            "other":      "a software project",
        }
        type_desc = type_hints.get(project_type, type_hints["other"])

        system = (
            "You are a wildly creative game designer and software inventor. "
            "You output ONLY valid JSON — no markdown, no explanation outside the JSON."
        )
        hint_line = f"\nCreative nudge from the human: {hint}" if hint else ""
        prompt = f"""Invent a completely original concept for {type_desc}.{hint_line}

You have total creative freedom. No scope limits — if you want to design an MMO, a physics sandbox,
a generative art tool, or something that has never been built before, go for it.

Return a JSON object with this exact structure:
{{
  "project_name": "slug-style-name-no-spaces",
  "display_name": "Human Readable Title",
  "project_type": "{project_type}",
  "description": "2-4 sentences describing the concept, core mechanics, and what makes it interesting",
  "genre": "one word or short phrase",
  "ambition_level": "small | medium | large | massive"
}}

Rules:
- project_name must be lowercase, hyphens only, no spaces, 2-4 words
- Be genuinely creative — avoid the most obvious ideas (simple snake, basic platformer, todo app)
- description should be specific enough that an AI agent can start building immediately
- Output ONLY the JSON object, nothing else"""

        try:
            raw = _llm_call(prompt, system, config)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            concept = json.loads(raw)
            return jsonify(concept)
        except json.JSONDecodeError as e:
            return jsonify({"error": f"LLM returned invalid JSON: {e}", "raw": raw[:500]}), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/wizard/create-instant", methods=["POST"])
    def wizard_create_instant():
        """Imagine + plan + create in one shot. No human review step.

        Body (all optional):
          project_type  — godot | python | typescript | other (default: godot)
          hint          — creative nudge passed to the imagine step
          count         — create N projects (default: 1, max: 50)
          min_tasks     — passed to planner (default: 10)
          max_tasks     — passed to planner (default: 80)
        """
        import uuid as _uuid
        data = request.json or {}
        project_type = _normalize_project_type(data.get("project_type", "godot"))
        hint = data.get("hint", "").strip()
        count = max(1, min(int(data.get("count", 1)), 50))
        min_tasks = int(data.get("min_tasks", 10))
        max_tasks = int(data.get("max_tasks", 80))

        results = []
        for i in range(count):
            entry = {"index": i + 1}
            try:
                # Step 1: Imagine
                imagine_resp = app.test_client().post(
                    "/api/wizard/imagine",
                    json={"project_type": project_type, "hint": hint},
                )
                concept = json.loads(imagine_resp.data)
                if "error" in concept:
                    entry["error"] = f"imagine failed: {concept['error']}"
                    results.append(entry)
                    continue

                project_name = concept["project_name"]
                description = concept["description"]
                entry["project_name"] = project_name
                entry["concept"] = concept
                print(f"[Instant] ({i+1}/{count}) Conceived: {project_name} — {concept.get('genre','')}")

                # Step 2: Plan
                plan_resp = app.test_client().post(
                    "/api/wizard/plan",
                    json={
                        "project_name": project_name,
                        "project_type": project_type,
                        "description": description,
                        "min_tasks": min_tasks,
                        "max_tasks": max_tasks,
                    },
                )
                plan = json.loads(plan_resp.data)
                if "error" in plan:
                    entry["error"] = f"plan failed: {plan['error']}"
                    results.append(entry)
                    continue

                tasks = plan.get("tasks", [])
                entry["tasks_planned"] = len(tasks)
                print(f"[Instant] ({i+1}/{count}) Planned {len(tasks)} tasks for {project_name}")

                # Step 3: Create
                create_resp = app.test_client().post(
                    "/api/wizard/create",
                    json={
                        "project_name": project_name,
                        "project_type": project_type,
                        "notes": description,
                        "tasks": tasks,
                    },
                )
                result = json.loads(create_resp.data)
                if "error" in result:
                    details = result.get("details", [])
                    detail_str = "; ".join(details) if details else ""
                    entry["error"] = f"create failed: {result['error']}" + (f" — {detail_str}" if detail_str else "")
                    results.append(entry)
                    continue

                entry["tasks_created"] = result.get("tasks_created", 0)
                entry["gitea_url"] = result.get("gitea_url")
                entry["success"] = True
                print(f"[Instant] ({i+1}/{count}) Created {project_name} with {entry['tasks_created']} tasks")

            except Exception as e:
                entry["error"] = str(e)

            results.append(entry)

        successful = [r for r in results if r.get("success")]
        return jsonify({
            "requested": count,
            "created": len(successful),
            "results": results,
        })

    @app.route("/api/wizard/plan", methods=["POST"])
    def wizard_plan():
        """Call the LLM to generate a task plan for a new project."""
        data = request.json or {}
        project_name = data.get("project_name", "").strip()
        project_type = _normalize_project_type(data.get("project_type", "godot"))
        description  = data.get("description", "").strip()
        min_tasks    = int(data.get("min_tasks", 10))
        max_tasks    = int(data.get("max_tasks", 80))
        if not project_name or not description:
            return jsonify({"error": "project_name and description required"}), 400

        type_hints = {
            "godot":      "Godot 4 game (GDScript). Tasks use type: feature/bug/refactor/polish.",
            "python":     "Python project. Tasks use type: feature/bug/refactor.",
            "typescript": "TypeScript/Node project. Tasks use type: feature/bug/refactor.",
            "other":      "Software project. Tasks use type: feature/bug/refactor.",
        }
        type_hint = type_hints.get(project_type, type_hints["other"])

        system = (
            "You are a senior software architect helping plan a development project. "
            "You output ONLY valid JSON — no markdown, no explanation outside the JSON."
        )
        prompt = f"""Plan the full implementation of this project as a comprehensive, atomic list of tasks for autonomous AI agents.

Project name: {project_name}
Project type: {type_hint}
Goal: {description}

Return a JSON object with this exact structure:
{{
  "tasks": [
    {{
      "type": "feature",
      "priority": 50,
      "description": "Concise, actionable description of what the agent should implement",
      "depends_on": []
    }}
  ]
}}

ATOMICITY RULES — each task must be a single, self-contained unit of work:
- One task = one logical concern (one script, one scene, one system, one mechanic)
- NEVER bundle implementation + tests in one task — always split into separate tasks with a dependency
- NEVER combine two distinct systems in one task — any "and" or "+" in a description is a split signal
- One task should touch at most 2–3 files
- Target size: what one agent can do in ~30 tool loops

DEPENDENCY GRAPH RULES:
- Use a DAG (directed acyclic graph) with meaningful branching AND convergence.
- A task only depends on another if it literally cannot start without that task's output.
- Foundation tasks (data models, core loop, base classes) come first; systems that build on them fan out in parallel.
- Integration/wiring tasks come last and depend on the systems they connect.
- WRONG: A → B → C → D → E (pure chain, wastes parallelism)
- WRONG: A, B, C, D, E (all roots — no sequencing, everything launches at once)
- RIGHT: A → [B, C, D in parallel] → E (fan out, then converge)
- RULE: No more than half of all tasks should be root tasks (no dependencies). Most tasks should depend on something.

SPLITTING HEURISTICS:
- "Create X and write tests" → Task 1: Create X / Task 2: Write tests (depends on Task 1)
- "Create scene, script, and wire up" → Task 1: Script / Task 2: Scene (depends on Task 1) / Task 3: Wire up (depends on Task 2)
- "Fix X + add Y" → Task 1: Fix X / Task 2: Add Y (no dependency — independent systems)
- "Game needs: player, enemies, items, HUD" →
    Task 0: Core game loop (foundation)
    Task 1: Player system (depends on 0)
    Task 2: Enemy system (depends on 0)      ← parallel with Task 1
    Task 3: Item system (depends on 0)       ← parallel with Tasks 1 and 2
    Task 4: HUD (depends on 0)               ← parallel with Tasks 1, 2, 3
    Task 5: Wire up all systems (depends on 1, 2, 3, 4)
- "Implement A and B" → Task 1: Implement A / Task 2: Implement B (parallel, no dependency unless A's output feeds B)

SCOPE: Break the entire project into ALL the tasks needed to fully implement it, from foundational systems to polish. Generate between {min_tasks} and {max_tasks} tasks. Do not summarize or abbreviate — enumerate every distinct piece of work.

TASK TYPES:
- feature: new functionality
- bug: fix a defect
- refactor: improve structure without changing behavior
- polish: UX, animations, sound, visual feedback

Rules:
- type must be one of: feature, bug, refactor, polish
- priority: 80=critical/foundational, 60=important, 50=normal, 40=nice-to-have/polish
- depends_on: list of 0-based indices of tasks this task depends on (e.g. [0, 1])
- Each description must be specific and actionable — one or two sentences the agent can act on directly
- No setup tasks (assume repo already exists); focus on actual implementation work
- Output ONLY the JSON object, nothing else"""

        try:
            raw = _llm_call(prompt, system, config)
            # Extract JSON from response (strip any accidental markdown fences)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            plan = json.loads(raw)
            tasks = plan.get("tasks", [])
            return jsonify({"tasks": tasks})
        except json.JSONDecodeError as e:
            return jsonify({"error": f"LLM returned invalid JSON: {e}", "raw": raw[:500]}), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/wizard/create", methods=["POST"])
    def wizard_create():
        """Create a project and its tasks from the wizard output."""
        import uuid as _uuid
        data = request.json or {}
        project_name = data.get("project_name", "").strip()
        project_type = _normalize_project_type(data.get("project_type", "godot"))
        notes        = data.get("notes", "").strip()
        tasks_in     = data.get("tasks", [])

        if not project_name:
            return jsonify({"error": "project_name required"}), 400
        if not tasks_in:
            return jsonify({"error": "at least one task required"}), 400

        # Build a project notes artifact from notes + task titles and store it in project metadata.
        task_titles = "\n".join(f"- {t.get('description','').split(chr(10))[0]}" for t in tasks_in)
        notes_title = "Game Design" if project_type == "godot" else "Project Brief"
        game_design_notes = f"# {project_name} — {notes_title}\n\n"
        if notes:
            game_design_notes += f"{notes}\n\n"
        game_design_notes += f"## Planned Features\n{task_titles}\n"

        workspace = Path(config.get("workspace", "."))
        project_path = workspace / project_name
        try:
            git_log, gitea_url = _scaffold_project_repo(
                project_name,
                project_path,
                project_type,
                game_design_notes,
                config,
            )
        except Exception as e:
            return jsonify({"error": f"Project scaffold failed: {e}"}), 400

        # Create or ensure project exists
        existing = db.project_get(project_name)
        if not existing:
            db.project_upsert({"name": project_name, "status": "active", "path": str(project_path), "file_extensions": _project_file_extensions(project_type),
                               "managed": True, "profile": project_type, "notes": game_design_notes})
        else:
            db.project_upsert({**existing, "managed": True, "file_extensions": _project_file_extensions(project_type), "profile": project_type})
            db.project_set_notes(project_name, game_design_notes)
        head_id = ensure_project_head(db, project_name)

        # Add to managed_projects if not already there, and persist to config.json
        if project_name not in orchestrator.MANAGED_PROJECTS:
            orchestrator.MANAGED_PROJECTS.append(project_name)
            config["managed_projects"] = orchestrator.MANAGED_PROJECTS
            try:
                with _config_write_lock:
                    cfg = json.loads(config_file.read_text()) if config_file.exists() else {}
                    cfg["managed_projects"] = orchestrator.MANAGED_PROJECTS
                    config_file.write_text(json.dumps(cfg, indent=2) + "\n")
            except Exception as e:
                print(f"[Wizard] Warning: could not persist managed_projects to config.json: {e}")

        # Auto-scan the new project so file sizes are populated immediately
        try:
            exts = tuple(_project_file_extensions(project_type))
            orchestrator.update_project_registry(exts)
        except Exception as e:
            print(f"[Wizard] Warning: auto-scan failed: {e}")

        # Assign stable IDs and resolve depends_on indices → task IDs
        task_ids = [f"{project_name}-t{i+1}-{_uuid.uuid4().hex[:4]}" for i in range(len(tasks_in))]
        pending_batch = []
        for i, t in enumerate(tasks_in):
            raw_deps = t.get("depends_on", []) or t.get("dependencies", [])
            deps = []
            for d in raw_deps:
                if isinstance(d, int) and 0 <= d < len(task_ids) and d != i:
                    deps.append(task_ids[d])
                elif isinstance(d, str) and d in task_ids:
                    deps.append(d)
            pending_batch.append({
                "id": task_ids[i],
                "project": project_name,
                "type": t.get("type", "feature"),
                "priority": int(t.get("priority", 50)),
                "description": t.get("description", "").strip(),
                "status": "pending",
                "attempts": 0,
                "max_attempts": 3,
                "dependencies": deps,
                "metadata": {"wizard_created": True, "project_head_id": head_id},
                "created": datetime.now().isoformat(),
            })
        created = []
        for task in anchor_project_batch_roots(pending_batch, head_id):
            try:
                db.task_upsert(task)
            except ValueError as e:
                _cleanup_failed_project_creation(
                    db, project_name, project_path, config,
                    created_task_ids=[t["id"] for t in created],
                    config_file=config_file,
                    config_write_lock=_config_write_lock,
                )
                return jsonify({"error": f"Invalid dependency graph in task list: {e}"}), 400
            created.append(task)

        validation_errors = _project_creation_validation_errors(
            project_path,
            created,
            allowed_external_roots=[head_id],
            project_type=project_type,
        )
        if validation_errors:
            _cleanup_failed_project_creation(
                db, project_name, project_path, config,
                created_task_ids=[t["id"] for t in created],
                config_file=config_file,
                config_write_lock=_config_write_lock,
            )
            return jsonify({"error": "Project creation validation failed", "details": validation_errors}), 400

        return jsonify({"project": project_name, "tasks_created": len(created),
                        "task_ids": task_ids, "git_log": git_log, "gitea_url": gitea_url})
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
    from swarm.api_chat import requests as _requests
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
                resp = _requests.post(url, headers=headers, json=body, stream=True, timeout=(10, 300))
                resp.raise_for_status()
                return _parse_sse_stream(resp)
            resp = _requests.post(url, headers=headers, json=body, timeout=(10, 180))
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


