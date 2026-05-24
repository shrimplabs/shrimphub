#!/usr/bin/env python3
"""game_harness.py — LLM-driven game player using the QA pipeline.

Launches a Godot game, reads its state, and lets an LLM decide what input
to send every step — no swarm, no Flask, no SQLite.

Usage:
    .venv/bin/python tools/game_harness.py <project_name>
    .venv/bin/python tools/game_harness.py anti-grav-rush --goal "reach 1000 score"
    .venv/bin/python tools/game_harness.py anti-grav-rush --steps 30 --provider claude
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

SWARM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SWARM_ROOT))

# Load .env so API keys are available without manual export
_env_path = SWARM_ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())


def load_config() -> dict:
    cfg_path = SWARM_ROOT / "config.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text())
        except Exception as e:
            print(f"[warn] Could not read config.json: {e}")
    return {}


def resolve_project_path(project_name: str, config: dict) -> Path:
    workspace = config.get("workspace", "~/workspace")
    return Path(os.path.expanduser(workspace)) / project_name


# ---------------------------------------------------------------------------
# LLM interface — replace this one function to swap providers
# ---------------------------------------------------------------------------

# Built-in provider table (mirrors swarm/provider_utils.py)
_BUILTIN_PROVIDERS = {
    "minimax": {
        "base_url": "https://api.minimax.io/anthropic/v1",
        "model": "MiniMax-M2.7",
        "api_key_env": "MINIMAX_API_KEY",
        "format": "anthropic",
        "max_tokens": 4096,
    },
    "claude": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-6",
        "api_key_env": "ANTHROPIC_API_KEY",
        "format": "anthropic_native",
        "max_tokens": 4096,
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "anthropic/claude-3.5-sonnet",
        "api_key_env": "OPENROUTER_API_KEY",
        "format": "openai",
        "max_tokens": 4096,
    },
    "kimi": {
        "base_url": "https://api.kimi.com/coding/v1",
        "model": "k2p5",
        "api_key_env": "KIMI_API_KEY",
        "format": "anthropic",
        "max_tokens": 4096,
    },
}


def _resolve_provider_cfg(provider_name: str, config: dict) -> dict:
    """Merge built-in provider defaults with any config.json overrides."""
    # Start from built-ins, then overlay per-provider config overrides
    cfg = dict(_BUILTIN_PROVIDERS.get(provider_name, _BUILTIN_PROVIDERS["claude"]))
    overrides = config.get("llm_providers", {}).get(provider_name, {})
    cfg.update(overrides)
    return cfg


def _llm_text(prompt: str, system: str, provider_name: str, config: dict) -> str:
    """Send a text prompt + system message to the planning LLM. Returns text reply."""
    cfg = _resolve_provider_cfg(provider_name, config)
    api_key_env = cfg.get("api_key_env", "ANTHROPIC_API_KEY")
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise RuntimeError(
            f"API key not set: {api_key_env} (provider: {provider_name}). "
            f"Set the env var or use a different --provider."
        )

    base_url = cfg["base_url"].rstrip("/")
    model = cfg["model"]
    fmt = cfg.get("format", "anthropic_native")
    max_tokens = cfg.get("max_tokens", 512)  # actions are short

    messages = [{"role": "user", "content": prompt}]

    if fmt == "anthropic_native":
        url = f"{base_url}/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        # Skip thinking blocks — find first text block
        for block in result.get("content", []):
            if block.get("type") == "text":
                return block["text"]
        return result["content"][0].get("text", "")

    elif fmt == "openai":
        url = f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        oai_messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        body = {"model": model, "max_tokens": max_tokens, "messages": oai_messages}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        return result["choices"][0]["message"]["content"]

    else:
        # Anthropic-compatible Bearer (minimax, kimi, etc.)
        url = f"{base_url}/messages"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        # Include system as first user message for providers that don't support system role
        full_prompt = f"[INSTRUCTIONS]\n{system}\n\n[TASK]\n{prompt}"
        body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": full_prompt}]}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        if "content" in result:
            for block in result["content"]:
                if block.get("type") == "text":
                    return block["text"]
            return result["content"][0].get("text", "")
        return result["choices"][0]["message"]["content"]


def ask_llm(
    prompt: str,
    *,
    image_path: str | None = None,
    provider_name: str = "claude",
    config: dict,
    system: str = "",
) -> str:
    """Two-step: (1) vision describes the screenshot, (2) planning LLM picks an action.

    Returns the planning LLM's text reply (should be a JSON tool call).
    """
    vision_description = ""
    if image_path and os.path.exists(image_path):
        try:
            from swarm.vision import call_vision
            vision_provider = config.get("vision_provider", "local-powerful")
            vision_description = call_vision(
                image_path,
                "Describe what is happening in this game screenshot in 2-3 sentences. "
                "Focus on: player position/movement, HUD info (health/score/lap), obstacles, UI elements.",
                vision_provider,
                config,
            )
            print(f"  vision: {vision_description[:120]}")
        except Exception as e:
            print(f"  [warn] Vision failed: {e}")

    # Build the planning prompt
    if vision_description:
        full_prompt = f"Screenshot description: {vision_description}\n\n{prompt}"
    else:
        full_prompt = prompt

    return _llm_text(full_prompt, system or SYSTEM_PROMPT, provider_name, config)


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool_call(tool_call: dict, qa) -> dict:
    """Dispatch a parsed LLM tool call to the appropriate qa_tools function.

    Supported tools:
      key_hold(key, duration)
      key_press(key)
      press_button(text)
      play_macro(actions)
      wait(seconds)
    """
    tool = tool_call.get("tool", "")
    args = tool_call.get("args", {})

    if tool == "key_hold":
        key = args.get("key", args.get("action", ""))
        duration = float(args.get("duration", 1.0))
        return qa.key_hold(key, duration)

    elif tool == "key_press":
        key = args.get("key", args.get("action", ""))
        return qa.qa_key_press(key)

    elif tool == "press_button":
        text = args.get("text", args.get("id", args.get("label", "")))
        return qa.qa_press_button(text)

    elif tool == "play_macro":
        actions = args.get("actions", [])
        return qa.play_macro(actions)

    elif tool == "wait":
        seconds = float(args.get("seconds", 1.0))
        return qa.qa_wait(seconds)

    elif tool == "click_at":
        x = int(args.get("x", 0))
        y = int(args.get("y", 0))
        return qa._qa_click_at(x, y)

    else:
        return {"ok": False, "error": f"Unknown tool: {tool!r}"}


def parse_tool_call(text: str) -> dict | None:
    """Extract the first JSON object that has a 'tool' key from the LLM reply."""
    # Find all top-level { ... } blocks by tracking brace depth
    candidates = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start != -1:
                candidates.append(text[start:i+1])
                start = -1

    # Try each candidate; prefer ones that have a "tool" key
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and "tool" in obj:
                return obj
        except json.JSONDecodeError:
            pass

    # Fall back: try any valid JSON object
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Harness loop
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an AI playing a video game. Each step you receive a screenshot and game state, then output ONE action.

CRITICAL: Your entire reply must be a single JSON object. No text before it. No text after it. No markdown.

The JSON must have exactly these two keys: "tool" and "args".

VALID TOOLS (use ONLY these exact tool names):

key_hold — hold a key/action for N seconds (use this for movement, acceleration)
  {"tool": "key_hold", "args": {"key": "ship_thrust", "duration": 2.0}}
  {"tool": "key_hold", "args": {"key": "turn_right", "duration": 1.0}}
  {"tool": "key_hold", "args": {"key": "move_right", "duration": 1.5}}

key_press — tap a key once (use for single actions like jump, shoot)
  {"tool": "key_press", "args": {"key": "jump"}}
  {"tool": "key_press", "args": {"key": "space"}}

press_button — click a UI button by label (use for menus)
  {"tool": "press_button", "args": {"text": "Start Game"}}
  {"tool": "press_button", "args": {"text": "Play"}}

play_macro — run a sequence of actions (thrust + steer combo)
  {"tool": "play_macro", "args": {"actions": [{"type": "hold", "action": "ship_thrust", "duration": 2.0}, {"type": "hold", "action": "turn_right", "duration": 0.5}]}}

wait — pause for N seconds
  {"tool": "wait", "args": {"seconds": 1.0}}

INVALID (never use): ui_accept, godot, game_state, set_game_state, action, method

For anti-gravity racing: use ship_thrust to accelerate, turn_left/turn_right to steer.
"""


def harness_loop(
    project_name: str,
    project_path: Path,
    config: dict,
    goal: str,
    max_steps: int,
    provider_name: str,
):
    import swarm.qa_tools as qa

    # Patch qa_tools globals
    qa.WORKSPACE = project_path.parent
    qa.PROJECT = project_name
    qa.PROJECT_PATH_OVERRIDE = str(project_path)
    qa.QA_CONFIG = config
    qa.DATA_DIR = str(SWARM_ROOT / "data")

    godot_path = config.get("godot_path", "")
    if godot_path:
        os.environ.setdefault("GODOT_PATH", godot_path)

    # Screenshots go into <project>/harness_screenshots/
    screenshot_dir = project_path / "harness_screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    # Read game design doc for context
    game_design_text = ""
    game_design_path = project_path / "GAME_DESIGN.md"
    if game_design_path.exists():
        game_design_text = game_design_path.read_text(encoding="utf-8", errors="replace")[:3000]
        print(f"[harness] Loaded GAME_DESIGN.md ({len(game_design_text)} chars)")
    else:
        print("[harness] No GAME_DESIGN.md found — proceeding without game context")

    # Launch
    print(f"\n[harness] Launching {project_name}…")
    # Patch port allocation to use a high range agents won't collide with
    # (agents start from 11009; harness uses 12500+ to avoid port-squatting)
    _orig_find = qa._find_free_port_pair
    qa._find_free_port_pair = lambda base=12500, max_range=200: _orig_find(base=12500, max_range=max_range)
    launch_result = qa.launch_game(str(project_path))
    qa._find_free_port_pair = _orig_find
    if not launch_result.get("ok"):
        print(f"[harness] Launch failed: {launch_result.get('error')}")
        return

    pid = launch_result.get("pid")
    print(f"[harness] Game running (pid={pid})")

    # Wait for StateServer to be ready
    print("[harness] Waiting for StateServer…", end="", flush=True)
    ready = False
    for _ in range(20):
        state = qa.get_game_state()
        if "error" not in state:
            ready = True
            break
        print(".", end="", flush=True)
        time.sleep(0.5)
    print()

    if not ready:
        print("[harness] StateServer did not come up in time — aborting")
        qa.kill_game()
        return

    # Session tracking
    session_log: list[dict] = []

    controls_hint = (
        "Common Godot actions: move_left, move_right, move_up, move_down, "
        "ship_thrust, turn_left, turn_right, jump, shoot, ui_accept"
    )

    print(f"\n[harness] Goal: {goal}")
    print(f"[harness] Max steps: {max_steps}")
    print(f"[harness] LLM provider: {provider_name}")
    print()

    for step in range(1, max_steps + 1):
        print(f"--- Step {step}/{max_steps} ---")

        # Check game is still running
        if qa._qa_game_process is not None and qa._qa_game_process.poll() is not None:
            print("[harness] Game process died — stopping")
            break

        # a) Screenshot — save directly to harness dir
        ss_name = f"harness_step_{step:03d}"
        ss_path_full = str(screenshot_dir / f"{ss_name}.png")
        ss_image_path = None
        ss_result = qa._state_server_send({"command": "screenshot_b64"}, timeout=30.0)
        if "image_base64" in ss_result:
            import base64
            img_bytes = base64.b64decode(ss_result["image_base64"])
            with open(ss_path_full, "wb") as f:
                f.write(img_bytes)
            ss_image_path = ss_path_full
            print(f"  screenshot → {ss_path_full}")
        else:
            # StateServer unavailable — skip screenshot rather than grabbing wrong window
            print(f"  screenshot skipped (StateServer unavailable: {ss_result.get('error', 'unknown')})")

        # b) Game state
        state = qa.get_game_state()
        game_state = state.get("game_state", {})
        scene_root = state.get("scene_tree", {}).get("name", "?")
        print(f"  game_state = {json.dumps(game_state)[:200]}")
        print(f"  scene = {scene_root}")

        # c) Build prompt for LLM
        state_summary = json.dumps(game_state, indent=2) if game_state else "(empty — game_state() not implemented)"
        user_prompt = (
            f"You are playing the game '{project_name}'.\n"
            f"Goal: {goal}\n\n"
        )
        if game_design_text:
            user_prompt += f"Game Design:\n{game_design_text}\n\n"
        user_prompt += (
            f"Controls hint: {controls_hint}\n\n"
            f"Current scene root: {scene_root}\n"
            f"Current game state:\n{state_summary}\n\n"
            f"Step {step}/{max_steps}. What is the single best action to take right now?\n"
            f"Reply with ONE JSON tool call."
        )

        # d) Ask LLM
        print("  Asking LLM…", end="", flush=True)
        try:
            reply = ask_llm(
                user_prompt,
                image_path=ss_image_path,
                provider_name=provider_name,
                config=config,
                system=SYSTEM_PROMPT,
            )
            print(f" done")
            print(f"  LLM reply: {reply[:200]}")
        except Exception as e:
            print(f"\n  LLM error: {e}")
            session_log.append({"step": step, "error": str(e)})
            time.sleep(2)
            continue

        # e) Parse tool call
        tool_call = parse_tool_call(reply)
        if not tool_call:
            print(f"  Could not parse tool call from reply — skipping step")
            session_log.append({"step": step, "reply": reply, "tool_call": None, "result": None})
            continue

        print(f"  tool_call = {json.dumps(tool_call)}")

        # f) Execute
        result = execute_tool_call(tool_call, qa)
        print(f"  result = {result}")

        session_log.append({
            "step": step,
            "game_state": game_state,
            "scene": scene_root,
            "tool_call": tool_call,
            "result": result,
        })

        # g) Check goal (simple heuristic via vision query if game_state is available)
        if game_state:
            goal_lower = goal.lower()
            # Common win/loss keys
            for key in ("game_over", "won", "victory", "finished", "complete"):
                val = game_state.get(key)
                if val is True or val == 1:
                    print(f"\n[harness] Terminal state detected: {key}={val}")
                    break

        time.sleep(0.2)  # brief pause between steps

    # Kill game
    print("\n[harness] Killing game…")
    qa.kill_game()

    # Session summary
    print()
    print("=" * 60)
    print("SESSION SUMMARY")
    print("=" * 60)
    print(f"  project:   {project_name}")
    print(f"  goal:      {goal}")
    print(f"  steps run: {len(session_log)}")
    tool_ok = sum(1 for e in session_log if e.get("result", {}) and e["result"].get("ok"))
    tool_fail = sum(1 for e in session_log if e.get("result", {}) and not e["result"].get("ok"))
    print(f"  tools ok:  {tool_ok}")
    print(f"  tools fail:{tool_fail}")
    print()

    # Save log
    log_path = screenshot_dir / "session_log.json"
    log_path.write_text(json.dumps(session_log, indent=2))
    print(f"  full log → {log_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="LLM-driven game player using the QA pipeline",
    )
    parser.add_argument("project_name", help="Name of the project (e.g. anti-grav-rush)")
    parser.add_argument(
        "--goal",
        default="Play the game as effectively as possible",
        help="High-level goal for the LLM (default: play effectively)",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=50,
        help="Maximum number of action steps (default: 50)",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="LLM provider to use: claude, minimax, openrouter, kimi (default: from config.json)",
    )
    args = parser.parse_args()

    config = load_config()
    provider_name = args.provider or config.get("llm_provider", "claude")
    project_path = resolve_project_path(args.project_name, config)

    if not project_path.exists():
        print(f"Error: project path does not exist: {project_path}")
        sys.exit(1)

    if not (project_path / "project.godot").exists():
        print(f"Warning: no project.godot found at {project_path}")

    print(f"Game Harness — {args.project_name}")
    print(f"  path:     {project_path}")
    print(f"  goal:     {args.goal}")
    print(f"  steps:    {args.steps}")
    print(f"  provider: {provider_name}")

    harness_loop(
        project_name=args.project_name,
        project_path=project_path,
        config=config,
        goal=args.goal,
        max_steps=args.steps,
        provider_name=provider_name,
    )


if __name__ == "__main__":
    main()
