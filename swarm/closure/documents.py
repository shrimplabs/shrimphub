"""Helpers for rendering and syncing repo-local closure contract documents."""

from __future__ import annotations

from pathlib import Path


def render_project_closure_doc(project_name: str, proposal: dict | None) -> str:
    proposal = proposal or {}
    closure_spec = dict(proposal.get("closure_spec") or {})
    verification = dict(closure_spec.get("verification") or {})
    gates = dict(closure_spec.get("gates") or {})
    autonomy = dict(closure_spec.get("autonomy") or {})
    boot = dict(closure_spec.get("boot") or {})
    ready_check = dict(boot.get("ready_check") or {})
    assumptions = list(proposal.get("assumptions") or [])
    critical_flows = list(closure_spec.get("critical_flows") or [])
    smoke_checks = list(verification.get("smoke_checks") or [])

    lines: list[str] = [
        f"# {project_name} Closure Contract",
        "",
        "## Summary",
        "",
        f"- source: {proposal.get('source', 'unknown')}",
        f"- profile: {proposal.get('profile', 'unknown')}",
        f"- mode: {closure_spec.get('mode', 'build')}",
        "",
        "## Boot",
        "",
    ]

    command = str(boot.get("command") or "").strip()
    if command:
        lines.append(f"- command: `{command}`")
    if ready_check:
        lines.append(f"- ready_check.type: `{ready_check.get('type', 'unknown')}`")
        for key in ("command", "url", "target", "scene"):
            value = ready_check.get(key)
            if value:
                lines.append(f"- ready_check.{key}: `{value}`")
    lines.append("")

    lines.extend(["## Verification", ""])
    unit_cmd = str(verification.get("unit_test_command") or "").strip()
    integration_cmd = str(verification.get("integration_test_command") or "").strip()
    if unit_cmd:
        lines.append(f"- unit_test_command: `{unit_cmd}`")
    if integration_cmd:
        lines.append(f"- integration_test_command: `{integration_cmd}`")
    if smoke_checks:
        lines.append("- smoke_checks:")
        for check in smoke_checks:
            check_id = check.get("id", "smoke-check")
            check_type = check.get("type", "unknown")
            target = check.get("target") or check.get("command") or check.get("scene") or ""
            suffix = f" -> `{target}`" if target else ""
            lines.append(f"  - `{check_id}` ({check_type}){suffix}")
    else:
        lines.append("- smoke_checks: none")
    lines.append("")

    lines.extend(["## Critical Flows", ""])
    if critical_flows:
        for flow in critical_flows:
            lines.append(f"- `{flow.get('id', 'flow')}`: {flow.get('description', '')}".rstrip())
    else:
        lines.append("- none")
    lines.append("")

    lines.extend(["## Gates", ""])
    if gates:
        for key, value in gates.items():
            lines.append(f"- {key}: `{value}`")
    else:
        lines.append("- none")
    lines.append("")

    lines.extend(["## Autonomy", ""])
    if autonomy:
        for key, value in autonomy.items():
            lines.append(f"- {key}: `{value}`")
    else:
        lines.append("- none")
    lines.append("")

    lines.extend(["## Assumptions", ""])
    if assumptions:
        for assumption in assumptions:
            lines.append(f"- {assumption}")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def write_project_closure_doc(project_path: Path, project_name: str, proposal: dict | None) -> Path:
    project_path.mkdir(parents=True, exist_ok=True)
    output_path = project_path / "PROJECT_CLOSURE.md"
    output_path.write_text(render_project_closure_doc(project_name, proposal))
    return output_path
