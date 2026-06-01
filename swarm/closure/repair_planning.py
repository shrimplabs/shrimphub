"""Repair task planning from closure regressions."""

from __future__ import annotations

import json
import os
from hashlib import sha1
from pathlib import Path
from typing import Any, Mapping

from swarm import db
from swarm.task_chains import append_project_head


def plan_repair_tasks_for_run(run_id: str) -> list[dict[str, Any]]:
    run = db.verification_run_get(run_id)
    if not run:
        raise ValueError(f"verification run not found: {run_id}")

    regressions = [
        regression for regression in db.regression_list_by_project(run["project"], status="open")
        if regression.get("source_run_id") == run_id
    ]
    planned: list[dict[str, Any]] = []
    for regression in regressions:
        task = ensure_repair_task_for_regression(regression["id"])
        if task is not None:
            planned.append(task)
    return planned


def ensure_repair_task_for_regression(regression_id: str) -> dict[str, Any] | None:
    regression = db.regression_get(regression_id)
    if not regression:
        raise ValueError(f"regression not found: {regression_id}")

    linked_task_id = regression.get("linked_task_id")
    if linked_task_id:
        existing = db.task_get(linked_task_id)
        if existing and existing.get("status") in {"pending", "in_progress"}:
            return existing
        # If a triage task was already spawned (even if failed/deleted), don't
        # re-enter the repair cycle — the regression is stalled and needs human
        # attention or a manual reset.
        if existing and existing.get("metadata", {}).get("is_closure_triage_task"):
            print(f"[RepairPlanning] Regression {regression_id} has a triage task ({linked_task_id}) -- skipping re-entry")
            return None
        if regression.get("status") == "stalled":
            print(f"[RepairPlanning] Regression {regression_id} is stalled -- skipping (needs manual reset)")
            return None

    run = db.verification_run_get(regression.get("source_run_id") or "")
    if not run:
        return None

    project = regression["project"]
    repair_round = _next_repair_round(project, regression["id"])
    repair_budget = _get_repair_budget(project)

    prior_history = _collect_prior_repair_history(project, regression["id"])

    if repair_round > repair_budget:
        # Budget exhausted — spawn an autonomous triage agent instead of another blind repair
        task = _build_autonomous_triage_task(regression, run, prior_history, repair_round, repair_budget)
        db.task_upsert(task)
        db.regression_update(regression_id, {"linked_task_id": task["id"], "status": "stalled"})
        return db.task_get(task["id"])

    task = _build_repair_task(regression, run, prior_history, repair_round)
    db.task_upsert(task)
    db.regression_update(regression_id, {"linked_task_id": task["id"]})
    return db.task_get(task["id"])


def _get_repair_budget(project: str) -> int:
    project_row = db.project_get(project)
    if not project_row:
        return 5
    spec = project_row.get("closure_spec") or {}
    autonomy = spec.get("autonomy") or {}
    try:
        return max(1, int(autonomy.get("repair_budget") or 5))
    except (TypeError, ValueError):
        return 5


def _collect_prior_repair_history(project: str, regression_id: str) -> list[dict[str, Any]]:
    """Gather metadata from all previous repair tasks for this regression."""
    history: list[dict[str, Any]] = []
    for task in db.task_get_by_project(project):
        metadata = task.get("metadata") or {}
        if metadata.get("source_regression_id") != regression_id:
            continue
        if not metadata.get("is_closure_repair_task"):
            continue
        round_num = int(metadata.get("closure_repair_round") or 0)
        if round_num == 0:
            continue

        # Find the agent that ran this task to get diff + output
        agent_output = ""
        agent_diff = ""
        conn = db._connect()
        rows = conn.execute(
            "SELECT output, metadata FROM agents WHERE task_id=? ORDER BY spawned_at DESC LIMIT 1",
            (task["id"],),
        ).fetchall()
        if rows:
            agent_output = (rows[0][0] or "")[-800:]
            agent_meta = {}
            try:
                agent_meta = json.loads(rows[0][1] or "{}")
            except (json.JSONDecodeError, TypeError):
                pass
            agent_diff = agent_meta.get("diff_stat", "")

        history.append({
            "round": round_num,
            "task_id": task["id"],
            "status": task.get("status"),
            "attempts": task.get("attempts", 0),
            "agent_output_tail": agent_output,
            "diff_stat": agent_diff,
        })

    history.sort(key=lambda x: x["round"])
    return history


def _build_repair_task(
    regression: Mapping[str, Any],
    run: Mapping[str, Any],
    prior_history: list[dict[str, Any]],
    repair_round: int,
) -> dict[str, Any]:
    project = regression["project"]
    fingerprint = regression["fingerprint"]
    details = regression.get("details_json") if isinstance(regression.get("details_json"), Mapping) else {}
    results = details.get("results") if isinstance(details.get("results"), Mapping) else {}
    artifacts = details.get("artifacts") if isinstance(details.get("artifacts"), Mapping) else {}
    task_id = _repair_task_id(project, fingerprint, repair_round)
    trigger_task_id = run.get("trigger_task_id")

    deps = []
    if isinstance(trigger_task_id, str) and trigger_task_id.strip():
        deps.append(trigger_task_id.strip())
    deps = append_project_head(db, project, deps, task_id=task_id, ensure_head=True)

    description = _repair_description(project, fingerprint, run, regression, results, artifacts, prior_history, repair_round)
    metadata = {
        "is_closure_repair_task": True,
        "source_verification_run_id": run["id"],
        "source_regression_id": regression["id"],
        "closure_fingerprint": fingerprint,
        "closure_occurrences": regression.get("occurrences", 1),
        "closure_severity": regression.get("severity", "medium"),
        "closure_repair_round": repair_round,
        "closure_results": results,
        "closure_artifacts": artifacts,
    }
    return {
        "id": task_id,
        "project": project,
        "type": "bug",
        "priority": _priority_for_regression(regression),
        "description": description,
        "status": "pending",
        "dependencies": deps,
        "metadata": metadata,
        "attempts": 0,
        "max_attempts": 3,
    }


def _build_autonomous_triage_task(
    regression: Mapping[str, Any],
    run: Mapping[str, Any],
    prior_history: list[dict[str, Any]],
    repair_round: int,
    repair_budget: int,
) -> dict[str, Any]:
    """
    Build a high-autonomy triage task that diagnoses WHY repair is looping and fixes
    the root cause — including the closure spec itself if that's the problem.

    This agent has full write access and explicit permission to:
    - Fix the closure spec via the API if the verification command is wrong
    - Fix code if code is genuinely broken
    - Adjust gate thresholds if they are impossible to satisfy
    - Drop closure mode to 'build' if the spec is fundamentally broken
    - Re-run verification to confirm the fix before completing
    """
    project = regression["project"]
    fingerprint = regression["fingerprint"]
    details = regression.get("details_json") if isinstance(regression.get("details_json"), Mapping) else {}
    results = details.get("results") if isinstance(details.get("results"), Mapping) else {}
    artifacts = details.get("artifacts") if isinstance(details.get("artifacts"), Mapping) else {}
    task_id = _triage_task_id(project, fingerprint, repair_round)
    trigger_task_id = run.get("trigger_task_id")

    deps = []
    if isinstance(trigger_task_id, str) and trigger_task_id.strip():
        deps.append(trigger_task_id.strip())
    deps = append_project_head(db, project, deps, task_id=task_id, ensure_head=True)

    prior_summary = _format_prior_history(prior_history)

    checks = artifacts.get("checks") if isinstance(artifacts.get("checks"), list) else []
    check_detail = ""
    for item in checks[:5]:
        if not isinstance(item, Mapping):
            continue
        outcome = item.get("outcome") if isinstance(item.get("outcome"), Mapping) else {}
        check_detail += (
            f"\n  [{item.get('category')}/{item.get('id')}] "
            f"cmd={outcome.get('command')} "
            f"status={outcome.get('status')} "
            f"error={str(outcome.get('error') or '')[:300]}"
            f"\n  stderr: {str(outcome.get('stderr') or '')[:400]}"
        )

    description = f"""AUTONOMOUS CLOSURE TRIAGE — repair budget exhausted after {repair_budget} rounds.

Project: {project}
Fingerprint: {fingerprint}
Regression: {regression['id']}
Occurrences: {regression.get('occurrences', 1)}

## Why you exist
{repair_budget} repair agents tried to fix this regression and failed. You are NOT another repair
agent. Your job is to diagnose WHY the loop is happening and fix the root cause — which may be the
verification command itself, the closure spec gates, or genuinely broken code.

## Failing verification evidence
- smoke_ok: {results.get('smoke_ok')}
- boot_ok: {results.get('boot_ok')}
- tests_ok: {results.get('tests_ok')}
- critical_flows: {results.get('critical_flows')}
- errors: {results.get('errors')}

Check detail:{check_detail}

## Prior repair attempts ({len(prior_history)} rounds)
{prior_summary}

## Your diagnostic process (follow in order)

### Step 1 — Run the failing check command directly
Extract the exact command from "Check detail" above and run it yourself with run_command().
Read the actual output carefully. Does it:
a) Fail because the COMMAND ITSELF is wrong (e.g. runs without autoloads, bad path, wrong flags)?
b) Fail because CODE is genuinely broken?
c) Time out because Godot hangs on this command style?

### Step 2 — Check the closure spec
Read the current closure spec:
  run_command("curl -s http://localhost:{_get_api_port()}/api/projects/{project}/closure")

Compare the smoke check command against what actually works. Run check_scripts.gd to verify
the codebase compiles cleanly:
  run_command("godot --headless --path . --script res://check_scripts.gd --quit")

### Step 3 — Fix the root cause

**If the verification command is wrong** (most common cause of loops):
Fix the closure spec via the API:
  run_command(\"\"\"curl -s -X POST http://localhost:{_get_api_port()}/api/projects/{project}/closure/spec \\
    -H 'Content-Type: application/json' \\
    -d '{{"closure_spec": {{"verification": {{"smoke_checks": [{{"id": "...", "type": "command", "command": "CORRECT_COMMAND"}}]}}}}}}'
  \"\"\")

For Godot projects, the correct smoke command pattern is:
  godot --headless --path . --script res://addons/gut/gut_cmdln.gd -- -gdir=res://tests -ginclude_subdirs -gexit

**If code is genuinely broken**: Fix the code. Check what prior agents tried (above) and avoid
repeating the same failed approach.

**If gates are impossible** (e.g. boot_ok required but no boot command configured): Fix the gates:
  run_command("curl -s -X POST http://localhost:{_get_api_port()}/api/projects/{project}/closure/spec -H 'Content-Type: application/json' -d '{{\"closure_spec\": {{\"gates\": {{\"boot_ok\": false, \"tests_ok\": false}}}}}}'")

**If fundamentally broken beyond repair**: Drop to build mode to unfreeze the project:
  run_command("curl -s -X POST http://localhost:{_get_api_port()}/api/projects/{project}/closure/mode -H 'Content-Type: application/json' -d '{{\"mode\": \"build\"}}'")

### Step 4 — Verify the fix
After your fix, trigger a fresh verification:
  run_command("curl -s -X POST http://localhost:{_get_api_port()}/api/projects/{project}/closure/verify -H 'Content-Type: application/json' -d '{{}}'")

Read the result. If status=passed, you are done. If still failing, return to Step 1 with
what you now know. Do NOT complete this task until verification passes or you have dropped
to build mode with a clear explanation of why.

### Step 5 — Document what you found
Before completing, append a brief note to PROJECT_CLOSURE.md explaining:
- Root cause of the loop
- What you fixed
- Any known remaining issues
"""

    metadata = {
        "is_closure_repair_task": True,
        "is_closure_triage_task": True,
        "source_verification_run_id": run["id"],
        "source_regression_id": regression["id"],
        "closure_fingerprint": fingerprint,
        "closure_occurrences": regression.get("occurrences", 1),
        "closure_severity": regression.get("severity", "medium"),
        "closure_repair_round": repair_round,
        "closure_results": results,
        "closure_artifacts": artifacts,
        "repair_budget_exhausted": True,
        "prior_repair_rounds": len(prior_history),
    }
    return {
        "id": task_id,
        "project": project,
        "type": "bug",
        "priority": 90,
        "description": description,
        "status": "pending",
        "dependencies": deps,
        "metadata": metadata,
        "attempts": 0,
        "max_attempts": 2,
    }


def _get_api_port() -> int:
    try:
        from swarm.tools.core import API_PORT
        return API_PORT
    except Exception:
        return 5001


def _repair_description(
    project: str,
    fingerprint: str,
    run: Mapping[str, Any],
    regression: Mapping[str, Any],
    results: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    prior_history: list[dict[str, Any]],
    repair_round: int,
) -> str:
    lines = [
        f"CLOSURE REPAIR TASK (round {repair_round}):",
        "",
        f"Project: {project}",
        f"Fingerprint: {fingerprint}",
        f"Verification run: {run['id']}",
        f"Regression id: {regression['id']}",
        f"Occurrences: {regression.get('occurrences', 1)}",
        "",
        "Current verification facts:",
        f"- boot_ok: {results.get('boot_ok')}",
        f"- tests_ok: {results.get('tests_ok')}",
        f"- smoke_ok: {results.get('smoke_ok')}",
    ]

    critical_flows = results.get("critical_flows")
    if isinstance(critical_flows, Mapping) and critical_flows:
        lines.append(f"- critical_flows: {dict(critical_flows)}")

    errors = results.get("errors")
    if isinstance(errors, list) and errors:
        lines.append("")
        lines.append("Primary errors:")
        for error in errors[:5]:
            if isinstance(error, str) and error.strip():
                lines.append(f"- {error[:400]}")

    checks = artifacts.get("checks")
    if isinstance(checks, list) and checks:
        lines.append("")
        lines.append("Check evidence:")
        for item in checks[:5]:
            if not isinstance(item, Mapping):
                continue
            category = item.get("category") or "check"
            check_id = item.get("id") or item.get("outcome", {}).get("check_type") or "unknown"
            outcome = item.get("outcome") if isinstance(item.get("outcome"), Mapping) else {}
            cmd = str(outcome.get("command") or "")
            lines.append(
                f"- {category}/{check_id}: status={outcome.get('status')} error={str(outcome.get('error') or '')[:200]}"
            )
            if cmd:
                lines.append(f"  command: {cmd[:300]}")
            stdout = str(outcome.get('stdout') or '').strip()
            if stdout:
                # Include tail of stdout — this is where test failures, stack traces, etc. appear
                lines.append(f"  stdout (tail):\n    " + "\n    ".join(stdout[-1500:].splitlines()))
            stderr = str(outcome.get('stderr') or '').strip()
            if stderr:
                lines.append(f"  stderr: {stderr[:400]}")

    if prior_history:
        lines.append("")
        lines.append(f"Prior repair attempts ({len(prior_history)} rounds — do NOT repeat these approaches):")
        lines.append(_format_prior_history(prior_history))

    lines.extend([
        "",
        "Your job:",
        "1. Fix the exact failure described above.",
        "2. Preserve unrelated working project behavior.",
        "3. Re-run the narrowest relevant verification before broader checks.",
        "4. Finish only when the specific failing verification evidence is resolved.",
        "",
        "IMPORTANT: Before writing any code, run the failing check command directly with",
        "run_command() and read the actual output. Prior agents may have already fixed the",
        "code — if so, the problem may be in the verification command or spec itself.",
    ])
    return "\n".join(lines)


def _format_prior_history(prior_history: list[dict[str, Any]]) -> str:
    if not prior_history:
        return "  (none)"
    lines = []
    for entry in prior_history:
        lines.append(f"  Round {entry['round']} (task {entry['task_id']}, status={entry['status']}):")
        if entry.get("diff_stat"):
            lines.append(f"    Files changed: {entry['diff_stat'][:300]}")
        if entry.get("agent_output_tail"):
            tail = entry["agent_output_tail"].strip()[-500:]
            lines.append(f"    Agent output tail: {tail}")
    return "\n".join(lines)


def _priority_for_regression(regression: Mapping[str, Any]) -> int:
    severity = regression.get("severity") or "medium"
    if severity == "high":
        return 90
    if severity == "low":
        return 60
    return 75


def _repair_task_id(project: str, fingerprint: str, repair_round: int) -> str:
    digest = sha1(f"{project}:{fingerprint}".encode("utf-8")).hexdigest()[:10]
    return f"closure-repair-{digest}-{repair_round}"


def _triage_task_id(project: str, fingerprint: str, repair_round: int) -> str:
    digest = sha1(f"{project}:{fingerprint}".encode("utf-8")).hexdigest()[:10]
    return f"closure-triage-{digest}-{repair_round}"


def _next_repair_round(project: str, regression_id: str) -> int:
    current = 0
    for task in db.task_get_by_project(project):
        metadata = task.get("metadata") or {}
        if metadata.get("source_regression_id") != regression_id:
            continue
        # Count both repair tasks AND triage tasks — triage means budget was
        # already exhausted at least once, so the round counter should reflect that.
        if not (metadata.get("is_closure_repair_task") or metadata.get("is_closure_triage_task")):
            continue
        current = max(current, int(metadata.get("closure_repair_round") or 0))
    return current + 1
