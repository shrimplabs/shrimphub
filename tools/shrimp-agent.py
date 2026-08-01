#!/usr/bin/env python3
"""
shrimp-agent — interactive local coding agent powered by your swarm LLMs.

Usage:
  shrimp-agent                        # interactive REPL in current directory
  shrimp-agent "fix the login bug"    # one-shot task, exits when done
  shrimp-agent --provider minimax     # override provider
  shrimp-agent --provider athena-qwen35
  shrimp-agent --dir /path/to/project # run in a different directory

Environment:
  SWARM_PROVIDER   Default provider (minimax, athena-qwen35, claude, etc.)
  MINIMAX_API_KEY  / ANTHROPIC_API_KEY / OPENROUTER_API_KEY / KIMI_API_KEY

Config is read from the swarm-controller config.json automatically so all
your registered providers and API keys are available.
"""

import argparse
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

_ANSI_ESCAPE = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def _strip_escapes(text: str) -> str:
    """Remove ANSI/VT escape sequences that terminals inject into input."""
    return _ANSI_ESCAPE.sub('', text).strip()

# ── locate swarm-controller root ─────────────────────────────────────────────

_TOOL_DIR = Path(__file__).resolve().parent
_SWARM_ROOT = _TOOL_DIR.parent
sys.path.insert(0, str(_SWARM_ROOT))

# ── colour helpers ────────────────────────────────────────────────────────────

_NO_COLOR = not sys.stdout.isatty() or os.environ.get("NO_COLOR")

def _c(code: str, text: str) -> str:
    if _NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def cyan(t):    return _c("36", t)
def green(t):   return _c("32", t)
def yellow(t):  return _c("33", t)
def red(t):     return _c("31", t)
def bold(t):    return _c("1", t)
def dim(t):     return _c("2", t)

# ── config loading ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    cfg_path = _SWARM_ROOT / "config.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text())
        except Exception:
            pass
    return {}

def _load_env():
    """Load .env from swarm root into os.environ."""
    env_path = _SWARM_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val

# ── provider setup ────────────────────────────────────────────────────────────

def _setup_provider(provider_name: str, config: dict):
    """Configure swarm module globals for the chosen provider."""
    import swarm.agent_runtime as rt
    import swarm.provider_utils as pu

    all_providers = {}
    # built-in providers
    for name, cfg in pu.LLM_PROVIDERS.items():
        all_providers[name] = cfg
    # config.json overrides
    for name, cfg in config.get("llm_providers", {}).items():
        all_providers[name] = cfg

    rt.LLM_PROVIDER = provider_name
    rt.LLM_PROVIDERS = all_providers

    # resolve to confirm it works
    pconf = pu.resolve_provider(provider_name, all_providers)
    return pconf

# ── runtime env setup ─────────────────────────────────────────────────────────

_TASK_COUNTER = 0

def _setup_runtime(project_path: Path, provider: str, task_desc: str, config: dict):
    """Set all agent_runtime module globals before calling main()."""
    import swarm.agent_runtime as rt
    import swarm.tools.core as tc

    global _TASK_COUNTER
    _TASK_COUNTER += 1

    project_name = project_path.name
    task_id = f"shrimp-{int(time.time())}-{_TASK_COUNTER:04d}"

    # Core paths
    rt.WORKSPACE = project_path.parent
    rt.PROJECT = project_name
    rt.PROJECT_PATH_OVERRIDE = str(project_path)
    rt.TASK_ID = task_id
    rt.TASK_TYPE = "feature"
    rt.TASK_DESC = task_desc
    rt.MAX_TOOL_LOOPS = 150
    rt.DATA_DIR = str(_SWARM_ROOT / "data")

    # Provider
    rt.LLM_PROVIDER = provider
    rt.SCOUT_PROVIDER = ""
    rt.WORK_PROVIDER = ""
    rt.SYNTHESIZE_PROVIDER = ""
    rt.PLAN_PROVIDER = ""
    rt.COMPACTION_PROVIDER = ""
    rt.LLM_PROVIDERS = _setup_provider(provider, config)

    # Pipeline — flat mode (no phases, just the tool loop)
    rt.PIPELINE = []
    rt.ADAPTIVE_FLAT = False
    rt.LOOP_MODEL_ROUTING = {}
    rt.PHASE_LOOP_LIMITS = {}

    # Misc
    rt.TASK_COMPLETE_REQUIRES_COMMIT = False
    rt.READ_ONLY = False
    rt.QA_CYCLE = 0
    rt.QA_MAX_CYCLES = 3
    rt.COMPACT_TOKEN_THRESHOLD = 120_000
    rt.THINKING_ENABLED = False
    rt.THINKING_BUDGET = 0
    rt.META_INVESTIGATION_ENABLED = False
    rt.AGENT_TIMEOUT = 0
    rt.MAX_LINES = 5000
    rt.GIT_BRANCH = "main"
    rt.LAST_FAILURE = ""
    rt.RESEARCH_CONTEXT = ""
    rt.ATTEMPT_HISTORY = ""
    rt.MCP_SERVERS = {}
    rt.RAG_ENABLED = False
    rt.TASK_METADATA = {}

    # tool core globals
    tc.PROJECT = project_name
    tc.WORKSPACE = str(project_path.parent)
    tc.PROJECT_PATH_OVERRIDE = str(project_path)
    tc.READ_ONLY = False
    tc.TASK_ID = task_id

    return task_id

# ── custom system prompt ──────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are shrimp-agent, an interactive coding assistant. You are working in the
project directory: {project_path}

You have access to tools for reading and writing files, running shell commands,
searching the web, and committing changes with git.

Guidelines:
- Work directly in the project directory
- Use run_command for shell commands (build, test, lint, etc.)
- Write clean, idiomatic code in the project's language
- Commit your work with git_commit when you're done with a change
- When finished, say TASK_COMPLETE

If the user's request is conversational (a question, explanation, status check),
just answer it and say TASK_COMPLETE — you don't need to use tools for everything.
"""

# ── output rendering ──────────────────────────────────────────────────────────

_TOOL_ICONS = {
    "run_command":     "$ ",
    "write_file":      "✎ ",
    "read_file":       "📄",
    "patch_file":      "✎ ",
    "list_files":      "ls",
    "git_commit":      "✔ ",
    "git_push":        "↑ ",
    "web_search":      "🔍",
    "fetch_url":       "🌐",
    "read_file_range": "📄",
}

_CURRENT_LOOP = [0]

def _patch_logging(project_path: Path):
    """Redirect agent runtime log() to informative terminal output."""
    import swarm.agent_runtime as rt

    def _pretty_log(msg: str, *args):
        # Loop counter
        m = re.search(r'loop (\d+)/(\d+)', msg, re.IGNORECASE)
        if m:
            loop, total = m.group(1), m.group(2)
            _CURRENT_LOOP[0] = int(loop)
            print(dim(f"\n─── loop {loop}/{total} ───"), flush=True)
            return

        # Tool execution
        if "Executing tool:" in msg:
            tool = msg.split("Executing tool:")[-1].strip()
            icon = _TOOL_ICONS.get(tool, "⚙ ")
            print(f"  {cyan(icon)} {bold(tool)}", end="  ", flush=True)
            return

        # Thinking blocks
        if "[LLM] thinking:" in msg:
            thought = msg.split("[LLM] thinking:")[-1].strip()
            if len(thought) > 100:
                thought = thought[:100] + "…"
            print(dim(f"  💭 {thought}"), flush=True)
            return

        # LLM call — show model
        if "[LLM] provider=" in msg:
            m2 = re.search(r'model=(\S+)', msg)
            model = m2.group(1) if m2 else "?"
            print(dim(f"  🦐 {model}"), flush=True)
            return

        # Warnings / special events
        if "WARNING" in msg or "nudging" in msg:
            print(yellow(f"  ⚠  {msg.strip()}"), flush=True)
            return

        if msg.startswith("[") and "]" in msg:
            tag = msg[1:msg.index("]")]
            body = msg[msg.index("]")+1:].strip()
            if tag in ("WrapUp", "Stall", "Meta", "Hint"):
                print(yellow(f"  [{tag}] {body}"), flush=True)
            # suppress other internal tags
            return

        # Plain prose from agent
        if msg.strip() and not msg.startswith("["):
            print(msg, flush=True)

    rt.log = _pretty_log


def _format_tool_call_display(text: str):
    """Show tool calls with key args before execution."""
    calls = re.findall(r'\[TOOL_CALL\](.*?)\[/TOOL_CALL\]', text, re.DOTALL)
    for raw in calls:
        try:
            import json as _json
            tc = _json.loads(raw.strip())
            tool = tc.get("tool", "?")
            args = tc.get("args", {})
            key_arg = ""
            for k in ("command", "path", "message", "query", "url", "filename"):
                if k in args:
                    val = str(args[k])[:60]
                    key_arg = f" {dim(val)}"
                    break
            icon = _TOOL_ICONS.get(tool, "⚙ ")
            print(f"  {cyan(icon)} {bold(tool)}{key_arg}", flush=True)
        except Exception:
            pass


def _patch_llm_streaming():
    """Wrap call_llm to show prose and tool calls as they arrive."""
    import swarm.llm_utils as lu

    _orig = lu.call_llm

    def _wrapped(sys_prompt, messages, provider=None):
        result = _orig(sys_prompt, messages, provider=provider)
        text = result[0] if isinstance(result, tuple) else result

        _format_tool_call_display(text)

        # Show prose
        clean = re.sub(r'\[TOOL_CALL\].*?\[/TOOL_CALL\]', '', text, flags=re.DOTALL)
        clean = re.sub(r'<tool_call>.*?</tool_call>', '', clean, flags=re.DOTALL)
        clean = clean.replace("TASK_COMPLETE", "").strip()
        if clean:
            print(f"\n{cyan('●')} {clean}\n", flush=True)

        return result

    lu.call_llm = _wrapped

# ── one-shot task runner ──────────────────────────────────────────────────────

def run_task(description: str, project_path: Path, provider: str, config: dict) -> int:
    import swarm.agent_runtime as rt

    _setup_runtime(project_path, provider, description, config)
    _patch_logging(project_path)
    _patch_llm_streaming()

    # Override the system/user prompts that main() would pick for "feature" type
    sys_prompt = _SYSTEM_PROMPT.format(project_path=project_path)
    rt.FEATURE_SYSTEM = sys_prompt
    rt.FEATURE_USER = description
    rt.system_prompt = sys_prompt
    rt.user_prompt = description
    # Also override the Python feature prompts in case of fallthrough
    rt.PYTHON_FEATURE_SYSTEM = sys_prompt
    rt.PYTHON_FEATURE_USER = description

    print(f"\n{bold('shrimp-agent')} {dim(f'@ {project_path.name}')} {dim(f'[{provider}]')}\n")
    print(f"{cyan('▶')} {description}\n")

    try:
        exit_code = rt.main()
    except KeyboardInterrupt:
        print(f"\n{yellow('interrupted')}", flush=True)
        exit_code = 1
    except Exception as e:
        print(f"\n{red('error:')} {e}", flush=True)
        traceback.print_exc()
        exit_code = 1

    return exit_code

# ── REPL ──────────────────────────────────────────────────────────────────────

def repl(project_path: Path, provider: str, config: dict):
    # Disable readline terminal capability queries — prevents escape sequences
    # being injected into input when running inside shrimpterm or other SSH clients
    try:
        import readline
        readline.parse_and_bind("")
    except ImportError:
        pass
    # Also tell the terminal we don't want to query its capabilities
    os.environ.setdefault("TERM", "dumb")

    print(f"\n{bold('🦐 shrimp-agent')} {dim(f'v0.1 @ {project_path}')}")
    print(f"{dim(f'provider: {provider}  •  Ctrl+C to exit')}\n")

    # Persistent conversation — we'll accumulate messages across turns
    conversation: list[dict] = []

    import swarm.agent_runtime as rt
    import swarm.llm_utils as lu
    from swarm.provider_utils import resolve_provider

    _setup_runtime(project_path, provider, "", config)
    _patch_logging(project_path)

    pconf = resolve_provider(provider, rt.LLM_PROVIDERS)
    sys_prompt = _SYSTEM_PROMPT.format(project_path=project_path)

    while True:
        try:
            user_input = _strip_escapes(input(f"{green('you')} › "))
        except (KeyboardInterrupt, EOFError):
            print(f"\n{dim('bye')}")
            break

        if not user_input:
            continue
        if user_input.lower() in ("/exit", "/quit", "exit", "quit"):
            print(dim("bye"))
            break
        if user_input.lower() == "/clear":
            conversation.clear()
            print(dim("conversation cleared"))
            continue
        if user_input.lower() == "/provider":
            print(dim(f"provider: {provider}  model: {pconf.get('model','?')}"))
            continue

        conversation.append({"role": "user", "content": user_input})

        print(f"\n{cyan('shrimp')} › ", end="", flush=True)

        try:
            result = lu.call_llm(sys_prompt, conversation, provider=provider)
            text = result[0] if isinstance(result, tuple) else result
            tokens = result[1] if isinstance(result, tuple) else {}
        except Exception as e:
            print(f"\n{red('LLM error:')} {e}", flush=True)
            conversation.pop()
            continue

        # Strip tool calls from display but execute them
        tool_calls = re.findall(
            r'\[TOOL_CALL\](.*?)\[/TOOL_CALL\]', text, re.DOTALL
        )
        display_text = re.sub(r'\[TOOL_CALL\].*?\[/TOOL_CALL\]', '', text, flags=re.DOTALL).strip()
        display_text = display_text.replace("TASK_COMPLETE", "").strip()

        if display_text:
            print(display_text, flush=True)

        # Execute any tool calls
        if tool_calls:
            from swarm.tool_dispatch import execute_tool
            tool_results = []
            for tc_raw in tool_calls:
                try:
                    tc_data = json.loads(tc_raw.strip())
                    tool_name = tc_data.get("tool", "")
                    tool_args = tc_data.get("args", {})
                    print(f"\n  {dim('⚙ ' + tool_name)}", flush=True)
                    result_data = execute_tool(tool_name, tool_args, str(project_path), provider)
                    result_str = json.dumps(result_data) if isinstance(result_data, dict) else str(result_data)
                    # Show brief result
                    brief = result_str[:200] + ("…" if len(result_str) > 200 else "")
                    print(f"  {dim(brief)}", flush=True)
                    tool_results.append({"tool": tool_name, "result": result_data})
                except Exception as e:
                    print(f"  {red('tool error:')} {e}", flush=True)
                    tool_results.append({"tool": "?", "error": str(e)})

            # Feed tool results back as assistant + tool_result turn
            conversation.append({"role": "assistant", "content": text})
            tool_result_content = "\n".join(
                f"[{r.get('tool','?')}]: {json.dumps(r.get('result', r.get('error')))}"
                for r in tool_results
            )
            conversation.append({"role": "user", "content": f"Tool results:\n{tool_result_content}"})
        else:
            conversation.append({"role": "assistant", "content": text})

        # Token info
        if tokens:
            in_t = tokens.get("input", 0)
            out_t = tokens.get("output", 0)
            print(f"\n{dim(f'  [{in_t}in {out_t}out]')}", flush=True)

        print()

# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="shrimp-agent",
        description="Interactive local coding agent powered by your swarm LLMs",
    )
    parser.add_argument("task", nargs="?", help="One-shot task description (omit for REPL)")
    parser.add_argument("--provider", "-p", default="", help="LLM provider name")
    parser.add_argument("--dir", "-d", default="", help="Project directory (default: cwd)")
    args = parser.parse_args()

    _load_env()
    config = _load_config()

    # Resolve project path
    project_path = Path(args.dir).resolve() if args.dir else Path.cwd().resolve()
    if not project_path.exists():
        print(red(f"error: directory not found: {project_path}"), file=sys.stderr)
        sys.exit(1)

    # Resolve provider
    provider = (
        args.provider
        or os.environ.get("SWARM_PROVIDER", "")
        or config.get("llm_provider", "minimax")
    )

    if args.task:
        # One-shot mode
        exit_code = run_task(args.task, project_path, provider, config)
        sys.exit(exit_code)
    else:
        # Interactive REPL
        repl(project_path, provider, config)

if __name__ == "__main__":
    main()
