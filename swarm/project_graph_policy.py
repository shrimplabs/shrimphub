"""Project task-graph validation and repair prompt policy.

This module is intentionally pure: it does not call the LLM, mutate the DB, or
register routes. API code can use it to validate generated project plans and to
construct repair prompts when the graph is not acceptable.
"""

from __future__ import annotations

import json
from pathlib import Path


def normalize_project_type(project_type: str | None) -> str:
    value = (project_type or "godot").strip().lower()
    if value in {"game", "godot-game", "godot_game"}:
        return "godot"
    if value in {"software", "programming", "app", "python-app", "python_app"}:
        return "python"
    if value in {"godot", "python"}:
        return value
    return "python"


def task_title(task_or_desc):
    desc = task_or_desc if isinstance(task_or_desc, str) else (task_or_desc.get("description", "") or "")
    return (desc.splitlines()[0] if desc else "").strip()


def task_graph_quality_errors(tasks, allowed_external_roots=None, project_type: str = "godot"):
    errors = []
    if not tasks:
        return ["No tasks were generated."]
    project_type = normalize_project_type(project_type)
    allowed_external_roots = {x for x in (allowed_external_roots or []) if isinstance(x, str) and x}

    ids = [t.get("id", "") for t in tasks]
    id_set = {tid for tid in ids if tid}
    if len(id_set) != len(ids):
        errors.append("Task IDs must be unique.")

    roots = []
    non_root_tasks = []

    for task in tasks:
        tid = task.get("id", "")
        deps = list(task.get("dependencies") or [])
        if not deps or all(dep in allowed_external_roots for dep in deps):
            roots.append(tid)
            if not deps:
                continue
        if tid in deps:
            errors.append(f"Task {tid} depends on itself.")
        unknown = [dep for dep in deps if dep not in id_set and dep not in allowed_external_roots]
        if unknown:
            errors.append(f"Task {tid} has unknown dependencies: {', '.join(unknown)}.")
        if len(deps) != len(set(deps)):
            errors.append(f"Task {tid} has duplicate dependencies.")
        non_root_tasks.append(task)

    if not roots:
        errors.append("Graph must have at least one root task.")

    if len(tasks) >= 6:
        id_set_local = set(ids)
        internal_dep_sets = [
            frozenset(d for d in (task.get("dependencies") or []) if d in id_set_local)
            for task in tasks
        ]
        non_empty_internal = [d for d in internal_dep_sets if d]
        if non_empty_internal and all(len(d) == 1 for d in non_empty_internal) and len(set(non_empty_internal)) == 1:
            errors.append(
                "Generated dependency graph is a trivial star: every task depends only on the same single task. "
                "A project plan must encode meaningful parallel branches and convergences."
            )
        elif not non_empty_internal:
            errors.append(
                "Generated dependency graph has no inter-task dependencies: every task depends only on the "
                "external anchor with no sequencing between tasks. "
                "A project plan must encode meaningful parallel branches and convergences."
            )
        else:
            root_like_count = sum(
                1
                for task in tasks
                if not (task.get("dependencies") or [])
                or all(dep in allowed_external_roots for dep in (task.get("dependencies") or []))
            )
            if len(tasks) >= 8 and root_like_count > len(tasks) // 2:
                errors.append(
                    "Generated dependency graph is too flat: too many tasks remain attached directly to the external "
                    "anchor instead of depending on preceding task outputs. "
                    "A project plan must encode more of the intended sequencing between systems."
                )

        graph_ids = {t.get("id", "") for t in tasks if t.get("id")}
        indegree = {tid: 0 for tid in graph_ids}
        outdegree = {tid: 0 for tid in graph_ids}
        for task in tasks:
            tid = task.get("id", "")
            for dep in (task.get("dependencies") or []):
                if dep in graph_ids and tid in outdegree:
                    indegree[tid] += 1
                    outdegree[dep] += 1
        branch_nodes = [tid for tid, degree in outdegree.items() if degree > 1]
        convergence_nodes = [tid for tid, degree in indegree.items() if degree > 1]
        root_nodes = [
            t.get("id", "")
            for t in tasks
            if not (t.get("dependencies") or [])
            or all(dep in allowed_external_roots for dep in (t.get("dependencies") or []))
        ]
        if len(tasks) >= 8 and len(root_nodes) <= 1 and len(branch_nodes) <= 1 and len(convergence_nodes) <= 1:
            errors.append(
                "Generated dependency graph is too chain-like: it has too little branching and convergence for a "
                "project plan of this size. A project plan must expose multiple parallel branches."
            )

    setup_ids = set()
    if project_type == "godot":
        setup_ids = {
            t["id"]
            for t in tasks
            if t.get("id") and any(
                phrase in task_title(t).lower()
                for phrase in (
                    "set up gut",
                    "test infrastructure",
                    "project scaffolding",
                    "project setup",
                    "bootstrap project",
                )
            )
        }
    if setup_ids:
        errors.append(
            "Do not create generic Project Setup / GUT setup / harness setup tasks for new Godot projects. "
            "Canonical bootstrap is installed automatically during project creation. "
            f"Offending setup tasks: {', '.join(sorted(setup_ids))}."
        )
        root_like_ids = {
            t.get("id", "")
            for t in tasks
            if t.get("id") and (
                not (t.get("dependencies") or [])
                or set(t.get("dependencies") or []).issubset(allowed_external_roots)
            )
        }
        non_setup_roots = [tid for tid in root_like_ids if tid not in setup_ids]
        if non_setup_roots:
            errors.append(
                "Setup/infrastructure tasks should anchor the project graph before other implementation roots. "
                f"Offending root tasks: {', '.join(sorted(non_setup_roots))}."
            )

    return errors


def task_graph_semantic_warnings(tasks) -> list[str]:
    warnings = []
    title_by_id = {t["id"]: task_title(t).lower() for t in tasks if t.get("id")}

    def _find_title_contains(*needles):
        return {
            t["id"]
            for t in tasks
            if all(n in task_title(t).lower() for n in needles)
        }

    semantic_rules = [
        (("currency", "hud"), _find_title_contains("currency", "system"), "Currency HUD should depend on Currency System."),
        (("wave", "hud"), _find_title_contains("wave", "manager") | _find_title_contains("wave", "progression"), "Wave HUD should depend on wave state tasks."),
        (("game", "over", "screen"), _find_title_contains("win", "lose") | _find_title_contains("wave", "progression"), "Game Over Screen should depend on gameplay state tasks."),
        (("damage", "application"), _find_title_contains("enemy", "hp") | _find_title_contains("tower", "shooting") | _find_title_contains("projectile") | _find_title_contains("beam"), "Damage Application should depend on attack and enemy-health tasks."),
        (("tower", "fusion"), _find_title_contains("overlap", "detection") | _find_title_contains("fusion", "lookup"), "Tower Fusion should depend on overlap detection and fusion rules."),
        (("spawn", "timing"), _find_title_contains("wave", "manager"), "Spawn Timing should depend on Wave Manager."),
    ]

    def _ids_with_any(*needles):
        lowered = tuple(n.lower() for n in needles if n)
        return {
            t["id"]
            for t in tasks
            if t.get("id") and any(n in task_title(t).lower() for n in lowered)
        }

    mission_state_ids = _ids_with_any("mission state", "mission manager", "state manager")
    layout_ids = _ids_with_any("level layout", "layout", "map", "mission 1", "room")
    interaction_ids = _ids_with_any("interaction", "door", "terminal")
    alert_ids = _ids_with_any("alert", "escalation", "detection", "guard detection")
    wave_ids = _ids_with_any("wave", "enemy count", "spawn", "spawning")
    player_ids = _ids_with_any("player control", "operative switching", "player")
    ability_ids = _ids_with_any("abilities", "ability", "operative abilities")
    weapon_state_ids = _ids_with_any("weapon", "ammo", "reload", "modifier", "modifiers")
    hud_ids = _ids_with_any("hud", "feedback", "ui")
    menu_ids = _ids_with_any("menu", "restart", "pause")
    objective_ids = _ids_with_any("objective", "extraction", "mission objective", "win", "lose")

    for task in tasks:
        tid = task.get("id", "")
        title = task_title(task).lower()
        dep_ids = set(task.get("dependencies") or [])

        for needles, expected_ids, message in semantic_rules:
            if all(n in title for n in needles) and expected_ids and not dep_ids.intersection(expected_ids):
                warnings.append(f"{message} Offending task: {tid}.")

        if "mission objective" in title or ("objective flow" in title):
            if mission_state_ids and not dep_ids.intersection(mission_state_ids):
                warnings.append(f"Mission Objective Flow should depend on mission-state tasks. Offending task: {tid}.")
            if layout_ids and not dep_ids.intersection(layout_ids):
                warnings.append(f"Mission Objective Flow should depend on layout/level tasks. Offending task: {tid}.")
            if interaction_ids and not dep_ids.intersection(interaction_ids):
                warnings.append(f"Mission Objective Flow should depend on interaction tasks. Offending task: {tid}.")
            if alert_ids and not dep_ids.intersection(alert_ids):
                warnings.append(f"Mission Objective Flow should depend on alert/detection tasks. Offending task: {tid}.")

        if "hud" in title or "feedback" in title:
            categories_hit = 0
            if player_ids and dep_ids.intersection(player_ids):
                categories_hit += 1
            if mission_state_ids.union(objective_ids).union(wave_ids) and dep_ids.intersection(mission_state_ids.union(objective_ids).union(wave_ids)):
                categories_hit += 1
            if alert_ids.union(ability_ids).union(weapon_state_ids) and dep_ids.intersection(alert_ids.union(ability_ids).union(weapon_state_ids)):
                categories_hit += 1
            if categories_hit < 2:
                warnings.append(
                    f"HUD/Feedback tasks should depend on multiple gameplay state categories they display. Offending task: {tid}."
                )

        if "menu" in title or "restart" in title or "pause" in title:
            if hud_ids and not dep_ids.intersection(hud_ids):
                warnings.append(f"Menu/Restart should depend on HUD/UI tasks. Offending task: {tid}.")
            if objective_ids.union(mission_state_ids) and not dep_ids.intersection(objective_ids.union(mission_state_ids)):
                warnings.append(f"Menu/Restart should depend on mission outcome/state tasks. Offending task: {tid}.")

        if "integration" in title or "polish" in title or "vertical slice" in title:
            if len([dep for dep in dep_ids if dep in title_by_id]) < 3:
                warnings.append(f"Integration/Polish tasks should converge multiple prior systems. Offending task: {tid}.")
            categories_hit = 0
            if layout_ids and dep_ids.intersection(layout_ids):
                categories_hit += 1
            if hud_ids.union(menu_ids) and dep_ids.intersection(hud_ids.union(menu_ids)):
                categories_hit += 1
            if objective_ids.union(mission_state_ids) and dep_ids.intersection(objective_ids.union(mission_state_ids)):
                categories_hit += 1
            if player_ids.union(ability_ids).union(interaction_ids).union(alert_ids) and dep_ids.intersection(player_ids.union(ability_ids).union(interaction_ids).union(alert_ids)):
                categories_hit += 1
            if categories_hit < 3:
                warnings.append(
                    f"Integration/Polish tasks should converge multiple system categories, not act as a vague sink. Offending task: {tid}."
                )

    return warnings


def task_graph_shape_summary(tasks, allowed_external_roots=None) -> dict[str, object]:
    allowed_external_roots = {x for x in (allowed_external_roots or []) if isinstance(x, str) and x}
    ids = [t.get("id", "") for t in tasks if t.get("id")]
    id_set = set(ids)
    root_like = []
    indegree = {tid: 0 for tid in ids}
    outdegree = {tid: 0 for tid in ids}
    external_only = []

    for task in tasks:
        tid = task.get("id", "")
        deps = [d for d in (task.get("dependencies") or []) if isinstance(d, str) and d]
        internal = [d for d in deps if d in id_set]
        if not deps or all(dep in allowed_external_roots for dep in deps):
            root_like.append(tid)
        if deps and not internal and all(dep in allowed_external_roots for dep in deps):
            external_only.append(tid)
        for dep in internal:
            indegree[tid] += 1
            outdegree[dep] += 1

    branch_nodes = [tid for tid, degree in outdegree.items() if degree > 1]
    convergence_nodes = [tid for tid, degree in indegree.items() if degree > 1]
    sink_nodes = [tid for tid, degree in outdegree.items() if degree == 0]
    return {
        "task_count": len(tasks),
        "root_like": [tid for tid in root_like if tid],
        "external_only": [tid for tid in external_only if tid],
        "branch_nodes": branch_nodes,
        "convergence_nodes": convergence_nodes,
        "sink_nodes": sink_nodes,
    }


def render_graph_repair_context(tasks, validation_errors, allowed_external_roots=None) -> str:
    shape = task_graph_shape_summary(tasks, allowed_external_roots=allowed_external_roots)
    lines = [
        f"Task count: {shape['task_count']}",
        f"Root-like tasks: {', '.join(shape['root_like']) or '(none)'}",
        f"Tasks depending only on external anchors: {', '.join(shape['external_only']) or '(none)'}",
        f"Branch nodes: {', '.join(shape['branch_nodes']) or '(none)'}",
        f"Convergence nodes: {', '.join(shape['convergence_nodes']) or '(none)'}",
        f"Leaf/sink nodes: {', '.join(shape['sink_nodes']) or '(none)'}",
        "Validation errors:",
    ]
    lines.extend(f"- {err}" for err in (validation_errors or []))
    return "\n".join(lines)


def graph_diagnostics_payload(tasks, validation_errors=None, allowed_external_roots=None) -> dict[str, object]:
    shape = task_graph_shape_summary(tasks or [], allowed_external_roots=allowed_external_roots)
    return {
        "shape": shape,
        "validation_errors": list(validation_errors or []),
        "warnings": task_graph_semantic_warnings(tasks or []),
    }


def build_graph_repair_prompt(project_name, overview_text, tasks, validation_errors, allowed_external_roots=None) -> str:
    allowed_external_roots = [x for x in (allowed_external_roots or []) if isinstance(x, str) and x]
    repair_context = render_graph_repair_context(
        tasks,
        validation_errors,
        allowed_external_roots=allowed_external_roots,
    )
    task_dump = json.dumps([
        {
            "id": t.get("id"),
            "description": t.get("description", ""),
            "type": t.get("type", "feature"),
            "priority": t.get("priority", 50),
            "dependencies": list(t.get("dependencies") or []),
        }
        for t in tasks
    ], indent=2)
    root_hint = (
        f"Treat these IDs as external anchors that do not need to appear in the JSON array: {', '.join(allowed_external_roots)}.\n"
        "Tasks may depend on those anchors only if they are true project roots. Prefer empty dependency lists for logical roots; "
        "the server will re-anchor them during creation.\n"
        if allowed_external_roots else
        ""
    )
    return f"""You are repairing an invalid task dependency graph for a new project.

Project: {project_name}
Overview:
{overview_text or "(no overview provided)"}

Current graph diagnostics:
{repair_context}

Current tasks:
{task_dump}

Requirements:
- Return ONLY a JSON array of task objects.
- Preserve every task id exactly as-is.
- Preserve descriptions, types, and priorities unless changing dependencies alone cannot fix the graph.
- Prefer fixing dependencies over rewriting task content.
- Do not rename, remove, merge, or split tasks.
- Produce a real DAG with meaningful parallel branches and convergences.
- Do not return a trivial star graph where every task depends only on the same root/foundation task.
- Do not return a graph where too many tasks still depend only on the external anchor.
- Do not return a pseudo-chain for a project-sized plan unless the task list truly forces it.
- If there is a setup/scaffolding/test-infrastructure task, make it the earliest implementation prerequisite instead of leaving it as a side root.
- Remove generic Project Setup / GUT setup / harness setup tasks entirely when they only duplicate automatic bootstrap work.
- UI/HUD tasks should depend on the systems whose state they display.
- Polish/effects/screen tasks should depend on the gameplay or UI systems they enhance.
- If a task title implies "after" another system, encode that as a dependency.
{root_hint}
Return format example:
[
  {{"id":"{project_name}-t01","description":"Foundation","type":"feature","priority":50,"dependencies":[]}},
  {{"id":"{project_name}-t02","description":"System A","type":"feature","priority":50,"dependencies":["{project_name}-t01"]}}
]
"""


def normalize_repaired_tasks(current_tasks, repaired) -> list[dict]:
    repaired_by_id = {
        t.get("id"): t for t in repaired
        if isinstance(t, dict) and isinstance(t.get("id"), str) and t.get("id")
    }
    normalized = []
    for base in current_tasks:
        tid = base.get("id")
        candidate = repaired_by_id.get(tid, {})
        updated = dict(base)
        if candidate.get("description"):
            updated["description"] = candidate["description"]
        if candidate.get("type"):
            updated["type"] = candidate["type"]
        if isinstance(candidate.get("priority"), int):
            updated["priority"] = candidate["priority"]
        deps = candidate.get("dependencies", base.get("dependencies") or [])
        updated["dependencies"] = [
            d for d in (deps or [])
            if isinstance(d, str) and d and d != tid
        ]
        normalized.append(updated)
    return normalized


def project_creation_validation_errors(project_path: Path, created_tasks, allowed_external_roots=None, project_type: str = "godot"):
    errors = []
    if not project_path.exists():
        errors.append(f"Project folder was not created at {project_path}.")
        return errors
    if not (project_path / ".git").exists():
        errors.append("Git repository was not initialized.")
    if not (project_path / "README.md").exists():
        errors.append("README.md was not created.")
    if not (project_path / ".gitignore").exists():
        errors.append(".gitignore was not created.")
    if not created_tasks:
        errors.append("No tasks were created.")
    errors.extend(task_graph_quality_errors(
        created_tasks,
        allowed_external_roots=allowed_external_roots,
        project_type=project_type,
    ))
    return errors
