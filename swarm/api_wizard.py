"""Project wizard route handlers for the Swarm API.

Routes: POST /api/wizard/plan, POST /api/wizard/create
"""

import json
import os
from pathlib import Path

from flask import jsonify, request

from swarm.api_chat import (
    _cleanup_failed_project_creation,
    _normalize_project_type,
    _project_file_extensions,
    _project_creation_validation_errors,
    _scaffold_project_repo,
)
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
                    entry["error"] = f"create failed: {result['error']}"
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
- Use a DAG (directed acyclic graph), NOT a chain. Most tasks should run in parallel.
- A task only depends on another if it literally cannot start without that task's output.
- Ask: "Can this task be worked on at the same time as another?" If yes, they should be parallel (no dependency between them).
- Foundation tasks (data models, core loop, base classes) come first; everything that builds on them fans out in parallel.
- Integration/wiring tasks come last and depend on the systems they connect.
- WRONG: A → B → C → D → E (pure chain, wastes parallelism)
- RIGHT: A → [B, C, D in parallel] → E (fan out, then converge)

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
