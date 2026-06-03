"""
Plan Phase

Uses a strong model to frame the task: goal, constraints, success criteria,
unknowns, and risk areas. Output is a structured plan dict written to TaskState.

The plan is passed to subsequent phases as their primary context — they do not
receive the raw task description or conversation history.
"""

from __future__ import annotations

import json
import re

from swarm.pipeline import Phase, TaskState, register_phase


_PLAN_SYSTEM = """\
You are a senior software engineer producing a concise task plan.
You will be given a task description and project context.
Respond ONLY with a JSON object — no markdown, no explanation outside the JSON.

The JSON must have exactly these keys:
{
  "goal": "one sentence describing what success looks like",
  "constraints": ["list of hard constraints or invariants to preserve"],
  "success_criteria": ["specific, verifiable criteria for done"],
  "unknowns": ["open questions that need investigation before implementation"],
  "risk_areas": ["files, systems, or patterns most likely to cause problems"],
  "scope": "trivial | small | medium | large",
  "fast_path": true or false
}

Set fast_path=true if the task is trivially small (single obvious fix, <10 lines changed).
A fast_path task may skip the scout phase.
"""


def _extract_json(text: str) -> dict:
    """Extract JSON from LLM response, tolerating markdown fences."""
    text = text.strip()
    # Strip ```json ... ``` fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


@register_phase
class PlanPhase(Phase):
    name = "plan"

    def run(self, state: TaskState) -> TaskState:
        from swarm.llm_utils import call_llm
        import swarm.agent_runtime as rt

        provider = self.config.get("plan_provider") or rt.LLM_PROVIDER
        rt._ROUTING_PHASE = "plan"

        project_context = f"Project: {state.project}\nPath: {state.project_path}"
        user_msg = f"{project_context}\n\nTask ({state.task_type}):\n{state.description}"

        self.log(f"Calling {provider} to frame task...")
        messages = [{"role": "user", "content": user_msg}]
        text, tokens = call_llm(_PLAN_SYSTEM, messages, provider=provider)

        try:
            plan = _extract_json(text)
        except Exception as exc:
            self.log(f"Failed to parse plan JSON: {exc}\nRaw: {text[:500]}")
            # Fallback: minimal plan so pipeline can continue
            plan = {
                "goal": state.description[:200],
                "constraints": [],
                "success_criteria": ["task completes without errors"],
                "unknowns": [],
                "risk_areas": [],
                "scope": "medium",
                "fast_path": False,
            }

        self.log(f"Goal: {plan.get('goal', '?')[:120]}")
        self.log(f"Scope: {plan.get('scope')} | fast_path: {plan.get('fast_path')}")

        state.plan = plan
        return state
