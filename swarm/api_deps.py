"""Dependency graph route handlers for the Swarm API."""

import json as _json
import os as _os
from flask import jsonify, request as _req

from swarm.maintenance import plans as plan_cleanup
from swarm.maintenance import file_locks as file_lock_cleanup
from swarm.maintenance.project_heads import get_project_head
from swarm.task_mutations import cancel_task_with_metadata, replace_task_dependencies
from swarm.integrity import is_valid_project_head


def _dot_quote(value: str) -> str:
    s = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _ghost_timestamp(task: dict, project: str) -> float:
    """Extract a sortable timestamp from a ghost task dict.

    Prefers the ISO ``completed`` field, then falls back to parsing the
    numeric suffix of the task ID (assumed unix-ms).  Returns 0.0 when no
    ordering signal is available.
    """
    completed = task.get("completed") or task.get("completed_at") or ""
    if completed:
        try:
            if isinstance(completed, (int, float)):
                return float(completed)
            if isinstance(completed, str):
                if completed.isdigit() and len(completed) >= 13:
                    return float(completed) / 1000.0
                from datetime import datetime
                return datetime.fromisoformat(
                    completed.replace("Z", "+00:00")
                ).timestamp()
        except Exception:
            pass
    # Fall back to task-ID suffix
    tid = task.get("id", "")
    if tid.startswith(project + "-"):
        suffix = tid[len(project) + 1:]
        for part in reversed(suffix.split("-")):
            if part.isdigit() and len(part) >= 10:
                try:
                    ts = float(part)
                    # unix-ms (>1e12) vs unix-s
                    return ts if ts > 1e12 else ts * 1000.0
                except ValueError:
                    pass
    return 0.0


def _load_history_tasks(data_dir, project=None, max_entries=500):
    """Return completed/failed tasks from task-history.jsonl."""
    history_path = _os.path.join(str(data_dir), "task-history.jsonl")
    if not _os.path.exists(history_path):
        return []
    tasks = []
    try:
        with open(history_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        for line in lines[-max_entries:]:
            line = line.strip()
            if not line:
                continue
            try:
                t = _json.loads(line)
                if project and t.get("project") != project:
                    continue
                tasks.append(t)
            except Exception:
                pass
    except Exception:
        pass
    return tasks


def _completed_ids_from_history(data_dir, project=None) -> set[str]:
    """Return completed task IDs from task-history.jsonl."""
    return {
        t.get("id")
        for t in _load_history_tasks(data_dir, project=project)
        if t.get("id") and t.get("status") == "completed"
    }


def _resolve_project_head_for_dot(db, project: str, history_tasks: list[dict]) -> str | None:
    """Return the best project HEAD marker for dependency DOT rendering.

    ``get_project_head`` validates against live tasks and the compact completed
    archive. Historical graph rendering can still have a richer task record in
    ``task-history.jsonl`` after the live row was pruned, so accept a stored
    HEAD when the local history record itself is a valid project task.
    """
    head = get_project_head(db, project)
    if head:
        return head

    try:
        stored_head = (db.project_get(project) or {}).get("head_task_id")
    except Exception:
        stored_head = None
    if not stored_head:
        return None

    by_id = {task.get("id"): task for task in history_tasks if task.get("id")}
    if is_valid_project_head(by_id.get(stored_head), project):
        return stored_head
    return None


def _select_history_with_ancestry(history_tasks, seed_ids=None, limit=500, max_depth=None):
    """Select a history slice while preserving dependency ancestry.

    ``limit`` only controls how many seed tail nodes are chosen when no explicit
    ``seed_ids`` are provided. ``max_depth`` controls how many ancestor steps to
    follow from those seeds.
    """
    if not history_tasks:
        return []

    ordered = list(history_tasks)
    by_id = {task.get("id"): task for task in ordered if task.get("id")}
    if seed_ids:
        seeds = [sid for sid in seed_ids if sid in by_id]
    else:
        tail_count = max(0, int(limit))
        seeds = [
            task.get("id")
            for task in ordered[-tail_count:] if task.get("id")
        ] if tail_count else []

    included = set(seeds)
    # stack entries: (task_id, depth)
    stack = [(sid, 0) for sid in seeds]
    while stack:
        task_id, depth = stack.pop()
        if max_depth is not None and depth >= max_depth:
            continue
        task = by_id.get(task_id)
        if not task:
            continue
        for dep in _norm_deps(task.get("dependencies")):
            if dep in by_id and dep not in included:
                included.add(dep)
                stack.append((dep, depth + 1))

    return [task for task in ordered if task.get("id") in included]


def _ghost_lines_for_tasks(history_tasks, active_ids, shown_ids,
                           head_task_id=None, project=None):
    """
    Build DOT fragment lines for ghost nodes.

    Ghost nodes are sorted chronologically and grouped into ``rank=same``
    clusters (30-second buckets) for top-to-bottom ordering in the graph.
    The project HEAD node is rendered with a distinct star marker and gold
    border so the chain tip is visually obvious.

    Parameters
    ----------
    history_tasks : list[dict]
        Task dicts from task-history.jsonl (or plan task_graph).
    active_ids : set[str]
        IDs already rendered as active nodes — ghost counterparts skipped.
    shown_ids : set[str]
        Mutable set tracking which ghost IDs have been emitted (dedup).
    head_task_id : str, optional
        Current HEAD task ID for the project — rendered distinctly.
    project : str, optional
        Project name, used for timestamp extraction.

    Returns
    -------
    list[str]
        DOT lines: node definitions, edges, and rank=same groupings.
    """
    # Sort by timestamp so the chain runs top → bottom
    def _sort_key(ht):
        proj = project or ht.get("project", "") or ""
        return _ghost_timestamp(ht, proj)

    sorted_tasks = sorted(history_tasks, key=_sort_key)

    lines: list[str] = []
    rank_groups: list[list[str]] = []   # each inner list = one rank=same bucket
    BUCKET = 30.0   # seconds per rank bucket

    for ht in sorted_tasks:
        tid = ht.get("id", "")
        if not tid or tid in active_ids:
            continue

        proj = ht.get("project", "") or project or ""
        status = ht.get("status", "completed")
        deps = ht.get("dependencies") or []
        ts = _ghost_timestamp(ht, proj)

        if tid not in shown_ids:
            shown_ids.add(tid)
            short = tid
            if proj and short.startswith(proj + "-"):
                short = short[len(proj) + 1:]
            if len(short) > 26:
                short = short[:24] + "..."

            is_head = (tid == head_task_id)

            if status == "failed":
                label = f"{short} x"
                fc, tc, bc = "#0d1117", "#da3633", "#da3633"
                style = "dashed,filled"
                penwidth = 1.2
            elif is_head:
                # Star marker + gold border = project HEAD tip
                label = f"{short} *"
                fc, tc, bc = "#0d1117", "#f0e68c", "#d4af37"
                style = "dashed,filled,bold"
                penwidth = 2.5
            else:
                label = f"{short} ok"
                fc, tc, bc = "#0d1117", "#3fb950", "#3fb950"
                style = "dashed,filled"
                penwidth = 1.2

            lines.append(
                f"  {_dot_quote(tid)} [label={_dot_quote(label)}, "
                f"fillcolor=\"{fc}\", fontcolor=\"{tc}\", "
                f"color=\"{bc}\", style=\"{style}\", penwidth={penwidth}];"
            )

        # Accumulate into rank buckets by timestamp
        if ts > 0:
            if not rank_groups:
                rank_groups.append([])
            else:
                last_ts = rank_groups[-1][-1][1]  # (tid, ts) pairs
                if int(ts / BUCKET) != int(last_ts / BUCKET):
                    rank_groups.append([])
            rank_groups[-1].append((tid, ts))
        else:
            # No timestamp — solo bucket so it doesn't pollute timed ordering
            rank_groups.append([(tid, 0.0)])

        # Edges from each dep → this task (ghost→ghost and ghost→active).
        # Only emit if the dep has a node definition; otherwise Graphviz
        # auto-creates an undefined grey node for it.
        for dep in _norm_deps(deps):
            if dep in shown_ids or dep in active_ids:
                lines.append(
                    f"  {_dot_quote(dep)} -> {_dot_quote(tid)} "
                    f"[color=\"#3fb950\", style=dashed, penwidth=1.0];"
                )

    # Emit rank=same groups for buckets with ≥ 2 nodes
    for bucket in rank_groups:
        ids_in = [t[0] for t in bucket if t[0] in shown_ids]
        if len(ids_in) > 1:
            ids_str = "; ".join(_dot_quote(i) for i in ids_in)
            lines.append(f"  {{ rank=same; {ids_str} }}")

    return lines


def _arg_bool(name: str, default: bool = False) -> bool:
    raw = (_req.args.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _build_adjacency(tasks):
    by_id = {t["id"]: t for t in tasks}
    return {
        tid: [d for d in _norm_deps(t.get("dependencies")) if d in by_id]
        for tid, t in by_id.items()
    }


def _norm_deps(raw):
    deps = raw or []
    out = []
    seen = set()
    for d in deps:
        if not isinstance(d, str):
            continue
        dep = d.strip()
        if not dep or dep in seen:
            continue
        seen.add(dep)
        out.append(dep)
    return out


def _find_cycle_nodes(tasks):
    graph = _build_adjacency(tasks)
    visited, visiting, in_cycle = set(), set(), set()

    def _dfs(node):
        visiting.add(node)
        for dep in graph.get(node, []):
            if dep in visiting:
                in_cycle.add(node)
                in_cycle.add(dep)
                continue
            if dep not in visited:
                _dfs(dep)
        visiting.remove(node)
        visited.add(node)

    for nid in list(graph.keys()):
        if nid not in visited:
            _dfs(nid)
    return sorted(in_cycle)


def _active_integrity(tasks, completed_ids):
    active_ids = {t["id"] for t in tasks}
    self_deps, missing, edge_count = [], [], 0
    for t in tasks:
        tid = t["id"]
        deps = _norm_deps(t.get("dependencies"))
        edge_count += len(deps)
        if tid in deps:
            self_deps.append(tid)
        missing_deps = [d for d in deps if d not in active_ids and d not in completed_ids]
        if missing_deps:
            missing.append({"task_id": tid, "missing_dependencies": missing_deps})
    cycle_nodes = _find_cycle_nodes(tasks)
    return {
        "task_count": len(tasks),
        "edge_count": edge_count,
        "self_dependencies": sorted(self_deps),
        "missing_dependencies": missing,
        "cycle_nodes": cycle_nodes,
        "has_issues": bool(self_deps or missing or cycle_nodes),
    }


def _continuation_candidates(all_active):
    by_failed: dict[str, list[dict]] = {}
    for task in all_active:
        meta = task.get("metadata") or {}
        keys = []
        failed_task_id = meta.get("failed_task_id")
        if failed_task_id:
            keys.append(failed_task_id)
        continuation_for = meta.get("continuation_for_failed_task")
        if continuation_for:
            keys.append(continuation_for)
        for key in keys:
            by_failed.setdefault(key, []).append(task)
    return by_failed


def _pick_canonical_continuation(candidates):
    live = [t for t in candidates if (t.get("status") in ("pending", "in_progress", "completed"))]
    if not live:
        return None
    live.sort(key=lambda t: (
        0 if t.get("status") == "in_progress" else 1,
        0 if t.get("status") == "pending" else 1,
        t.get("created") or "",
        t.get("id") or "",
    ))
    return live[0]


def _audit_findings(db, active_tasks, completed_ids, project=None, project_registry=None):
    active_by_id = {t["id"]: t for t in active_tasks}
    all_active = db.task_get_all()
    if project:
        all_active = [t for t in all_active if t.get("project") == project]
    all_by_id = {t["id"]: t for t in all_active}
    continuation_by_failed = _continuation_candidates(all_active)

    stale_heads = []
    projects = [project] if project else sorted({t.get("project") for t in all_active if t.get("project")})
    for project_name in projects:
        proj = db.project_get(project_name)
        if not proj:
            continue
        stored_head = proj.get("head_task_id")
        if not stored_head:
            stale_heads.append({"project": project_name, "head_task_id": None, "reason": "missing_head"})
            continue
        task = db.task_get(stored_head) or db.task_get_completed_record(stored_head)
        if not is_valid_project_head(task, project_name):
            stale_heads.append({"project": project_name, "head_task_id": stored_head, "reason": "invalid_head"})

    stale_plans = []
    for project_name in projects:
        for plan in db.plan_get_by_project(project_name):
            reasons = plan_cleanup.plan_staleness_reasons(db, project_name, plan)
            if reasons:
                stale_plans.append({"project": project_name, "plan_id": plan["id"], "reasons": reasons})

    orphaned_agents = []
    for agent in db.agent_get_active():
        task_id = agent.get("task_id")
        task = db.task_get(task_id) if task_id else None
        if task is None:
            orphaned_agents.append({"agent_id": agent["id"], "task_id": task_id, "reason": "missing_task"})
        elif task.get("status") != "in_progress" or task.get("agent_id") != agent.get("id"):
            orphaned_agents.append({"agent_id": agent["id"], "task_id": task_id, "reason": "ownership_mismatch"})

    stale_file_locks = []
    if project_registry is not None:
        stale_file_locks = file_lock_cleanup.find_stale_file_locks(
            db, project_registry, project=project
        )

    recursive_recovery = []
    dead_blockers = []
    continuity_gaps = []
    for task in active_tasks:
        tid = task["id"]
        meta = task.get("metadata") or {}
        if meta.get("is_recovery_task"):
            upstream = all_by_id.get(meta.get("failed_task_id"))
            if upstream and (upstream.get("metadata") or {}).get("is_recovery_task"):
                recursive_recovery.append({"task_id": tid, "failed_task_id": meta.get("failed_task_id")})

        deps = _norm_deps(task.get("dependencies"))
        dead = []
        live = []
        for dep in deps:
            dep_task = all_by_id.get(dep)
            if dep in completed_ids or (dep_task and dep_task.get("status") == "completed"):
                continue
            if dep_task is None or dep_task.get("status") in ("failed", "cancelled"):
                candidates = continuation_by_failed.get(dep, [])
                canonical = _pick_canonical_continuation(candidates)
                dead.append({
                    "dependency": dep,
                    "reason": "missing" if dep_task is None else dep_task.get("status"),
                    "continuation_task_id": canonical.get("id") if canonical else None,
                })
                if canonical is None:
                    continuity_gaps.append({"project": task.get("project"), "task_id": tid, "blocked_dependency": dep})
            else:
                live.append(dep)
        if dead and not live:
            dead_blockers.append({
                "project": task.get("project"),
                "task_id": tid,
                "dead_dependencies": dead,
            })

    return {
        "stale_heads": stale_heads,
        "stale_plans": stale_plans,
        "orphaned_agents": orphaned_agents,
        "stale_file_locks": stale_file_locks,
        "dead_blockers": dead_blockers,
        "recursive_recovery": recursive_recovery,
        "continuity_gaps": continuity_gaps,
    }


def _split_integrity_findings(findings):
    live = {
        "stale_heads": findings.get("stale_heads", []),
        "orphaned_agents": findings.get("orphaned_agents", []),
        "stale_file_locks": findings.get("stale_file_locks", []),
        "dead_blockers": findings.get("dead_blockers", []),
        "recursive_recovery": findings.get("recursive_recovery", []),
        "continuity_gaps": findings.get("continuity_gaps", []),
    }
    archival = {
        "stale_plans": findings.get("stale_plans", []),
    }
    return live, archival


def _summarize_integrity_findings(active, findings, history=None):
    live_findings, archival_findings = _split_integrity_findings(findings)
    live_counts = {key: len(items) for key, items in live_findings.items()}
    archival_counts = {key: len(items) for key, items in archival_findings.items()}
    summary = {
        "active": {
            "task_count": len(active.get("tasks", [])),
            "missing_dependencies": len(active.get("missing_dependencies", [])),
            "dependency_violations": len(active.get("dependency_violations", [])),
            "ready_tasks": len(active.get("ready_tasks", [])),
            "blocked_tasks": len(active.get("blocked_tasks", [])),
        },
        "live": {
            **live_counts,
            "problem_count": sum(live_counts.values()),
        },
        "archival": {
            **archival_counts,
            "problem_count": sum(archival_counts.values()),
        },
    }
    if history is not None:
        summary["archival"]["history_missing_dependencies"] = history.get("missing_dependencies_count", 0)
        summary["archival"]["history_task_count"] = history.get("task_count", 0)
    return summary, live_findings, archival_findings


def register_routes(app, task_source, db, data_dir=None, project_registry=None):
    """Register dependency graph routes on the Flask app."""
    # ---------- Dependency Graph ----------

    @app.route("/api/dependencies", methods=["GET"])
    def get_dependency_graph():
        from swarm.dependencies import build_graph_from_tasks
        tasks = task_source.get_all_tasks()
        graph = build_graph_from_tasks(tasks)
        return jsonify(graph.get_stats())

    @app.route("/api/dependencies/dot", methods=["GET"])
    def get_dependency_dot():
        from swarm.dependencies import build_graph_from_tasks

        active_tasks = task_source.get_all_tasks()
        project = _req.args.get("project", "").strip()
        try:
            history_depth = int((_req.args.get("history_depth") or _req.args.get("history_limit") or "2").strip())
        except Exception:
            history_depth = 2
        history_depth = max(0, min(history_depth, 25))
        if project:
            filtered = [t for t in active_tasks if t.project == project]
        else:
            filtered = active_tasks

        graph = build_graph_from_tasks(filtered)
        dot = graph.to_dot()
        active_ids = {t.id for t in filtered}
        shown_ids = set()
        extra_lines = []

        # Resolve project HEAD for distinct styling. Project views may refine
        # this against task-history.jsonl below when the live archive is sparse.
        head_task_id = None
        if project and project_registry is not None:
            head_task_id = get_project_head(db, project)

        if data_dir:
            if project:
                # --- Project view: only ancestors of live tasks + project HEAD ---
                # Show only completed tasks that live tasks directly depend on,
                # plus their ancestry chain. Avoids rendering 100+ historical nodes
                # when a project has been running for a while.
                dep_ids_needed = {
                    dep
                    for t in filtered
                    for dep in (t.dependencies or [])
                    if dep not in active_ids
                }
                # Also anchor to the project HEAD so the chain tip is always visible
                if head_task_id and head_task_id not in active_ids:
                    dep_ids_needed.add(head_task_id)
                all_history = _load_history_tasks(data_dir, project=project, max_entries=5000)
                head_task_id = _resolve_project_head_for_dot(db, project, all_history)
                if head_task_id and head_task_id not in active_ids:
                    dep_ids_needed.add(head_task_id)
                if dep_ids_needed:
                    history = _select_history_with_ancestry(
                        all_history,
                        seed_ids=dep_ids_needed,
                        max_depth=history_depth,
                    )
                else:
                    # No live deps — show just the recent tail so graph isn't empty
                    history = _select_history_with_ancestry(
                        all_history,
                        limit=min(max(history_depth, 1), 10),
                        max_depth=history_depth,
                    )
                extra_lines = _ghost_lines_for_tasks(
                    history, active_ids, shown_ids,
                    head_task_id=head_task_id, project=project
                )
                try:
                    plan_tasks = plan_cleanup.latest_renderable_plan_tasks(db, project)
                    if plan_tasks:
                        extra_lines += _ghost_lines_for_tasks(
                            plan_tasks, active_ids, shown_ids,
                            head_task_id=head_task_id, project=project
                        )
                except Exception:
                    pass
            else:
                # --- Global view: only direct dep-ancestors ---
                dep_ids_needed = {
                    dep
                    for t in filtered
                    for dep in (t.dependencies or [])
                    if dep not in active_ids
                }
                if dep_ids_needed:
                    ancestors = _select_history_with_ancestry(
                        _load_history_tasks(data_dir, max_entries=5000),
                        seed_ids=dep_ids_needed,
                        max_depth=history_depth,
                    )
                    extra_lines = _ghost_lines_for_tasks(
                        ancestors, active_ids, shown_ids
                    )


        # Draw edges from live tasks to rendered ghost deps.
        # Only emit when the ghost dep is in shown_ids (already has a node
        # definition), so deep unrendered ancestors don't create grey nodes.
        live_to_ghost_edges: list[str] = []
        for t in filtered:
            for dep in (t.dependencies or []):
                if dep in shown_ids:
                    live_to_ghost_edges.append(
                        f"  {_dot_quote(dep)} -> {_dot_quote(t.id)} "
                        f'[color="#58a6ff", penwidth=1.0];'
                    )
        if live_to_ghost_edges:
            extra_lines = extra_lines + live_to_ghost_edges

        if extra_lines:
            dot = dot.rstrip()
            if dot.endswith("}"):
                dot = dot[:-1] + "\n" + "\n".join(extra_lines) + "\n}"

        has_nodes = any(
            '[label=' in line and '->' not in line
            for line in dot.splitlines()
        )
        if not has_nodes:
            dot = "digraph {}"

        return jsonify({"dot": dot})

    @app.route("/api/dependencies/ready", methods=["GET"])
    def get_ready_tasks():
        from swarm.dependencies import build_graph_from_tasks
        tasks = task_source.get_all_tasks()
        project = _req.args.get("project", "").strip()
        if project:
            tasks = [t for t in tasks if t.project == project]
        graph = build_graph_from_tasks(tasks)
        completed = set(db.task_get_completed_ids())
        if data_dir:
            completed |= _completed_ids_from_history(data_dir, project=project or None)
        completed |= {t.id for t in tasks if t.status == "completed"}
        ready_ids = graph.get_ready_tasks(completed)
        ready = [
            {
                "id": t.id, "project": t.project, "type": t.type,
                "description": t.description, "priority": t.priority,
                "dependencies": t.dependencies,
            }
            for tid in ready_ids
            for t in tasks
            if t.id == tid
        ]
        return jsonify({"ready": ready})

    @app.route("/api/dependencies/execution-order", methods=["GET"])
    def get_execution_order():
        from swarm.dependencies import build_graph_from_tasks
        tasks = task_source.get_all_tasks()
        graph = build_graph_from_tasks(tasks)
        return jsonify({"levels": graph.get_execution_order()})

    @app.route("/api/task-lookup/<task_id>", methods=["GET"])
    def task_lookup(task_id):
        """Return task details from active tasks or task-history.jsonl."""
        try:
            active = db.task_get(task_id)
            if active:
                return jsonify({"source": "active", "task": active})
        except Exception:
            pass
        if data_dir:
            history = _load_history_tasks(data_dir)
            for t in history:
                if t.get("id") == task_id:
                    return jsonify({"source": "history", "task": t})
        return jsonify({"error": "not found"}), 404

    @app.route("/api/dependencies/integrity", methods=["GET", "POST"])
    def dependency_integrity():
        project = _req.args.get("project", "").strip()
        include_history = _arg_bool("include_history", default=False)
        repair = _arg_bool("repair", default=False)
        try:
            sample_limit = int((_req.args.get("history_sample_limit") or "50").strip())
        except Exception:
            sample_limit = 50
        sample_limit = max(0, min(sample_limit, 500))

        all_active = db.task_get_all()
        active_tasks = [t for t in all_active if not project or t.get("project") == project]
        completed_ids = set(db.task_get_completed_ids())
        completed_ids |= {
            t["id"] for t in all_active
            if t.get("status") == "completed"
            and (not project or t.get("project") == project)
        }
        cancelled_ids = {t["id"] for t in all_active if t.get("status") == "cancelled"}

        active = _active_integrity(active_tasks, completed_ids)
        repaired = []
        findings = _audit_findings(
            db,
            active_tasks,
            completed_ids,
            project=project or None,
            project_registry=project_registry,
        )

        if repair:
            stale_locks_repaired = []
            if project_registry is not None:
                stale_locks_repaired = file_lock_cleanup.reconcile_stale_file_locks(
                    db, project_registry, project=project or None
                )

            missing_by_task = {
                m["task_id"]: set(m["missing_dependencies"])
                for m in active["missing_dependencies"]
            }
            for t in active_tasks:
                tid = t["id"]
                deps = t.get("dependencies") or []
                bad = set()
                if tid in missing_by_task:
                    bad |= missing_by_task[tid]
                bad |= {d for d in deps if d in cancelled_ids}
                if not bad:
                    continue
                new_deps = [d for d in deps if d not in bad]
                if new_deps != deps:
                    replace_task_dependencies(db, tid, {}, drop=bad)
                    repaired.append({
                        "task_id": tid,
                        "removed_dependencies": sorted(bad),
                        "new_dependencies": new_deps,
                    })

            continuity_repaired = []
            continuity_cancelled = []
            for finding in findings["dead_blockers"]:
                tid = finding["task_id"]
                task = db.task_get(tid)
                if not task or task.get("status") != "pending":
                    continue
                deps = _norm_deps(task.get("dependencies"))
                replacements = {}
                unresolved = []
                for dead in finding["dead_dependencies"]:
                    dep = dead["dependency"]
                    continuation = dead.get("continuation_task_id")
                    if continuation:
                        replacements[dep] = continuation
                    else:
                        unresolved.append(dep)
                if replacements:
                    new_deps = [replacements.get(dep, dep) for dep in deps if dep not in unresolved]
                    new_deps = list(dict.fromkeys(new_deps))
                    replace_task_dependencies(db, tid, replacements, drop=unresolved)
                    continuity_repaired.append({
                        "task_id": tid,
                        "new_dependencies": new_deps,
                        "replaced_dependencies": replacements,
                    })
                elif unresolved and all(dep in unresolved for dep in deps):
                    cancel_task_with_metadata(db, tid, {
                        "cancelled_by_dead_dependency_repair": True,
                        "dead_dependency_blockers": unresolved,
                    })
                    continuity_cancelled.append({
                        "task_id": tid,
                        "cancelled_dependencies": unresolved,
                    })
            all_active = db.task_get_all()
            active_tasks = [t for t in all_active if not project or t.get("project") == project]
            completed_ids = set(db.task_get_completed_ids())
            completed_ids |= {
                t["id"] for t in all_active
                if t.get("status") == "completed"
                and (not project or t.get("project") == project)
            }
            active = _active_integrity(active_tasks, completed_ids)
            findings = _audit_findings(
                db,
                active_tasks,
                completed_ids,
                project=project or None,
                project_registry=project_registry,
            )

        out = {
            "project": project or None,
            "include_history": include_history,
            "repair_applied": bool(repair),
            "repaired_count": len(repaired),
            "repaired": repaired,
            "active": active,
            "findings": findings,
        }
        if repair:
            out["continuity_repaired"] = continuity_repaired
            out["continuity_cancelled"] = continuity_cancelled
            out["stale_locks_repaired"] = stale_locks_repaired

        if include_history and data_dir:
            history_tasks = _load_history_tasks(data_dir, project=project or None)
            history_ids = {h.get("id") for h in history_tasks if h.get("id")}
            active_ids = {t["id"] for t in active_tasks}
            history_missing = []
            for h in history_tasks:
                tid = h.get("id")
                if not tid:
                    continue
                deps = _norm_deps(h.get("dependencies"))
                missing = [
                    d for d in deps
                    if d not in history_ids and d not in active_ids and d not in completed_ids
                ]
                if missing:
                    history_missing.append({"task_id": tid, "missing_dependencies": missing})
            out["history"] = {
                "task_count": len(history_tasks),
                "missing_dependencies_count": len(history_missing),
                "missing_dependencies_sample": history_missing[:sample_limit],
                "sample_truncated": len(history_missing) > sample_limit,
            }
        elif include_history:
            out["history"] = {
                "task_count": 0,
                "missing_dependencies_count": 0,
                "missing_dependencies_sample": [],
                "sample_truncated": False,
            }

        summary, live_findings, archival_findings = _summarize_integrity_findings(
            active,
            findings,
            history=out.get("history"),
        )
        out["summary"] = summary
        out["live_findings"] = live_findings
        out["archival_findings"] = archival_findings

        return jsonify(out)

    @app.route("/api/task-history", methods=["GET"])
    def get_task_history():
        keyword = (_req.args.get("q") or "").strip().lower()
        project = (_req.args.get("project") or "").strip()
        status = (_req.args.get("status") or "").strip().lower()
        try:
            limit = int((_req.args.get("limit") or "50").strip())
        except Exception:
            limit = 50
        limit = max(1, min(limit, 200))

        tasks = _load_history_tasks(data_dir, project=project or None)
        if keyword:
            tasks = [t for t in tasks if keyword in (t.get("description") or "").lower()]
        if status in ("completed", "failed"):
            tasks = [t for t in tasks if t.get("status") == status]

        total = len(tasks)
        tasks = tasks[:limit]
        results = [
            {
                "id": t.get("id"),
                "project": t.get("project"),
                "type": t.get("type"),
                "description": t.get("description"),
                "status": t.get("status"),
                "completed": t.get("completed"),
            }
            for t in tasks
        ]
        return jsonify({"results": results, "total": total})
