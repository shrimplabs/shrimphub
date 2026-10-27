"""
Art Phase

Visual assessment phase for art_pass tasks. Analogous to how diagnose gives
a gap-analysis brief for code tasks, art gives a visual brief for the work
phase: what needs replacing, what already looks acceptable, and whether GPU
generation or the asset library should be used for each element.

Write tools and game-mutating commands are blocked — art is read/observe-only.
The output is a structured ART_BRIEF injected into TaskState, which the work
phase reads to go straight to asset generation without spending loops reading
GDScript.

Design: art runs for up to _MAX_ART_LOOPS loops. It launches the game, takes
screenshots in multiple states (menu, gameplay), runs vision_query at
model="powerful", reads GAME_DESIGN.md, and produces ART_BRIEF + JSON.
"""

from __future__ import annotations

import json
import re

from swarm.pipeline import Phase, TaskState, register_phase
from swarm.tool_dispatch import execute_tool, validate_tool_call
from swarm.llm_utils import call_llm, parse_tool_calls

_MAX_ART_LOOPS = 14

# Tools the art phase may NOT use (write + commit + task tools)
_BLOCKED_TOOLS = frozenset({
    "write_file", "patch_file", "append_file",
    "run_python",
    "git_commit", "git_push",
    "create_task", "create_tasks", "create_subtask",
    "create_tasks_file_aware", "delegate_task_batch",
    # Asset generation is reserved for work phase — assess visually, don't generate yet
    "generate_image", "generate_3d_asset",
})

_ALLOWED_ART_TOOLS = frozenset({
    "read_file", "read_file_range", "list_files", "list_dir",
    "search_code", "search_files",
    "run_command",         # for asset library browsing, git log
    "launch_game",
    "kill_game",
    "get_game_state",
    "take_screenshot",
    "vision_query",
    "screenshot_burst",
    "click_element",
    "press_button",
    "key_press",
    "key_hold",
    "play_macro",
    "wait",
})

_ART_SYSTEM = """\
You are a visual art assessor for a Godot game project. Your job is to look at
the game running, identify which visual elements are placeholder/low-quality,
and produce a structured brief that a work agent will use to generate or
integrate real assets.

You OBSERVE and PLAN — you do NOT write files, generate images, or commit.
Generate_image and generate_3d_asset are NOT available in this phase.
The work agent that follows you will use your brief to execute.

WORKFLOW:
1. Read GAME_DESIGN.md to understand the visual style and target aesthetic.
2. Browse the project's assets/ folder to see what currently exists.
3. Launch the game: launch_game(project_path="<project_path>")
   Then: wait(3), then get_game_state(command="screenshot_b64")
   Save base64 result: run_command("echo '<base64>' | base64 -d > /tmp/art_menu.png")
4. Assess the menu state: vision_query("/tmp/art_menu.png", "Describe each visual element. Is it placeholder (solid color, generic shape, missing texture)? Is it acceptable?", model="powerful")
5. Navigate into gameplay and take more screenshots:
   - click_element or key_press("ui_accept") to start the game
   - wait(2), screenshot the gameplay state
   - Use key_hold for movement (not key_press) to see animated states
6. Assess gameplay visuals with vision_query — identify placeholders vs acceptable art.
7. List assets/ in the project and note what's already there.
8. Browse the asset library at the paths documented in GAME_DESIGN.md or prompts to see what's available.
9. Produce ART_BRIEF once you've seen enough (at least 2 game states):

ART_BRIEF
{
  "visual_style": "one paragraph: target aesthetic from GAME_DESIGN.md",
  "elements": [
    {
      "name": "player sprite",
      "current_state": "placeholder — solid blue rectangle",
      "target": "pixel art goblin alchemist, 128x128, transparent bg",
      "priority": "high",
      "method": "generate_image",
      "prompt_hint": "pixel art goblin alchemist holding a potion, transparent background, warm colors",
      "width": 128,
      "height": 128
    },
    {
      "name": "ingredient: slime",
      "current_state": "missing — no sprite file found",
      "target": "green slime blob icon, 64x64",
      "priority": "high",
      "method": "generate_image",
      "prompt_hint": "green cartoon slime blob, game icon style, transparent background",
      "width": 64,
      "height": 64
    },
    {
      "name": "background",
      "current_state": "acceptable — dark stone texture already in assets/",
      "target": "keep as-is",
      "priority": "skip",
      "method": "skip",
      "prompt_hint": ""
    }
  ],
  "screenshots_taken": ["/tmp/art_menu.png", "/tmp/art_gameplay.png"],
  "asset_library_notes": "Kenney packs at /path: has UI elements but no character sprites. Athena GPU available.",
  "work_order": ["player sprite", "ingredient: slime", "ingredient: crystal"],
  "confidence": 0.85
}

FIELD GUIDE:
- method: "generate_image" | "generate_3d_asset" | "asset_library" | "skip"
  Use "generate_image" when nothing in the asset library matches and a 2D sprite/icon is needed.
  Use "asset_library" when you found a suitable match while browsing.
  Use "skip" when the element already looks acceptable.
- priority: "high" | "medium" | "low" | "skip"
- Include slug hints in prompt_hint — short, specific descriptions produce better results.
- List work_order in priority order (high first). Skip "skip" elements.

RULES:
- You MUST see at least 2 different game states (menu + gameplay) before outputting ART_BRIEF.
- Do NOT generate, write, or copy any files. Your deliverable is only the ART_BRIEF JSON.
- Kill the game before outputting ART_BRIEF: kill_game()
- If the game won't launch or crashes, skip the screenshot steps and assess from directory listing alone.
- Focus on what the PLAYER sees: sprites, HUD elements, backgrounds, UI. Ignore internal code.

To call a tool:
[TOOL_CALL]{"tool": "launch_game", "args": {"project_path": "<project_path>"}}[/TOOL_CALL]
[TOOL_CALL]{"tool": "get_game_state", "args": {"command": "screenshot_b64"}}[/TOOL_CALL]
[TOOL_CALL]{"tool": "vision_query", "args": {"image_path": "/tmp/art_menu.png", "question": "...", "model": "powerful"}}[/TOOL_CALL]

Output ONLY ONE tool call per response. After kill_game(), output ART_BRIEF immediately.
"""


def _build_art_prompt(state: TaskState) -> str:
    lines = [
        f"Project: {state.project}",
        f"Path: {state.project_path}",
        "",
        f"TASK: {state.description}",
        "",
        "Begin by reading GAME_DESIGN.md, then launch the game and assess the visual state.",
        "Output ART_BRIEF + JSON when you have seen enough (menu + gameplay, at least 2 screenshots).",
        "",
        f"You have {_MAX_ART_LOOPS} loops — aim to output ART_BRIEF by loop {_MAX_ART_LOOPS - 2}.",
    ]

    # Inject previous failure context if this is a retry
    failure_ctx = state.plan.get("failure_context") or ""
    if failure_ctx:
        lines.insert(3, "PREVIOUS ATTEMPT NOTES:")
        lines.insert(4, failure_ctx[:1000])
        lines.insert(5, "")

    return "\n".join(lines)


def _extract_art_brief(text: str) -> dict | None:
    if "ART_BRIEF" not in text:
        return None
    after = text[text.index("ART_BRIEF") + len("ART_BRIEF"):].strip()
    after = re.sub(r"^```(?:json)?\s*", "", after)
    after = re.sub(r"\s*```$", "", after)
    try:
        data = json.loads(after)
    except Exception:
        # Try to extract partial JSON
        brace_start = after.find("{")
        if brace_start >= 0:
            try:
                data = json.loads(after[brace_start:])
            except Exception:
                return None
        else:
            return None

    data.setdefault("visual_style", "")
    data.setdefault("elements", [])
    data.setdefault("screenshots_taken", [])
    data.setdefault("asset_library_notes", "")
    data.setdefault("work_order", [])
    data.setdefault("confidence", 0.5)
    return data


def _fallback_brief_from_history(messages: list[dict], state: TaskState) -> dict:
    observations: list[str] = []
    for msg in messages:
        if msg["role"] == "assistant":
            clean = re.sub(r"\[TOOL_CALL\].*?\[/TOOL_CALL\]", "", msg.get("content", ""), flags=re.DOTALL).strip()
            if clean:
                observations.append(clean[:300])
    return {
        "visual_style": "Unable to determine — art phase hit loop limit",
        "elements": [{"name": "unknown", "current_state": "not assessed", "target": "see task description",
                      "priority": "high", "method": "generate_image", "prompt_hint": state.description,
                      "width": 512, "height": 512}],
        "screenshots_taken": [],
        "asset_library_notes": " ".join(observations)[:500],
        "work_order": [],
        "confidence": 0.1,
    }


@register_phase
class ArtPhase(Phase):
    name = "art"

    def run(self, state: TaskState) -> TaskState:
        import swarm.agent_runtime as rt

        # Use scout/cheap provider — visual assessment doesn't need the big model
        provider = self.config.get("scout_provider") or rt.SCOUT_PROVIDER or rt.LLM_PROVIDER
        rt._ROUTING_PHASE = "scout"
        self.log(f"Using provider: {provider}")

        system = _ART_SYSTEM.replace("<project_path>", state.project_path)
        messages = [{"role": "user", "content": _build_art_prompt(state)}]
        brief = None
        _consecutive_stalls = 0

        for loop in range(1, _MAX_ART_LOOPS + 1):
            self.log(f"Art loop {loop}/{_MAX_ART_LOOPS}")

            if loop == _MAX_ART_LOOPS - 2:
                messages.append({
                    "role": "user",
                    "content": (
                        f"You have {_MAX_ART_LOOPS - loop + 1} loops remaining. "
                        "Kill the game now and output ART_BRIEF + JSON immediately. "
                        "Use what you have observed so far — partial assessment is fine."
                    ),
                })

            text, _tokens, _thinking = call_llm(system, messages, provider=provider)
            messages.append({"role": "assistant", "content": text})

            brief = _extract_art_brief(text)
            if brief is not None:
                self.log(f"ART_BRIEF received at loop {loop}: {len(brief.get('elements', []))} elements")
                break

            tool_calls = parse_tool_calls(text)
            if not tool_calls:
                _consecutive_stalls += 1
                self.log(f"No tool calls and no ART_BRIEF — stall {_consecutive_stalls}")
                if _consecutive_stalls >= 2:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Output ART_BRIEF followed by the JSON object now. "
                            "Use what you have seen so far. Start with kill_game() first if the game is running."
                        ),
                    })
                else:
                    messages.append({
                        "role": "user",
                        "content": "Continue assessing the game visuals or output ART_BRIEF + JSON.",
                    })
                continue

            _consecutive_stalls = 0
            tool_results = []
            tool_names = [tc.get("tool", "") for tc in tool_calls]
            self.log(f"Tools: {', '.join(tool_names)}")

            for tc in tool_calls:
                tool_name = tc.get("tool", "")
                if tool_name in _BLOCKED_TOOLS:
                    tool_results.append(
                        f"[{tool_name}] BLOCKED: {tool_name} is not available in the art assessment phase. "
                        "Save asset generation for the work phase. Observe and plan only."
                    )
                    continue
                if tool_name not in _ALLOWED_ART_TOOLS:
                    tool_results.append(
                        f"[{tool_name}] BLOCKED: art phase only allows visual assessment tools "
                        "(launch_game, get_game_state, take_screenshot, vision_query, read_file, list_files, run_command for browsing)"
                    )
                    continue
                err = validate_tool_call(tc)
                if err:
                    tool_results.append(f"[{tool_name}] Validation error: {err}")
                    continue
                result = execute_tool(tc)
                result_str = json.dumps(result) if isinstance(result, dict) else str(result)
                tool_results.append(f"[{tool_name}]\n{result_str[:6000]}")

            messages.append({"role": "user", "content": "\n\n".join(tool_results)})

        state.record_phase_loops("art", loop)

        if brief is None:
            self.log("Art phase hit loop limit — building fallback brief")
            brief = _fallback_brief_from_history(messages, state)

        n_elements = len(brief.get("elements", []))
        n_high = sum(1 for e in brief.get("elements", []) if e.get("priority") == "high")
        self.log(f"Brief: {n_elements} elements ({n_high} high priority), confidence={brief.get('confidence', '?')}")
        self.log(f"Work order: {brief.get('work_order', [])}")

        # Store brief in scout_report so work phase picks it up via its standard
        # handoff path — work already renders scout_report.recommended_actions.
        # We also inject into handoff for richer context.
        state.scout_report = {
            "files_inspected": brief.get("screenshots_taken", []),
            "findings": [
                f"{e['name']}: {e['current_state']}"
                for e in brief.get("elements", [])
                if e.get("priority") != "skip"
            ],
            "hypotheses": [brief.get("visual_style", "")],
            "recommended_actions": _build_recommended_actions(brief),
            "confidence": brief.get("confidence", 0.5),
            # Raw brief for work phase to read directly
            "_art_brief": brief,
        }

        handoff = dict(state.handoff or {})
        handoff["art_brief"] = brief
        handoff["goal"] = state.description
        handoff["facts"] = [f"Asset library notes: {brief.get('asset_library_notes', '')}"]
        handoff["next_actions"] = brief.get("work_order", [])
        state.handoff = handoff

        return state


def _build_recommended_actions(brief: dict) -> list[str]:
    """Convert ART_BRIEF elements into work-phase recommended_actions strings."""
    actions = []
    for name in brief.get("work_order", []):
        element = next((e for e in brief.get("elements", []) if e.get("name") == name), None)
        if element is None or element.get("priority") == "skip":
            continue
        method = element.get("method", "generate_image")
        hint = element.get("prompt_hint", "")
        w = element.get("width", 512)
        h = element.get("height", 512)
        target = element.get("target", "")

        if method == "generate_image":
            actions.append(
                f"generate_image: {name} — prompt=\"{hint}\", {w}x{h}px. "
                f"Target: {target}. Copy result to project assets/."
            )
        elif method == "generate_3d_asset":
            actions.append(
                f"generate_image then generate_3d_asset: {name} — prompt=\"{hint}\", {w}x{h}px. "
                f"Target: {target}. quality=draft. Copy .glb to project assets/."
            )
        elif method == "asset_library":
            actions.append(
                f"asset_library: {name} — find and copy from asset library. "
                f"Target: {target}."
            )

    return actions
