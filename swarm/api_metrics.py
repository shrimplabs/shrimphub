"""Metrics route handlers for the Swarm API."""

from flask import jsonify
import glob
import os
import subprocess


def register_routes(app, data_dir, workspace, config, db, agent_tracker):
    """Register routes on the Flask app."""
    @app.route("/api/metrics", methods=["GET"])
    def get_metrics():
        """Return agent effectiveness metrics."""
        # --- task history ---
        tasks_completed = 0
        tasks_failed = 0
        total_attempts = 0
        first_try_success = 0
        validation_bugs = 0

        task_hist = os.path.join(data_dir, "task-history.jsonl")
        if os.path.exists(task_hist):
            try:
                import json as _json
                with open(task_hist) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            t = _json.loads(line)
                        except Exception:
                            continue
                        if t.get("status") == "completed":
                            tasks_completed += 1
                            attempts = t.get("attempts", 0)
                            total_attempts += attempts
                            if attempts <= 1:
                                first_try_success += 1
                            meta = t.get("metadata") or {}
                            if t.get("type") == "bug" and meta.get("is_validation_bug"):
                                validation_bugs += 1
                        elif t.get("status") == "failed":
                            tasks_failed += 1
            except Exception:
                pass

        # --- agent history ---
        total_input_tokens = 0
        total_output_tokens = 0
        loop_counts = []
        num_agents = 0

        agent_hist = os.path.join(data_dir, "agent-history.jsonl")
        if os.path.exists(agent_hist):
            try:
                import json as _json
                with open(agent_hist) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            a = _json.loads(line)
                        except Exception:
                            continue
                        total_input_tokens += a.get("input_tokens", 0)
                        total_output_tokens += a.get("output_tokens", 0)
                        lc = a.get("loop_count")
                        if lc is not None:
                            loop_counts.append(lc)
                        num_agents += 1
            except Exception:
                pass

        # --- web_search_calls: grep agent log files ---
        web_search_calls = 0
        try:
            log_files = glob.glob(os.path.join(data_dir, "agent_*.log"))
            if log_files:
                r = subprocess.run(
                    ["grep", "-c", "Executing tool: web_search"] + log_files,
                    capture_output=True, text=True
                )
                for line in r.stdout.strip().split(chr(10)):
                    if ":" in line:
                        try:
                            web_search_calls += int(line.rsplit(":", 1)[-1])
                        except ValueError:
                            pass
        except Exception:
            pass

        # --- knowledge_files_written: count AGENT_KNOWLEDGE.md in workspaces ---
        knowledge_files_written = 0
        workspace_dir = str(workspace)
        if os.path.isdir(workspace_dir):
            try:
                for root, dirs, files in os.walk(workspace_dir):
                    if "AGENT_KNOWLEDGE.md" in files:
                        knowledge_files_written += 1
            except Exception:
                pass

        # --- derive metrics ---
        avg_attempts = round(total_attempts / tasks_completed, 2) if tasks_completed > 0 else 0
        first_try_rate = round(first_try_success / tasks_completed, 2) if tasks_completed > 0 else 0
        val_bug_rate = round(validation_bugs / tasks_completed, 2) if tasks_completed > 0 else 0
        avg_in = round(total_input_tokens / num_agents) if num_agents > 0 else 0
        avg_out = round(total_output_tokens / num_agents) if num_agents > 0 else 0
        avg_loops = round(sum(loop_counts) / len(loop_counts), 1) if loop_counts else 0

        return jsonify({
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed,
            "avg_attempts_per_task": avg_attempts,
            "first_attempt_success_rate": first_try_rate,
            "validation_bug_rate": val_bug_rate,
            "avg_input_tokens": avg_in,
            "avg_output_tokens": avg_out,
            "avg_loops_per_agent": avg_loops,
            "web_search_calls": web_search_calls,
            "knowledge_files_written": knowledge_files_written,
        })
