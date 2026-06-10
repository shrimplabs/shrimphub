"""Metrics route handlers for the Swarm API."""

from flask import jsonify, request, Response
import glob
import os
import subprocess


def _aggregate_source_tasks(records: list) -> list:
    """Aggregate raw experiment_metrics rows to source-task level.

    Key: (experiment_id, source_project, source_task_id, experiment_variant)

    Recovery tasks and research feeder rows are counted as metadata against the
    source task, not as independent completion events. This prevents retry
    cascades from inflating attempt/loop counts.
    """
    from collections import defaultdict

    groups: dict = defaultdict(lambda: {
        # identity
        "experiment_id": "",
        "source_project": "",
        "source_task_id": "",
        "experiment_variant": "",
        "task_type": "",
        # primary scoring
        "completed": False,
        "failed": False,
        "total_attempts": 0,
        # secondary scoring
        "total_work_loops": 0,
        "total_wall_seconds": 0.0,
        "diff_insertions": 0,
        "diff_deletions": 0,
        "follow_on_task_count": 0,
        "validation_passes": 0,
        "validation_runs": 0,
        # failure breakdown
        "infrastructure_failures": 0,
        "repair_loop_count": 0,
        "feeder_cycles": 0,
        "recovery_tasks_spawned": 0,
        "bug_spawned": False,
        # metadata
        "final_validation_status": None,
        "completed_at": None,
        "rows": 0,
    })

    for r in records:
        src_task = r.get("source_task_id") or r.get("task_id", "")
        key = (
            r.get("experiment_id", ""),
            r.get("source_project", ""),
            src_task,
            r.get("experiment_variant", ""),
        )
        g = groups[key]
        g["experiment_id"] = key[0]
        g["source_project"] = key[1]
        g["source_task_id"] = key[2]
        g["experiment_variant"] = key[3]
        if not g["task_type"]:
            g["task_type"] = r.get("task_type", "")
        g["rows"] += 1

        # Skip recovery/feeder rows for primary metrics — count them as overhead
        if r.get("is_recovery_task"):
            g["recovery_tasks_spawned"] += 1
            continue
        if r.get("is_research_feeder"):
            g["feeder_cycles"] = max(g["feeder_cycles"], int(r.get("research_feeder_cycles") or 1))
            continue

        # Primary: completion status (latest row wins)
        status = r.get("status") or ("completed" if r.get("validation_passed") else "")
        if status == "completed" or r.get("validation_passed"):
            g["completed"] = True
            g["failed"] = False
        elif status == "failed" and not g["completed"]:
            g["failed"] = True

        attempts = int(r.get("attempts") or 0)
        g["total_attempts"] = max(g["total_attempts"], attempts)

        # Validation tracking
        if r.get("validation_passed") is not None:
            g["validation_runs"] += 1
            if r.get("validation_passed"):
                g["validation_passes"] += 1
        if r.get("pipeline_validation_passed") is not None and r.get("validation_passed") is None:
            g["validation_runs"] += 1
            if r.get("pipeline_validation_passed"):
                g["validation_passes"] += 1

        # Secondary: aggregate continuous metrics
        g["total_work_loops"] += int(r.get("work_loops") or 0)
        g["diff_insertions"] += int(r.get("diff_insertions") or 0)
        g["diff_deletions"] += int(r.get("diff_deletions") or 0)
        if r.get("bug_spawned"):
            g["bug_spawned"] = True
        if r.get("infrastructure_failure"):
            g["infrastructure_failures"] += 1
        g["repair_loop_count"] += int(r.get("repair_attempts") or 0)
        g["feeder_cycles"] = max(g["feeder_cycles"], int(r.get("research_feeder_cycles") or 0))

        # Track latest completion timestamp
        cat = r.get("completed_at")
        if cat and (g["completed_at"] is None or cat > g["completed_at"]):
            g["completed_at"] = cat
            g["final_validation_status"] = "passed" if r.get("validation_passed") else "failed"

    result = []
    for g in groups.values():
        val_rate = round(g["validation_passes"] / g["validation_runs"], 3) if g["validation_runs"] > 0 else None
        result.append({
            "experiment_id": g["experiment_id"],
            "source_project": g["source_project"],
            "source_task_id": g["source_task_id"],
            "experiment_variant": g["experiment_variant"],
            "task_type": g["task_type"],
            # primary
            "completed": g["completed"],
            "failed": g["failed"],
            "total_attempts": g["total_attempts"],
            "feeder_cycles": g["feeder_cycles"],
            "recovery_tasks_spawned": g["recovery_tasks_spawned"],
            "final_validation_status": g["final_validation_status"],
            "infrastructure_failure_count": g["infrastructure_failures"],
            "repair_loop_count": g["repair_loop_count"],
            # secondary
            "total_work_loops": g["total_work_loops"],
            "diff_insertions": g["diff_insertions"],
            "diff_deletions": g["diff_deletions"],
            "bug_spawned": g["bug_spawned"],
            "validation_pass_rate": val_rate,
            "completed_at": g["completed_at"],
            "raw_rows": g["rows"],
        })

    # Sort by source_task_id for stable output
    result.sort(key=lambda x: (x["experiment_id"], x["source_project"], x["source_task_id"], x["experiment_variant"]))
    return result


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

        project_filter = request.args.get("project")
        if project_filter:
            records = [r for r in records if r.get("project") == project_filter]
        source_filter = request.args.get("source_project")
        if source_filter:
            records = [r for r in records if r.get("source_project") == source_filter]

        # Group by experiment_variant label first; fall back to pipeline_variant phases
        groups = defaultdict(list)
        for r in records:
            exp_label = r.get("experiment_variant", "")
            variant = r.get("pipeline_variant")
            if exp_label:
                key = exp_label
            elif variant:
                key = "→".join(variant) if isinstance(variant, list) else str(variant)
            else:
                continue
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
            # For variant D: tally phase orderings used
            phase_order_counts: dict = {}
            for r in rows:
                po = r.get("phase_order")
                if po and isinstance(po, list):
                    pk = "→".join(po)
                    phase_order_counts[pk] = phase_order_counts.get(pk, 0) + 1

            summary[variant_key] = {
                "n": len(rows),
                "pipeline": rows[0].get("pipeline_variant") if rows else [],
                "experiment_variant": rows[0].get("experiment_variant", "") if rows else "",
                "experiment_arm": rows[0].get("experiment_arm", "") if rows else "",
                "work_loops_mean": _mean(loops),
                "work_loops_median": _median(loops),
                "work_loops_stddev": _stddev(loops),
                "validation_pass_rate": round(len(passed) / len(rows), 3) if rows else None,
                "bug_spawn_rate": round(len(bugged) / len(rows), 3) if rows else None,
                "diff_insertions_mean": _mean(insertions),
                "diff_deletions_mean": _mean(deletions),
                "phase_order_counts": phase_order_counts or None,
                "valid_order_count": len([r for r in rows if r.get("is_valid_order") is True]),
                "invalid_order_count": len([r for r in rows if r.get("is_valid_order") is False]),
            }

        # Statistical significance vs control (plan→scout→work→validate)
        control_key = "control" if "control" in groups else "plan→scout→work→validate"
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

        # Per-task comparison: group by source_task_id, show each variant's result side by side
        per_task: dict = {}
        for r in records:
            stid = r.get("source_task_id")
            if not stid:
                continue
            if source_filter and r.get("source_project") != source_filter:
                continue
            if stid not in per_task:
                per_task[stid] = {
                    "source_task_id": stid,
                    "source_project": r.get("source_project", ""),
                    "task_type": r.get("task_type", ""),
                    "variants": {},
                }
            exp = r.get("experiment_variant") or (
                "→".join(r["pipeline_variant"]) if isinstance(r.get("pipeline_variant"), list) else str(r.get("pipeline_variant", ""))
            )
            per_task[stid]["variants"][exp] = {
                "project": r.get("project"),
                "task_id": r.get("task_id"),
                "work_loops": r.get("work_loops"),
                "validation_passed": r.get("validation_passed"),
                "bug_spawned": r.get("bug_spawned"),
                "attempts": r.get("attempts"),
                "diff_insertions": r.get("diff_insertions"),
                "diff_deletions": r.get("diff_deletions"),
                "phase_order": r.get("phase_order"),
                "is_valid_order": r.get("is_valid_order"),
                "invalidity_reason": r.get("invalidity_reason"),
                "completed_at": r.get("completed_at"),
            }

        return jsonify({
            "total_records": len(records),
            "variants": summary,
            "per_task": list(per_task.values()),
            "metrics_file": metrics_path,
        })

    @app.route("/api/experiment/source-tasks", methods=["GET"])
    def get_source_task_metrics():
        """Source-task-level experiment metrics aggregation (WS6).

        Groups raw experiment_metrics rows by (experiment_id, source_project,
        source_task_id, experiment_variant).  Recovery tasks and research
        feeder rows are counted as overhead rather than independent events so
        that retry cascades do not inflate completion scores.

        Optional query params:
          ?experiment_id=   filter by experiment
          ?source_project=  filter by source project
          ?experiment_variant= filter by variant label
          ?format=csv       return CSV instead of JSON
        """
        import json as _json

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

        exp_filter = request.args.get("experiment_id")
        src_filter = request.args.get("source_project")
        var_filter = request.args.get("experiment_variant")
        if exp_filter:
            records = [r for r in records if r.get("experiment_id") == exp_filter]
        if src_filter:
            records = [r for r in records if r.get("source_project") == src_filter]
        if var_filter:
            records = [r for r in records if r.get("experiment_variant") == var_filter]

        aggregated = _aggregate_source_tasks(records)

        fmt = request.args.get("format", "json")
        if fmt == "csv":
            import io
            import csv
            buf = io.StringIO()
            if aggregated:
                writer = csv.DictWriter(buf, fieldnames=list(aggregated[0].keys()))
                writer.writeheader()
                writer.writerows(aggregated)
            return Response(buf.getvalue(), mimetype="text/csv",
                            headers={"Content-Disposition": "attachment; filename=source_tasks.csv"})

        return jsonify({
            "total_source_tasks": len(aggregated),
            "raw_records_used": len(records),
            "source_tasks": aggregated,
            "metrics_file": metrics_path,
        })
