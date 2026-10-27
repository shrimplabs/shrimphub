"""Broadcast log route handlers for the Swarm API.

Provides per-project broadcast logs for inter-agent communication.
Logs are stored as data/broadcast/{project}.log and auto-trimmed to 400 lines.
"""

from datetime import datetime
from pathlib import Path

from flask import jsonify, request


MAX_LOG_LINES = 400
TRIGGER_TRIM_AT = 500


def register_routes(app, data_dir):
    """Register broadcast routes on the Flask app."""

    # Ensure the broadcast directory exists on startup
    broadcast_dir = Path(data_dir) / "broadcast"
    broadcast_dir.mkdir(parents=True, exist_ok=True)

    def _log_path(project: str) -> Path:
        """Return the log file path for a project."""
        safe = project.replace("/", "_").replace("\\", "_")
        return broadcast_dir / f"{safe}.log"

    @app.route("/api/broadcast/<project>", methods=["GET"])
    def get_broadcast(project):
        """Return the last N lines of a project's broadcast log.

        Query params:
            tail  int  — number of lines to return (default 50)

        Returns: {"lines": ["...", "..."]}
        """
        tail = min(int(request.args.get("tail", 50)), 500)
        log_path = _log_path(project)
        if not log_path.exists():
            return jsonify({"lines": []})
        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return jsonify({"lines": all_lines[-tail:]})

    @app.route("/api/broadcast/<project>", methods=["POST"])
    def post_broadcast(project):
        """Append a line to a project's broadcast log.

        Body: {"agent_id": str, "task_id": str, "message": str}
        Server prepends: "[YYYY-MM-DD HH:MM | task-id] message"

        If the log exceeds {TRIGGER_TRIM_AT} lines, trim to {MAX_LOG_LINES}.
        """
        data = request.json or {}
        agent_id = str(data.get("agent_id", "unknown"))
        task_id  = str(data.get("task_id",  ""))
        message  = str(data.get("message",  ""))

        if not message:
            return jsonify({"error": "message required"}), 400

        ts   = datetime.now().strftime("%Y-%m-%d %H:%M")
        line = f"[{ts} | {task_id}] {message}"

        log_path = _log_path(project)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

        # Auto-trim if over limit
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > TRIGGER_TRIM_AT:
                log_path.write_text("\n".join(lines[-MAX_LOG_LINES:]) + "\n", encoding="utf-8")

        return jsonify({"ok": True, "line": line})
