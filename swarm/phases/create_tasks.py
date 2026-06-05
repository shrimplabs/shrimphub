"""
Create Tasks Phase

Takes the synthesis output and creates tasks in the swarm via the API.
Uses the batch endpoint with integer depends_on indices for reliable DAG wiring.

This phase is intentionally simple — it reads state.synthesis and calls the
swarm API. No LLM needed; the synthesis phase already did the reasoning.

If the synthesis has no proposed tasks, the phase logs and exits cleanly
(the research agent may have concluded no work is needed).
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error

from swarm.pipeline import Phase, TaskState, register_phase


@register_phase
class CreateTasksPhase(Phase):
    name = "create_tasks"

    def run(self, state: TaskState) -> TaskState:
        synthesis = getattr(state, "synthesis", None) or {}
        proposed = synthesis.get("proposed_tasks", [])

        if not proposed:
            self.log("No tasks proposed by synthesis — nothing to create")
            state.tasks_created = []
            return state

        self.log(f"Creating {len(proposed)} tasks for project '{state.project}'")

        # Build batch payload — proposed_tasks already use integer depends_on indices
        batch_tasks = []
        for t in proposed:
            batch_tasks.append({
                "type": t.get("type", "feature"),
                "description": t.get("description", ""),
                "priority": t.get("priority", 50),
                "depends_on": t.get("depends_on", []),
                "metadata": {
                    "rationale": t.get("rationale", ""),
                    "files_likely_touched": t.get("files_likely_touched", []),
                    "source": "research_pipeline",
                },
            })

        payload = json.dumps({
            "project": state.project,
            "tasks": batch_tasks,
        }).encode()

        swarm_url = self.config.get("swarm_url", "http://localhost:5001")
        url = f"{swarm_url}/api/tasks/batch"

        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            self.log(f"HTTP error creating tasks: {e.code} — {body[:300]}")
            state.errors.append(f"create_tasks: HTTP {e.code}: {body[:200]}")
            state.tasks_created = []
            return state
        except Exception as exc:
            self.log(f"Error creating tasks: {exc}")
            state.errors.append(f"create_tasks: {exc}")
            state.tasks_created = []
            return state

        ids = result.get("ids", [])
        id_map = result.get("id_map", {})
        self.log(f"Created {len(ids)} tasks: {ids}")

        # Log summary
        if synthesis.get("summary"):
            self.log(f"Synthesis summary: {synthesis['summary'][:200]}")

        state.tasks_created = ids
        state.work_report = {
            "completed": True,
            "loops_used": 1,
            "commit_sha": None,
            "patches_applied": False,
            "tasks_created": ids,
            "id_map": id_map,
        }
        return state
