"""
Synthesize Phase

Runs on a smart model (Kimi K2.6 or M3) to reason over the plan and scout
findings and produce structured conclusions: what was found, what it means,
and what tasks should be created.

This is the "brain" of the research pipeline — it takes raw observations and
turns them into actionable insight before the create_tasks phase builds the DAG.

Stores result in state.synthesis:
  {
    "summary": "...",             # 2–4 sentence synthesis of what was found
    "key_conclusions": [...],     # list of concrete conclusions
    "proposed_tasks": [           # structured task proposals
      {
        "type": "bug|feature|refactor|research",
        "description": "...",
        "priority": 80,
        "rationale": "...",
        "depends_on": [0, 1],     # integer indices into proposed_tasks
        "files_likely_touched": [...]
      }
    ],
    "confidence": 0.85
  }
"""

from __future__ import annotations

import json
import re

from swarm.pipeline import Phase, TaskState, register_phase
from swarm.llm_utils import call_llm


_SYNTHESIZE_SYSTEM = """\
You are a senior engineering lead synthesizing research findings into an actionable task plan.

You will receive:
1. A goal description and plan from the plan phase
2. Scout findings (files inspected, facts discovered, hypotheses)

Your job:
1. Reason carefully over the findings
2. Identify what needs to be done and in what order
3. Produce a structured JSON output with proposed tasks

Output format — output ONLY the JSON, no other text:

{
  "summary": "2-4 sentence overview of what was found and what needs to happen",
  "key_conclusions": [
    "Concrete conclusion from the research"
  ],
  "proposed_tasks": [
    {
      "type": "bug",
      "description": "Clear, actionable task description an agent can execute",
      "priority": 80,
      "rationale": "Why this task is needed based on the findings",
      "depends_on": [],
      "files_likely_touched": ["path/to/file.py"]
    }
  ],
  "confidence": 0.85
}

Task types: bug (80), feature (50), refactor (100), polish (40)
DO NOT propose research tasks — the output must be actionable implementation tasks only.
depends_on: list of integer indices into proposed_tasks (0-based)
Keep descriptions specific and actionable — an agent will execute them without further context.
Do not propose more than 8 tasks. Consolidate micro-steps. Focus on the highest-value work only.
"""


def _build_synthesize_prompt(state: TaskState) -> str:
    plan = state.plan
    scout = state.scout_report
    lines = [
        f"Project: {state.project}",
        f"Path: {state.project_path}",
        f"",
        f"GOAL: {plan.get('goal', state.description)}",
        f"",
    ]

    if plan.get("success_criteria"):
        lines.append("SUCCESS CRITERIA:")
        for sc in plan.get("success_criteria", []):
            lines.append(f"  - {sc}")
        lines.append("")

    if scout.get("findings"):
        lines.append("SCOUT FINDINGS:")
        for f in scout.get("findings", []):
            lines.append(f"  - {f}")
        lines.append("")

    if scout.get("hypotheses"):
        lines.append("HYPOTHESES:")
        for h in scout.get("hypotheses", []):
            lines.append(f"  - {h}")
        lines.append("")

    if scout.get("recommended_actions"):
        lines.append("SCOUT RECOMMENDED ACTIONS:")
        for a in scout.get("recommended_actions", []):
            lines.append(f"  - {a}")
        lines.append("")

    if scout.get("files_inspected"):
        lines.append("FILES INSPECTED BY SCOUT:")
        for f in scout.get("files_inspected", [])[:20]:
            lines.append(f"  {f}")
        lines.append("")

    lines.append("Synthesize the above into a structured task plan. Output ONLY the JSON.")
    return "\n".join(lines)


def _parse_synthesis(text: str) -> dict | None:
    """Extract JSON from synthesis output."""
    # Try raw parse first
    text = text.strip()
    # Strip markdown code fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        result = json.loads(text)
        if isinstance(result, dict) and "proposed_tasks" in result:
            return result
    except Exception:
        pass

    # Try to extract JSON object from within prose
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            result = json.loads(match.group(0))
            if isinstance(result, dict) and "proposed_tasks" in result:
                return result
        except Exception:
            pass

    return None


@register_phase
class SynthesizePhase(Phase):
    name = "synthesize"

    def run(self, state: TaskState) -> TaskState:
        import swarm.agent_runtime as rt

        provider = (
            self.config.get("synthesize_provider")
            or getattr(rt, "SYNTHESIZE_PROVIDER", "")
            or rt.LLM_PROVIDER
        )
        rt._ROUTING_PHASE = "synthesize"
        self.log(f"Using provider: {provider}")

        prompt = _build_synthesize_prompt(state)
        messages = [{"role": "user", "content": prompt}]

        # Synthesize in 1-2 calls — this is a smart model, it should get it right first try
        synthesis = None
        for attempt in range(1, 3):
            self.log(f"Synthesize attempt {attempt}/2")
            text, _tokens, _thinking = call_llm(_SYNTHESIZE_SYSTEM, messages, provider=provider)
            messages.append({"role": "assistant", "content": text})

            synthesis = _parse_synthesis(text)
            if synthesis is not None:
                self.log(f"Synthesis complete: {len(synthesis.get('proposed_tasks', []))} tasks proposed")
                break

            self.log(f"Failed to parse synthesis output — retrying with nudge")
            messages.append({
                "role": "user",
                "content": (
                    "Your response was not valid JSON or was missing 'proposed_tasks'. "
                    "Output ONLY the JSON object, starting with { and ending with }. "
                    "No other text, no markdown code fences."
                ),
            })

        if synthesis is None:
            self.log("Synthesis failed — using fallback from scout findings")
            # Build a minimal synthesis from scout findings so create_tasks can still run
            scout = state.scout_report
            synthesis = {
                "summary": state.description[:500],
                "key_conclusions": scout.get("findings", [])[:5],
                "proposed_tasks": [],
                "confidence": 0.3,
            }

        state.synthesis = synthesis
        return state
