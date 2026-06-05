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
            "agents_run": num_agents,
            "first_attempt_success_rate": first_try_rate,
            "total_tokens": total_input_tokens + total_output_tokens,
            "avg_input_tokens": avg_in,
            "avg_output_tokens": avg_out,
            "avg_loops_per_agent": avg_loops,
            "web_search_calls": web_search_calls,
            "knowledge_files_written": knowledge_files_written,
        })

    @app.route("/api/metrics/knowledge-files", methods=["GET"])
    def get_knowledge_files():
        """Return list of all AGENT_KNOWLEDGE.md files with their content."""
        workspace_dir = str(workspace)
        files = []
        if os.path.isdir(workspace_dir):
            try:
                for root, dirs, filenames in os.walk(workspace_dir):
                    if "AGENT_KNOWLEDGE.md" in filenames:
                        path = os.path.join(root, "AGENT_KNOWLEDGE.md")
                        project = os.path.relpath(root, workspace_dir)
                        try:
                            content = open(path).read()
                        except Exception:
                            content = "(unreadable)"
                        files.append({"project": project, "path": path, "content": content})
            except Exception:
                pass
        files.sort(key=lambda f: f["project"])
        return jsonify({"files": files})

    @app.route("/api/experiment/results", methods=["GET"])
    def get_experiment_results():
        """Aggregate pipeline A/B experiment metrics from experiment_metrics.jsonl.

        Groups completed tasks by pipeline_variant and computes:
        - count, mean/median work_loops, validation_pass_rate, bug_spawn_rate
        - Mann-Whitney U p-value for loops vs control (if scipy available)
        - Fisher's exact p-value for pass rates vs control (if scipy available)
        """
        import json as _json
        import math
        from collections import defaultdict

        metrics_path = os.path.join(data_dir, "experiment_metrics.jsonl")
        records = []
        if os.path.exists(metrics_path):
            with open(metrics_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(_json.loads(line))
                    except Exception:
                        pass

        # Group by pipeline_variant (serialize list → string key)
        groups = defaultdict(list)
        for r in records:
            variant = r.get("pipeline_variant")
            if not variant:
                continue
            key = "→".join(variant) if isinstance(variant, list) else str(variant)
            groups[key].append(r)

        def _mean(vals):
            return round(sum(vals) / len(vals), 2) if vals else None

        def _median(vals):
            if not vals:
                return None
            s = sorted(vals)
            n = len(s)
            mid = n // 2
            return s[mid] if n % 2 else round((s[mid - 1] + s[mid]) / 2, 2)

        def _stddev(vals):
            if len(vals) < 2:
                return None
            m = sum(vals) / len(vals)
            variance = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
            return round(math.sqrt(variance), 2)

        summary = {}
        for variant_key, rows in groups.items():
            loops = [r["work_loops"] for r in rows if "work_loops" in r]
            passed = [r for r in rows if r.get("validation_passed")]
            bugged = [r for r in rows if r.get("bug_spawned")]
            insertions = [r.get("diff_insertions", 0) for r in rows]
            deletions = [r.get("diff_deletions", 0) for r in rows]
            summary[variant_key] = {
                "n": len(rows),
                "pipeline": rows[0].get("pipeline_variant") if rows else [],
                "work_loops_mean": _mean(loops),
                "work_loops_median": _median(loops),
                "work_loops_stddev": _stddev(loops),
                "validation_pass_rate": round(len(passed) / len(rows), 3) if rows else None,
                "bug_spawn_rate": round(len(bugged) / len(rows), 3) if rows else None,
                "diff_insertions_mean": _mean(insertions),
                "diff_deletions_mean": _mean(deletions),
            }

        # Statistical significance vs control (plan→scout→work→validate)
        control_key = "plan→scout→work→validate"
        control_rows = groups.get(control_key, [])
        if control_rows:
            try:
                from scipy import stats as _stats
                control_loops = [r["work_loops"] for r in control_rows if "work_loops" in r]
                ctrl_pass = len([r for r in control_rows if r.get("validation_passed")])
                ctrl_fail = len(control_rows) - ctrl_pass

                for variant_key, stat in summary.items():
                    if variant_key == control_key:
                        continue
                    variant_rows = groups[variant_key]
                    v_loops = [r["work_loops"] for r in variant_rows if "work_loops" in r]
                    v_pass = len([r for r in variant_rows if r.get("validation_passed")])
                    v_fail = len(variant_rows) - v_pass

                    if len(v_loops) >= 3 and len(control_loops) >= 3:
                        _, p_loops = _stats.mannwhitneyu(v_loops, control_loops, alternative="two-sided")
                        stat["loops_vs_control_p"] = round(float(p_loops), 4)

                    if v_pass + v_fail > 0 and ctrl_pass + ctrl_fail > 0:
                        _, p_pass = _stats.fisher_exact([[v_pass, v_fail], [ctrl_pass, ctrl_fail]])
                        stat["pass_rate_vs_control_p"] = round(float(p_pass), 4)
            except ImportError:
                pass  # scipy optional

        return jsonify({
            "total_records": len(records),
            "variants": summary,
            "metrics_file": metrics_path,
        })
