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


_SYNTHESIZE_SYSTEM_RESEARCH = """\
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

_SYNTHESIZE_SYSTEM_IMPLEMENTATION = """\
You are a senior software engineer synthesizing a plan and scout findings into a
precise implementation brief for another engineer who will do the actual coding.

You will receive:
1. The task goal, plan, and constraints
2. Scout findings — files inspected, discovered facts, hypotheses, recommended actions

Your job:
1. Reason over what was found
2. Produce a tight implementation brief: exactly what to change, where, and how
3. This brief goes directly to the work phase — be concrete, not abstract

Output ONLY the JSON, no other text:

{
  "summary": "1-3 sentence synthesis of what needs to happen",
  "key_conclusions": [
    "Concrete finding that affects implementation"
  ],
  "implementation_steps": [
    {
      "file": "relative/path/to/file.gd",
      "action": "create|modify|delete",
      "description": "Exactly what to do in this file and why",
      "code_hint": "Optional: a code snippet or pseudocode to guide the change"
    }
  ],
  "risks": [
    "Known risk or gotcha to watch out for"
  ],
  "confidence": 0.85
}

Be specific about file paths, function names, and the exact nature of each change.
Do not propose tasks — the work agent will implement everything in this brief directly.
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

    if plan.get("constraints"):
        lines.append("CONSTRAINTS:")
        for c in plan.get("constraints", []):
            lines.append(f"  - {c}")
        lines.append("")

    if plan.get("implementation_steps"):
        lines.append("PLAN IMPLEMENTATION STEPS:")
        for step in plan.get("implementation_steps", []):
            lines.append(f"  - {step}")
        lines.append("")

    if plan.get("likely_files_to_change"):
        lines.append("PLAN LIKELY FILES:")
        for f in plan.get("likely_files_to_change", []):
            lines.append(f"  {f}")
        lines.append("")

    if plan.get("test_plan"):
        lines.append("PLAN TESTS:")
        for t in plan.get("test_plan", []):
            lines.append(f"  - {t}")
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

    lines.append("Synthesize the above into a structured output. Output ONLY the JSON.")
    return "\n".join(lines)


def _is_implementation_mode(config: dict) -> bool:
    """Return True if synthesize is feeding a work phase (not create_tasks).

    Detected by checking the pipeline list in config — if 'work' follows
    'synthesize', we're in implementation mode.
    """
    pipeline = config.get("pipeline", [])
    if not pipeline:
        return False
    try:
        idx = pipeline.index("synthesize")
        rest = pipeline[idx + 1:]
        return "work" in rest and "create_tasks" not in rest
    except (ValueError, IndexError):
        return False


def _parse_synthesis(text: str, implementation_mode: bool = False) -> dict | None:
    """Extract JSON from synthesis output."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    required_key = "implementation_steps" if implementation_mode else "proposed_tasks"

    try:
        result = json.loads(text)
        if isinstance(result, dict) and required_key in result:
            return result
    except Exception:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            result = json.loads(match.group(0))
            if isinstance(result, dict) and required_key in result:
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
        impl_mode = _is_implementation_mode(self.config)
        system_prompt = _SYNTHESIZE_SYSTEM_IMPLEMENTATION if impl_mode else _SYNTHESIZE_SYSTEM_RESEARCH
        self.log(f"Using provider: {provider}, mode: {'implementation' if impl_mode else 'research'}")

        prompt = _build_synthesize_prompt(state)
        state.messages.append({"role": "user", "content": prompt})
        messages = state.messages

        # Synthesize in 1-2 calls — this is a smart model, it should get it right first try
        from swarm.constants import SYNTHESIZE_MAX_ATTEMPTS as _MAX_SYNTH
        synthesis = None
        for attempt in range(1, _MAX_SYNTH + 1):
            self.log(f"Synthesize attempt {attempt}/{_MAX_SYNTH}")
            text, _tokens, _thinking = call_llm(system_prompt, messages, provider=provider)
            messages.append({"role": "assistant", "content": text})

            synthesis = _parse_synthesis(text, implementation_mode=impl_mode)
            if synthesis is not None:
                if impl_mode:
                    self.log(f"Synthesis complete: {len(synthesis.get('implementation_steps', []))} steps")
                else:
                    self.log(f"Synthesis complete: {len(synthesis.get('proposed_tasks', []))} tasks proposed")
                break

            required_key = "implementation_steps" if impl_mode else "proposed_tasks"
            self.log(f"Failed to parse synthesis output — retrying with nudge")
            messages.append({
                "role": "user",
                "content": (
                    f"Your response was not valid JSON or was missing '{required_key}'. "
                    "Output ONLY the JSON object, starting with { and ending with }. "
                    "No other text, no markdown code fences."
                ),
            })

        state.record_phase_loops("synthesize", attempt)

        if synthesis is None:
            self.log("Synthesis failed — using fallback from scout findings")
            scout = state.scout_report
            if impl_mode:
                synthesis = {
                    "summary": state.description[:500],
                    "key_conclusions": scout.get("findings", [])[:5],
                    "implementation_steps": [],
                    "risks": [],
                    "confidence": 0.3,
                }
            else:
                synthesis = {
                    "summary": state.description[:500],
                    "key_conclusions": scout.get("findings", [])[:5],
                    "proposed_tasks": [],
                    "confidence": 0.3,
                }

        state.synthesis = synthesis
        return state
