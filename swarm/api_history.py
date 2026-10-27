"""History route handlers for the Swarm API."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict

from flask import Flask, jsonify, request

import json

from swarm.task_chains import chain_to_project_head


def register_routes(
    app: Flask,
    *,
    task_source: Any,
    db: Any,
    data_dir: Path,
    orchestrator: Any,
    config: Dict[str, Any],
) -> None:
    """Register history routes on the Flask app."""

    # ---------- History ----------

    @app.route("/api/history", methods=["GET"])
    def agent_history():
        history_file = data_dir / "agent-history.jsonl"
        entries = []
        if history_file.exists():
            for line in history_file.read_text().splitlines():
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
        # Optional time filter: ?since=2h or ?since=30m
        since_param = request.args.get("since")
        if since_param:
            import re as _re
            m = _re.match(r"(\d+)([hm])", since_param)
            if m:
                val, unit = int(m.group(1)), m.group(2)
                minutes = val * 60 if unit == "h" else val
                cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat()
                entries = [e for e in entries if (e.get("completed") or e.get("started") or "") >= cutoff]
        limit = int(request.args.get("limit", 50))
        return jsonify({"agents": entries[-limit:][::-1]})

    @app.route("/api/history/<agent_id>/requeue", methods=["POST"])
    def requeue_from_history(agent_id):
        """Re-queue a failed task from history back to pending.

        Looks up the agent record in agent-history.jsonl, finds its task_id,
        then either resets the existing task (if still in DB) or resurrects it
        from task-history.jsonl with a fresh attempt counter.

        Optional body:
          note  str  \u2014 recovery note prepended to description
        """
        data = request.get_json(force=True) or {}
        note = (data.get("note") or "").strip()

        # Find the agent record in history
        history_file = data_dir / "agent-history.jsonl"
        agent_record = None
        if history_file.exists():
            for line in history_file.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("id") == agent_id:
                        agent_record = rec
                        break
                except Exception:
                    pass

        if not agent_record:
            return jsonify({"error": f"Agent {agent_id} not found in history"}), 404

        task_id = agent_record.get("task_id")
        if not task_id:
            return jsonify({"error": "Agent record has no task_id"}), 400

        # Case 1: task still in DB \u2014 just reset it
        existing = db.task_get(task_id)
        if existing:
            meta = existing.get("metadata") or {}
            meta.pop("last_failure", None)
            meta.pop("failure_attempt", None)
            updates = {"status": "pending", "attempts": 0, "metadata": meta, "agent_id": None}
            if note:
                updates["description"] = f"RECOVERY NOTE:\n{note}\n\n---\n\n{existing.get('description', '')}"
            db.task_update(task_id, updates)
            return jsonify({"success": True, "task_id": task_id, "action": "reset"})

        # Case 2: DEPRECATED fallback for pre-migration tasks that were pruned
        # from the DB before immutable history was enabled. Remove after 2025-07-01.
        task_record = None
        task_history_file = data_dir / "task-history.jsonl"
        if task_history_file.exists():
            for line in task_history_file.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("id") == task_id:
                        task_record = rec
                        # Keep looking \u2014 use the most recent entry
                except Exception:
                    pass

        if not task_record:
            # Last resort: reconstruct minimal task from agent record
            task_record = {
                "id": task_id,
                "project": agent_record.get("project", "unknown"),
                "type": agent_record.get("task_type", "feature"),
                "description": f"(Resurrected from history \u2014 original description unavailable)\nAgent: {agent_id}",
                "priority": 50,
                "dependencies": [],
                "metadata": {},
            }

        desc = task_record.get("description", "")
        if note:
            desc = f"RECOVERY NOTE:\n{note}\n\n---\n\n{desc}"

        new_task = {
            "id": task_id,
            "project": task_record.get("project"),
            "type": task_record.get("type", "feature"),
            "description": desc,
            "priority": task_record.get("priority", 50),
            "status": "pending",
            "attempts": 0,
            "max_attempts": task_record.get("max_attempts", 3),
            "dependencies": chain_to_project_head(
                db,
                task_record.get("project"),
                task_record.get("dependencies", []),
                task_id=task_id,
                ensure_head=True,
            ),
            "metadata": {"resurrected_from_history": True},
        }
        db.task_upsert(new_task)
        return jsonify({"success": True, "task_id": task_id, "action": "resurrected"})

    @app.route("/api/history/prune", methods=["POST"])
    def prune_history():
        """Remove history entries older than ?hours=N (default 24)."""
        hours = int(request.args.get("hours", request.json.get("hours", 24) if request.is_json else 24))
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        history_file = data_dir / "agent-history.jsonl"
        if not history_file.exists():
            return jsonify({"removed": 0, "remaining": 0})
        entries = []
        for line in history_file.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
        kept = [e for e in entries if (e.get("completed") or e.get("started") or "") >= cutoff]
        removed = len(entries) - len(kept)
        history_file.write_text("\n".join(json.dumps(e) for e in kept) + ("\n" if kept else ""))
        return jsonify({"removed": removed, "remaining": len(kept)})
