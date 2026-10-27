#!/usr/bin/env python3
"""
Daily completion rate comparison by LLM model.
Reads agent log files to determine which model was used.

Usage:
  python3 scripts/model_stats.py          # all time
  python3 scripts/model_stats.py --days 7 # last 7 days
"""

import argparse
import glob
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"


def parse_args():
    p = argparse.ArgumentParser(description="Model completion rate stats")
    p.add_argument("--days", type=int, default=0, help="Limit to last N days (0 = all time)")
    p.add_argument("--by-type", action="store_true", help="Break down by task type")
    return p.parse_args()


def parse_log(log_path: str) -> tuple[str, int]:
    """Extract (model, max_loop) from agent log."""
    model = "unknown"
    max_loop = 0
    try:
        with open(log_path) as f:
            for line in f:
                if model == "unknown":
                    m = re.search(r'model=(MiniMax-\S+|claude-\S+|gpt-\S+)', line)
                    if m:
                        model = m.group(1)
                lm = re.search(r'loop (\d+)/\d+', line)
                if lm:
                    max_loop = max(max_loop, int(lm.group(1)))
    except Exception:
        pass
    return model, max_loop


def model_from_log(log_path: str) -> str:
    return parse_log(log_path)[0]


def main():
    args = parse_args()
    cutoff = datetime.now() - timedelta(days=args.days) if args.days else None

    history_file = DATA_DIR / "agent-history.jsonl"
    if not history_file.exists():
        print("No agent-history.jsonl found")
        sys.exit(1)

    # Build log path index for fast lookup
    log_index = {p.stem.replace("agent_", ""): p for p in DATA_DIR.glob("agent_*.log")}

    stats = defaultdict(lambda: defaultdict(lambda: {"completed": 0, "failed": 0, "total": 0, "loops": 0, "loop_agents": 0, "loops_completed": 0, "loop_agents_completed": 0}))

    def _normalize_model(model):
        if "M3" in model:
            return "MiniMax-M3"
        elif "M2" in model or "minimax" in model.lower():
            return "MiniMax-M2.7"
        return model

    def _record(model, key, status, log_path):
        _, loops = parse_log(log_path) if log_path else ("", 0)
        stats[model][key]["total"] += 1
        if status == "completed":
            stats[model][key]["completed"] += 1
        elif status == "failed":
            stats[model][key]["failed"] += 1
        if loops > 0:
            stats[model][key]["loops"] += loops
            stats[model][key]["loop_agents"] += 1
            if status == "completed":
                stats[model][key]["loops_completed"] += loops
                stats[model][key]["loop_agents_completed"] += 1

    with open(history_file) as f:
        for line in f:
            try:
                a = json.loads(line)
            except Exception:
                continue

            spawned = a.get("spawned_at", "")
            if cutoff and spawned:
                try:
                    dt = datetime.fromisoformat(spawned)
                    if dt < cutoff:
                        continue
                except Exception:
                    pass

            agent_id = a.get("id", "")
            log_path = a.get("log_path") or str(log_index.get(agent_id, ""))
            model = _normalize_model(model_from_log(log_path) if log_path else "unknown")
            task_type = a.get("task_type", "unknown")
            key = task_type if args.by_type else "all"
            _record(model, key, a.get("status", "unknown"), log_path)

    # Also check live DB agents
    db_file = DATA_DIR / "swarm.db"
    if db_file.exists():
        import sqlite3
        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, status, task_type, log_path, spawned_at FROM agents WHERE status IN ('completed','failed')"
        ).fetchall()
        for row in rows:
            spawned = row["spawned_at"] or ""
            if cutoff and spawned:
                try:
                    dt = datetime.fromisoformat(spawned)
                    if dt < cutoff:
                        continue
                except Exception:
                    pass
            log_path = row["log_path"] or ""
            model = _normalize_model(model_from_log(log_path) if log_path else "unknown")
            task_type = row["task_type"] or "unknown"
            key = task_type if args.by_type else "all"
            _record(model, key, row["status"], log_path)
        conn.close()

    period = f"last {args.days} days" if args.days else "all time"
    print(f"\n=== Model completion stats ({period}) ===\n")

    for model in sorted(stats.keys()):
        model_stats = stats[model]
        if args.by_type:
            print(f"{model}:")
            for task_type in sorted(model_stats.keys()):
                s = model_stats[task_type]
                t = s["total"]
                c = s["completed"]
                rate = f"{c/t*100:.1f}%" if t else "n/a"
                avg_all = f"{s['loops']/s['loop_agents']:.1f}" if s["loop_agents"] else "n/a"
                avg_ok = f"{s['loops_completed']/s['loop_agents_completed']:.1f}" if s["loop_agents_completed"] else "n/a"
                print(f"  {task_type:18} {c:4}/{t:4} = {rate:6}  loops(all)={avg_all:6}  loops(done)={avg_ok}")
        else:
            s = model_stats["all"]
            t = s["total"]
            c = s["completed"]
            f_ = s["failed"]
            rate = f"{c/t*100:.1f}%" if t else "n/a"
            avg_loops_all = f"{s['loops']/s['loop_agents']:.1f}" if s["loop_agents"] else "n/a"
            avg_loops_ok = f"{s['loops_completed']/s['loop_agents_completed']:.1f}" if s["loop_agents_completed"] else "n/a"
            print(f"{model:20} completed={c:5}  failed={f_:5}  total={t:5}  rate={rate}  avg_loops(all)={avg_loops_all}  avg_loops(completed)={avg_loops_ok}")
    print()


if __name__ == "__main__":
    main()
