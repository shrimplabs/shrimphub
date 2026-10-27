#!/usr/bin/env python3
"""
Pipeline agent metrics — read/write ratio in work phase only.
Excludes adaptive-flat (single-loop) agents.

Usage: python3 tools/pipeline-metrics.py [--since <iso-date>]
"""
import re, sys
from pathlib import Path
from datetime import datetime, timezone

DATA = Path(__file__).parent.parent / "data"
WORK_PY = Path(__file__).parent.parent / "swarm/phases/work.py"

READ_TOOLS   = {"read_file", "read_file_range", "list_files", "search_code"}
WRITE_TOOLS  = {"write_file", "patch_file", "append_file"}

# Default cutoff: when work.py was last modified (i.e. when fix landed)
cutoff_ts = WORK_PY.stat().st_mtime
if "--since" in sys.argv:
    idx = sys.argv.index("--since")
    cutoff_ts = datetime.fromisoformat(sys.argv[idx+1]).timestamp()

rows = []
for f in sorted(DATA.glob("agent_*.log"), key=lambda x: x.stat().st_mtime):
    if f.stat().st_mtime < cutoff_ts:
        continue
    text = f.read_text(errors="ignore")
    if "Pipeline] Starting:" not in text:
        continue

    agent_id    = f.stem.replace("agent_", "")[:8]
    plan_loops  = text.count("Pipeline:plan] Plan loop")
    scout_loops = text.count("Pipeline:scout] Scout loop")
    work_loops  = text.count("Pipeline:work] Work loop")
    compacted   = text.count("Scout handoff: compacted")

    pipeline_m  = re.search(r'Pipeline\] Starting: (.+)', text)
    pipeline    = pipeline_m.group(1).strip() if pipeline_m else "?"

    work_start  = text.find("PHASE: WORK")
    if work_start < 0:
        work_section = ""
    else:
        ends = [i for tag in ("PHASE: VALIDATE", "Pipeline] Done", "Pipeline] FAILED")
               for i in [text.find(tag, work_start + 10)] if i > 0]
        work_end     = min(ends) if ends else len(text)
        work_section = text[work_start:work_end]

    reads = writes = commits = 0
    for m in re.finditer(r'Executing tool: (\w+)', work_section):
        t = m.group(1)
        if t in READ_TOOLS:     reads += 1
        elif t in WRITE_TOOLS:  writes += 1
        elif t == "git_commit":  commits += 1

    no_op            = "[NoOp]" in work_section
    uncommitted      = "[UncommittedWrites]" in work_section
    silent_loops     = len(re.findall(r'No tool calls parsed at loop', work_section))

    result_m = re.search(r'Pipeline\] Done\. (OK|FAILED)', text)
    result   = result_m.group(1) if result_m else "running"
    ratio    = f"{writes/reads:.1f}x" if reads > 0 else ("all-write" if writes > 0 else "n/a")

    rows.append(dict(
        agent=agent_id, pipeline=pipeline,
        plan=plan_loops, scout=scout_loops, work=work_loops,
        reads=reads, writes=writes, commits=commits,
        ratio=ratio, result=result, compacted=compacted,
        no_op=no_op, uncommitted=uncommitted, silent_loops=silent_loops,
    ))

if not rows:
    print("No pipeline agents found after cutoff.")
    sys.exit(0)

print(f"{'agent':10} {'pipeline':30} {'plan':>5} {'scout':>6} {'work':>5} {'reads':>7} {'writes':>7} {'commits':>8} {'ratio':>10} {'result':>8} {'cmpct':>6} {'noop':>6} {'uncommit':>8} {'silent':>7}")
print("-" * 130)
for r in rows:
    flags = ("Y" if r["no_op"] else "") + ("U" if r["uncommitted"] else "")
    print(f"{r['agent']:10} {r['pipeline']:30} {r['plan']:>5} {r['scout']:>6} {r['work']:>5} {r['reads']:>7} {r['writes']:>7} {r['commits']:>8} {r['ratio']:>10} {r['result']:>8} {r['compacted']:>6} {('Y' if r['no_op'] else '-'):>6} {('Y' if r['uncommitted'] else '-'):>8} {r['silent_loops']:>7}")

done = [r for r in rows if r["result"] == "OK"]
print()
print(f"Agents: {len(rows)} total, {len(done)} completed OK, {sum(1 for r in rows if r['result']=='FAILED')} failed, {sum(1 for r in rows if r['result']=='running')} running")
if done:
    print(f"Avg work loops : {sum(r['work'] for r in done)/len(done):.1f}")
    print(f"Avg work reads : {sum(r['reads'] for r in done)/len(done):.1f}")
    print(f"Avg work writes: {sum(r['writes'] for r in done)/len(done):.1f}")
    print(f"Avg commits    : {sum(r['commits'] for r in done)/len(done):.1f}")
    tot_r = sum(r['reads'] for r in done)
    tot_w = sum(r['writes'] for r in done)
    print(f"Overall w/r    : {tot_w/tot_r:.2f}x" if tot_r > 0 else "Overall w/r: n/a")
    print(f"Compacted      : {sum(r['compacted'] for r in done)}/{len(done)} agents")
    noop_count = sum(1 for r in done if r["no_op"])
    uncommit_count = sum(1 for r in done if r["uncommitted"])
    silent_total = sum(r["silent_loops"] for r in done)
    if noop_count:
        print(f"No-op completions: {noop_count}/{len(done)} (flagged: WORK_COMPLETE with zero mutations)")
    if uncommit_count:
        print(f"Uncommitted writes: {uncommit_count}/{len(done)} (wrote files but never git_commit)")
    if silent_total:
        print(f"Silent loops total: {silent_total} (no tool calls parsed — check model formatting)")
