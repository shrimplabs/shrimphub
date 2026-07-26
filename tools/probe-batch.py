#!/usr/bin/env python3
"""Batch pipeline probe runner.

Launches multiple pipeline-probe.py runs in parallel (or sequentially),
collects their JSON summaries, and prints a comparison table.

Usage:
    python3 tools/probe-batch.py <spec.json> [--parallel N] [--out dir]

Spec file format (JSON):
    {
      "default": {
        "dir": "/abs/path/to/project",
        "type": "bug",
        "provider": "minimax"
      },
      "runs": [
        {"name": "plan-scout-work",      "pipeline": "plan->scout->work",
         "task": "Fix score not saving"},
        {"name": "plan-scout-diag-work", "pipeline": "plan->scout->diagnose->work",
         "task": "Fix score not saving",
         "dir": "/other/project"}
      ]
    }

Each run entry merges with "default" (run keys override). Required per-run:
  name      - short label for the comparison table
  pipeline  - phase sequence, arrow-separated
  task      - task description

Optional per-run (override default):
  dir       - project directory
  type      - task type (bug/feature/refactor/...)
  provider  - LLM provider name

Output:
  <out>/<name>.json  for each run
  <out>/summary.json  rolled-up comparison table
  Comparison table printed to stdout at the end
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).parent.parent
PROBE = ROOT / "tools" / "pipeline-probe.py"
PYTHON = ROOT / ".venv" / "bin" / "python3"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

p = argparse.ArgumentParser(description="Batch pipeline probe runner")
p.add_argument("spec", help="Path to batch spec JSON file")
p.add_argument("--parallel", type=int, default=3,
               help="Max concurrent probes (default: 3)")
p.add_argument("--out", default=None,
               help="Output directory for JSON summaries (default: /tmp/probe-batch-<ts>)")
p.add_argument("--dry-run", action="store_true",
               help="Print commands that would run, then exit")
args = p.parse_args()

spec_path = Path(args.spec)
if not spec_path.exists():
    print(f"ERROR: spec file not found: {spec_path}", file=sys.stderr)
    sys.exit(1)

spec = json.loads(spec_path.read_text())
defaults = spec.get("default", {})
runs = spec.get("runs", [])

if not runs:
    print("ERROR: spec has no 'runs' entries", file=sys.stderr)
    sys.exit(1)

out_dir = Path(args.out) if args.out else Path(f"/tmp/probe-batch-{int(time.time())}")
out_dir.mkdir(parents=True, exist_ok=True)

print(f"Batch: {len(runs)} runs  |  parallel={args.parallel}  |  out={out_dir}")
print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Build run configs
# ---------------------------------------------------------------------------

def resolve_run(run: dict) -> dict:
    merged = {**defaults, **run}
    missing = [k for k in ("name", "pipeline", "task") if not merged.get(k)]
    if missing:
        raise ValueError(f"Run missing required keys: {missing}  run={run}")
    if not merged.get("dir"):
        raise ValueError(f"Run '{merged['name']}' has no 'dir' (and default has none)")
    return merged


resolved = []
for r in runs:
    try:
        resolved.append(resolve_run(r))
    except ValueError as e:
        print(f"SKIP: {e}", file=sys.stderr)

if not resolved:
    print("ERROR: no valid runs after resolving", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Run a single probe
# ---------------------------------------------------------------------------

def run_probe(cfg: dict) -> dict:
    name     = cfg["name"]
    pipeline = cfg["pipeline"]
    task     = cfg["task"]
    proj_dir = cfg["dir"]
    task_type= cfg.get("type", "bug")
    provider = cfg.get("provider")
    json_out = str(out_dir / f"{name}.json")
    log_out  = str(out_dir / f"{name}.log")

    cmd = [
        str(PYTHON), str(PROBE),
        "--task",     task,
        "--dir",      proj_dir,
        "--pipeline", pipeline,
        "--type",     task_type,
        "--json-out", json_out,
    ]
    if provider:
        cmd += ["--provider", provider]
    phase_config = cfg.get("phase_config")
    if phase_config:
        import tempfile as _tf, json as _json
        _pc_file = _tf.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        _json.dump(phase_config, _pc_file)
        _pc_file.flush()
        cmd += ["--phase-config", _pc_file.name]

    if args.dry_run:
        print(f"  [dry-run] {' '.join(cmd)}")
        return {"name": name, "status": "dry-run", "json_out": json_out}

    t0 = time.time()
    print(f"  START  {name:<30}  {pipeline}")
    try:
        with open(log_out, "w") as lf:
            result = subprocess.run(
                cmd, stdout=lf, stderr=subprocess.STDOUT,
                timeout=1800,  # 30 min hard cap per probe
            )
        elapsed = time.time() - t0
        ok = result.returncode == 0

        summary = {}
        if Path(json_out).exists():
            try:
                summary = json.loads(Path(json_out).read_text())
            except Exception:
                pass

        status = "OK" if ok else "FAIL"
        print(f"  {status:<6} {name:<30}  {elapsed:.0f}s  "
              f"calls={summary.get('total_llm_calls', '?')}  "
              f"out_tok={summary.get('total_output_tokens', '?')}")

        return {
            "name":    name,
            "status":  status,
            "elapsed": elapsed,
            "log":     log_out,
            "json_out":json_out,
            "summary": summary,
        }
    except subprocess.TimeoutExpired:
        elapsed = time.time() - t0
        print(f"  TIMEOUT {name:<29}  {elapsed:.0f}s")
        return {"name": name, "status": "TIMEOUT", "elapsed": elapsed, "log": log_out}
    except Exception as exc:
        elapsed = time.time() - t0
        print(f"  ERROR  {name:<30}  {exc}")
        return {"name": name, "status": "ERROR", "elapsed": elapsed, "error": str(exc)}


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

if args.dry_run:
    for cfg in resolved:
        run_probe(cfg)
    sys.exit(0)

results = []
with ThreadPoolExecutor(max_workers=args.parallel) as ex:
    futures = {ex.submit(run_probe, cfg): cfg["name"] for cfg in resolved}
    for fut in as_completed(futures):
        results.append(fut.result())

# Sort by original order
name_order = {cfg["name"]: i for i, cfg in enumerate(resolved)}
results.sort(key=lambda r: name_order.get(r["name"], 999))


# ---------------------------------------------------------------------------
# Comparison table
# ---------------------------------------------------------------------------

print(f"\n{'='*60}")
print(f"  COMPARISON")
print(f"{'='*60}")
print(f"  {'Name':<28} {'Status':<7} {'Elapsed':>7} {'Calls':>6} {'OutTok':>8} {'Mutations':>9} {'Tasks':>6}")
print(f"  {'-'*28} {'-'*7} {'-'*7} {'-'*6} {'-'*8} {'-'*9} {'-'*6}")

for r in results:
    s = r.get("summary", {})
    work = s.get("work") or {}
    synth = s.get("synthesis") or {}
    mutations = work.get("mutations", "?") if isinstance(work, dict) else "?"
    tasks = synth.get("tasks_proposed", "?") if isinstance(synth, dict) else "?"
    print(
        f"  {r['name']:<28} {r['status']:<7} {r.get('elapsed', 0):>6.0f}s "
        f"{s.get('total_llm_calls', '?'):>6} {s.get('total_output_tokens', '?'):>8} "
        f"{mutations!s:>9} {tasks!s:>6}"
    )

# Per-phase breakdown if all runs have summaries
all_have_phases = all(r.get("summary", {}).get("phases") for r in results)
if all_have_phases:
    print(f"\n  Per-phase output tokens:")
    all_phases = sorted({ph for r in results for ph in r["summary"]["phases"]})
    header = f"  {'Name':<28} " + " ".join(f"{ph[:8]:>9}" for ph in all_phases)
    print(header)
    print(f"  {'-'*28} " + " ".join(f"{'-'*9}" for _ in all_phases))
    for r in results:
        phases = r["summary"]["phases"]
        row = f"  {r['name']:<28} " + " ".join(
            f"{phases.get(ph, {}).get('output_tokens', 0):>9,}" for ph in all_phases
        )
        print(row)

print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Rolled-up JSON
# ---------------------------------------------------------------------------

batch_summary = {
    "spec":    str(spec_path),
    "out_dir": str(out_dir),
    "ran_at":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "results": results,
}
summary_path = out_dir / "summary.json"
summary_path.write_text(json.dumps(batch_summary, indent=2, default=str))
print(f"Batch summary written to: {summary_path}")
