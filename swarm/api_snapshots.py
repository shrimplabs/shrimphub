"""Project snapshot and clone routes.

Endpoints
---------
POST /api/projects/<name>/snapshot          — save snapshot (JSON export + git tag)
GET  /api/projects/<name>/snapshots         — list snapshots for a project
POST /api/projects/<name>/restore           — restore snapshot in-place (same project name)
POST /api/projects/<name>/clone             — clone snapshot into a new project name
DELETE /api/projects/<name>/snapshots/<tag> — delete a snapshot
"""

import json
import random
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Ordered pool of valid pipeline phases for variant D randomisation.
_PIPELINE_PHASES = ["plan", "scout", "work", "validate"]

# Named pipeline presets that can be requested by name in the clone call.
PIPELINE_PRESETS: dict[str, list] = {
    "control":   ["plan", "scout", "work", "validate"],
    "variant-a": ["plan", "work", "validate"],
    "variant-b": ["plan", "scout", "synthesize", "work", "validate"],
    "variant-c": ["scout", "work", "validate"],
    "variant-d": "random",   # resolved per-task at clone time
    "variant-f": [],          # flat loop, no pipeline
}


def _phase_order_metadata(phases: list) -> dict:
    reasons = []
    positions = {phase: i for i, phase in enumerate(phases)}
    if "validate" in positions and "work" in positions and positions["validate"] < positions["work"]:
        reasons.append("validate_before_work")
    if "synthesize" in positions and "scout" in positions and positions["synthesize"] < positions["scout"]:
        reasons.append("synthesize_before_scout")
    if "synthesize" in positions and "work" in positions and positions["synthesize"] > positions["work"]:
        reasons.append("synthesize_after_work")
    return {
        "phase_order": phases,
        "is_valid_order": not reasons,
        "invalidity_reason": ",".join(reasons),
    }

from flask import jsonify, request


def register_routes(app, project_registry, workspace, db, config, data_dir, orchestrator=None, config_file=None):
    snapshot_dir = data_dir / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ helpers

    def _snapshot_path(project: str, tag: str) -> Path:
        safe_proj = re.sub(r"[^a-zA-Z0-9_\-]", "_", project)
        safe_tag  = re.sub(r"[^a-zA-Z0-9_\-]", "_", tag)
        return snapshot_dir / f"{safe_proj}__{safe_tag}.json"

    def _save_config() -> None:
        if not config_file:
            return
        try:
            Path(config_file).write_text(json.dumps(config, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[Snapshots] WARNING: failed to persist config: {e}")

    def _list_snapshots(project: str) -> list[dict]:
        safe_proj = re.sub(r"[^a-zA-Z0-9_\-]", "_", project)
        snaps = []
        for p in sorted(snapshot_dir.glob(f"{safe_proj}__*.json")):
            tag = p.stem[len(safe_proj) + 2:]  # strip "<proj>__"
            stat = p.stat()
            snaps.append({
                "tag": tag,
                "file": p.name,
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "size_bytes": stat.st_size,
            })
        return snaps

    def _export_project(project: str) -> dict:
        """Dump all project DB state to a plain dict."""
        proj_row = db.project_get(project) or {}
        tasks = db.task_get_by_project(project)
        return {
            "version": 1,
            "project": project,
            "exported_at": datetime.now().isoformat(),
            "project_row": proj_row,
            "tasks": tasks,
        }

    def _import_project(snapshot: dict, target_project: str, reset_status: bool = True):
        """Write snapshot data into the DB under target_project name.

        reset_status=True → all tasks become pending/0 attempts (clean start).
        reset_status=False → preserve original status/attempts (exact restore).
        """
        from swarm.tasks import Task

        # Remap task IDs if we're cloning to a different project name
        source_project = snapshot["project"]
        remap: dict[str, str] = {}  # old_id → new_id (only populated when cloning)

        tasks: list[dict] = snapshot["tasks"]

        if source_project != target_project:
            # Build new IDs with the target project prefix
            ts = int(time.time() * 1000)
            for i, t in enumerate(tasks):
                old_id = t["id"]
                is_genesis = (
                    t.get("description", "").lower().startswith("project registered")
                    or "genesis" in old_id.lower()
                    or (t.get("metadata") or {}).get("archived") is True
                )
                if is_genesis:
                    new_id = f"{target_project}-genesis"
                else:
                    new_id = f"{target_project[:20].replace(' ', '-')}-{ts}-{i:04d}"
                remap[old_id] = new_id

        def _remap_deps(deps):
            if not deps:
                return []
            if isinstance(deps, str):
                try:
                    deps = json.loads(deps)
                except Exception:
                    return []
            return [remap.get(d, d) for d in deps if d]

        # Upsert project row
        proj_row = dict(snapshot.get("project_row") or {})
        proj_row["name"] = target_project
        # Clear live state so we don't inherit stale agent PIDs etc.
        proj_row.pop("status", None)
        proj_row.pop("head_task_id", None)
        db.project_upsert(proj_row)

        # Upsert tasks
        for t in tasks:
            row = dict(t)
            original_meta = dict(t.get("metadata") or {})
            row["project"] = target_project
            if remap:
                row["id"] = remap[t["id"]]
            row["dependencies"] = _remap_deps(row.get("dependencies"))
            is_genesis = (
                t.get("description", "").lower().startswith("project registered")
                or "genesis" in t.get("id", "").lower()
                or (t.get("metadata") or {}).get("archived") is True
            )
            if reset_status and not is_genesis:
                row["status"] = "pending"
                row["attempts"] = 0
                row["agent_id"] = None
                row["started"] = None
                row["completed"] = None
                # Start fresh, but preserve experiment labels/pipeline metadata.
                # Failure context from the source project must not leak into the
                # clone, while treatment assignment must remain stable.
                meta = {
                    k: v for k, v in original_meta.items()
                    if k.startswith("experiment_")
                    or k.startswith("phase_")
                    or k in {
                        "pipeline", "pipeline_variant", "flat_provider",
                        "is_valid_order", "invalidity_reason",
                    }
                }
            else:
                meta = dict(original_meta)
            # Always record the original task ID so we can cross-reference results
            meta["source_task_id"] = t["id"]
            meta["source_project"] = source_project
            row["metadata"] = meta
            db.task_upsert(row)

        # Update head_task_id to the remapped value if available
        proj_row_db = db.project_get(target_project) or {}
        old_head = (snapshot.get("project_row") or {}).get("head_task_id")
        if old_head:
            new_head = remap.get(old_head, old_head)
            db.project_upsert({**proj_row_db, "name": target_project, "head_task_id": new_head})

    def _git_tag(project: str, tag: str) -> Optional[str]:
        """Create a git tag on the project repo. Returns error string or None."""
        proj_path = workspace / project
        if not proj_path.exists():
            return "project directory not found"
        try:
            r = subprocess.run(
                ["git", "tag", tag],
                cwd=str(proj_path), capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return r.stderr.strip() or "git tag failed"
        except Exception as e:
            return str(e)
        return None

    def _git_checkout_tag(project: str, tag: str) -> Optional[str]:
        """Hard-reset the project repo to the given git tag. Returns error or None."""
        proj_path = workspace / project
        if not proj_path.exists():
            return "project directory not found"
        try:
            r = subprocess.run(
                ["git", "reset", "--hard", tag],
                cwd=str(proj_path), capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return r.stderr.strip() or "git reset failed"
        except Exception as e:
            return str(e)
        return None

    def _git_clone_to(source_project: str, target_project: str) -> Optional[str]:
        """Copy the git repo to a new directory (local clone). Returns error or None."""
        src = workspace / source_project
        dst = workspace / target_project
        if not src.exists():
            return f"source directory {src} not found"
        if dst.exists():
            return f"target directory {dst} already exists"
        try:
            subprocess.run(
                ["git", "clone", str(src), str(dst)],
                capture_output=True, text=True, timeout=60, check=True,
            )
        except subprocess.CalledProcessError as e:
            return e.stderr.strip() or "git clone failed"
        except Exception as e:
            return str(e)
        return None

    # ------------------------------------------------------------------ routes

    @app.route("/api/projects/<project_name>/snapshot", methods=["POST"])
    def save_snapshot(project_name):
        """Save a named snapshot of a project's DB state + git tag."""
        data = request.json or {}
        tag = (data.get("tag") or f"snap-{datetime.now().strftime('%Y%m%d-%H%M%S')}").strip()
        if not re.match(r"^[a-zA-Z0-9_\-]+$", tag):
            return jsonify({"error": "Tag may only contain letters, digits, hyphens, underscores"}), 400

        proj = project_registry.get(project_name)
        if proj is None:
            return jsonify({"error": f"Project '{project_name}' not found"}), 404

        # Check no active agents on this project
        active = [a for a in db.agent_get_active() if a.get("project") == project_name]
        if active:
            return jsonify({"error": f"{len(active)} agent(s) running on this project — stop them before snapshotting"}), 409

        snap_path = _snapshot_path(project_name, tag)
        if snap_path.exists():
            return jsonify({"error": f"Snapshot '{tag}' already exists"}), 409

        export = _export_project(project_name)
        snap_path.write_text(json.dumps(export, indent=2, default=str))

        # Tag the git repo
        git_err = _git_tag(project_name, tag)
        git_note = f"Git tag failed: {git_err}" if git_err else None

        result = {"tag": tag, "file": snap_path.name, "tasks": len(export["tasks"])}
        if git_note:
            result["warning"] = git_note
        return jsonify(result)

    @app.route("/api/projects/<project_name>/snapshots", methods=["GET"])
    def list_snapshots(project_name):
        return jsonify({"snapshots": _list_snapshots(project_name)})

    @app.route("/api/projects/<project_name>/snapshots/<tag>", methods=["DELETE"])
    def delete_snapshot(project_name, tag):
        snap_path = _snapshot_path(project_name, tag)
        if not snap_path.exists():
            return jsonify({"error": "Snapshot not found"}), 404
        snap_path.unlink()
        return jsonify({"deleted": tag})

    @app.route("/api/projects/<project_name>/restore", methods=["POST"])
    def restore_snapshot(project_name):
        """Restore a snapshot in-place (same project name, same git dir)."""
        data = request.json or {}
        tag = (data.get("tag") or "").strip()
        if not tag:
            return jsonify({"error": "tag is required"}), 400

        proj = project_registry.get(project_name)
        if proj is None:
            return jsonify({"error": f"Project '{project_name}' not found"}), 404

        active = [a for a in db.agent_get_active() if a.get("project") == project_name]
        if active:
            return jsonify({"error": f"{len(active)} agent(s) running — stop them before restoring"}), 409

        snap_path = _snapshot_path(project_name, tag)
        if not snap_path.exists():
            return jsonify({"error": f"Snapshot '{tag}' not found"}), 404

        snapshot = json.loads(snap_path.read_text())

        # Delete existing tasks for this project, then re-import
        existing = db.task_get_by_project(project_name)
        conn = db._connect()
        conn.execute("DELETE FROM tasks WHERE project=?", (project_name,))
        conn.commit()

        reset = data.get("reset_status", True)
        _import_project(snapshot, project_name, reset_status=reset)

        # Reset git repo to tag
        git_err = _git_checkout_tag(project_name, tag)
        git_note = f"Git reset failed: {git_err}" if git_err else None

        result = {
            "restored": tag,
            "tasks_deleted": len(existing),
            "tasks_restored": len(snapshot["tasks"]),
        }
        if git_note:
            result["warning"] = git_note
        return jsonify(result)

    @app.route("/api/projects/<project_name>/clone", methods=["POST"])
    def clone_snapshot(project_name):
        """Clone a snapshot into a brand-new project with a different name.

        Optional body fields:
          pipeline: preset name ("control", "variant-a" … "variant-f") or explicit
                    list of phase names. Omit to inherit the source project's pipeline.
          flat_provider: for variant-f, which provider to use (default: "minimax")
        """
        data = request.json or {}
        tag = (data.get("tag") or "").strip()
        new_name = (data.get("new_name") or "").strip()
        pipeline_arg = data.get("pipeline")  # preset name, list, or None
        flat_provider = data.get("flat_provider", "minimax")
        experiment_id = (
            data.get("experiment_id")
            or f"{project_name}-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
        )

        if not tag:
            return jsonify({"error": "tag is required"}), 400
        if not new_name:
            return jsonify({"error": "new_name is required"}), 400
        if not re.match(r"^[a-zA-Z0-9_\-]+$", new_name):
            return jsonify({"error": "new_name may only contain letters, digits, hyphens, underscores"}), 400
        if project_registry.get(new_name):
            return jsonify({"error": f"Project '{new_name}' already exists"}), 409

        snap_path = _snapshot_path(project_name, tag)
        if not snap_path.exists():
            return jsonify({"error": f"Snapshot '{tag}' not found"}), 404

        snapshot = json.loads(snap_path.read_text())

        # Resolve pipeline
        resolved_pipeline = None  # None = inherit global config
        is_random = False
        is_flat = False

        if pipeline_arg is not None:
            if isinstance(pipeline_arg, str) and pipeline_arg in PIPELINE_PRESETS:
                preset = PIPELINE_PRESETS[pipeline_arg]
                if preset == "random":
                    is_random = True
                elif preset == []:
                    is_flat = True
                else:
                    resolved_pipeline = preset
            elif isinstance(pipeline_arg, list):
                resolved_pipeline = pipeline_arg
            elif pipeline_arg == "random":
                is_random = True
            elif pipeline_arg in ("flat", "none", ""):
                is_flat = True

        # Clone the git repo
        git_err = _git_clone_to(project_name, new_name)
        if git_err:
            return jsonify({"error": f"Git clone failed: {git_err}"}), 500

        # Checkout the snapshot tag in the clone
        git_err2 = _git_checkout_tag(new_name, tag)
        git_note = f"Git checkout tag failed: {git_err2}" if git_err2 else None

        # Import DB state under new project name (tasks reset to pending)
        project_registry.add_project(new_name, managed=True)

        tasks = snapshot["tasks"]

        # For random variant: assign a random phase ordering to each task's metadata
        if is_random:
            for t in tasks:
                phases = list(_PIPELINE_PHASES)
                seed = random.randint(0, 2**31 - 1)
                random.Random(seed).shuffle(phases)
                meta = dict(t.get("metadata") or {})
                meta["experiment_id"] = experiment_id
                meta["experiment_arm"] = "exploratory"
                meta["pipeline"] = phases
                meta["pipeline_variant"] = phases
                meta["experiment_variant"] = "variant-d"
                meta["phase_random_seed"] = seed
                meta.update(_phase_order_metadata(phases))
                t["metadata"] = meta
        elif is_flat:
            for t in tasks:
                meta = dict(t.get("metadata") or {})
                meta["experiment_id"] = experiment_id
                meta["experiment_arm"] = "confirmatory"
                meta["pipeline"] = []
                meta["pipeline_variant"] = []
                meta["experiment_variant"] = "variant-f"
                meta["phase_order"] = []
                meta["is_valid_order"] = True
                meta["invalidity_reason"] = ""
                meta["flat_provider"] = flat_provider
                t["metadata"] = meta
        elif resolved_pipeline is not None:
            variant_label = pipeline_arg if isinstance(pipeline_arg, str) else "custom"
            for t in tasks:
                meta = dict(t.get("metadata") or {})
                meta["experiment_id"] = experiment_id
                meta["experiment_arm"] = "confirmatory"
                meta["pipeline"] = resolved_pipeline
                meta["pipeline_variant"] = resolved_pipeline
                meta["experiment_variant"] = variant_label
                meta.update(_phase_order_metadata(list(resolved_pipeline)))
                t["metadata"] = meta

        _import_project(snapshot, new_name, reset_status=True)

        # Persist project-level pipeline override for tasks that don't have per-task metadata
        if resolved_pipeline is not None and not is_random and not is_flat:
            pp = config.setdefault("project_pipelines", {})
            pp[new_name] = {
                "*": resolved_pipeline,
                "_experiment": {
                    "experiment_id": experiment_id,
                    "experiment_arm": "confirmatory",
                    "experiment_variant": pipeline_arg if isinstance(pipeline_arg, str) else "custom",
                    "pipeline_mode": "fixed",
                    "pipeline": resolved_pipeline,
                    "source_project": project_name,
                },
            }

        # For flat variant: store provider override in project_pipelines as sentinel
        if is_flat:
            pp = config.setdefault("project_pipelines", {})
            pp[new_name] = {
                "*": [],
                "_flat_provider": flat_provider,
                "_experiment": {
                    "experiment_id": experiment_id,
                    "experiment_arm": "confirmatory",
                    "experiment_variant": "variant-f",
                    "pipeline_mode": "flat",
                    "flat_provider": flat_provider,
                    "source_project": project_name,
                },
            }

        # For variant D: new tasks created later must keep sampling random orders.
        if is_random:
            pp = config.setdefault("project_pipelines", {})
            pp[new_name] = {
                "*": "random",
                "_experiment": {
                    "experiment_id": experiment_id,
                    "experiment_arm": "exploratory",
                    "experiment_variant": "variant-d",
                    "pipeline_mode": "random",
                    "phase_pool": list(_PIPELINE_PHASES),
                    "source_project": project_name,
                },
            }

        # Sync managed_projects config
        if new_name not in config.get("managed_projects", []):
            config.setdefault("managed_projects", []).append(new_name)
        _save_config()

        result = {
            "cloned_from": project_name,
            "tag": tag,
            "new_project": new_name,
            "tasks": len(snapshot["tasks"]),
            "pipeline": "random" if is_random else ("flat" if is_flat else resolved_pipeline),
            "experiment_variant": pipeline_arg,
            "experiment_id": experiment_id,
        }
        if git_note:
            result["warning"] = git_note
        return jsonify(result)
