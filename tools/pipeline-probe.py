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
import swarm.llm_utils as _llm_mod

_orig_call_llm = _llm_mod.call_llm
_analytics: dict = {
    "phases": {},       # phase_name → {calls, input_tokens, output_tokens, cache_read, tools: {name: count}}
    "_current_phase": None,
}

def _track_phase(name: str):
    _analytics["_current_phase"] = name
    if name not in _analytics["phases"]:
        _analytics["phases"][name] = {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read": 0, "cache_write": 0, "tools": {}, "elapsed_s": 0.0,
            "_t0": time.time(),
        }

def _instrumented_call_llm(system, messages, **kwargs):
    text, tokens, thinking = _orig_call_llm(system, messages, **kwargs)
    phase = _analytics["_current_phase"]
    if phase and phase in _analytics["phases"]:
        p = _analytics["phases"][phase]
        p["calls"] += 1
        if isinstance(tokens, dict):
            p["input_tokens"]  += tokens.get("input", tokens.get("input_tokens", 0))
            p["output_tokens"] += tokens.get("output", tokens.get("output_tokens", 0))
            p["cache_read"]    += tokens.get("cache_read", tokens.get("cache_read_input_tokens", 0))
            p["cache_write"]   += tokens.get("cache_write", tokens.get("cache_creation_input_tokens", 0))
        # count tool calls — matches [TOOL_CALL]{"name":"foo"...}[/TOOL_CALL]
        for tool_name in _re.findall(r'\[TOOL_CALL\]\s*\{[^}]*"name"\s*:\s*"([^"]+)"', text):
            p["tools"][tool_name] = p["tools"].get(tool_name, 0) + 1
    if args.verbose:
        prose = _re.sub(r"\[TOOL_CALL\].*?\[/TOOL_CALL\]", "", text, flags=_re.DOTALL).strip()
        if prose:
            print(f"\n\033[2m--- model reasoning ---\033[0m")
            print(f"\033[2m{prose[:800]}\033[0m")
            if len(prose) > 800:
                print(f"\033[2m  ... ({len(prose)-800} chars truncated)\033[0m")
    return text, tokens, thinking

_llm_mod.call_llm = _instrumented_call_llm

# Intercept pipeline phase entry to track which phase is active
from swarm import pipeline as _pipeline_mod
_orig_run_pipeline = _pipeline_mod.run_pipeline

def _instrumented_run_pipeline(phases, state, provider_cfg, log_fn=None):
    _orig_log = log_fn or (lambda x: None)
    def _phase_tracking_log(msg):
        # "  PHASE: PLAN  (1/4)" → start tracking that phase
        m_start = _re.search(r"PHASE:\s*([A-Z_]+)", msg)
        if m_start:
            phase = m_start.group(1).lower()
            _track_phase(phase)
        # "  ✓ PLAN complete (12.3s)" → record elapsed
        m_done = _re.search(r"✓\s+([A-Z_]+)\s+complete\s+\(([0-9.]+)s\)", msg)
        if m_done:
            phase = m_done.group(1).lower()
            elapsed_s = float(m_done.group(2))
            if phase in _analytics["phases"]:
                _analytics["phases"][phase]["elapsed_s"] = elapsed_s
        # "[Pipeline:plan] Tools: read_file, search_code" → count tool calls
        m_tools = _re.search(r"\[Pipeline:[^\]]+\]\s+Tools:\s+(.+)", msg)
        if m_tools and _analytics["_current_phase"]:
            p = _analytics["phases"].get(_analytics["_current_phase"])
            if p:
                for tool_name in _re.split(r"[,\s]+", m_tools.group(1).strip()):
                    tool_name = tool_name.strip()
                    if tool_name:
                        p["tools"][tool_name] = p["tools"].get(tool_name, 0) + 1
        _orig_log(msg)
    return _orig_run_pipeline(phases, state, provider_cfg, log_fn=_phase_tracking_log)

_pipeline_mod.run_pipeline = _instrumented_run_pipeline

# Patch Phase.log so per-phase tool counts are captured from stdout lines
from swarm.pipeline import Phase as _Phase
_orig_phase_log = _Phase.log

def _analytics_phase_log(self, msg: str) -> None:
    _orig_phase_log(self, msg)
    phase = self.name
    if phase not in _analytics["phases"]:
        _track_phase(phase)
    p = _analytics["phases"].get(phase)
    if p:
        m_tools = _re.search(r"^Tools:\s+(.+)", msg)
        if m_tools:
            for tool_name in _re.split(r"[,\s]+", m_tools.group(1).strip()):
                tool_name = tool_name.strip()
                if tool_name:
                    p["tools"][tool_name] = p["tools"].get(tool_name, 0) + 1

_Phase.log = _analytics_phase_log

# ── run ───────────────────────────────────────────────────────────────────────
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

# ── per-phase analytics ───────────────────────────────────────────────────────
total_calls  = sum(p["calls"]         for p in _analytics["phases"].values())
total_in_tok = sum(p["input_tokens"]  for p in _analytics["phases"].values())
total_out_tok= sum(p["output_tokens"] for p in _analytics["phases"].values())
total_cache  = sum(p["cache_read"]    for p in _analytics["phases"].values())

if _analytics["phases"]:
    print(f"\n{'='*60}")
    print(f"  ANALYTICS")
    print(f"{'='*60}")
    print(f"  {'Phase':<16} {'Calls':>5}  {'In tok':>8}  {'Out tok':>8}  {'Cache':>7}  {'Time':>7}  Top tools")
    print(f"  {'-'*16} {'-'*5}  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*7}  ---------")
    for pname, ps in _analytics["phases"].items():
        top_tools = ", ".join(
            f"{t}×{n}" for t, n in sorted(ps["tools"].items(), key=lambda x: -x[1])[:3]
        )
        print(f"  {pname:<16} {ps['calls']:>5}  {ps['input_tokens']:>8,}  {ps['output_tokens']:>8,}  "
              f"{ps['cache_read']:>7,}  {ps['elapsed_s']:>6.1f}s  {top_tools}")
    print(f"  {'TOTAL':<16} {total_calls:>5}  {total_in_tok:>8,}  {total_out_tok:>8,}  {total_cache:>7,}")
    if total_in_tok > 0:
        cache_pct = 100 * total_cache / total_in_tok
        cost_saved = total_cache * 0.9  # MiniMax cache = 90% discount on cache hits
        print(f"\n  Cache hit rate: {cache_pct:.1f}% of input tokens  (~{cost_saved:,.0f} tok discount)")
    print(f"{'='*60}")

# ── JSON summary output ───────────────────────────────────────────────────────
import json as _json

_summary = {
    "task_id": _rt.TASK_ID,
    "project": args.dir,
    "task_type": args.type,
    "pipeline": phases,
    "provider": args.provider,
    "elapsed_s": elapsed,
    "failed": bool(final.failed),
    "errors": list(getattr(final, "errors", []) or []),
    "total_llm_calls": total_calls,
    "total_input_tokens": total_in_tok,
    "total_output_tokens": total_out_tok,
    "total_cache_read_tokens": total_cache,
    "phases": {
        pname: {
            "calls": ps["calls"],
            "input_tokens": ps["input_tokens"],
            "output_tokens": ps["output_tokens"],
            "cache_read_tokens": ps["cache_read"],
            "elapsed_s": ps["elapsed_s"],
            "tools": ps["tools"],
        }
        for pname, ps in _analytics["phases"].items()
    },
    "work": getattr(final, "work_report", None),
    "scout": {
        "files_inspected": len((getattr(final, "scout_report", None) or {}).get("findings", [])),
        "findings_count": len((getattr(final, "scout_report", None) or {}).get("findings", [])),
    } if getattr(final, "scout_report", None) else None,
    "synthesis": {
        "tasks_proposed": len((getattr(final, "synthesis", None) or {}).get("proposed_tasks", []) or []),
        "confidence": (getattr(final, "synthesis", None) or {}).get("confidence"),
    } if getattr(final, "synthesis", None) else None,
}

if args.json_out:
    with open(args.json_out, "w") as _jf:
        _json.dump(_summary, _jf, indent=2, default=str)
    print(f"\n  JSON summary written to: {args.json_out}")
else:
    print(f"\n  JSON summary (use --json-out <path> to save):")
    print("  " + _json.dumps(_summary, default=str)[:300] + " ...")

print(f"{'='*60}\n")
sys.exit(0 if not final.failed else 1)
