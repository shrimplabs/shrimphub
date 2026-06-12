"""Dependency graph route handlers for the Swarm API."""

import json as _json
import os as _os
from flask import jsonify, request as _req

from swarm.maintenance import plans as plan_cleanup
from swarm.maintenance import file_locks as file_lock_cleanup
from swarm.maintenance.project_heads import get_project_head
from swarm.task_mutations import cancel_task_with_metadata, replace_task_dependencies
from swarm.integrity import is_valid_project_head
from swarm.orchestrator import _is_expansion_blocked


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
    """Return completed/failed tasks from task-history.jsonl.

    Uses a tail-read strategy: seeks to the end of the file and reads backward
    in 256KB chunks to avoid loading the entire (potentially 100MB+) file.
    """
    history_path = _os.path.join(str(data_dir), "task-history.jsonl")
    if not _os.path.exists(history_path):
        return []
    tasks = []
    try:
        chunk_size = 256 * 1024  # 256 KB
        with open(history_path, "rb") as fh:
            fh.seek(0, 2)
            file_size = fh.tell()
            # Read up to max_entries * ~2KB average line size from the end
            read_size = min(file_size, chunk_size * max(1, max_entries // 100))
            fh.seek(max(0, file_size - read_size))
            raw = fh.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        # Take only the last max_entries lines
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


def _completed_ids_from_history(data_dir, project=None, db=None) -> set[str]:
    """Return completed task IDs, preferring the live DB over JSONL.

    Now that completed tasks stay in the tasks table, this is primarily a
    JSONL fallback for pre-migration data.  When *db* is provided, any ID
    already present in the live tasks table is excluded from the JSONL
    results to avoid duplicates (DB wins).
    """
    # DEPRECATED: JSONL scan is a fallback for pre-migration tasks only.
    # Remove after 2025-07-01 once all DBs have accumulated enough history.
    jsonl_ids = {
        t.get("id")
        for t in _load_history_tasks(data_dir, project=project)
        if t.get("id") and t.get("status") == "completed"
    }
    if db is not None:
        # Dedupe: any task ID in the live DB takes precedence over JSONL
        # Use task_get_completed_ids() — much cheaper than full task_get_all()
        live_ids = set(db.task_get_completed_ids())
        jsonl_ids -= live_ids
    return jsonl_ids


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

    # Detect expansion-gated repair deadlock: a stalled/frozen project has pending
    # bug/closure-repair tasks whose dep chain passes through an expansion-blocked
    # feature task, so the repairs that would clear the stall can never run.
    repair_path_blocked = []
    if project_registry is not None:
        proj_rows = {}
        for p in {t.get("project") for t in all_active if t.get("project")}:
            proj_obj = project_registry.get(p)
            if proj_obj is None:
                continue
            # project_registry.get() may return a Project dataclass or a dict
            proj_rows[p] = proj_obj.to_dict() if hasattr(proj_obj, "to_dict") else proj_obj
    else:
        proj_rows = {}
    if proj_rows:
        tasks_by_project: dict = {}
        for t in all_active:
            tasks_by_project.setdefault(t.get("project") or "", []).append(t)

        for proj_name, proj_row in proj_rows.items():
            closure_status = (proj_row.get("closure_status") or "").strip().lower()
            if closure_status not in {"frozen", "stalled"}:
                continue
            open_regressions = int(proj_row.get("open_regression_count") or 0)
            if open_regressions == 0:
                continue

            proj_tasks = tasks_by_project.get(proj_name, [])
            proj_by_id = {t["id"]: t for t in proj_tasks}

            # Find pending repair tasks (bug type or closure_repair metadata, not already runnable)
            repair_candidates = [
                t for t in proj_tasks
                if t.get("status") == "pending"
                and (
                    (t.get("type") or "").lower() == "bug"
                    or (t.get("metadata") or {}).get("is_closure_repair_task")
                )
            ]

            for repair_task in repair_candidates:
                # Walk deps one level — if any direct dep is expansion-blocked, flag it
                blocking_via = []
                for dep_id in _norm_deps(repair_task.get("dependencies")):
                    dep = proj_by_id.get(dep_id)
                    if dep is None:
                        continue
                    if dep.get("status") != "pending":
                        continue
                    if _is_expansion_blocked(dep, proj_rows):
                        blocking_via.append(dep_id)
                if blocking_via:
                    repair_path_blocked.append({
                        "project": proj_name,
                        "repair_task_id": repair_task["id"],
                        "blocked_by_expansion_gated": blocking_via,
                        "suggestion": (
                            f"Remove dep on expansion-blocked task(s) so repair can run: "
                            f"DELETE /api/tasks/{repair_task['id']}/dependencies/<dep_id>"
                        ),
                    })

    return {
        "stale_heads": stale_heads,
        "stale_plans": stale_plans,
        "orphaned_agents": orphaned_agents,
        "stale_file_locks": stale_file_locks,
        "dead_blockers": dead_blockers,
        "recursive_recovery": recursive_recovery,
        "continuity_gaps": continuity_gaps,
        "repair_path_blocked": repair_path_blocked,
    }


def _split_integrity_findings(findings):
    live = {
        "stale_heads": findings.get("stale_heads", []),
        "orphaned_agents": findings.get("orphaned_agents", []),
        "stale_file_locks": findings.get("stale_file_locks", []),
        "dead_blockers": findings.get("dead_blockers", []),
        "recursive_recovery": findings.get("recursive_recovery", []),
        "continuity_gaps": findings.get("continuity_gaps", []),
        "repair_path_blocked": findings.get("repair_path_blocked", []),
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
        tasks = task_source.get_all_tasks(exclude_statuses=("completed", "archived", "cancelled"))
        graph = build_graph_from_tasks(tasks)
        return jsonify(graph.get_stats())

    @app.route("/api/dependencies/dot", methods=["GET"])
    def get_dependency_dot():
        from swarm.dependencies import build_graph_from_tasks

        active_tasks = task_source.get_all_tasks(exclude_statuses=("archived",))
        project = _req.args.get("project", "").strip()
        try:
            history_depth = int((_req.args.get("history_depth") or _req.args.get("history_limit") or "2").strip())
        except Exception:
            history_depth = 2
        history_depth = max(0, min(history_depth, 25))
        # Exclude scaffolding noise from graph view: research feeders, QA reruns,
        # completed/archived planners, and old cancelled sprint task chains.
        _LIVE_EXCLUDE_TYPES = frozenset({"research", "harness_qa", "project_plan"})
        _LIVE_EXCLUDE_ID_PATS = ("rerun", "feeder")
        _LIVE_EXCLUDE_STATUSES = frozenset({"archived"})

        def _is_live_noise(t) -> bool:
            status = getattr(t, "status", None)
            if status in _LIVE_EXCLUDE_STATUSES:
                return True
            # Never hide tasks that are actively running or waiting to run
            if status in ("in_progress", "pending"):
                return False
            if getattr(t, "type", None) in _LIVE_EXCLUDE_TYPES:
                return True
            tid = getattr(t, "id", "") or ""
            if any(p in tid for p in _LIVE_EXCLUDE_ID_PATS):
                return True
            # Cancelled tasks with -agent suffix are old sprint chains — historical noise
            if status == "cancelled" and tid.endswith("-agent"):
                return True
            # Legacy recovery task chains are retry scaffolding — exclude from graph
            # regardless of status (completed recovery chains pollute the graph)
            if tid.startswith("recovery-"):
                return True
            if "bug-recovery-" in tid or "-bug-recovery-" in tid:
                return True
            return False

        if project:
            filtered = [t for t in active_tasks if t.project == project and not _is_live_noise(t)]
        else:
            filtered = [t for t in active_tasks if not _is_live_noise(t)]

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
                # Query DB for completed/cancelled/failed tasks (immutable history).
                # Fall back to JSONL only for pre-migration tasks not in DB.
                # Exclude scaffolding task types that pollute the graph — research
                # feeders, QA reruns, and completed planners are internal machinery,
                # not meaningful chain nodes. Archived tasks are pre-migration legacy
                # rows that should not appear in the graph.
                _GRAPH_EXCLUDE_TYPES = frozenset({"research", "harness_qa", "project_plan"})
                _GRAPH_EXCLUDE_ID_PATTERNS = ("rerun", "feeder")
                _GRAPH_EXCLUDE_STATUSES = frozenset({"archived"})

                def _is_graph_noise(t: dict) -> bool:
                    if t.get("status") in _GRAPH_EXCLUDE_STATUSES:
                        return True
                    if t.get("type") in _GRAPH_EXCLUDE_TYPES:
                        return True
                    tid = t.get("id", "")
                    if any(pat in tid for pat in _GRAPH_EXCLUDE_ID_PATTERNS):
                        return True
                    # Old sprint tasks (cancelled with -agent suffix) are historical
                    # scaffolding noise — they form long cancelled chains under project_plans
                    # and make the graph unreadable.
                    if t.get("status") == "cancelled" and tid.endswith("-agent"):
                        return True
                    # Legacy recovery task chains (recovery-*, bug-bug-recovery-*,
                    # bug-bug-bug-recovery-*) accumulate as dead floating chains with no
                    # live descendants. They're retry scaffolding, not meaningful work nodes.
                    if tid.startswith("recovery-"):
                        return True
                    if "bug-recovery-" in tid or "-bug-recovery-" in tid:
                        return True
                    return False

                db_history = [
                    dict(t) for t in db.task_get_by_project(project)
                    if t.get("status") in ("completed", "cancelled", "failed")
                    and t.get("id") not in active_ids
                    and not _is_graph_noise(t)
                ]
                # task-history.jsonl is a legacy fallback for tasks that predate the
                # "completed tasks stay in DB" migration. Only read it when the DB
                # history is thin — avoids the 93MB file scan on healthy DBs.
                if len(db_history) < 10:
                    jsonl_history = _load_history_tasks(data_dir, project=project, max_entries=200)
                    db_ids = {t["id"] for t in db_history}
                    jsonl_only = [t for t in jsonl_history if t.get("id") not in db_ids and not _is_graph_noise(t)]
                    all_history = db_history + jsonl_only
                else:
                    all_history = db_history

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
                    # DB-first: completed tasks are in the tasks table now.
                    # Exclude archived (pre-migration legacy rows), scaffolding types,
                    # cancelled -agent sprint tasks, and legacy recovery chains.
                    _GLOBAL_EXCLUDE_TYPES = frozenset({"research", "harness_qa", "project_plan"})
                    _GLOBAL_EXCLUDE_ID_PATS = ("rerun", "feeder")

                    def _global_noise(t: dict) -> bool:
                        if t.get("type") in _GLOBAL_EXCLUDE_TYPES:
                            return True
                        tid = t.get("id") or ""
                        if any(p in tid for p in _GLOBAL_EXCLUDE_ID_PATS):
                            return True
                        if t.get("status") == "cancelled" and tid.endswith("-agent"):
                            return True
                        if tid.startswith("recovery-"):
                            return True
                        if "bug-recovery-" in tid or "-bug-recovery-" in tid:
                            return True
                        return False

                    db_hist_global = [
                        dict(t) for t in db.task_get_recent_by_statuses(
                            ("completed", "cancelled", "failed"), limit=500
                        )
                        if t.get("id") not in active_ids
                        and not _global_noise(t)
                    ]
                    ancestors = _select_history_with_ancestry(
                        db_hist_global,
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

    @app.route("/api/dependencies/svg", methods=["GET"])
    def get_dependency_svg():
        """Render the dependency graph server-side via system graphviz dot binary.

        Accepts the same query params as /api/dependencies/dot.
        Returns SVG text directly (Content-Type: image/svg+xml).
        Falls back to returning {dot: ...} JSON if graphviz is not available.
        """
        import subprocess, shutil
        dot_bin = shutil.which("dot")
        if not dot_bin:
            return get_dependency_dot()

        dot_resp = get_dependency_dot()
        # get_dependency_dot returns a Response — extract the dot string
        try:
            dot_data = dot_resp.get_json()
            dot_src = dot_data.get("dot", "") if dot_data else ""
        except Exception:
            return get_dependency_dot()

        if not dot_src or dot_src.strip() in ("digraph {}", ""):
            return get_dependency_dot()

        try:
            result = subprocess.run(
                [dot_bin, "-Tsvg"],
                input=dot_src.encode(),
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0:
                from flask import Response
                return Response(result.stdout, mimetype="image/svg+xml")
        except Exception:
            pass

        return get_dependency_dot()

    @app.route("/api/dependencies/ready", methods=["GET"])
    def get_ready_tasks():
        from swarm.dependencies import build_graph_from_tasks
        tasks = task_source.get_all_tasks(exclude_statuses=("archived",))
        project = _req.args.get("project", "").strip()
        if project:
            tasks = [t for t in tasks if t.project == project]
        graph = build_graph_from_tasks(tasks)
        completed = set(db.task_get_completed_ids())
        if data_dir:
            completed |= _completed_ids_from_history(data_dir, project=project or None, db=db)
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
        tasks = task_source.get_all_tasks(exclude_statuses=("archived",))
        graph = build_graph_from_tasks(tasks)
        return jsonify({"levels": graph.get_execution_order()})

    @app.route("/api/task-lookup/<task_id>", methods=["GET"])
    def task_lookup(task_id):
        """Return task details from the live DB, falling back to task-history.jsonl for pre-migration data."""
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

    @app.route("/api/dependencies/bulk", methods=["POST"])
    def bulk_dependency_mutations():
        """Apply multiple add/remove dependency operations atomically.

        Body: {"ops": [{"action": "add"|"remove", "task_id": "...", "dep_id": "..."}, ...]}
        Returns: {"applied": N, "skipped": [...], "errors": [...]}
        """
        body = _req.get_json(force=True, silent=True) or {}
        ops = body.get("ops") or []
        if not ops:
            return jsonify({"error": "ops list is required"}), 400

        applied = 0
        skipped = []
        errors = []

        for op in ops:
            action = (op.get("action") or "").strip().lower()
            task_id = (op.get("task_id") or "").strip()
            dep_id = (op.get("dep_id") or "").strip()
            if action not in ("add", "remove") or not task_id or not dep_id:
                skipped.append({"op": op, "reason": "invalid op"})
                continue
            try:
                task = db.task_get(task_id)
                if not task:
                    skipped.append({"op": op, "reason": f"task {task_id} not found"})
                    continue
                deps = list(task.get("dependencies") or [])
                if action == "add":
                    if dep_id in deps:
                        skipped.append({"op": op, "reason": "already present"})
                        continue
                    # Cycle check before adding
                    test_deps = deps + [dep_id]
                    all_tasks = db.task_get_all()
                    from swarm.dependencies import build_graph_from_tasks
                    test_tasks = []
                    for t in all_tasks:
                        if t["id"] == task_id:
                            test_tasks.append({**t, "dependencies": test_deps})
                        else:
                            test_tasks.append(t)
                    g = build_graph_from_tasks(test_tasks)
                    if g.has_cycle():
                        errors.append({"op": op, "reason": "would create a cycle"})
                        continue
                    db.task_update(task_id, {"dependencies": test_deps})
                else:  # remove
                    if dep_id not in deps:
                        skipped.append({"op": op, "reason": "not present"})
                        continue
                    db.task_update(task_id, {"dependencies": [d for d in deps if d != dep_id]})
                applied += 1
            except Exception as exc:
                errors.append({"op": op, "reason": str(exc)})

        return jsonify({"applied": applied, "skipped": skipped, "errors": errors})

    @app.route("/api/dependencies/subgraph", methods=["GET"])
    def get_subgraph():
        """Return tasks reachable from a root task.

        Query params:
          root      — task ID (required)
          direction — downstream (default) | upstream | both
          depth     — max hops, 0 = unlimited (default)
          include_completed — include completed/failed tasks (default false)
        """
        root_id = (_req.args.get("root") or "").strip()
        if not root_id:
            return jsonify({"error": "root param is required"}), 400
        direction = (_req.args.get("direction") or "downstream").strip().lower()
        if direction not in ("downstream", "upstream", "both"):
            return jsonify({"error": "direction must be downstream, upstream, or both"}), 400
        try:
            depth = int(_req.args.get("depth") or 0)
        except Exception:
            depth = 0
        include_completed = _arg_bool("include_completed", False)

        all_tasks = db.task_get_all()
        if not include_completed:
            all_tasks = [t for t in all_tasks if t.get("status") not in ("completed", "failed")]

        from swarm.dependencies import build_graph_from_tasks
        graph = build_graph_from_tasks(all_tasks)
        task_ids = graph.get_subgraph(root_id, direction=direction, depth=depth)

        task_map = {t["id"]: t for t in all_tasks}
        tasks_out = []
        for tid in task_ids:
            t = task_map.get(tid)
            if t:
                tasks_out.append({
                    "id": t["id"],
                    "project": t.get("project"),
                    "type": t.get("type"),
                    "status": t.get("status"),
                    "description": (t.get("description") or "").split("\n")[0][:120],
                    "dependencies": t.get("dependencies") or [],
                })

        return jsonify({
            "root": root_id,
            "direction": direction,
            "depth": depth,
            "count": len(tasks_out),
            "tasks": tasks_out,
        })

    @app.route("/api/dependencies/critical-path", methods=["GET"])
    def get_critical_path():
        """Return the longest pending task chain (critical path).

        Query params:
          project — filter to a project (optional)
        """
        project = (_req.args.get("project") or "").strip()

        all_tasks = db.task_get_all()
        from swarm.dependencies import build_graph_from_tasks
        graph = build_graph_from_tasks(all_tasks)
        path_ids = graph.get_critical_path(project=project)

        task_map = {t["id"]: t for t in all_tasks}
        path_out = []
        for tid in path_ids:
            t = task_map.get(tid)
            if t:
                path_out.append({
                    "id": t["id"],
                    "project": t.get("project"),
                    "type": t.get("type"),
                    "status": t.get("status"),
                    "description": (t.get("description") or "").split("\n")[0][:120],
                })

        return jsonify({
            "project": project or None,
            "length": len(path_out),
            "path": path_out,
        })

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

    # ---------- Dep Graph Playback ----------

    def _parse_iso(s):
        """Parse ISO timestamp string to datetime, return None on failure."""
        from datetime import datetime, timezone
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    def _ts_to_iso(s):
        """Normalise a raw timestamp field to ISO string, or return None."""
        if not s:
            return None
        if isinstance(s, (int, float)):
            from datetime import datetime, timezone
            return datetime.fromtimestamp(float(s), tz=timezone.utc).isoformat()
        return str(s)

    @app.route("/api/dependencies/playback-events", methods=["GET"])
    def get_playback_events():
        """Return a sorted list of timeline events for dep graph playback.

        Query params:
          project=<name>  (required)

        Response:
          {events: [{timestamp, iso, label, kind, task_id, project}], first, last, total_tasks}

        Event kinds: task_created, task_started, task_completed, task_failed
        Interesting callouts: first_task, first_completion, sprint_burst, recovery_spawn
        """
        from datetime import datetime, timezone

        project = (_req.args.get("project") or "").strip()
        if not project:
            return jsonify({"error": "project parameter required"}), 400

        # Gather all tasks: live DB (all statuses) + JSONL history (pre-migration)
        db_tasks = [dict(t) for t in db.task_get_all() if t.get("project") == project]
        jsonl_tasks = _load_history_tasks(data_dir, project=project, max_entries=2000)
        db_ids = {t["id"] for t in db_tasks}
        all_tasks = db_tasks + [t for t in jsonl_tasks if t.get("id") not in db_ids]

        # Build raw events from timestamps on each task
        raw_events = []
        for t in all_tasks:
            tid = t.get("id", "")
            ttype = t.get("type", "")
            desc = (t.get("description") or "").split("\n")[0][:60]
            short = tid[len(project) + 1:] if tid.startswith(project + "-") else tid
            if len(short) > 22:
                short = short[:20] + ".."

            created = _ts_to_iso(t.get("created"))
            started = _ts_to_iso(t.get("started"))
            completed = _ts_to_iso(t.get("completed") or t.get("completed_at"))
            status = t.get("status", "")

            if created:
                raw_events.append({
                    "iso": created,
                    "kind": "task_created",
                    "task_id": tid,
                    "label": f"Created: {short} ({ttype})",
                    "desc": desc,
                    "project": project,
                })
            if started:
                raw_events.append({
                    "iso": started,
                    "kind": "task_started",
                    "task_id": tid,
                    "label": f"Started: {short} ({ttype})",
                    "desc": desc,
                    "project": project,
                })
            if completed:
                kind = "task_failed" if status == "failed" else "task_completed"
                raw_events.append({
                    "iso": completed,
                    "kind": kind,
                    "task_id": tid,
                    "label": f"{'Failed' if kind == 'task_failed' else 'Done'}: {short} ({ttype})",
                    "desc": desc,
                    "project": project,
                })

        # Sort by iso timestamp
        raw_events.sort(key=lambda e: e["iso"] or "")

        # Add callout labels for interesting moments
        first_created = False
        first_completed = False
        completion_times = []  # for sprint_burst detection

        for ev in raw_events:
            ev["callout"] = None
            if ev["kind"] == "task_created" and not first_created:
                first_created = True
                ev["callout"] = "first_task"
            if ev["kind"] == "task_completed":
                if not first_completed:
                    first_completed = True
                    ev["callout"] = "first_completion"
                completion_times.append(_parse_iso(ev["iso"]))
            # recovery_spawn: task IDs containing 'recovery'
            if ev["kind"] == "task_created" and "recovery" in ev["task_id"]:
                ev["callout"] = "recovery_spawn"

        # Sprint burst: >3 completions within any 60-second window
        completion_times_sorted = sorted(t for t in completion_times if t)
        burst_isos = set()
        for i, t in enumerate(completion_times_sorted):
            window = [s for s in completion_times_sorted[i:] if (s - t).total_seconds() <= 60]
            if len(window) > 3:
                burst_isos.add(t.isoformat())

        for ev in raw_events:
            if ev["kind"] == "task_completed" and ev["iso"] in burst_isos and not ev["callout"]:
                ev["callout"] = "sprint_burst"

        # Add unix timestamp for easier JS use
        for ev in raw_events:
            dt = _parse_iso(ev["iso"])
            ev["timestamp"] = dt.timestamp() if dt else None

        first_iso = raw_events[0]["iso"] if raw_events else None
        last_iso = raw_events[-1]["iso"] if raw_events else None

        return jsonify({
            "events": raw_events,
            "first": first_iso,
            "last": last_iso,
            "total_tasks": len(all_tasks),
        })

    @app.route("/api/dependencies/dot-at", methods=["GET"])
    def get_dependency_dot_at():
        """Return the dep graph DOT as it looked at a given point in time.

        Query params:
          project=<name>  (required)
          at=<ISO>        (required) — reconstruct graph state at this timestamp

        Nodes are classified by their status at time `at`:
          - pending   (created <= at, not yet started)
          - in_progress (started <= at, not yet completed/failed)
          - completed / failed (completed <= at)
          - future    (created > at — not shown)

        Returns same {dot: '...'} shape as /api/dependencies/dot.
        """
        project = (_req.args.get("project") or "").strip()
        at_str = (_req.args.get("at") or "").strip()
        if not project:
            return jsonify({"error": "project parameter required"}), 400
        if not at_str:
            return jsonify({"error": "at parameter required"}), 400

        at_dt = _parse_iso(at_str)
        if not at_dt:
            return jsonify({"error": f"invalid at timestamp: {at_str!r}"}), 400

        # Gather all tasks for the project (live + JSONL)
        db_tasks = [dict(t) for t in db.task_get_all() if t.get("project") == project]
        jsonl_tasks = _load_history_tasks(data_dir, project=project, max_entries=2000)
        db_ids = {t["id"] for t in db_tasks}
        all_tasks = db_tasks + [t for t in jsonl_tasks if t.get("id") not in db_ids]

        # Classify each task's status at time `at`
        def _classify(t):
            created = _parse_iso(_ts_to_iso(t.get("created")))
            started = _parse_iso(_ts_to_iso(t.get("started")))
            completed = _parse_iso(_ts_to_iso(t.get("completed") or t.get("completed_at")))
            real_status = t.get("status", "")

            if not created or created > at_dt:
                return None  # doesn't exist yet
            if completed and completed <= at_dt:
                return "failed" if real_status == "failed" else "completed"
            if started and started <= at_dt:
                return "in_progress"
            return "pending"

        # Status colours matching the live graph palette
        _STATUS_COLORS = {
            "pending":     ("#161b22", "#e6edf3", "#58a6ff", "filled", 1.5),
            "in_progress": ("#161b22", "#e6edf3", "#d29922", "filled", 2.0),
            "completed":   ("#0d1117", "#3fb950", "#3fb950", "dashed,filled", 1.2),
            "failed":      ("#0d1117", "#da3633", "#da3633", "dashed,filled", 1.2),
        }

        visible = {}  # id -> (status_at_time, task_dict)
        for t in all_tasks:
            s = _classify(t)
            if s is not None:
                visible[t["id"]] = (s, t)

        if not visible:
            return jsonify({"dot": "digraph {}", "task_count": 0})

        lines = [
            'digraph G {',
            '  rankdir=LR;',
            '  bgcolor="transparent";',
            '  node [shape=box, fontname="sans-serif", fontsize=11];',
            '  edge [fontname="sans-serif", fontsize=9, color="#58a6ff"];',
        ]

        for tid, (status, t) in visible.items():
            fc, tc, bc, style, pw = _STATUS_COLORS[status]
            short = tid[len(project) + 1:] if tid.startswith(project + "-") else tid
            if len(short) > 24:
                short = short[:22] + ".."
            suffix = {"completed": " ok", "failed": " x", "in_progress": " ▶", "pending": ""}.get(status, "")
            label = short + suffix
            lines.append(
                f"  {_dot_quote(tid)} [label={_dot_quote(label)}, "
                f'fillcolor="{fc}", fontcolor="{tc}", color="{bc}", '
                f'style="{style}", penwidth={pw}];'
            )

        # Edges: only between visible nodes
        for tid, (_, t) in visible.items():
            for dep in (t.get("dependencies") or []):
                if dep in visible:
                    lines.append(f"  {_dot_quote(dep)} -> {_dot_quote(tid)};")

        lines.append("}")
        dot = "\n".join(lines)
        return jsonify({"dot": dot, "task_count": len(visible)})
