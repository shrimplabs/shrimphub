#!/usr/bin/env python3
"""Scan all learning files, cluster patterns, and generate the audit report."""
import re
from collections import defaultdict
from pathlib import Path
from datetime import date

LEARNINGS_DIR = Path("data/learnings")
OUTPUT_PATH = Path("data/AUDIT_LEARNINGS_REPORT.md")

TASK_TYPES = [
    "bug", "feature", "refactor", "polish", "plan", "qa",
    "harness_qa", "hybrid_qa", "audit", "research", "art_pass",
    "phase_gate", "project_plan", "audit_learnings"
]

DATE_RE = re.compile(
    r"^## (\d{4}-\d\d-\d\d) (\d\d:\d\d) \u2014 (.+?) \((\d+) loops?\)",
    re.MULTILINE
)
BULLET_RE = re.compile(r"^[\-\*] \*\*(.+?)\*\*(?:[\s\-]+)(.*)")

# Keyword clusters per task type.
BUG_CLUSTERS = {
    "godot4_api": {
        "keywords": ["godot 4", "gdscript", "godot4", "scene", "node", "autoload",
                     "viewport", "curve3d", "collision", "area3d", "signal", "input",
                     "treenode", "nodepath", "tilemap", "sprite2d", "rigidbody",
                     "get_node", "get_tree", "_ready", "_input", "set_input_as_handled",
                     "get_viewport", "null > 0"],
        "label": "Godot 4 API gotchas",
        "high_value": True,
    },
    "gut_signals": {
        "keywords": ["gut", "signal", "bind", "lambda", ".connect("],
        "label": "GUT signal-test patterns",
        "high_value": True,
    },
    "lock_conflict": {
        "keywords": ["lock", "conflict", "concurrent", "race", "broadcast", "fan-out"],
        "label": "Lock-conflict protocol",
        "high_value": True,
    },
    "validation": {
        "keywords": ["validat", "test", "compile", "syntax", "scene load",
                     "headless", "godot --headless", "py_compile", "pytest", "script parse",
                     "three-stage"],
        "label": "Three-stage validation",
        "high_value": True,
    },
    "git_operations": {
        "keywords": ["git pull", "git push", "remote", "origin", "branch", "commit"],
        "label": "Git operations",
        "high_value": False,
    },
    "tool_calls": {
        "keywords": ["write_file", "patch_file", "read_file", "run_command",
                     "broadcast_write", "tool call", "malformed"],
        "label": "Tool-call correctness",
        "high_value": False,
    },
    "imports": {
        "keywords": ["import", "circular", "re-export", "reexport", "module", "F401", "E402"],
        "label": "Import / module system",
        "high_value": False,
    },
    "llm_provider": {
        "keywords": ["llm", "api key", "quota", "provider", "billing", "402", "429"],
        "label": "LLM provider issues",
        "high_value": False,
    },
    "node_wiring": {
        "keywords": ["nodepath", "wiring", "missing button", "null", "empty path"],
        "label": "Node/scene wiring",
        "high_value": False,
    },
    "other": {
        "keywords": [],
        "label": "Other",
        "high_value": False,
    },
}

FEATURE_CLUSTERS = {
    "godot4_api": {
        "keywords": ["godot 4", "gdscript", "scene", "node", "autoload", "input",
                     "viewport", "collision", "area3d", "signal", "treenode",
                     "get_node", "get_tree", "sub_resource", "instance=extresource",
                     "class_name"],
        "label": "Godot 4 API correctness",
        "high_value": True,
    },
    "scene_construction": {
        "keywords": ["scene nesting", "add child", "ext_resource", "uid=", "instance=",
                     "sub_resource", "node block", "parent="],
        "label": "Scene construction (3-step pattern)",
        "high_value": True,
    },
    "launch_verification": {
        "keywords": ["launch_game", "get_game_state", "headless", "verification",
                     "screenshot", "state_server"],
        "label": "Launch-game verification",
        "high_value": True,
    },
    "input_handling": {
        "keywords": ["input", "call_deferred", "process_frame", "input_event",
                     "parse_input_event", "InputMap"],
        "label": "Input handling patterns",
        "high_value": True,
    },
    "private_methods": {
        "keywords": ["private", "wrapper", "public method", "workaround"],
        "label": "Private-method workaround",
        "high_value": False,
    },
    "tool_usage": {
        "keywords": ["write_file", "patch_file", "broadcast_write", "run_command"],
        "label": "Tool usage",
        "high_value": False,
    },
    "other": {
        "keywords": [],
        "label": "Other",
        "high_value": False,
    },
}

REFACTOR_CLUSTERS = {
    "reexport_breakage": {
        "keywords": ["re-export", "reexport", "missing re-export", "split", "module extraction"],
        "label": "Re-export chain breakage",
        "high_value": True,
    },
    "circular_imports": {
        "keywords": ["circular", "import", "_shared", "call-time import", "module-load"],
        "label": "Circular import prevention",
        "high_value": True,
    },
    "ruff_fix": {
        "keywords": ["ruff", "--fix", "ruff fix"],
        "label": "Ruff --fix side-effects",
        "high_value": True,
    },
    "concurrent_edits": {
        "keywords": ["concurrent", "write_file", "broadcast_write", "heredoc", "race"],
        "label": "Concurrent edit safety",
        "high_value": False,
    },
    "git_preflight": {
        "keywords": ["git remote", "git push", "git commit", "git status", "git log"],
        "label": "Git pre-flight checks",
        "high_value": False,
    },
    "other": {
        "keywords": [],
        "label": "Other",
        "high_value": False,
    },
}

QA_CLUSTERS = {
    "launch_game": {
        "keywords": ["launch_game", "get_game_state", "screenshot", "state_server", "tcp"],
        "label": "StateServer / game launch patterns",
        "high_value": True,
    },
    "godot_api": {
        "keywords": ["godot", "headless", "gut", "testharness", "scene", "script"],
        "label": "Godot headless / GUT testing",
        "high_value": True,
    },
    "input_toggle": {
        "keywords": ["input", "toggle", "button", "click", "ui_accept", "overlay"],
        "label": "Input / button testing",
        "high_value": False,
    },
    "loop_robustness": {
        "keywords": ["retry", "timeout", "loop", "robustness", "error handling"],
        "label": "Loop robustness",
        "high_value": False,
    },
    "other": {
        "keywords": [],
        "label": "Other",
        "high_value": False,
    },
}

AUDIT_CLUSTERS = {
    "grep_strategy": {
        "keywords": ["grep", "search", "find", "scan", "targeted"],
        "label": "Search strategy",
        "high_value": True,
    },
    "tool_call_correctness": {
        "keywords": ["run_command", "command:", "cmd:", "tool call", "malformed"],
        "label": "Tool-call correctness",
        "high_value": True,
    },
    "project_godot": {
        "keywords": ["project.godot", "autoload", "autoloads", "godot"],
        "label": "project.godot autoload inspection",
        "high_value": True,
    },
    "other": {
        "keywords": [],
        "label": "Other",
        "high_value": False,
    },
}

CLUSTER_MAP = {
    "bug": BUG_CLUSTERS,
    "feature": FEATURE_CLUSTERS,
    "refactor": REFACTOR_CLUSTERS,
    "qa": QA_CLUSTERS,
    "harness_qa": QA_CLUSTERS,
    "hybrid_qa": QA_CLUSTERS,
    "audit": AUDIT_CLUSTERS,
}


def cluster_pattern(pattern_text, clusters):
    pt = pattern_text.lower()
    best = "other"
    best_count = 0
    for key, cl in clusters.items():
        if key == "other":
            continue
        count = sum(1 for kw in cl["keywords"] if kw in pt)
        if count > best_count:
            best_count = count
            best = key
    return best


def parse(path):
    entries = []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return entries
    sections = re.split(
        r"(?=^## \d{4}-\d\d-\d\d \d\d:\d\d)", content, flags=re.MULTILINE
    )
    for section in sections:
        section = section.strip()
        if not section or not section.startswith("## "):
            continue
        m = DATE_RE.match(section)
        if not m:
            continue
        status_part, loops = m.group(3), int(m.group(4))
        completed = "completed" in status_part.lower()
        failed = "failed" in status_part.lower()
        if not completed and not failed:
            completed = True
        patterns = []
        for line in section.split("\n"):
            line = line.strip()
            if line.startswith("- **") or line.startswith("* **"):
                bm = BULLET_RE.match(line)
                if bm:
                    patterns.append((bm.group(1).strip(), bm.group(2).strip()))
        entries.append({
            "completed": completed,
            "failed": failed,
            "loops": loops,
            "patterns": patterns,
        })
    return entries


def main():
    project_dirs = sorted([d for d in LEARNINGS_DIR.iterdir() if d.is_dir()])
    stats = {tt: {"files": 0, "completed": 0, "failed": 0, "total_loops": 0}
             for tt in TASK_TYPES}
    patterns_by_type = defaultdict(list)

    for project_dir in project_dirs:
        for tt in TASK_TYPES:
            f = project_dir / (tt + ".md")
            if not f.exists():
                continue
            entries = parse(f)
            if not entries:
                continue
            stats[tt]["files"] += 1
            for entry in entries:
                if entry["completed"]:
                    stats[tt]["completed"] += 1
                if entry["failed"]:
                    stats[tt]["failed"] += 1
                stats[tt]["total_loops"] += entry["loops"]
                for pat_text, detail in entry["patterns"]:
                    if pat_text and len(pat_text) > 3:
                        patterns_by_type[tt].append({
                            "project": project_dir.name,
                            "pattern": pat_text,
                            "detail": detail,
                            "completed": entry["completed"],
                            "failed": entry["failed"],
                        })

    # Aggregate into cluster buckets
    clusters_by_type = defaultdict(
        lambda: defaultdict(lambda: {"c": 0, "f": 0, "projects": set(), "examples": []})
    )
    for tt, type_patterns in patterns_by_type.items():
        if tt not in CLUSTER_MAP:
            continue
        clusters = CLUSTER_MAP[tt]
        for p in type_patterns:
            key = cluster_pattern(p["pattern"], clusters)
            b = clusters_by_type[tt][key]
            if p["completed"]:
                b["c"] += 1
            if p["failed"]:
                b["f"] += 1
            b["projects"].add(p["project"])
            if len(b["examples"]) < 3:
                b["examples"].append(p["pattern"][:120])

    total_files = sum(s["files"] for s in stats.values())
    print(f"Scanned {len(project_dirs)} projects, {total_files} learning files")
    for tt in TASK_TYPES:
        s = stats[tt]
        if s["files"] > 0:
            print(f"  {tt}: {s['files']} files | {s['completed']} done / {s['failed']} failed | "
                  f"{s['total_loops']} loops | {len(patterns_by_type[tt])} patterns")

    # Build report
    lines = []
    today = date.today().isoformat()

    lines.append(f"# Daily Swarm Health Audit -- {today}\n")
    lines.append(f"Scanned {len(project_dirs)} projects and {total_files} learning files under `data/learnings/`.\n")
    lines.append("Patterns grouped by task type. No cross-type poisoning.\n")
    lines.append("---\n")

    # Summary table
    lines.append("## Summary\n")
    lines.append("| Task type | Files | Completed | Failed | Loops | Patterns |")
    lines.append("|---|---|---|---|---|---")
    for tt in TASK_TYPES:
        s = stats[tt]
        if s["files"] > 0:
            total = s["completed"] + s["failed"]
            pct = f"{s['failed']/total*100:.0f}%" if total > 0 else "0%"
            lines.append(
                f"| `{tt}` | {s['files']} | {s['completed']} | "
                f"{s['failed']} ({pct}) | {s['total_loops']} | {len(patterns_by_type[tt])} |"
            )
    lines.append("")

    for tt in TASK_TYPES:
        s = stats[tt]
        if s["files"] == 0:
            continue
        type_patterns = patterns_by_type[tt]
        clusters = CLUSTER_MAP.get(tt, {})

        total = s["completed"] + s["failed"]
        lines.append(f"## {tt.upper()} tasks ({s['completed']} completed / {s['failed']} failed)\n")

        if not type_patterns:
            lines.append("No distinct patterns emerged -- sample size too small.\n")
            continue

        high_val = []
        other_buckets = []
        for key, cl in clusters.items():
            if key == "other":
                continue
            bucket = clusters_by_type[tt].get(
                key, {"c": 0, "f": 0, "projects": set(), "examples": []}
            )
            bucket_total = bucket["c"] + bucket["f"]
            if bucket_total == 0:
                continue
            if cl["high_value"]:
                high_val.append((key, cl["label"], bucket, cl))
            else:
                other_buckets.append((key, cl["label"], bucket, cl))

        high_val.sort(key=lambda x: x[2]["c"] + x[2]["f"], reverse=True)

        for key, label, bucket, cl in high_val:
            total_b = bucket["c"] + bucket["f"]
            fail_rate = f"{bucket['f']/total_b*100:.0f}%" if total_b > 0 else "0%"
            lines.append(f"### {label} -- {bucket['c']} ok / {bucket['f']} failed (fail rate {fail_rate})\n")
            for ex in bucket["examples"]:
                lines.append(f"- **{ex}**")
            lines.append("")

        if other_buckets:
            lines.append("### Other patterns\n")
            other_buckets.sort(key=lambda x: x[2]["c"] + x[2]["f"], reverse=True)
            for key, label, bucket, cl in other_buckets:
                total_b = bucket["c"] + bucket["f"]
                lines.append(
                    f"- **{label}** ({bucket['c']} ok / {bucket['f']} failed, "
                    f"{len(bucket['projects'])} projects)"
                )
            lines.append("")

    # Cross-cutting section
    lines.append("---\n")
    lines.append("## Cross-cutting observations\n")

    # Patterns by project coverage across types
    pat_occurrences = defaultdict(
        lambda: {"c": 0, "f": 0, "projects": set(), "types": set(), "text": ""}
    )
    seen = set()
    for tt, pats in patterns_by_type.items():
        for p in pats:
            key = (p["project"], p["pattern"][:80].lower())
            if key in seen:
                continue
            seen.add(key)
            bucket = pat_occurrences[p["pattern"][:80].lower()]
            if p["completed"]:
                bucket["c"] += 1
            if p["failed"]:
                bucket["f"] += 1
            bucket["projects"].add(p["project"])
            bucket["types"].add(tt)
            bucket["text"] = p["pattern"][:120]

    top = sorted(pat_occurrences.values(), key=lambda x: len(x["projects"]), reverse=True)
    lines.append("### Highest-value patterns (by project coverage)\n")
    for v in top[:20]:
        types = ", ".join(sorted(v["types"]))
        lines.append(f"- **{v['text']}** -- {len(v['projects'])} projects ({types})")
    lines.append("")

    lines.append("### Loop-count analysis by task type\n")
    lines.append("| Task type | Avg loops/task | Total loops |")
    lines.append("|---|---|---|")
    for tt in TASK_TYPES:
        s = stats[tt]
        total = s["completed"] + s["failed"]
        if total == 0:
            continue
        avg = s["total_loops"] / total
        lines.append(f"| `{tt}` | {avg:.1f} | {s['total_loops']} |")
    lines.append("")

    OUTPUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
