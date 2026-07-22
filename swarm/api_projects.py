"""Projects route handlers for the Swarm API."""

from flask import jsonify, request

import json
from pathlib import Path
from datetime import datetime, timedelta
import time
import threading

from swarm.maintenance.project_heads import reconcile_project_head
from swarm.maintenance.file_locks import (
    file_lock_owner_is_live,
    reconcile_stale_file_locks,
)
from swarm.task_chains import chain_to_project_head
from swarm.task_mutations import reset_task_to_pending
from swarm.branch_intent import branch_intent_metadata, format_branch_intent
from swarm.experiment_metadata import stamp_experiment_metadata
from swarm.closure.documents import write_project_closure_doc
from swarm.closure.proposals import propose_project_closure
from swarm.closure.specs import normalize_project_spec
from swarm.closure.runs import list_verification_runs
from swarm.closure.repair_planning import plan_repair_tasks_for_run
from swarm.closure.escalation import build_stalled_project_bundle

# Per-project locks for atomic lock-conflict handoff (check-or-create).
# Keyed by project name; created on first use.
_handoff_locks: dict[str, threading.Lock] = {}
_handoff_locks_mutex = threading.Lock()


def _load_project_task_history(data_dir: Path, project_name: str, db=None) -> dict[str, dict]:
    """Load completed/failed tasks for a project from the DB.

    Tasks stay in the tasks table permanently — no JSONL fallback needed.
    """
    records: dict[str, dict] = {}
    if db is not None:
        for row in db.task_get_by_project(project_name):
            if row.get("id") and row.get("status") in ("completed", "failed", "cancelled"):
                records[row["id"]] = row
    return records


def _restore_project_branch_from_history(db, data_dir: Path, project_name: str, head_task_id: str | None) -> list[str]:
    """Resurrect a missing live branch from task-history around the stored head."""
    history_by_id = _load_project_task_history(data_dir, project_name, db=db)
    if not history_by_id:
        return []

    root_id = head_task_id if head_task_id in history_by_id else None
    if root_id is None:
        candidates = [
            rec for rec in history_by_id.values()
            if rec.get("status") in {"failed", "pending", "in_progress", "cancelled"}
        ]
        if not candidates:
            return []
        candidates.sort(key=lambda rec: (rec.get("completed") or rec.get("created") or "", rec.get("id") or ""))
        root_id = candidates[-1]["id"]

    all_live_ids = {t["id"] for t in db.task_get_all()}
    completed_ids = set(db.task_get_completed_ids())

    restore_ids: set[str] = set()
    stack = [root_id]
    while stack:
        task_id = stack.pop()
        if task_id in restore_ids:
            continue
        rec = history_by_id.get(task_id)
        if not rec:
            continue
        restore_ids.add(task_id)
        for dep_id in rec.get("dependencies") or []:
            if dep_id in all_live_ids or dep_id in completed_ids:
                continue
            if dep_id in history_by_id:
                stack.append(dep_id)

    ordered_restore_ids = sorted(
        restore_ids,
        key=lambda tid: (
            history_by_id[tid].get("created") or "",
            history_by_id[tid].get("completed") or "",
            tid,
        ),
    )

    restored: list[str] = []
    for task_id in ordered_restore_ids:
        rec = history_by_id[task_id]
        live_resolvable_deps = [
            dep_id
            for dep_id in (rec.get("dependencies") or [])
            if dep_id in all_live_ids or dep_id in completed_ids or dep_id in restore_ids
        ]
        metadata = dict(rec.get("metadata") or {})
        metadata["resurrected_from_history"] = True
        dropped_archival = [
            dep_id for dep_id in (rec.get("dependencies") or [])
            if dep_id not in live_resolvable_deps
        ]
        if dropped_archival:
            metadata["dropped_archival_dependencies"] = dropped_archival
        db.task_upsert({
            "id": task_id,
            "project": rec.get("project", project_name),
            "type": rec.get("type", "feature"),
            "description": rec.get("description", "(Resurrected from history)"),
            "priority": rec.get("priority", 50),
            "status": "pending",
            "attempts": 0,
            "max_attempts": rec.get("max_attempts", 3),
            "dependencies": chain_to_project_head(
                db,
                rec.get("project", project_name),
                live_resolvable_deps,
                task_id=task_id,
                ensure_head=True,
            ),
            "metadata": metadata,
        })
        restored.append(task_id)
        all_live_ids.add(task_id)

    return restored


def _get_handoff_lock(project: str) -> threading.Lock:
    with _handoff_locks_mutex:
        if project not in _handoff_locks:
            _handoff_locks[project] = threading.Lock()
        return _handoff_locks[project]


def _sync_managed_projects(config, project_registry, orchestrator, config_file=None, config_write_lock=None):
    """Sync orchestrator.MANAGED_PROJECTS from registry state and persist to config.json.

    Call this whenever a project is registered or its managed flag changes so that:
    - dashboard sidebar shows the project immediately
    - project survives server restarts
    """
    managed = sorted(
        name for name, proj in project_registry.get_all().items()
        if getattr(proj, "managed", True)
    )
    orchestrator.MANAGED_PROJECTS = managed
    config["managed_projects"] = managed
    if config_write_lock is not None and config_file is not None:
        try:
            with config_write_lock:
                cfg_data = json.loads(config_file.read_text()) if config_file.exists() else {}
                cfg_data["managed_projects"] = managed
                config_file.write_text(json.dumps(cfg_data, indent=2))
        except Exception as e:
            print(f"[api_projects] Warning: could not persist managed_projects: {e}")


def _normalize_handoff_dependencies(db, project_name: str, task_id: str, deps, owner_task_id: str) -> list[str]:
    """Keep reused lock-conflict continuations from retaining stale owner edges."""
    out: list[str] = []
    seen: set[str] = set()
    for dep in deps or []:
        if not isinstance(dep, str):
            continue
        dep_id = dep.strip()
        if not dep_id or dep_id == task_id or dep_id in seen:
            continue
        if dep_id == owner_task_id:
            owner = db.task_get(owner_task_id) if owner_task_id else None
            if owner is None or owner.get("status") in ("failed", "cancelled"):
                continue
        seen.add(dep_id)
        out.append(dep_id)
    return out


def register_routes(app, project_registry, workspace, task_source, orchestrator, generate_task_script, db, config, data_dir, config_file=None, config_write_lock=None):
    """Register routes on the Flask app."""

    def _project_or_404(project_name: str):
        project = project_registry.get(project_name)
        if project is None:
            return None, (jsonify({"error": "Project not found"}), 404)
        return project, None

    def _closure_payload(project_name: str):
        project = db.project_get(project_name)
        if project is None:
            return None
        runs = list_verification_runs(project_name, limit=1)
        return {
            "project": project_name,
            "closure_mode": project.get("closure_mode", "build"),
            "closure_status": project.get("closure_status", "yellow"),
            "closure_spec": normalize_project_spec(project.get("closure_spec"), project.get("profile")),
            "last_verification_at": project.get("last_verification_at"),
            "last_verification_status": project.get("last_verification_status"),
            "open_regression_count": int(project.get("open_regression_count") or 0),
            "stall_count": int(project.get("stall_count") or 0),
            "last_verification_run": runs[0] if runs else None,
        }

    def _write_live_closure_doc(project_name: str, profile: str | None = None, source: str = "live-spec"):
        current = db.project_get(project_name) or {}
        live_profile = profile or current.get("profile")
        proposal = {
            "project": project_name,
            "profile": live_profile or "unknown",
            "source": source,
            "closure_spec": normalize_project_spec(current.get("closure_spec"), live_profile),
            "assumptions": [],
        }
        return write_project_closure_doc(workspace / project_name, project_name, proposal)

    def _latest_playthrough_payload(project_name: str, playthrough_by_project: dict = None) -> dict | None:
        if playthrough_by_project is not None:
            tasks = playthrough_by_project.get(project_name, [])
        else:
            tasks = [t for t in db.task_get_by_project(project_name) if t.get("type") == "playthrough_bot"]
        if not tasks:
            return None
        tasks.sort(
            key=lambda t: (
                t.get("completed") or "",
                t.get("started") or "",
                t.get("created") or "",
                t.get("id") or "",
            ),
            reverse=True,
        )
        task = tasks[0]
        meta = task.get("metadata") or {}
        result = meta.get("playthrough_result") if isinstance(meta.get("playthrough_result"), dict) else {}
        progress = result.get("progress") if isinstance(result.get("progress"), dict) else {}
        agency = progress.get("agency_evidence") if isinstance(progress.get("agency_evidence"), dict) else {}
        return {
            "task_id": task.get("id"),
            "task_status": task.get("status"),
            "agent_id": result.get("agent_id") or task.get("agent_id"),
            "outcome": result.get("outcome"),
            "status": result.get("status"),
            "reason": result.get("reason"),
            "completed": progress.get("completed"),
            "score": progress.get("score"),
            "level": progress.get("level"),
            "agency_evidence": agency,
            "trace_path": result.get("trace_path"),
            "receipt_path": result.get("receipt_path"),
            "original_trace_path": result.get("original_trace_path"),
            "started": task.get("started"),
            "completed_at": task.get("completed"),
        }

    def _project_payload(name: str, project, playthrough_by_project: dict = None) -> dict:
        payload = project.to_dict()
        payload["latest_playthrough"] = _latest_playthrough_payload(name, playthrough_by_project)
        return payload

# ---------- Projects ----------
    @app.route("/api/projects", methods=["GET"])
    def list_projects():
        projects = project_registry.get_all()
        # Fetch all playthrough tasks in one query and group by project,
        # rather than one query per project (N=34+ queries → 1).
        all_playthroughs = db.task_get_by_type("playthrough_bot")
        playthrough_by_project: dict = {}
        for t in all_playthroughs:
            playthrough_by_project.setdefault(t["project"], []).append(t)
        return jsonify({
            "projects": {k: _project_payload(k, v, playthrough_by_project) for k, v in projects.items()}
        })
    @app.route("/api/projects", methods=["POST"])
    def add_project():
        data = request.json or {}
        name = data.get("name")
        if not name:
            return jsonify({"error": "name required"}), 400
        requested_profile = data.get("profile")
        explicit_spec = data.get("closure_spec")
        if explicit_spec is not None:
            closure_proposal = {
                "project": name,
                "profile": requested_profile or "python",
                "source": "explicit",
                "closure_spec": normalize_project_spec(explicit_spec, requested_profile),
                "assumptions": [],
            }
        else:
            closure_proposal = propose_project_closure(name, workspace / name, requested_profile)
        project = project_registry.add_project(
            name=name,
            status=data.get("status", "active"),
            managed=bool(data.get("managed", True)),
            profile=closure_proposal["profile"],
            closure_mode=closure_proposal["closure_spec"].get("mode", "build"),
            closure_spec=closure_proposal["closure_spec"],
        )
        # Auto-create genesis task -- the root commit equivalent for every project
        _now = datetime.now().isoformat()
        _genesis_id = f"{name}-genesis"
        db.task_upsert({
            "id": _genesis_id,
            "project": name,
            "type": "feature",
            "description": "Project registered -- genesis anchor",
            "status": "completed",
            "created": _now,
            "completed": _now,
            "priority": 50,
        })
        db.task_record_completed(_genesis_id, project=name)
        project_registry.set_head_task_id(name, _genesis_id)
        project = project_registry.get(name) or project
        _write_live_closure_doc(name, profile=closure_proposal["profile"], source=closure_proposal.get("source", "proposal"))
        _sync_managed_projects(config, project_registry, orchestrator, config_file, config_write_lock)
        return jsonify({"project": project.to_dict(), "closure_proposal": closure_proposal})
    @app.route("/api/projects/<project_name>", methods=["DELETE"])
    def delete_project(project_name):
        """Remove a project from the registry and delete all its tasks.

        Does NOT touch the git repo on disk — that stays untouched.
        Refuses if any agent is currently running on this project.
        """
        active = [a for a in db.agent_get_active() if a.get("project") == project_name]
        if active:
            return jsonify({"error": f"{len(active)} agent(s) still running on this project — stop them first"}), 409

        # Delete all tasks for this project
        conn = db._connect()
        deleted_tasks = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE project=?", (project_name,)
        ).fetchone()[0]
        conn.execute("DELETE FROM tasks WHERE project=?", (project_name,))
        conn.commit()

        # Remove from registry
        removed = project_registry.remove_project(project_name)

        # Remove from managed_projects config
        managed = config.get("managed_projects", [])
        if project_name in managed:
            managed.remove(project_name)
            config["managed_projects"] = managed
            _sync_managed_projects(config, project_registry, orchestrator, config_file, config_write_lock)

        print(f"[api_projects] Deleted project '{project_name}': {deleted_tasks} tasks removed, registry={removed}")
        return jsonify({"deleted": project_name, "tasks_deleted": deleted_tasks})

    @app.route("/api/projects/<project_name>", methods=["GET"])
    def get_project(project_name):
        project, error = _project_or_404(project_name)
        if error:
            return error
        return jsonify({"project": _project_payload(project_name, project)})
    @app.route("/api/projects/<project_name>", methods=["PUT"])
    def update_project(project_name):
        data = request.json or {}
        if "status" in data:
            project_registry.set_status(project_name, data["status"])
        if "managed" in data:
            project_registry.set_managed(project_name, bool(data["managed"]))
        if "locked" in data:
            if data["locked"]:
                project_registry.lock(project_name)
            else:
                project_registry.unlock(project_name)
        project = project_registry.get(project_name)
        _sync_managed_projects(config, project_registry, orchestrator, config_file, config_write_lock)
        return jsonify({"project": project.to_dict() if project else None})
    @app.route("/api/projects/<project_name>/closure", methods=["GET"])
    def get_project_closure(project_name):
        project, error = _project_or_404(project_name)
        if error:
            return error
        return jsonify({"closure": _closure_payload(project_name), "project": project.to_dict()})
    @app.route("/api/projects/<project_name>/closure/proposal", methods=["GET"])
    def get_project_closure_proposal(project_name):
        project, error = _project_or_404(project_name)
        if error:
            return error
        proposal = propose_project_closure(
            project_name,
            workspace / project_name,
            getattr(project, "profile", None),
        )
        return jsonify({"proposal": proposal, "project": project.to_dict()})
    @app.route("/api/projects/<project_name>/closure/proposal/apply", methods=["POST"])
    def apply_project_closure_proposal(project_name):
        project, error = _project_or_404(project_name)
        if error:
            return error
        proposal = propose_project_closure(
            project_name,
            workspace / project_name,
            getattr(project, "profile", None),
        )
        db.project_update(project_name, {
            "profile": proposal["profile"],
            "closure_mode": proposal["closure_spec"].get("mode", "build"),
            "closure_spec": proposal["closure_spec"],
        })
        write_project_closure_doc(workspace / project_name, project_name, proposal)
        return jsonify({"proposal": proposal, "closure": _closure_payload(project_name)})
    @app.route("/api/projects/<project_name>/closure/spec", methods=["POST"])
    def set_project_closure_spec(project_name):
        project, error = _project_or_404(project_name)
        if error:
            return error
        data = request.json or {}
        profile = data.get("profile") or getattr(project, "profile", None)
        normalized = normalize_project_spec(data.get("closure_spec"), profile)
        db.project_update(project_name, {
            "closure_spec": normalized,
            "closure_mode": normalized.get("mode", "build"),
        })
        _write_live_closure_doc(project_name, profile=profile)
        return jsonify({"closure": _closure_payload(project_name)})
    @app.route("/api/projects/<project_name>/closure/mode", methods=["POST"])
    def set_project_closure_mode(project_name):
        project, error = _project_or_404(project_name)
        if error:
            return error
        data = request.json or {}
        mode = data.get("mode")
        if not isinstance(mode, str) or not mode.strip():
            return jsonify({"error": "mode required"}), 400
        current = db.project_get(project_name) or {}
        normalized = normalize_project_spec({
            **(current.get("closure_spec") or {}),
            "mode": mode.strip(),
        }, getattr(project, "profile", None))
        db.project_update(project_name, {
            "closure_mode": normalized.get("mode", "build"),
            "closure_spec": normalized,
        })
        _write_live_closure_doc(project_name, profile=getattr(project, "profile", None))
        return jsonify({"closure": _closure_payload(project_name)})
    @app.route("/api/projects/<project_name>/closure/doc/regenerate", methods=["POST"])
    def regenerate_project_closure_doc(project_name):
        project, error = _project_or_404(project_name)
        if error:
            return error
        output_path = _write_live_closure_doc(project_name, profile=getattr(project, "profile", None))
        return jsonify({
            "project": project_name,
            "path": str(output_path),
            "closure": _closure_payload(project_name),
        })
    @app.route("/api/projects/<project_name>/closure/verify", methods=["POST"])
    def verify_project_closure(project_name):
        project, error = _project_or_404(project_name)
        if error:
            return error
        from swarm import validation as _validation

        data = request.json or {}
        run = _validation.run_closure_verification(
            project_name,
            trigger_task_id=data.get("trigger_task_id"),
            project_path=(workspace / project_name),
            run_type="manual",
        )
        return jsonify({"closure": _closure_payload(project_name), "verification_run": run})
    @app.route("/api/projects/<project_name>/closure/repair", methods=["POST"])
    def repair_project_closure(project_name):
        project, error = _project_or_404(project_name)
        if error:
            return error
        data = request.json or {}
        run_id = data.get("verification_run_id")
        if not run_id:
            runs = list_verification_runs(project_name, limit=20)
            failed = [run for run in runs if run.get("status") in {"failed", "error"}]
            run_id = failed[0]["id"] if failed else None
        if not run_id:
            return jsonify({"error": "no failed verification run available"}), 404
        tasks = plan_repair_tasks_for_run(run_id)
        return jsonify({"project": project_name, "verification_run_id": run_id, "repair_tasks": tasks})
    @app.route("/api/projects/<project_name>/regressions", methods=["GET"])
    def list_project_regressions(project_name):
        _project, error = _project_or_404(project_name)
        if error:
            return error
        status = request.args.get("status")
        regressions = db.regression_list_by_project(project_name, status=status if status else None)
        return jsonify({"project": project_name, "regressions": regressions})
    @app.route("/api/projects/<project_name>/closure/escalation", methods=["GET"])
    def get_project_closure_escalation(project_name):
        _project, error = _project_or_404(project_name)
        if error:
            return error
        bundle = build_stalled_project_bundle(project_name)
        return jsonify({"project": project_name, "escalation": bundle})
    @app.route("/api/projects/<project_name>/locks", methods=["GET"])
    def get_project_locks(project_name):
        reconciled = reconcile_stale_file_locks(db, project_registry, project=project_name)
        locks = project_registry.get_locked_files(project_name)
        return jsonify({
            "reconciled_stale_locks": reconciled,
            "locks": {
                k: {
                    "file_path": v.file_path,
                    "locked_by": v.locked_by,
                    "locked_at": v.locked_at,
                    "task_id": v.task_id
                }
                for k, v in locks.items()
            }
        })
    @app.route("/api/projects/<project_name>/lock", methods=["POST"])
    def lock_file(project_name):
        data = request.json or {}
        file_path = data.get("file_path")
        agent_id = data.get("agent_id")
        task_id = data.get("task_id")
        if not file_path or not agent_id:
            return jsonify({"error": "file_path and agent_id required"}), 400
        success = project_registry.lock_file(project_name, file_path, agent_id, task_id)
        if success:
            return jsonify({"success": True})
        lock = project_registry.get_file_lock(project_name, file_path)
        if lock and not file_lock_owner_is_live(db, project_name, lock):
            replaced = project_registry.replace_file_lock(
                project_name,
                file_path,
                agent_id,
                task_id,
                previous_locked_by=lock.locked_by,
                previous_task_id=lock.task_id,
            )
            if replaced:
                return jsonify({
                    "success": True,
                    "stale_lock_replaced": True,
                    "replaced_lock": {
                        "file_path": lock.file_path,
                        "locked_by": lock.locked_by,
                        "locked_at": lock.locked_at,
                        "task_id": lock.task_id,
                    },
                })
            lock = project_registry.get_file_lock(project_name, file_path)
        return jsonify({
            "success": False,
            "lock": {
                "file_path": lock.file_path if lock else file_path,
                "locked_by": lock.locked_by if lock else None,
                "locked_at": lock.locked_at if lock else None,
                "task_id": lock.task_id if lock else None,
            } if lock else None,
        })
    @app.route("/api/projects/<project_name>/unlock", methods=["POST"])
    def unlock_file(project_name):
        data = request.json or {}
        file_path = data.get("file_path")
        agent_id = data.get("agent_id")
        if not file_path or not agent_id:
            return jsonify({"error": "file_path and agent_id required"}), 400
        success = project_registry.unlock_file(project_name, file_path, agent_id)
        return jsonify({"success": success})

    @app.route("/api/projects/<project_name>/lock-conflict-handoff", methods=["POST"])
    def lock_conflict_handoff(project_name):
        """Atomic check-or-create for lock-conflict continuation tasks.

        Prevents fan-out when N agents hit the same lock simultaneously.
        All concurrent callers for (project, owner_task_id) get back the same
        continuation task -- one is created, the rest receive the existing one.

        Body:
          owner_task_id  - task that holds the lock (continuation depends on this)
          blocked_task_id - the calling agent's task ID
          locked_path    - the contested file path
          task_type      - type of the continuation task
          priority       - priority of the continuation task
          description    - description for the continuation task
          dependencies   - full dep list (incl. owner_task_id + inherited deps)
        """
        data = request.json or {}
        owner_task_id = data.get("owner_task_id")
        if not owner_task_id:
            return jsonify({"error": "owner_task_id required"}), 400

        task_type    = data.get("task_type", "feature")
        priority     = data.get("priority", 50)
        description  = data.get("description", f"Lock-conflict continuation (blocked by {owner_task_id})")
        dependencies = data.get("dependencies") or []
        blocked_task_id = data.get("blocked_task_id", "")
        locked_path     = data.get("locked_path", "")
        posted_metadata = dict(data.get("metadata") or {})
        blocked_task = db.task_get(blocked_task_id) if blocked_task_id else None

        if blocked_task:
            description = (
                f"CONTINUATION of task {blocked_task_id} after lock conflict.\n\n"
                f"{format_branch_intent(blocked_task, heading='ORIGINAL TASK OBJECTIVE')}\n\n"
                f"This work was blocked because `{locked_path}` is currently owned by sibling task `{owner_task_id}`.\n"
                f"Continue this task only after `{owner_task_id}` completes. Re-check HEAD first: the sibling may have already satisfied part of the requirement.\n"
                f"If the needed change is already present, validate and finish without duplicating work."
            )
        elif posted_metadata.get("branch_intent_root_task_id"):
            fallback_task = {
                "id": blocked_task_id or posted_metadata.get("branch_intent_root_task_id") or owner_task_id,
                "type": posted_metadata.get("branch_intent_type") or task_type,
                "description": (
                    posted_metadata.get("branch_intent_full_description")
                    or posted_metadata.get("branch_intent_description")
                    or ""
                ),
                "metadata": posted_metadata,
            }
            description = (
                f"CONTINUATION of task {blocked_task_id or fallback_task['id']} after lock conflict.\n\n"
                f"{format_branch_intent(fallback_task, heading='ORIGINAL TASK OBJECTIVE')}\n\n"
                f"This work was blocked because `{locked_path}` is currently owned by sibling task `{owner_task_id}`.\n"
                f"Continue this task only after `{owner_task_id}` completes. Re-check HEAD first: the sibling may have already satisfied part of the requirement.\n"
                f"If the needed change is already present, validate and finish without duplicating work."
            )

        lock = _get_handoff_lock(project_name)
        with lock:
            # Check: find an existing pending/in_progress continuation for this owner
            existing = None
            for t in db.task_get_by_project(project_name):
                if t.get("status") not in ("pending", "in_progress"):
                    continue
                meta = t.get("metadata") or {}
                if (meta.get("created_by") == "lock_conflict_handoff"
                        and meta.get("blocked_by_task_id") == owner_task_id):
                    existing = t
                    break

            if existing:
                existing_deps = list(existing.get("dependencies") or [])
                normalized_deps = _normalize_handoff_dependencies(
                    db,
                    project_name,
                    existing.get("id") or "",
                    existing_deps,
                    owner_task_id,
                )
                if normalized_deps != existing_deps:
                    db.task_update(existing["id"], {"dependencies": normalized_deps})
                    existing = db.task_get(existing["id"]) or {**existing, "dependencies": normalized_deps}
                return jsonify({"task": existing, "created": False})

            # Create: no existing continuation for this owner
            import uuid as _uuid
            new_id = f"task-{_uuid.uuid4().hex[:12]}"
            metadata = {
                "created_by": "lock_conflict_handoff",
                "blocked_by_task_id": owner_task_id,
                "lock_conflict_followup_for": blocked_task_id,
                "blocked_file_path": locked_path,
                **posted_metadata,
                **branch_intent_metadata(blocked_task or {
                    "id": blocked_task_id or posted_metadata.get("branch_intent_root_task_id") or owner_task_id,
                    "type": posted_metadata.get("branch_intent_type") or task_type,
                    "description": (
                        posted_metadata.get("branch_intent_full_description")
                        or posted_metadata.get("branch_intent_description")
                        or ""
                    ),
                    "metadata": posted_metadata,
                }),
            }
            metadata = stamp_experiment_metadata(project_name, metadata, config=config)
            new_task = {
                "id": new_id,
                "project": project_name,
                "type": task_type,
                "description": description,
                "priority": priority,
                "status": "pending",
                "created": datetime.now().isoformat(),
                "dependencies": _normalize_handoff_dependencies(
                    db,
                    project_name,
                    new_id,
                    dependencies,
                    owner_task_id,
                ),
                "max_attempts": 3,
                "attempts": 0,
                "metadata": metadata,
            }
            db.task_upsert(new_task)
            return jsonify({"task": new_task, "created": True})

    @app.route("/api/projects/<project_name>/scan", methods=["POST"])
    def scan_project(project_name):
        project_path = workspace / project_name
        if not project_path.exists():
            return jsonify({"error": "Project not found"}), 404
        files = project_registry.scan_project_files(
            project_name,
            config.get("file_extensions", [".gd"]),
            set(config.get("ignore_dirs", []))
        )
        # Ensure managed=True for the project before scan so it auto-appears
        # in managed_projects. update_file_counts creates projects with managed=False.
        if not project_registry.get(project_name):
            project_registry.add_project(project_name, managed=True)
        project_registry.update_file_counts(project_name, files)
        _sync_managed_projects(config, project_registry, orchestrator, config_file, config_write_lock)
        return jsonify({"files": files})
    @app.route("/api/projects/<project_name>/health", methods=["GET"])
    def get_project_health(project_name):
        """Per-project health metrics (#10)."""
        from swarm import db as _db
        import subprocess as _sp
        project = project_registry.get(project_name)
        if project is None:
            return jsonify({"error": "Project not found"}), 404
        project_path = workspace / project_name
        # Tasks completed / failed (all live in the DB permanently)
        all_tasks = _db.task_get_by_project(project_name)
        tasks_completed = sum(1 for t in all_tasks if t["status"] == "completed")
        tasks_failed    = sum(1 for t in all_tasks if t["status"] == "failed")
        # Avg lines per file
        files = project.files if hasattr(project, "files") else (project.to_dict().get("files") or {})
        avg_lines = int(sum(files.values()) / len(files)) if files else 0
        max_file_lines = max(files.values(), default=0)
        # Last commit age
        last_commit_age_seconds = None
        if project_path.exists():
            try:
                result = _sp.run(
                    ["git", "log", "-1", "--format=%ct"],
                    cwd=str(project_path), capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    last_commit_ts = int(result.stdout.strip())
                    last_commit_age_seconds = int(time.time()) - last_commit_ts
            except Exception:
                pass
        # Simple health score: 0-100
        score = 100
        if max_file_lines > config.get("max_lines", 5000):
            score -= 20
        if tasks_failed > tasks_completed and tasks_failed > 0:
            score -= 30
        elif tasks_failed > 0:
            score -= 10
        if last_commit_age_seconds is not None and last_commit_age_seconds > 86400 * 7:
            score -= 20
        score = max(0, score)
        return jsonify({
            "project": project_name,
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed,
            "avg_lines": avg_lines,
            "max_file_lines": max_file_lines,
            "last_commit_age_seconds": last_commit_age_seconds,
            "health_score": score,
        })
    @app.route("/api/projects/<project_name>/notes", methods=["GET"])
    def get_project_notes(project_name):
        notes = db.project_get_notes(project_name)
        # Read first descriptive line of GAME_DESIGN.md as concept blurb
        concept = ""
        try:
            design_path = workspace / project_name / "GAME_DESIGN.md"
            if design_path.exists():
                for line in design_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("-"):
                        concept = line
                        break
        except Exception:
            pass
        return jsonify({"project": project_name, "notes": notes, "concept": concept})
    @app.route("/api/projects/<project_name>/notes", methods=["POST"])
    def set_project_notes(project_name):
        data = request.json or {}
        notes = data.get("notes", "")
        db.project_set_notes(project_name, notes)
        return jsonify({"success": True, "project": project_name, "notes": notes})
    @app.route("/api/projects/<project_name>/repair", methods=["POST"])
    def repair_project(project_name):
        """Surgical repair of a broken project.
        1. Reset failed tasks → pending, attempts=0
        2. Reset orphaned in_progress tasks (no live agent) → pending
        3. Resurrect dep tasks missing from DB → look up in task-history.jsonl
        """
        all_db_tasks = db.task_get_all()
        project_tasks = [t for t in all_db_tasks if t["project"] == project_name]
        project_row = db.project_get(project_name)
        history_restored = []
        actionable_project_tasks = [
            t for t in project_tasks
            if t.get("status") in {"pending", "in_progress", "failed", "cancelled"}
        ]
        if not actionable_project_tasks:
            history_restored = _restore_project_branch_from_history(
                db,
                data_dir,
                project_name,
                (project_row or {}).get("head_task_id"),
            )
            all_db_tasks = db.task_get_all()
            project_tasks = [t for t in all_db_tasks if t["project"] == project_name]
        if not project_tasks:
            return jsonify({"error": f"No tasks found for project '{project_name}'"}), 404
        head_repair = reconcile_project_head(db, project_name)
        all_task_ids = {t["id"] for t in all_db_tasks}
        completed_ids = db.task_get_completed_ids()
        completed_ids |= {t["id"] for t in all_db_tasks if t["status"] == "completed"}
        active_task_ids = {a["task_id"] for a in db.agent_get_active() if a.get("task_id")}
        reset_failed = []
        reset_orphaned = []
        resurrected = []
        reanchored = []
        for task in project_tasks:
            if task["status"] == "failed":
                reset_task_to_pending(db, task["id"])
                reset_failed.append(task["id"])
            elif task["status"] == "in_progress" and task["id"] not in active_task_ids:
                reset_task_to_pending(db, task["id"], reset_attempts=False)
                reset_orphaned.append(task["id"])
        all_db_tasks = db.task_get_all()
        project_tasks = [t for t in all_db_tasks if t["project"] == project_name]
        for task in project_tasks:
            metadata = task.get("metadata") or {}
            if task.get("status") not in ("pending", "in_progress", "failed"):
                continue
            if task.get("dependencies"):
                continue
            if not (
                metadata.get("is_recovery_task")
                or metadata.get("branch_continuation")
                or metadata.get("is_closure_repair_task")
            ):
                continue
            new_deps = chain_to_project_head(
                db,
                project_name,
                [],
                task_id=task["id"],
                ensure_head=True,
            )
            if new_deps:
                db.task_update(task["id"], {"dependencies": new_deps})
                reanchored.append({"task_id": task["id"], "dependencies": new_deps})
        # Find deps referenced by pending tasks that no longer exist in the DB
        broken_deps = set()
        for task in project_tasks:
            if task["status"] in ("pending", "failed"):
                for dep_id in (task.get("dependencies") or []):
                    if dep_id not in all_task_ids and dep_id not in completed_ids:
                        broken_deps.add(dep_id)

        # Build a task-ID lookup from the DB (tasks never leave the DB).
        # This replaces the old task-history.jsonl read.
        history_by_id = {t["id"]: t for t in db.task_get_all(
            exclude_statuses=("pending", "in_progress")
        )}
        history_ids = set(history_by_id.keys())

        # Ghost dep pruning: remove stale dep edges that point to IDs absent from
        # both the active DB and task history.  Tasks in history completed or
        # failed normally -- their dependents already unblocked.  IDs absent from
        # both are true ghosts (deleted / never created) and will never unblock
        # their dependents without this cleanup.
        pruned_ghost_deps = []
        for task in project_tasks:
            if task["status"] not in ("pending", "failed"):
                continue
            deps = list(task.get("dependencies") or [])
            ghost_deps = [
                d for d in deps
                if d not in all_task_ids
                and d not in completed_ids
                and d not in history_ids
            ]
            if ghost_deps:
                new_deps = [d for d in deps if d not in ghost_deps]
                db.task_update(task["id"], {"dependencies": new_deps})
                pruned_ghost_deps.append({"task_id": task["id"], "removed": ghost_deps})

        # Refresh project_tasks after pruning so resurrection/floating checks see
        # the updated dep lists.
        project_tasks = db.task_get_by_project(project_name)
        all_task_ids = db.task_get_all_ids()

        # Resurrect broken deps that ARE in the DB (completed/failed tasks whose
        # dependents still reference them by ID).
        resurrected_deps = broken_deps - {g for entry in pruned_ghost_deps for g in entry["removed"]}
        for dep_id in resurrected_deps:
            rec = history_by_id.get(dep_id, {})
            live_resolvable_deps = [
                d for d in (rec.get("dependencies") or [])
                if d in all_task_ids or d in completed_ids
            ]
            metadata = {"resurrected_by_repair": True}
            dropped_archival = [
                d for d in (rec.get("dependencies") or [])
                if d not in live_resolvable_deps
            ]
            if dropped_archival:
                metadata["dropped_archival_dependencies"] = dropped_archival
            db.task_upsert({
                "id": dep_id,
                "project": rec.get("project", project_name),
                "type": rec.get("type", "feature"),
                "description": rec.get("description", "(Resurrected by repair -- original description unavailable)"),
                "priority": rec.get("priority", 50),
                "status": "pending",
                "attempts": 0,
                "max_attempts": rec.get("max_attempts", 3),
                "dependencies": chain_to_project_head(
                    db,
                    rec.get("project", project_name),
                    live_resolvable_deps,
                    task_id=dep_id,
                    ensure_head=True,
                ),
                "metadata": metadata,
            })
            resurrected.append(dep_id)

        # Floating node detection: pending tasks with no deps and nothing
        # depending on them are disconnected islands -- wire them to the project
        # head so they run in the correct order rather than as rogue roots.
        depended_on = set()
        for task in project_tasks:
            for d in (task.get("dependencies") or []):
                depended_on.add(d)
        project_head = (db.project_get(project_name) or {}).get("head_task_id")
        rewired_floating = []
        for task in project_tasks:
            if task["status"] not in ("pending", "failed"):
                continue
            if task.get("dependencies"):
                continue
            if task["id"] in depended_on:
                continue
            if task["id"] == project_head:
                continue
            # Genuine floating node -- attach to project head
            new_deps = chain_to_project_head(
                db,
                project_name,
                [],
                task_id=task["id"],
                ensure_head=True,
            )
            if new_deps:
                db.task_update(task["id"], {"dependencies": new_deps})
                rewired_floating.append({"task_id": task["id"], "dependencies": new_deps})

        return jsonify({
            "project": project_name,
            "head_repair": head_repair,
            "history_restored": history_restored,
            "reset_failed": reset_failed,
            "reset_orphaned": reset_orphaned,
            "reanchored": reanchored,
            "resurrected": resurrected,
            "pruned_ghost_deps": pruned_ghost_deps,
            "rewired_floating": rewired_floating,
        })

    @app.route("/api/projects/<project_name>/reconcile-head", methods=["POST"])
    def reconcile_project(project_name):
        result = reconcile_project_head(db, project_name)
        status = 200 if result.get("ok") else 404
        return jsonify(result), status

    @app.route("/api/projects/<project_name>/cleanup-recovery", methods=["POST"])
    def cleanup_project_recovery(project_name):
        result = orchestrator.cleanup_recovery_branches(project_name)
        return jsonify(result)
    @app.route("/api/projects/<project_name>/restart", methods=["POST"])
    def restart_project(project_name):
        """Nuclear option: reset ALL tasks for the project to pending, attempts=0."""
        all_tasks = db.task_get_all()
        project_tasks = [t for t in all_tasks if t["project"] == project_name]
        if not project_tasks:
            return jsonify({"error": f"No tasks found for project '{project_name}'"}), 404
        for task in project_tasks:
            reset_task_to_pending(db, task["id"])
        return jsonify({"project": project_name, "reset": len(project_tasks)})
    @app.route("/api/projects/<project_name>/replan", methods=["POST"])
    def replan_project(project_name):
        """Spawn a project_plan task to re-analyse the codebase and generate new tasks."""
        import time as _time
        plan_id = f"project-plan-{project_name}-{int(_time.time())}"
        plan_deps = chain_to_project_head(db, project_name, task_id=plan_id, ensure_head=True)
        db.task_upsert({
            "id": plan_id,
            "project": project_name,
            "type": "project_plan",
            "description": (
                f"Generate a dependency-ordered task plan for {project_name}. "
                f"Read GAME_DESIGN.md and the existing codebase, then create all "
                f"necessary tasks via the API with proper dependencies so systems "
                f"are built and wired together in the correct order."
            ),
            "priority": 100,
            "status": "pending",
            "attempts": 0,
            "max_attempts": 2,
            "dependencies": plan_deps,
            "metadata": {},
        })
        return jsonify({"ok": True, "task_id": plan_id})
    @app.route("/api/projects/<project_name>/spawn", methods=["POST"])
    def spawn_parallel(project_name):
        """Spawn multiple agents for a single project (parallel work)."""
        data = request.json or {}
        count = data.get("count", 1)
        files = data.get("files", [])
        task_type = data.get("task_type", "refactor")
        if not project_registry.get(project_name):
            project_registry.add_project(project_name)
            # Sync managed_projects so the project is visible in dashboard and persists.
            _sync_managed_projects(config, project_registry, orchestrator, config_file, config_write_lock)
        if generate_task_script is None:
            return jsonify({"error": "generate_task_script not available"}), 500
        spawned = []
        for i in range(count):
            description = f"Parallel task {i+1}/{count}"
            if files and i < len(files):
                description = f"Work on {files[i]}"
            from swarm.tasks import Task
            parallel_id = f"parallel-{project_name}-{i}-{int(time.time())}"
            task_deps = chain_to_project_head(db, project_name, task_id=parallel_id, ensure_head=True)
            task = Task(
                id=parallel_id,
                project=project_name,
                type=task_type,
                description=description,
                priority=data.get("priority", 50),
                dependencies=task_deps,
                metadata={"parallel": True, "file": files[i] if files and i < len(files) else None}
            )
            task_source.add_task(task)
            agent_id = orchestrator.spawn_agent(task.to_dict(), generate_task_script)
            if agent_id:
                if files and i < len(files):
                    project_registry.lock_file(project_name, files[i], agent_id, task.id)
                spawned.append(agent_id)
        return jsonify({"spawned": spawned, "count": len(spawned)})
