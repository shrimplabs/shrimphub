"""Tasks route handlers for the Swarm API."""

from flask import jsonify, request

import json
from datetime import datetime
from pathlib import Path
import shutil
import time

from swarm.task_chains import chain_to_project_head, ensure_project_head
from swarm.task_mutations import reset_task_to_pending


def register_routes(app, task_source, db, workspace):
    """Register routes on the Flask app."""

    _repo_root = Path(__file__).resolve().parent.parent
    _canonical_harness = _repo_root / "templates" / "godot" / "autoload" / "test_harness.gd"
    _required_harness_markers = (
        "Synchronous checkpoint protocol for AI-driven testing.",
        'func checkpoint(state: Dictionary) -> Dictionary:',
        '"goto_scene"',
        '"input_action"',
        '"press_button"',
        '"screenshot"',
        "DEFAULT_PORT := 11010",
    )

    def _normalize_dependencies(raw):
        """Return a deduped list of dependency IDs or None on invalid input."""
        if raw is None:
            return []
        if isinstance(raw, str):
            deps_str = raw.strip()
            if not deps_str:
                return []
            if deps_str.startswith("["):
                try:
                    raw = json.loads(deps_str)
                except Exception:
                    raw = [d.strip().strip('"\'') for d in deps_str.strip("[]").split(",") if d.strip().strip('"\'')]
            else:
                raw = [d.strip().strip('"\'') for d in deps_str.split(",") if d.strip()]
        if not isinstance(raw, list):
            return None

        out = []
        seen = set()
        for d in raw:
            if not isinstance(d, str):
                continue
            dep = d.strip()
            if not dep or dep in seen:
                continue
            seen.add(dep)
            out.append(dep)
        return out

    def _project_root(project_name: str) -> Path:
        return Path(workspace) / project_name

    def _looks_like_file_path(value: str) -> bool:
        if not isinstance(value, str):
            return False
        text = value.strip()
        if not text:
            return False
        if "/" in text or "\\" in text:
            return True
        lowered = text.lower()
        return lowered.endswith((".gd", ".tscn", ".tres", ".tscn.uid", ".png", ".wav", ".json", ".py", ".md"))

    def _validate_dependency_ids(project_name: str, deps: list[str], task_id: str, batch_ids: set[str] | None = None) -> str | None:
        batch_ids = batch_ids or set()
        completed_ids = db.task_get_completed_ids()
        for dep in deps:
            if dep == task_id or dep in batch_ids:
                continue
            if _looks_like_file_path(dep):
                return (
                    f"Invalid dependency '{dep}' for task '{task_id}'. "
                    "dependencies must be task IDs, not file paths. "
                    "Use depends_on for intra-batch ordering, or create_tasks_file_aware(files=[...]) for file-based planning."
                )
            if db.task_get(dep) is None and dep not in completed_ids and db.task_get_completed_record(dep) is None:
                return (
                    f"Unknown dependency '{dep}' for task '{task_id}'. "
                    "dependencies must reference an existing or completed task ID."
                )
        return None

    def _validate_task_dependency_update(project_name: str, deps: list[str], task_id: str) -> tuple[bool, str | None]:
        if task_id in deps:
            return False, "Task cannot depend on itself"
        dep_error = _validate_dependency_ids(project_name, deps, task_id)
        if dep_error:
            return False, dep_error
        return True, None

    def _harness_status(project_name: str) -> dict:
        project_path = _project_root(project_name)
        harness_path = project_path / "autoload" / "test_harness.gd"
        project_godot = project_path / "project.godot"
        file_exists = harness_path.exists()
        autoload_registered = False
        compatible = False

        if file_exists:
            try:
                text = harness_path.read_text()
                compatible = all(marker in text for marker in _required_harness_markers)
            except Exception:
                compatible = False

        if project_godot.exists():
            try:
                godot_text = project_godot.read_text()
                autoload_registered = 'TestHarness="*res://autoload/test_harness.gd"' in godot_text or 'TestHarness="res://autoload/test_harness.gd"' in godot_text
            except Exception:
                autoload_registered = False

        return {
            "project_path": project_path,
            "harness_path": harness_path,
            "project_godot": project_godot,
            "file_exists": file_exists,
            "autoload_registered": autoload_registered,
            "compatible": compatible,
        }

    def _project_supports_harness(project_name: str) -> bool:
        if not project_name:
            return False
        status = _harness_status(project_name)
        return status["compatible"] and status["autoload_registered"]

    def _ensure_test_harness_autoload(project_godot: Path) -> dict:
        if not project_godot.exists():
            return {"ok": False, "error": f"Missing project.godot at {project_godot}"}

        entry = 'TestHarness="*res://autoload/test_harness.gd"'
        text = project_godot.read_text()
        if 'TestHarness="*res://autoload/test_harness.gd"' in text or 'TestHarness="res://autoload/test_harness.gd"' in text:
            return {"ok": True, "changed": False}

        lines = text.splitlines()
        if "[autoload]" not in lines:
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"\n[autoload]\n{entry}\n"
            project_godot.write_text(text)
            return {"ok": True, "changed": True}

        start = lines.index("[autoload]")
        insert_at = start + 1
        while insert_at < len(lines) and not (lines[insert_at].startswith("[") and lines[insert_at].endswith("]")):
            insert_at += 1
        lines.insert(insert_at, entry)
        project_godot.write_text("\n".join(lines) + "\n")
        return {"ok": True, "changed": True}

    def _install_canonical_harness(project_name: str) -> dict:
        status = _harness_status(project_name)
        project_path = status["project_path"]
        harness_path = status["harness_path"]
        actions = []

        if not project_path.exists():
            return {"ok": False, "error": f"Project path does not exist: {project_path}"}
        if not _canonical_harness.exists():
            return {"ok": False, "error": f"Canonical harness template missing: {_canonical_harness}"}

        harness_path.parent.mkdir(parents=True, exist_ok=True)
        if not status["compatible"]:
            shutil.copyfile(_canonical_harness, harness_path)
            actions.append("installed_canonical_harness")

        autoload_result = _ensure_test_harness_autoload(status["project_godot"])
        if not autoload_result["ok"]:
            return autoload_result
        if autoload_result["changed"]:
            actions.append("registered_test_harness_autoload")

        refreshed = _harness_status(project_name)
        return {
            "ok": refreshed["compatible"] and refreshed["autoload_registered"],
            "actions": actions,
            "status": refreshed,
            "project_path": project_path,
            "harness_path": harness_path,
        }

    def _make_harness_upgrade_task(project_name: str, deps: list[str], install_result: dict) -> tuple[str, dict]:
        upgrade_id = f"feature-harness-integrate-{project_name}-{int(time.time() * 1000) % 10**9}"
        project_path = _project_root(project_name)
        upgrade_deps = chain_to_project_head(db, project_name, deps, task_id=upgrade_id, ensure_head=True)
        install_note = ", ".join(install_result.get("actions", [])) or "canonical_harness_already_present"
        description = (
            f"Finish integrating the already-installed canonical TestHarness for {project_name} so harness_qa can run.\n\n"
            "The controller has already installed `autoload/test_harness.gd` and registered the autoload.\n"
            "Do not rewrite or replace the harness protocol file unless there is a concrete compatibility bug.\n\n"
            "Required project-specific integration:\n"
            "1. Wire game logic to call `TestHarness.checkpoint(...)` at stable deterministic states.\n"
            "2. Keep `--test-harness` launch compatibility and port 11010 behavior aligned with the canonical harness.\n"
            "3. Add or verify `qa_label` metadata and any narrow scene hooks needed for deterministic navigation.\n"
            "4. Validate the installed harness against `harness_step(...)` expectations and project-specific tests.\n"
            "5. Keep StateServer/polling compatibility on port 11009.\n\n"
            f"Controller install actions: {install_note}\n"
            f"Project path: {project_path}"
        )
        task = {
            "id": upgrade_id,
            "project": project_name,
            "type": "feature",
            "description": description,
            "priority": 85,
            "status": "pending",
            "max_attempts": 3,
            "dependencies": upgrade_deps,
            "metadata": {
                "created_by": "harness_qa_guard",
                "purpose": "enable_harness_qa",
                "canonical_harness_installed": True,
                "install_actions": install_result.get("actions", []),
            },
        }
        return upgrade_id, task

# ---------- Tasks ----------
    @app.route("/api/tasks", methods=["GET"])
    def list_tasks():
        include_completed = request.args.get("include_completed", "").lower() in ("true", "1", "yes")
        status_filter = request.args.get("status")
        project_filter = request.args.get("project")

        all_tasks = task_source.get_all_tasks()

        if status_filter:
            tasks = [t for t in all_tasks if t.status == status_filter]
        elif include_completed:
            tasks = all_tasks
        else:
            # Default: active working set -- pending, in_progress, failed
            # Completed and cancelled are history; use ?include_completed=true to see them
            tasks = [t for t in all_tasks if t.status in ("pending", "in_progress", "failed")]

        if project_filter:
            tasks = [t for t in tasks if t.project == project_filter]

        return jsonify({
            "tasks": [t.to_dict() for t in tasks]
        })
    @app.route("/api/tasks", methods=["POST"])
    def add_task():
        data = request.json or {}
        from swarm.tasks import Task
        task_id = data.get("id", f"{data.get('type', 'task')}-{int(time.time() * 1000) % 10**9}-{__import__('random').randint(100, 999)}")
        deps = _normalize_dependencies(data.get("dependencies", []))
        if deps is None:
            return jsonify({"error": "dependencies must be a list (or list-like string)"}), 400
        if task_id in deps:
            return jsonify({"error": "Task cannot depend on itself"}), 400
        project_name = data.get("project", "")
        task_type = data.get("type", "feature")
        dep_error = _validate_dependency_ids(project_name, deps, task_id)
        if dep_error:
            return jsonify({"error": dep_error}), 400
        # Auto-chain to project HEAD when no deps supplied — keeps dep graph history connected
        warning = None
        if task_type == "harness_qa" and project_name and not _project_supports_harness(project_name):
            install_result = _install_canonical_harness(project_name)
            if not install_result.get("ok"):
                return jsonify({"error": f"Failed to install canonical harness: {install_result.get('error', 'unknown error')}"}), 400
            upgrade_id, upgrade_task = _make_harness_upgrade_task(project_name, deps, install_result)
            try:
                task_source.add_task(Task.from_dict(upgrade_task))
            except ValueError as e:
                return jsonify({"error": f"Failed to create harness upgrade prerequisite: {str(e)}"}), 400
            deps = [upgrade_id]
            warning = (
                f"Project '{project_name}' did not have a compatible harness. "
                f"Installed the canonical harness and inserted integration task '{upgrade_id}' before '{task_id}'."
            )
        elif not deps and project_name:
            deps = chain_to_project_head(db, project_name, deps, task_id=task_id, ensure_head=True)
            head = deps[0] if deps else None
            if head:
                # Always chain to head when no deps supplied — keeps history connected
                warning = f"Task {task_id} auto-chained to project head '{head}'."
        task = Task(
            id=task_id,
            project=project_name,
            type=task_type,
            description=data.get("description", ""),
            priority=data.get("priority", 50),
            status="pending",
            max_attempts=data.get("max_attempts", 3),
            dependencies=deps,
            metadata=data.get("metadata", {})
        )
        try:
            task_source.add_task(task)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        resp = {"task": task.to_dict()}
        if warning:
            resp["warning"] = warning
        return jsonify(resp)
    @app.route("/api/tasks/batch", methods=["POST"])
    def add_tasks_batch():
        """Create multiple tasks at once with reliable dependency wiring.

        Body:
          project        str   — default project for all items (overridable per item)
          chain          bool  — each task automatically depends on the previous one
          chain_to_head  bool  — attach project's head_task_id as a dependency on the first root task
          tasks          list  — task objects; each may include:
            depends_on  list[int]  — indices into this batch (resolved to IDs before creation)
            dependencies list[str] — explicit task IDs (merged with depends_on)

        Two-pass approach: all IDs are generated upfront so depends_on index
        references never collide and always resolve correctly.

        Returns {"created": N, "ids": [...], "id_map": {"0": "<id>", ...}, "warnings": [...]}
        """
        import random
        from swarm.tasks import Task
        data = request.json or {}
        task_list = data.get("tasks", [])
        chain = data.get("chain", False)
        chain_to_head = data.get("chain_to_head", True)
        default_project = data.get("project", "")
        if not isinstance(task_list, list) or not task_list:
            return jsonify({"error": "Expected {tasks: [...]}"}), 400

        # Pass 1: generate all IDs upfront — index suffix prevents same-millisecond collisions
        ts = int(time.time() * 1000) % 10**9
        task_ids = []
        for i, item in enumerate(task_list):
            if not isinstance(item, dict):
                task_ids.append(None)
                continue
            tid = item.get("id") or f"{item.get('type', 'task')}-{ts}-{i:02d}{random.randint(10, 99)}"
            task_ids.append(tid)

        # Pass 2: resolve depends_on indices + merge with explicit dep IDs
        resolved_deps = []
        for i, item in enumerate(task_list):
            if not isinstance(item, dict):
                resolved_deps.append([])
                continue
            deps = _normalize_dependencies(item.get("dependencies", [])) or []
            dep_on = item.get("depends_on", [])
            if not isinstance(dep_on, list):
                return jsonify({"error": "depends_on must be a list of indices", "index": i}), 400
            for idx in dep_on:
                if not isinstance(idx, int) or idx < 0 or idx >= len(task_ids):
                    return jsonify({"error": f"depends_on index {idx} out of range (batch has {len(task_ids)} tasks)", "index": i}), 400
                ref_id = task_ids[idx]
                if ref_id and ref_id not in deps:
                    deps.append(ref_id)
            if chain and i > 0 and task_ids[i - 1]:
                prev = task_ids[i - 1]
                if prev not in deps:
                    deps.append(prev)
            resolved_deps.append(deps)

        # Pass 2b: chain_to_head \u2014 attach project head_task_id to every root task
        warnings = []
        if chain_to_head and default_project:
            project_head = ensure_project_head(db, default_project)
            if project_head:
                chained_roots = 0
                for i, deps in enumerate(resolved_deps):
                    if not deps:
                        resolved_deps[i] = [project_head]
                        chained_roots += 1
                        warnings.append(
                            f"Task '{task_ids[i]}' had no dependencies \u2014 "
                            f"chained to project head '{project_head}'."
                        )
                if chained_roots == 0:
                    warnings.append(
                        f"chain_to_head=true but every task in batch already has dependencies \u2014 "
                        f"no task was chained to head '{project_head}'."
                    )

        def _rollback_added_tasks() -> None:
            for added_id in reversed(added):
                task_source.remove_task(added_id)

        # Pass 3: validate and create
        added = []
        batch_id_set = {tid for tid in task_ids if tid}
        for i, item in enumerate(task_list):
            if not isinstance(item, dict):
                continue
            task_id = task_ids[i]
            deps = resolved_deps[i]
            if task_id in deps:
                _rollback_added_tasks()
                return jsonify({"error": "Task cannot depend on itself", "task_id": task_id, "index": i}), 400
            dep_error = _validate_dependency_ids(item.get("project") or default_project, deps, task_id, batch_id_set)
            if dep_error:
                _rollback_added_tasks()
                return jsonify({"error": dep_error, "task_id": task_id, "index": i}), 400
            task = Task(
                id=task_id,
                project=item.get("project") or default_project,
                type=item.get("type", "feature"),
                description=item.get("description", ""),
                priority=item.get("priority", 50),
                status="pending",
                max_attempts=item.get("max_attempts", 3),
                dependencies=deps,
                metadata=item.get("metadata", {}),
            )
            try:
                task_source.add_task(task)
            except ValueError as e:
                _rollback_added_tasks()
                return jsonify({"error": str(e), "task_id": task_id, "index": i}), 400
            added.append(task.id)
            # Floating-task warning: task had no deps and project has a head_task_id
            if not resolved_deps[i] and not chain_to_head:
                proj_name = item.get("project") or default_project
                if proj_name:
                    project_head = ensure_project_head(db, proj_name)
                    if project_head:
                        warnings.append(
                            f"Task '{task_id}' has no dependencies \u2014 "
                            f"project '{proj_name}' head_task_id is "
                            f"'{project_head}'. Consider using chain_to_head."
                        )

        id_map = {str(i): tid for i, tid in enumerate(task_ids) if tid}
        resp = {"created": len(added), "ids": added, "id_map": id_map}
        if warnings:
            resp["warnings"] = warnings
        return jsonify(resp)
    @app.route("/api/tasks/batch-status", methods=["POST"])
    def batch_update_tasks_status():
        """PATCH all listed tasks to the given status in one call."""
        VALID_STATUSES = {"pending", "in_progress", "completed", "failed", "cancelled"}
        data = request.json or {}
        task_ids = data.get("ids", [])
        new_status = data.get("status", "").strip().lower()
        metadata_patch = data.get("metadata")

        if not isinstance(task_ids, list) or not task_ids:
            return jsonify({"error": "ids must be a non-empty list"}), 400
        if new_status not in VALID_STATUSES:
            return jsonify({
                "error": f"status must be one of: {', '.join(sorted(VALID_STATUSES))}"
            }), 400

        updated_ids = []
        errors = []
        for task_id in task_ids:
            task = task_source.get_task(task_id)
            if task is None:
                errors.append({"task_id": task_id, "error": "Task not found"})
                continue
            task.status = new_status
            if new_status == "in_progress" and not task.started:
                task.started = datetime.now().isoformat()
            if new_status in ["completed", "failed", "cancelled"] and not task.completed:
                task.completed = datetime.now().isoformat()
            if metadata_patch:
                task.metadata.update(metadata_patch)
            try:
                task_source.update_task(task)
                updated_ids.append(task_id)
            except ValueError as e:
                errors.append({"task_id": task_id, "error": str(e)})

        return jsonify({
            "updated": len(updated_ids),
            "ids": updated_ids,
            "errors": errors,
        })

    @app.route("/api/tasks/<task_id>", methods=["GET"])
    def get_task(task_id):
        task = task_source.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404
        return jsonify({"task": task.to_dict()})

    @app.route("/api/tasks/<task_id>/delegation", methods=["GET"])
    def get_task_delegation(task_id):
        task = db.task_get(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404

        meta = task.get("metadata") or {}
        batch_id = meta.get("delegation_batch_id")
        project = task.get("project") or ""
        project_tasks = db.task_get_by_project(project) if project else db.task_get_all()

        children = []
        for candidate in project_tasks:
            cmeta = candidate.get("metadata") or {}
            if cmeta.get("parent_task_id") != task_id:
                continue
            if batch_id and cmeta.get("delegation_batch_id") != batch_id:
                continue
            children.append(candidate)
        children.sort(key=lambda t: (t.get("created") or "", t.get("id") or ""))

        successor_id = meta.get("delegation_successor_task_id")
        successor = db.task_get(successor_id) if successor_id else None
        helper_history = list(meta.get("helper_delegations") or [])

        return jsonify({
            "task_id": task_id,
            "project": project,
            "delegation": {
                "batch_id": batch_id,
                "mode": meta.get("delegation_mode"),
                "state": meta.get("delegation_state"),
                "child_count": len(children),
                "children": children,
                "successor_task_id": successor_id,
                "successor_kind": meta.get("delegation_successor_kind"),
                "successor_task": successor,
                "helper_activity_count": len(helper_history),
                "helper_activity": helper_history,
            },
        })
    @app.route("/api/tasks/<task_id>", methods=["PUT", "PATCH"])
    def update_task(task_id):
        task = task_source.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404
        data = request.json or {}
        # Warn if description is updated on an already-running task — the agent
        # has a baked copy of the old description and won't see the change.
        warning = None
        if "description" in data and task.status == "in_progress":
            warning = (
                f"Task {task_id} is currently in_progress — the running agent "
                "already has the old description baked into its script and will "
                "not see this update until its next attempt."
            )
        for key in ["status", "priority", "description", "type", "project", "agent_id", "max_attempts", "dependencies", "metadata"]:
            if key in data:
                if key == "dependencies":
                    deps = _normalize_dependencies(data[key])
                    if deps is None:
                        return jsonify({"error": "dependencies must be a list (or list-like string)"}), 400
                    ok, dep_error = _validate_task_dependency_update(
                        data.get("project") or task.project,
                        deps,
                        task_id,
                    )
                    if not ok:
                        return jsonify({"error": dep_error}), 400
                    setattr(task, key, deps)
                    continue
                setattr(task, key, data[key])
        if data.get("status") == "in_progress" and not task.started:
            task.started = datetime.now().isoformat()
        if data.get("status") in ["completed", "failed"] and not task.completed:
            task.completed = datetime.now().isoformat()
        try:
            task_source.update_task(task)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        resp = {"task": task.to_dict()}
        if warning:
            resp["warning"] = warning
        return jsonify(resp)
    @app.route("/api/tasks/<task_id>", methods=["DELETE"])
    def delete_task(task_id):
        success = task_source.remove_task(task_id)
        if not success:
            return jsonify({"error": "Task not found"}), 404
        return jsonify({"success": True})
    @app.route("/api/tasks/<task_id>/dependencies", methods=["GET"])
    def get_task_dependencies(task_id):
        """Return the dependency list for a task."""
        task = task_source.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404
        return jsonify({"task_id": task_id, "dependencies": task.dependencies or []})

    @app.route("/api/tasks/<task_id>/dependencies", methods=["PUT"])
    def set_task_dependencies(task_id):
        """Replace the full dependency list. Body: {\"dependencies\": [...]}"""
        task = task_source.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404
        data = request.get_json(force=True) or {}
        deps = _normalize_dependencies(data.get("dependencies", []))
        if deps is None:
            return jsonify({"error": "dependencies must be a list (or list-like string)"}), 400
        ok, dep_error = _validate_task_dependency_update(task.project, deps, task_id)
        if not ok:
            return jsonify({"error": dep_error}), 400
        task.dependencies = deps
        try:
            task_source.update_task(task)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"task_id": task_id, "dependencies": task.dependencies})

    @app.route("/api/tasks/<task_id>/dependencies", methods=["POST"])
    def add_task_dependency(task_id):
        """Add one or more dependencies without replacing existing ones.
        Body: {\"dependency\": \"<id>\"} or {\"dependencies\": [...]}
        """
        task = task_source.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404
        data = request.get_json(force=True) or {}
        # Accept single dep or list
        if "dependency" in data and "dependencies" not in data:
            data["dependencies"] = [data["dependency"]]
        new_deps = _normalize_dependencies(data.get("dependencies", []))
        if new_deps is None:
            return jsonify({"error": "dependencies must be a list (or list-like string)"}), 400
        existing = set(task.dependencies or [])
        added = [d for d in new_deps if d not in existing]
        merged = list(existing) + added
        ok, dep_error = _validate_task_dependency_update(task.project, merged, task_id)
        if not ok:
            return jsonify({"error": dep_error}), 400
        task.dependencies = merged
        try:
            task_source.update_task(task)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"task_id": task_id, "dependencies": task.dependencies, "added": added})

    @app.route("/api/tasks/<task_id>/dependencies/<dep_id>", methods=["DELETE"])
    def remove_task_dependency(task_id, dep_id):
        """Remove a single dependency edge."""
        task = task_source.get_task(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404
        deps = list(task.dependencies or [])
        if dep_id not in deps:
            return jsonify({"error": "Dependency not found", "task_id": task_id, "dep_id": dep_id}), 404
        deps.remove(dep_id)
        task.dependencies = deps
        try:
            task_source.update_task(task)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"task_id": task_id, "dependencies": task.dependencies, "removed": dep_id})

    @app.route("/api/tasks/<task_id>/reset", methods=["POST"])
    def reset_task(task_id):
        """Reset a failed/stuck task so it can be retried.
        Body (all optional):
          reset_attempts  bool   — set attempts back to 0 (default true)
          note            str    — guidance prepended to description for next agent
          description     str    — replace description entirely
          max_attempts    int    — raise the attempt cap
        """
        task = db.task_get(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404
        data = request.get_json(force=True) or {}
        reset_attempts = data.get("reset_attempts", True)
        note = (data.get("note") or "").strip()
        new_desc = (data.get("description") or "").strip()
        new_max = data.get("max_attempts")
        updates = {"status": "pending"}
        if reset_attempts:
            reset_task_to_pending(db, task_id)
            refreshed = db.task_get(task_id) or {}
            updates = {"status": "pending", "attempts": refreshed.get("attempts", 0), "metadata": refreshed.get("metadata") or {}, "agent_id": None}
        else:
            updates["agent_id"] = None
        if note:
            base_desc = new_desc or task.get("description", "")
            updates["description"] = f"RECOVERY NOTE:\n{note}\n\n---\n\n{base_desc}"
        elif new_desc:
            updates["description"] = new_desc
        if new_max is not None:
            updates["max_attempts"] = int(new_max)
        if note or new_desc or new_max is not None or not reset_attempts:
            db.task_update(task_id, updates)
        return jsonify({"success": True, "task_id": task_id, "updates": list(updates.keys())})

    @app.route("/api/tasks/<task_id>/dependents", methods=["GET"])
    def get_task_dependents(task_id):
        """Return all active tasks that list task_id as a dependency."""
        task = db.task_get(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404
        ACTIVE_STATUSES = {"pending", "in_progress"}
        all_tasks = db.task_get_all()
        dependents = [
            {"id": t["id"], "project": t["project"], "type": t["type"],
             "description": t["description"], "status": t["status"]}
            for t in all_tasks
            if t["status"] in ACTIVE_STATUSES
            and t["id"] != task_id
            and task_id in (t.get("dependencies") or [])
        ]
        return jsonify({"task_id": task_id, "dependents": dependents})

    @app.route("/api/tasks/<task_id>/insert-after", methods=["POST"])
    def insert_task_after(task_id):
        """Atomically insert a new task into a dependency chain immediately after task_id.

        Body: {type, description, priority, metadata (all optional)}
        Returns: {inserted_task: {...}, reparented: [task_ids]}
        """
        from swarm.tasks import Task
        import random as _r

        # Verify source task exists
        src_task = db.task_get(task_id)
        if not src_task:
            return jsonify({"error": "Task not found"}), 404

        data = request.json or {}
        new_id = f"{data.get('type', 'task')}-{int(time.time() * 1000) % 10**9}-{_r.randint(100, 999)}"

        # Find all tasks whose dependencies include task_id (reparenting targets)
        all_tasks = db.task_get_all()
        reparented_ids = []
        for t in all_tasks:
            deps = t.get("dependencies") or []
            if task_id in deps:
                new_deps = [new_id if d == task_id else d for d in deps]
                db.task_update(t["id"], {"dependencies": new_deps})
                reparented_ids.append(t["id"])

        # Create the new task with dependency on task_id
        new_task = Task(
            id=new_id,
            project=src_task.get("project", ""),
            type=data.get("type", "feature"),
            description=data.get("description", ""),
            priority=data.get("priority", src_task.get("priority", 50)),
            status="pending",
            max_attempts=data.get("max_attempts", 3),
            dependencies=[task_id],
            metadata=data.get("metadata", {}),
        )
        try:
            task_source.add_task(new_task)
        except ValueError as e:
            # Rollback reparenting on failure
            for rid in reparented_ids:
                t = db.task_get(rid)
                if t:
                    fixed = [task_id if d == new_id else d for d in (t.get("dependencies") or [])]
                    db.task_update(rid, {"dependencies": fixed})
            return jsonify({"error": str(e)}), 400

        return jsonify({
            "inserted_task": new_task.to_dict(),
            "reparented": reparented_ids,
        })

    @app.route("/api/tasks/<gate_id>/insert-before-gate", methods=["POST"])
    def insert_task_before_phase_gate(gate_id):
        """Insert a task immediately before a pending phase gate.

        The new task inherits the gate's current dependencies, then the gate is
        rewired to depend only on the new task. This keeps the downstream phase
        paused while bug fixes, QA follow-ups, or signoff tasks run.
        """
        import random as _r

        gate = db.task_get(gate_id)
        if not gate:
            return jsonify({"error": "Task not found"}), 404
        if gate.get("type") != "phase_gate":
            return jsonify({"error": f"Task '{gate_id}' is not a phase_gate"}), 400
        if gate.get("status") != "pending":
            return jsonify({"error": f"Phase gate '{gate_id}' is not pending", "status": gate.get("status")}), 409

        data = request.get_json(force=True) or {}
        description = (data.get("description") or "").strip()
        if not description:
            return jsonify({"error": "description is required"}), 400

        project = gate.get("project", "")
        task_type = (data.get("type") or "bug").strip() or "bug"
        new_id = (data.get("id") or f"{project}-pre-gate-{int(time.time() * 1000) % 10**9}-{_r.randint(100, 999)}").strip()
        if db.task_get(new_id) or new_id in db.task_get_completed_ids():
            return jsonify({"error": f"Task id already exists: {new_id}"}), 400

        inherited_deps = list(gate.get("dependencies") or [])
        metadata = {
            **(data.get("metadata") or {}),
            "inserted_before_gate": True,
            "phase_gate_id": gate_id,
            "previous_gate_dependencies": inherited_deps,
        }
        new_task = {
            "id": new_id,
            "project": project,
            "type": task_type,
            "description": description,
            "priority": int(data.get("priority", 90)),
            "status": "pending",
            "attempts": 0,
            "max_attempts": int(data.get("max_attempts", 3)),
            "dependencies": inherited_deps,
            "metadata": metadata,
        }

        try:
            db.task_upsert(new_task)
            db.task_update(gate_id, {"dependencies": [new_id]})
        except ValueError as e:
            db.task_delete(new_id)
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            db.task_delete(new_id)
            return jsonify({"error": f"Insert before gate failed: {e}"}), 400

        return jsonify({
            "inserted_task": db.task_get(new_id) or new_task,
            "gate": db.task_get(gate_id),
            "previous_gate_dependencies": inherited_deps,
        })

    @app.route("/api/tasks/export", methods=["GET"])
    def export_tasks():
        """Export all pending tasks as YAML (F)."""
        import yaml
        tasks = [t.to_dict() for t in task_source.get_all_tasks() if t.status == "pending"]
        export = [{"project": t["project"], "type": t["type"], "description": t["description"],
                   "priority": t["priority"], "dependencies": t["dependencies"]} for t in tasks]
        return app.response_class(
            yaml.dump({"tasks": export}, default_flow_style=False, allow_unicode=True),
            mimetype="text/yaml",
            headers={"Content-Disposition": "attachment; filename=tasks.yaml"},
        )
    @app.route("/api/tasks/import", methods=["POST"])
    def import_tasks():
        """Bulk-import tasks from YAML body or JSON array (F)."""
        import yaml
        from swarm.tasks import Task
        content_type = request.content_type or ""
        if "yaml" in content_type or "text/plain" in content_type:
            try:
                data = yaml.safe_load(request.get_data(as_text=True))
            except Exception as e:
                return jsonify({"error": f"YAML parse error: {e}"}), 400
        else:
            data = request.json or {}
        task_list = data.get("tasks", data) if isinstance(data, dict) else data
        if not isinstance(task_list, list):
            return jsonify({"error": "Expected a list of tasks or {tasks: [...]}"}), 400
        added = []
        import random
        for item in task_list:
            if not isinstance(item, dict):
                continue
            task_id = item.get("id", f"{item.get('type','task')}-{int(time.time()*1000)%10**9}-{random.randint(100,999)}")
            deps = _normalize_dependencies(item.get("dependencies", []))
            if deps is None:
                return jsonify({"error": "dependencies must be a list (or list-like string)"}), 400
            if task_id in deps:
                return jsonify({"error": "Task cannot depend on itself", "task_id": task_id}), 400
            task = Task(
                id=task_id,
                project=item.get("project", ""),
                type=item.get("type", "feature"),
                description=item.get("description", ""),
                priority=item.get("priority", 50),
                status="pending",
                dependencies=deps,
                metadata=item.get("metadata", {}),
            )
            try:
                task_source.add_task(task)
            except ValueError as e:
                return jsonify({"error": str(e), "task_id": task_id}), 400
            added.append(task.id)
        return jsonify({"imported": len(added), "ids": added})
