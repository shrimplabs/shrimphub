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
import threading
import time
import traceback
from pathlib import Path

# ── heartbeat ─────────────────────────────────────────────────────────────────

class Heartbeat:
    """Prints a 'still working' line every N seconds of silence."""

    def __init__(self, interval: int = 30):
        self._interval = interval
        self._last = time.time()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def ping(self):
        """Call whenever real output is printed to reset the timer."""
        self._last = time.time()

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(5):
            if time.time() - self._last >= self._interval:
                loop = _CURRENT_LOOP[0]
                print(dim(f"  ⏳ still working... (loop {loop})"), flush=True)
                self._last = time.time()


_HEARTBEAT: Heartbeat | None = None

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
    rt.READONLY = False
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
    rt.API_PORT = 0  # disable swarm API calls

    # Disable file locking — multi-agent coordination, not needed standalone
    import swarm.runtime_helpers as _rh
    _rh._lock_project_file = lambda path: {"ok": True}
    _rh._unlock_claimed_files = lambda: None

    # Intercept execute_tool to stub out swarm-API-dependent tools that would
    # hang in standalone mode (list_tasks, create_task, etc.)
    import swarm.tool_dispatch as _td
    # Intercept execute_tool to stub out swarm-API-dependent tools that hang
    # when there's no swarm server running (list_tasks, create_task, etc.)
    import swarm.tool_dispatch as _td
    _orig_execute = _td.execute_tool
    _STANDALONE_NOOP = {"list_tasks", "list_subtasks"}
    _STANDALONE_BLOCK = {"create_task", "create_tasks", "create_tasks_file_aware"}
    def _patched_execute(tool_call: dict) -> dict:
        tool = tool_call.get("tool", "")
        if tool in _STANDALONE_NOOP:
            return {"ok": True, "tasks": []}
        if tool in _STANDALONE_BLOCK:
            return {"ok": False, "error": f"{tool} not available in standalone mode"}
        return _orig_execute(tool_call)
    _td.execute_tool = _patched_execute
    rt.execute_tool = _patched_execute  # patch the name bound in agent_runtime's namespace

    # Clear any stale broadcast claim files from previous swarm agents
    for f in ["broadcast_claims", ".broadcast_claims"]:
        stale = project_path / f
        if stale.exists():
            try:
                stale.unlink()
            except Exception:
                pass

    # tool core globals
    tc.PROJECT = project_name
    tc.WORKSPACE = str(project_path.parent)
    tc.PROJECT_PATH_OVERRIDE = str(project_path)
    tc.READ_ONLY = False
    tc.TASK_ID = task_id

    return task_id

# ── custom system prompt ──────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are shrimp-agent, an interactive coding assistant working in: {project_path}

## Tool call format

You MUST use this exact format for every tool call — no other format is accepted:

[TOOL_CALL]{{"tool": "run_command", "args": {{"command": "ls -la"}}}}[/TOOL_CALL]

Rules:
- The JSON must be on a single line between the tags
- Use "tool" and "args" as the keys (not "name"/"arguments"/"invoke")
- Only one tool call per [TOOL_CALL] block
- Do NOT use XML tags like <tool_call>, <invoke>, or <function_calls>

## Available tools

- run_command: run a shell command. args: {{"command": "..."}}
- list_files: list files in a directory. args: {{"path": "."}}
- read_file: read a file. args: {{"path": "..."}}
- read_file_range: read part of a file. args: {{"path": "...", "start_line": N, "end_line": N}}
- write_file: write a file. args: {{"path": "...", "content": "..."}}
- patch_file: apply a unified diff patch. args: {{"path": "...", "patch": "..."}}
- git_commit: commit staged changes. args: {{"message": "..."}}
- web_search: search the web. args: {{"query": "..."}}
- fetch_url: fetch a URL. args: {{"url": "..."}}

## Guidelines

- Work directly in the project directory
- Run commands to build and test your work
- Commit with git_commit when you finish a change
- When done, write TASK_COMPLETE on its own line

For conversational questions, just answer and write TASK_COMPLETE — no tools needed.
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
            if _HEARTBEAT:
                _HEARTBEAT.ping()
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
        # Short-circuit the graph reflection loop — requires swarm API
        if "GRAPH REFLECTION phase" in sys_prompt:
            return ("REFLECTION_COMPLETE", {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})

        result = _orig(sys_prompt, messages, provider=provider)
        text = result[0] if isinstance(result, tuple) else result

        _format_tool_call_display(text)

        if _HEARTBEAT:
            _HEARTBEAT.ping()
        # Show prose — strip all known tool call formats
        clean = re.sub(r'\[TOOL_CALL\].*?\[/TOOL_CALL\]', '', text, flags=re.DOTALL)
        clean = re.sub(r'<tool_call>.*?</tool_call>', '', clean, flags=re.DOTALL)
        clean = re.sub(r'<invoke[^>]*>.*?</invoke>', '', clean, flags=re.DOTALL)
        # Strip MiniMax bracket-encoded tool tags
        clean = re.sub(r'\]<\]minimax\[>\[.*?(?:\]<\]minimax\[>\[|$)', '', clean, flags=re.DOTALL)
        # Strip any remaining [TOOL_CALL] or [/TOOL_CALL] orphan tags
        clean = re.sub(r'\[/?TOOL_CALL\]', '', clean)
        clean = clean.replace("TASK_COMPLETE", "").strip()
        if clean:
            print(f"\n{cyan('●')} {clean}\n", flush=True)

        return result

    lu.call_llm = _wrapped

# ── one-shot task runner ──────────────────────────────────────────────────────

def run_task(description: str, project_path: Path, provider: str, config: dict) -> int:
    global _HEARTBEAT
    import swarm.agent_runtime as rt

    _setup_runtime(project_path, provider, description, config)
    _patch_logging(project_path)
    _patch_llm_streaming()
    _HEARTBEAT = Heartbeat(interval=30).start()

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
    finally:
        _HEARTBEAT.stop()
        _HEARTBEAT = None

    return exit_code

# ── REPL ──────────────────────────────────────────────────────────────────────

def repl(project_path: Path, provider: str, config: dict):
    # Disable readline terminal capability queries (prevents escape codes in shrimpterm)
    try:
        import readline
        readline.parse_and_bind("")
    except ImportError:
        pass
    os.environ.setdefault("TERM", "dumb")

    print(f"\n{bold('🦐 shrimp-agent')} {dim(f'v0.1 @ {project_path}')}")
    print(f"{dim(f'provider: {provider}  •  Ctrl+C or /exit to quit')}")
    print(dim("Each message runs a full agent loop. /clear resets context.\n"))

    while True:
        try:
            user_input = _strip_escapes(input(f"\n{green('you')} › "))
        except (KeyboardInterrupt, EOFError):
            print(f"\n{dim('bye')}")
            break

        if not user_input:
            continue
        if user_input.lower() in ("/exit", "/quit", "exit", "quit"):
            print(dim("bye"))
            break
        if user_input.lower() == "/clear":
            print(dim("context cleared (each turn is independent)"))
            continue
        if user_input.lower() == "/help":
            print(dim("/exit  quit  /help  /clear"))
            continue

        print(f"\n{dim('─' * 40)}", flush=True)
        run_task(user_input, project_path, provider, config)
        print(f"{dim('─' * 40)}", flush=True)

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
