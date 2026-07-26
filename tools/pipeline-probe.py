#!/usr/bin/env python3
"""Standalone pipeline harness — test phases against any provider, zero swarm overhead.

Usage:
    python3 tools/harness.py --task "add score display" --dir ~/workspace/my-game
    python3 tools/harness.py --task "fix the jump bug" --pipeline "scout → work" --provider athena
    python3 tools/harness.py --task "refactor main.gd" --pipeline "plan → scout → work" --provider ollama --model qwen2.5-coder:32b
"""
import argparse
import os
import sys
import time
from pathlib import Path

# ── bootstrap: make swarm importable ────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load .env so API keys are available
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ── CLI ──────────────────────────────────────────────────────────────────────
p = argparse.ArgumentParser(description="Standalone swarm pipeline harness")
p.add_argument("--task",     required=True,  help="Task description")
p.add_argument("--dir",      default=".",    help="Project directory (default: cwd)")
p.add_argument("--type",     default="feature", help="Task type (feature/bug/polish/refactor)")
p.add_argument("--pipeline", default="plan → scout → work",
               help='Phase sequence, e.g. "scout → work" or "plan → scout → work → validate"')
p.add_argument("--provider", default=None,   help="LLM provider name (minimax/athena/ollama/…)")
p.add_argument("--model",    default=None,   help="Override model name")
p.add_argument("--base-url", default=None,   help="Override provider base URL")
p.add_argument("--dry-run",  action="store_true", help="Print config and exit without running")
p.add_argument("--verbose",  action="store_true", help="Print model reasoning text between tool calls")
args = p.parse_args()

project_path = Path(args.dir).expanduser().resolve()
if not project_path.exists():
    sys.exit(f"Error: --dir {project_path} does not exist")

# ── patch agent_runtime globals before any swarm import ──────────────────────
import swarm.agent_runtime as _rt

_provider = args.provider or os.environ.get("LLM_PROVIDER", "minimax")
_rt.LLM_PROVIDER  = _provider
_rt.TASK_ID       = f"harness-{int(time.time())}"
_rt.TASK_TYPE     = args.type
_rt.PROJECT       = project_path.name
_rt.WORKSPACE     = str(project_path.parent)
_rt.TASK_DESC     = args.task
_rt.log           = lambda msg: print(f"  {msg}")

# Build provider config
from swarm.provider_utils import LLM_PROVIDERS as _BUILTIN
_providers = dict(_BUILTIN)

if args.model or args.base_url:
    cfg = dict(_providers.get(_provider, {}))
    if args.model:    cfg["model"]    = args.model
    if args.base_url: cfg["base_url"] = args.base_url
    _providers[_provider] = cfg

_rt.LLM_PROVIDERS = _providers

# Also sync the tools/_shared.py globals — search_code reads these, not _rt's copies
import swarm.tools._shared as _shared
_shared.WORKSPACE = str(project_path.parent)
_shared.PROJECT   = project_path.name
_shared.PROJECT_PATH_OVERRIDE = str(project_path)  # force search_code to use exact path

# ── parse pipeline ────────────────────────────────────────────────────────────
_SEP = {"→", "->", ">", ","}
phases = [p.strip() for p in args.pipeline.replace("->", "→").replace(",", "→").split("→")]
phases = [p for p in phases if p]

if args.dry_run:
    print(f"Provider : {_provider}")
    print(f"Model    : {_providers.get(_provider, {}).get('model', '(default)')}")
    print(f"Base URL : {_providers.get(_provider, {}).get('base_url', '(default)')}")
    print(f"Pipeline : {' → '.join(phases)}")
    print(f"Project  : {project_path}")
    print(f"Task     : {args.task}")
    sys.exit(0)

# ── verbose: intercept LLM calls to print model reasoning ────────────────────
if args.verbose:
    import re as _re
    import swarm.llm_utils as _llm_mod
    _orig_call_llm = _llm_mod.call_llm

    def _verbose_call_llm(system, messages, **kwargs):
        text, tokens, thinking = _orig_call_llm(system, messages, **kwargs)
        # Strip tool call blocks — print only reasoning prose
        prose = _re.sub(r"\[TOOL_CALL\].*?\[/TOOL_CALL\]", "", text, flags=_re.DOTALL).strip()
        if prose:
            print(f"\n\033[2m--- model reasoning ---\033[0m")
            print(f"\033[2m{prose[:800]}\033[0m")
            if len(prose) > 800:
                print(f"\033[2m  ... ({len(prose)-800} chars truncated)\033[0m")
        return text, tokens, thinking

    _llm_mod.call_llm = _verbose_call_llm

# ── run ───────────────────────────────────────────────────────────────────────
from swarm.pipeline import TaskState, run_pipeline

state = TaskState(
    task_id      = _rt.TASK_ID,
    task_type    = args.type,
    project      = project_path.name,
    description  = args.task,
    project_path = str(project_path),
    workspace    = str(project_path.parent),
)

provider_cfg = {
    "plan_provider":       _provider,
    "scout_provider":      _provider,
    "work_provider":       _provider,
    "synthesize_provider": _provider,
}

print(f"\n{'='*60}")
print(f"  Harness: {' → '.join(phases)}")
print(f"  Provider: {_provider}  model: {_providers.get(_provider,{}).get('model','?')}")
print(f"  Project: {project_path.name}  ({project_path})")
print(f"  Task: {args.task}")
print(f"{'='*60}\n")

t0 = time.time()
final = run_pipeline(phases, state, provider_cfg, log_fn=print)
elapsed = time.time() - t0

print(f"\n{'='*60}")
print(f"  Result : {'OK' if not final.failed else 'FAILED'}")
print(f"  Elapsed: {elapsed:.1f}s")
if final.errors:
    print(f"  Errors :")
    for e in final.errors: print(f"    - {e}")
if getattr(final, "work_report", None):
    wr = final.work_report
    print(f"  Work   : {wr.get('loops_used',0)} loops, "
          f"{wr.get('mutation_tool_calls',0)} mutations, "
          f"commit={wr.get('commit_sha') or 'none'}")
    if wr.get("no_op"):        print("  ⚠  NoOp: completed with zero mutations")
    if wr.get("uncommitted"):  print("  ⚠  UncommittedWrites: mutations but no commit")
print(f"{'='*60}\n")
sys.exit(0 if not final.failed else 1)
