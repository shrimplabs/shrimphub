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
p.add_argument("--json-out", default=None,        help="Write structured JSON summary to this path")
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

# Point DATA_DIR at an isolated scratch location so the probe never touches the live
# swarm.db — otherwise _has_active_sibling_tasks() sees real in-progress tasks and
# the broadcast_write gate blocks every file write.
import tempfile, atexit, shutil as _shutil
_probe_data_dir = tempfile.mkdtemp(prefix="pipeline-probe-")
atexit.register(_shutil.rmtree, _probe_data_dir, ignore_errors=True)
_rt.DATA_DIR = _probe_data_dir

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

# ── analytics instrumentation ─────────────────────────────────────────────────
import re as _re
sys.path.insert(0, str(ROOT / "tools"))
from probe_analytics import make_analytics, install as _install_analytics, print_table, build_summary

_analytics = make_analytics()
_install_analytics(_analytics)

# verbose mode: print model reasoning between tool calls
if args.verbose:
    import swarm.llm_utils as _llm_mod_v
    _orig_v = _llm_mod_v.call_llm

    def _verbose_wrap(system, messages, **kwargs):
        text, tokens, thinking = _orig_v(system, messages, **kwargs)
        prose = _re.sub(r"\[TOOL_CALL\].*?\[/TOOL_CALL\]", "", text, flags=_re.DOTALL).strip()
        if prose:
            print(f"\n\033[2m--- model reasoning ---\033[0m")
            print(f"\033[2m{prose[:800]}\033[0m")
            if len(prose) > 800:
                print(f"\033[2m  ... ({len(prose)-800} chars truncated)\033[0m")
        return text, tokens, thinking

    _llm_mod_v.call_llm = _verbose_wrap

# ── run ───────────────────────────────────────────────────────────────────────
from swarm import pipeline as _pipeline_mod
from swarm.pipeline import TaskState

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
final = _pipeline_mod.run_pipeline(phases, state, provider_cfg, log_fn=print)
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

# Show synthesis output if present
if getattr(final, "synthesis", None):
    synth = final.synthesis
    print(f"\n{'='*60}")
    print(f"  SYNTHESIS OUTPUT")
    print(f"{'='*60}")
    if synth.get("summary"):
        print(f"  Summary: {synth['summary']}")
    if synth.get("key_conclusions"):
        print(f"  Conclusions:")
        for c in synth["key_conclusions"]:
            print(f"    - {c}")
    proposed = synth.get("proposed_tasks") or synth.get("implementation_steps") or []
    if proposed:
        label = "proposed_tasks" if synth.get("proposed_tasks") else "implementation_steps"
        print(f"  {label} ({len(proposed)}):")
        for i, t in enumerate(proposed):
            dep_str = f" deps={t['depends_on']}" if t.get("depends_on") else ""
            if label == "proposed_tasks":
                print(f"    [{i}] {t.get('type','feature')} p={t.get('priority',50)}{dep_str}: {t.get('description','')[:100]}")
            else:
                print(f"    [{i}] {t.get('action','?')} {t.get('file','?')}: {t.get('description','')[:80]}")
    print(f"{'='*60}\n")

# ── per-phase analytics + JSON summary ───────────────────────────────────────
import json as _json

print_table(_analytics)

_summary = build_summary(
    _analytics,
    task_id=_rt.TASK_ID,
    project=args.dir,
    task_type=args.type,
    pipeline=phases,
    provider=args.provider,
    elapsed_s=elapsed,
    final_state=final,
)

if args.json_out:
    with open(args.json_out, "w") as _jf:
        _json.dump(_summary, _jf, indent=2, default=str)
    print(f"\n  JSON summary written to: {args.json_out}")
else:
    print(f"\n  (use --json-out <path> to save JSON summary)")

print(f"{'='*60}\n")
sys.exit(0 if not final.failed else 1)
