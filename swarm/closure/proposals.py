"""Heuristic closure-spec proposals for new and existing projects."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from swarm.closure.project_seeds import get_representative_project_seed
from swarm.closure.specs import GODOT_BOOT_COMMAND, GODOT_GUT_COMMAND, normalize_closure_profile, normalize_project_spec


def _safe_relpath(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _safe_read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _infer_profile(project_root: Path, requested_profile: str | None) -> str:
    if requested_profile:
        return normalize_closure_profile(requested_profile)
    if (project_root / "project.godot").exists():
        return "godot"
    if (project_root / "package.json").exists():
        return "typescript"
    return "python"


def _find_python_package(project_root: Path) -> str | None:
    for child in sorted(project_root.iterdir() if project_root.exists() else []):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name == "tests":
            continue
        if (child / "__init__.py").exists():
            return child.name
    return None


def _find_first(root: Path, patterns: list[str]) -> Path | None:
    for pattern in patterns:
        matches = sorted(root.glob(pattern))
        if matches:
            return matches[0]
    return None


def _slug(text: str) -> str:
    value = "".join(ch.lower() if ch.isalnum() else "-" for ch in text)
    while "--" in value:
        value = value.replace("--", "-")
    return value.strip("-") or "main-flow"


def _context_lines(context: Mapping[str, Any] | None, key: str) -> list[str]:
    if not context:
        return []
    value = context.get(key)
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, Sequence):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out
    return []


def _context_tasks(context: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not context:
        return []
    tasks = context.get("tasks")
    if not isinstance(tasks, Sequence):
        return []
    return [dict(task) for task in tasks if isinstance(task, Mapping)]


def _task_text(task: Mapping[str, Any]) -> str:
    parts = [str(task.get("id") or "").strip(), str(task.get("description") or "").strip()]
    return "\n".join(part for part in parts if part)


def _infer_test_command_from_quality_gates(quality_gates: list[str], profile: str) -> str | None:
    combined = "\n".join(quality_gates).lower()
    if not combined:
        return None
    if "gut" in combined:
        return GODOT_GUT_COMMAND if profile == "godot" else "gut --run --exit"
    if "pytest" in combined:
        return "python3 -m pytest -q" if profile == "python" else "pytest -q"
    if "playwright" in combined:
        return "npm run test:e2e"
    if re.search(r"\bnpm test\b", combined):
        return "npm test"
    return None


def _classify_context_flow(task: Mapping[str, Any]) -> str:
    text = _task_text(task).lower()

    presentation_needles = ("hud", "ui", "overlay", "menu", "camera", "screen", "letterboxing")
    terminal_needles = ("victory", "win", "lose", "game over", "endless", "restart", "play again")
    strong_terminal_needles = ("victory", "win", "lose", "game over", "endless")
    progression_needles = ("wave", "upgrade", "boss", "mode", "difficulty", "stream")
    gameplay_action_needles = (
        "spawn",
        "merge",
        "absorb",
        "fight",
        "combat",
        "ai",
        "hunger",
        "movement",
        "wander",
        "chase",
        "flee",
        "seek",
        "collision",
        "physics",
        "control",
    )
    gameplay_needles = (
        "spawn",
        "blob",
        "enemy",
        "food",
        "mass",
        "merge",
        "absorb",
        "fight",
        "combat",
        "ai",
        "hunger",
        "movement",
        "wander",
        "chase",
        "flee",
        "seek",
        "pellet",
        "arena",
        "collision",
        "physics",
        "player",
    )

    has_presentation = any(needle in text for needle in presentation_needles)
    has_terminal = any(needle in text for needle in terminal_needles)
    has_strong_terminal = any(needle in text for needle in strong_terminal_needles)
    has_progression = any(needle in text for needle in progression_needles)
    has_gameplay_actions = any(needle in text for needle in gameplay_action_needles)
    has_gameplay = any(needle in text for needle in gameplay_needles)
    presentation_first = (
        has_presentation
        and not has_strong_terminal
        and not has_gameplay_actions
        and "upgrade" not in text
        and (
            text.startswith(("menu", "hud", "ui", "overlay", "camera", "screen"))
            or "menu +" in text
            or "hud +" in text
        )
    )

    if presentation_first:
        return "presentation"
    if has_terminal:
        return "terminal"
    if has_progression:
        return "progression"
    if has_gameplay:
        return "gameplay"
    if has_presentation:
        return "presentation"
    return "misc"


def _pick_context_flows(tasks: list[dict[str, Any]], *, limit: int = 3) -> list[dict[str, str]]:
    if not tasks:
        return []

    keywords = (
        ("victory", 12),
        ("win", 12),
        ("lose", 11),
        ("game over", 11),
        ("wave", 12),
        ("upgrade", 8),
        ("boss", 10),
        ("difficulty", 6),
        ("stream", 6),
        ("vertical slice", 10),
        ("integration", 9),
        ("spawn", 8),
        ("food", 8),
        ("merge", 8),
        ("enemy", 7),
        ("menu", 4),
        ("hud", 2),
    )

    category_bonus = {
        "terminal": 28,
        "progression": 20,
        "gameplay": 16,
        "presentation": -8,
        "misc": 0,
    }

    def _score(task: dict[str, Any]) -> tuple[int, int, int]:
        text = _task_text(task).lower()
        category = _classify_context_flow(task)
        score = 0
        for needle, weight in keywords:
            if needle in text:
                score += weight
        score += category_bonus.get(category, 0)
        if "ui overlay" in text or "overlay" in text:
            score -= 10
        if "hud" in text:
            score -= 12
        if "menu" in text and category == "presentation":
            score -= 8
        if "win/lose" in text or "win and lose" in text or "game modes" in text:
            score += 8
        if "upgrade" in text:
            score += 2
        if "between-wave" in text:
            score -= 4
        if "spawn" in text and "player" in text:
            score += 6
        if "enemy" in text and "wave" in text:
            score += 6
        deps = task.get("dependencies") or []
        dep_count = len(deps) if isinstance(deps, Sequence) else 0
        score += min(dep_count, 4)
        return score, dep_count, len(text)

    ranked = sorted(tasks, key=lambda task: _score(task), reverse=True)

    def _append_flow(task: dict[str, Any], out: list[dict[str, str]], seen: set[str]) -> bool:
        score, _, _ = _score(task)
        if score <= 0:
            return False
        title = str(task.get("description") or task.get("id") or "main flow").splitlines()[0].strip()
        flow_id = _slug(str(task.get("id") or title))
        if flow_id in seen:
            return False
        seen.add(flow_id)
        out.append({
            "id": flow_id,
            "description": title or "Representative project flow.",
        })
        return True

    flows: list[dict[str, str]] = []
    seen: set[str] = set()
    preferred_categories = ("terminal", "progression", "gameplay")

    for category in preferred_categories:
        for task in ranked:
            if _classify_context_flow(task) != category:
                continue
            if _append_flow(task, flows, seen):
                break
        if len(flows) >= limit:
            return flows

    for task in ranked:
        if _classify_context_flow(task) == "presentation":
            continue
        _append_flow(task, flows, seen)
        if len(flows) >= limit:
            return flows

    for task in ranked:
        if _classify_context_flow(task) != "presentation":
            continue
        _append_flow(task, flows, seen)
        if len(flows) >= limit:
            break
    return flows


def _python_proposal(project_name: str, project_root: Path, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    package_name = _find_python_package(project_root)
    streamlit_entry = None
    quality_gates = _context_lines(context, "quality_gates")
    context_tasks = _context_tasks(context)
    for candidate in [
        project_root / "app.py",
        project_root / "main.py",
        project_root / "src" / "app.py",
    ]:
        if candidate.exists():
            try:
                if "streamlit" in candidate.read_text().lower():
                    streamlit_entry = candidate
                    break
            except Exception:
                continue

    spec: dict[str, Any] = {"mode": "build"}
    assumptions: list[str] = []

    if streamlit_entry is not None:
        rel = _safe_relpath(streamlit_entry, project_root)
        spec["boot"] = {
            "command": f"streamlit run {rel} --server.headless true --server.port 8510",
            "ready_check": {"type": "http", "url": "http://127.0.0.1:8510"},
        }
        assumptions.append(f"Detected Streamlit entrypoint at {rel}.")
    elif package_name and (project_root / package_name / "cli.py").exists():
        spec["boot"] = {
            "command": f"python3 -m {package_name}.cli",
            "ready_check": {"type": "command", "command": f"python3 -m {package_name}.cli"},
        }
        assumptions.append(f"Detected runnable CLI module {package_name}.cli.")
    elif package_name:
        spec["boot"] = {
            "command": None,
            "ready_check": {"type": "command", "command": f"python3 -c \"import {package_name}\""},
        }
        assumptions.append(f"Detected importable package {package_name}.")
    else:
        spec["boot"] = {
            "command": None,
            "ready_check": {"type": "command", "command": "python3 -c \"print('boot check placeholder')\""},
        }
        assumptions.append("No obvious runnable Python entrypoint was detected; boot check is a placeholder.")

    verification: dict[str, Any] = {}
    quality_gate_test_command = _infer_test_command_from_quality_gates(quality_gates, "python")
    if quality_gate_test_command:
        verification["unit_test_command"] = quality_gate_test_command
        assumptions.append(f"Derived unit test command from quality gates: {quality_gate_test_command}.")
    elif (project_root / "tests").exists():
        verification["unit_test_command"] = "python3 -m pytest -q"
        assumptions.append("Detected tests/ directory, so pytest is proposed as the base test command.")

    smoke_target = _find_first(
        project_root,
        [
            "tests/test_*vertical*.py",
            "tests/test_*smoke*.py",
            "tests/test_*integration*.py",
            "tests/test_*flow*.py",
            "tests/test_*scenario*.py",
            "tests/test_*.py",
        ],
    )
    if smoke_target is not None:
        rel = _safe_relpath(smoke_target, project_root)
        flow_id = _slug(smoke_target.stem.replace("test_", ""))
        verification["smoke_checks"] = [
            {
                "id": flow_id,
                "type": "command",
                "command": f"python3 -m pytest -q {rel}",
            }
        ]
        spec["critical_flows"] = [
            {
                "id": flow_id,
                "description": f"Run representative test flow from {rel}.",
            }
        ]
        assumptions.append(f"Selected {rel} as the first representative Python smoke flow.")
    else:
        flows = _pick_context_flows(context_tasks, limit=2)
        if flows:
            spec["critical_flows"] = flows
            assumptions.append("Derived representative Python flows from the incoming task graph.")

    if verification:
        spec["verification"] = verification
        spec["mode"] = "stabilize"

    return {
        "project": project_name,
        "profile": "python",
        "source": "heuristic",
        "closure_spec": normalize_project_spec(spec, "python"),
        "assumptions": assumptions,
    }


def _typescript_proposal(project_name: str, project_root: Path, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    package_json = _safe_read_json(project_root / "package.json")
    scripts = package_json.get("scripts") or {}
    deps = {
        **(package_json.get("dependencies") or {}),
        **(package_json.get("devDependencies") or {}),
    }
    quality_gates = _context_lines(context, "quality_gates")
    context_tasks = _context_tasks(context)
    assumptions: list[str] = []
    spec: dict[str, Any] = {"mode": "build"}

    dev_command = None
    for key in ("dev:client", "dev", "start"):
        if isinstance(scripts.get(key), str) and scripts[key].strip():
            dev_command = f"npm run {key}"
            break
    if dev_command:
        port = "5173" if "vite" in deps or (project_root / "vite.config.ts").exists() or (project_root / "vite.config.js").exists() else "3000"
        spec["boot"] = {
            "command": dev_command,
            "ready_check": {"type": "http", "url": f"http://127.0.0.1:{port}"},
        }
        assumptions.append(f"Detected frontend dev command `{dev_command}`.")

    verification: dict[str, Any] = {}
    quality_gate_test_command = _infer_test_command_from_quality_gates(quality_gates, "typescript")
    if quality_gate_test_command:
        verification["unit_test_command"] = quality_gate_test_command
        assumptions.append(f"Derived test command from quality gates: {quality_gate_test_command}.")
    elif "test" in scripts:
        verification["unit_test_command"] = "npm test"
        assumptions.append("Detected npm test script.")
    if "test:e2e" in scripts:
        verification["integration_test_command"] = "npm run test:e2e"
        assumptions.append("Detected npm run test:e2e script.")

    smoke_target = _find_first(
        project_root,
        [
            "e2e/*spec.ts",
            "e2e/*spec.tsx",
            "tests/e2e/*spec.ts",
            "tests/e2e/*spec.tsx",
        ],
    )
    if smoke_target is not None:
        rel = _safe_relpath(smoke_target, project_root)
        flow_id = _slug(smoke_target.stem.replace(".spec", "").replace("spec", ""))
        smoke_command = f"npm run test:e2e -- {rel}" if "test:e2e" in scripts else f"npx playwright test {rel}"
        verification["smoke_checks"] = [
            {
                "id": flow_id,
                "type": "command",
                "command": smoke_command,
            }
        ]
        spec["critical_flows"] = [
            {
                "id": flow_id,
                "description": f"Run representative browser flow from {rel}.",
            }
        ]
        assumptions.append(f"Selected {rel} as the representative browser smoke flow.")
    else:
        flows = _pick_context_flows(context_tasks, limit=2)
        if flows:
            spec["critical_flows"] = flows
            assumptions.append("Derived representative web flows from the incoming task graph.")

    if verification:
        spec["verification"] = verification
        spec["mode"] = "stabilize"

    return {
        "project": project_name,
        "profile": "typescript",
        "source": "heuristic",
        "closure_spec": normalize_project_spec(spec, "typescript"),
        "assumptions": assumptions,
    }


def _godot_proposal(project_name: str, project_root: Path, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    quality_gates = _context_lines(context, "quality_gates")
    context_tasks = _context_tasks(context)
    assumptions: list[str] = []
    spec: dict[str, Any] = {
        "mode": "build",
        "boot": {
            "command": GODOT_BOOT_COMMAND,
            "ready_check": {"type": "command", "command": GODOT_BOOT_COMMAND},
        },
    }
    assumptions.append("Using headless Godot boot validation to prove the project loads without immediate script errors.")

    verification: dict[str, Any] = {}
    quality_gate_test_command = _infer_test_command_from_quality_gates(quality_gates, "godot")
    if quality_gate_test_command:
        verification["unit_test_command"] = quality_gate_test_command
        assumptions.append(f"Derived Godot test command from quality gates: {quality_gate_test_command}.")

    smoke_target = _find_first(
        project_root,
        [
            "tests/test_vertical_slice.gd",
            "tests/test_smoke*.gd",
            "tests/test_*.gd",
        ],
    )
    if smoke_target is not None:
        rel = _safe_relpath(smoke_target, project_root)
        flow_id = _slug(smoke_target.stem.replace("test_", ""))
        verification["smoke_checks"] = [
            {
                "id": flow_id,
                "type": "command",
                "command": f"godot --headless --path . -s {rel}",
            }
        ]
        spec["critical_flows"] = [
            {
                "id": flow_id,
                "description": f"Run representative Godot slice from {rel}.",
            }
        ]
        assumptions.append(f"Selected {rel} as the representative Godot smoke flow.")
    else:
        flows = _pick_context_flows(context_tasks, limit=3)
        if flows:
            spec["critical_flows"] = flows
            assumptions.append("Derived representative Godot flows from the incoming task graph.")
            first_flow = flows[0]
            verification["smoke_checks"] = [
                {
                    "id": first_flow["id"],
                    "type": "command",
                    "command": quality_gate_test_command or GODOT_BOOT_COMMAND,
                }
            ]
        else:
            assumptions.append("No Godot test script was detected; proposal falls back to headless boot validation only.")

    if verification:
        spec["verification"] = verification
        spec["mode"] = "stabilize"

    return {
        "project": project_name,
        "profile": "godot",
        "source": "heuristic",
        "closure_spec": normalize_project_spec(spec, "godot"),
        "assumptions": assumptions,
    }


def propose_project_closure(
    project_name: str,
    project_root: Path,
    requested_profile: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        seed = get_representative_project_seed(project_name)
        seed["source"] = "representative_seed"
        return seed
    except Exception:
        pass

    profile = _infer_profile(project_root, requested_profile)
    if profile == "typescript":
        return _typescript_proposal(project_name, project_root, context)
    if profile == "godot":
        return _godot_proposal(project_name, project_root, context)
    return _python_proposal(project_name, project_root, context)
