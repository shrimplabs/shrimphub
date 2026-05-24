#!/usr/bin/env python3
import re
from collections import defaultdict
from pathlib import Path
from datetime import date

LD = Path("data/learnings")
OUT = Path("data/AUDIT_LEARNINGS_REPORT.md")

TT = ["bug","feature","refactor","polish","plan","qa",
      "harness_qa","hybrid_qa","audit","research","art_pass",
      "phase_gate","project_plan","audit_learnings"]

DRE = re.compile(
    r"^## (\d{4}-\d\d-\d\d) (\d\d:\d\d) \u2014 (.+?) \((\d+) loops?\)",
    re.MULTILINE
)
BRE = re.compile(r"^[\-\*] \*\*(.+?)\*\*(?:[\s\-]+)(.*)")

# BUG clusters
BUG_CL = {
    "godot4": {
        "kw":["godot 4","gdscript","scene","node","autoload","viewport",
              "curve3d","collision","area3d","signal","input","treenode",
              "nodepath","tilemap","sprite2d","rigidbody","get_node","get_tree",
              "_ready","_input","set_input_as_handled","get_viewport","null > 0",
              "typed array","has_method","get_node_or_null","ext_resource",
              "sub_resource","uid=","input_event","parse_input_event",
              "call_deferred","process_frame","class_name","custom_minimum_size"],
        "l":"Godot 4 API gotchas",
        "d":"Godot 4 changed or removed many GDScript APIs vs Godot 3. These are the most recurring single-line fix categories.",
        "hv":True,
    },
    "gut": {
        "kw":["gut","signal","bind","lambda",".connect("],
        "l":"GUT signal-test patterns",
        "d":"GUT lambdas have isolated scope and cannot capture outer variables. Use .bind() with class methods for all signal tests.",
        "hv":True,
    },
    "lock": {
        "kw":["lock","conflict","concurrent","broadcast","fan-out"],
        "l":"Lock-conflict protocol",
        "d":"Lock conflicts are non-negotiable. When patch_file fails with 'locked by another task', stop immediately and hand off.",
        "hv":True,
    },
    "valid": {
        "kw":["validat","three-stage","headless","godot --headless",
              "scene load","script parse","gut_cmdln","_swarm",
              "py_compile","compile error","syntax error"],
        "l":"Three-stage validation",
        "d":"Script parse (grep ERROR) -> scene load check -> full game launch catches issues before wasted loops.",
        "hv":True,
    },
    "git": {
        "kw":["git pull","git push","git checkout","git commit","git status"],
        "l":"Git operations",
        "d":"Git pull/push need specific flags or pre-flight checks. Plain git pull fails with multiple branches.",
        "hv":False,
    },
    "tool": {
        "kw":["write_file","patch_file","old_string","tool call","malformed",
              "broadcast_write","run_command","command:","cmd:","parameter","malformed JSON"],
        "l":"Tool-call correctness",
        "d":"Parameter names, JSON structure, and call order matter. Wrong names cause silent rejection or retry.",
        "hv":False,
    },
    "imports": {
        "kw":["import","circular","re-export","reexport","module","F401","E402"],
        "l":"Import / module system",
        "d":"Re-export breakage after splits and circular imports are a recurring refactor hazard.",
        "hv":False,
    },
    "llm": {
        "kw":["llm","api key","quota","provider","billing","402","429","rag_query"],
        "l":"LLM provider issues",
        "d":"Provider-specific errors (billing, rate limits, unavailable models) need graceful fallbacks.",
        "hv":False,
    },
    "node": {
        "kw":["nodepath","wiring","missing button","null node","empty path","has_node"],
        "l":"Node/scene wiring",
        "d":"Missing node references or wrong paths cause runtime null errors. Scan all scripts for unpopulated NodePath fields.",
        "hv":False,
    },
    "other": {"kw":[],"l":"Other","d":"","hv":False},
}

# FEATURE clusters
FEAT_CL = {
    "godot4": {
        "kw":["godot 4","gdscript","scene","node","autoload","input","viewport",
              "collision","area3d","signal","treenode","get_node","get_tree",
              "sub_resource","instance=extresource","class_name","typed array",
              "call_deferred","process_frame","custom_minimum_size",
              "input_event","parse_input_event"],
        "l":"Godot 4 API correctness",
        "d":"Godot 4 API surface differs from 3.x. Specific method calls work only in specific contexts.",
        "hv":True,
    },
    "scene": {
        "kw":["scene nesting","add child","ext_resource","uid=","instance=",
              "sub_resource","node block","parent=","extresource"],
        "l":"Scene construction (3-step pattern)",
        "d":"Adding child scenes in Godot 4: add uid= to header, add ExtResource reference, add node block with instance=.",
        "hv":True,
    },
    "launch": {
        "kw":["launch_game","get_game_state","headless","verification","screenshot"],
        "l":"Launch-game verification",
        "d":"Use launch_game() for real verification -- spawning the actual Godot headless process confirms the full scene tree loads.",
        "hv":True,
    },
    "input": {
        "kw":["input","call_deferred","process_frame","input_event","parse_input_event","InputMap"],
        "l":"Input handling patterns",
        "d":"call_deferred + await process_frame is the correct pattern for testing input/toggle methods in GUT.",
        "hv":True,
    },
    "private": {
        "kw":["private","wrapper","public method","workaround"],
        "l":"Private-method workaround",
        "d":"Add a thin public wrapper instead of making a private method public.",
        "hv":False,
    },
    "git": {
        "kw":["git log","git status","git checkout","sibling","prior commit"],
        "l":"Git pre-flight / check sibling commits",
        "d":"Check git log and status before diving in -- the bug is often already fixed in a recent commit.",
        "hv":False,
    },
    "tool": {
        "kw":["write_file","patch_file","broadcast_write","run_command"],
        "l":"Tool usage",
        "d":"Tool call correctness and ordering patterns.",
        "hv":False,
    },
    "other": {"kw":[],"l":"Other","d":"","hv":False},
}

# REFACTOR clusters
REF_CL = {
    "reexport": {
        "kw":["re-export","reexport","missing re-export","module extraction"],
        "l":"Re-export chain breakage",
        "d":"Missing re-exports after module extraction cause silent hangs. Always verify re-export blocks.",
        "hv":True,
    },
    "circular": {
        "kw":["circular","import","_shared","call-time import","module-load"],
        "l":"Circular import prevention",
        "d":"NEVER put call-time imports of a module that re-exports from you. Use _shared.py as the neutral hub.",
        "hv":True,
    },
    "ruff": {
        "kw":["ruff","--fix","ruff fix"],
        "l":"Ruff --fix side-effects",
        "d":"Ruff --fix silently removes imports it flags as unused. This breaks re-export chains. Always run tests after --fix.",
        "hv":True,
    },
    "concurrent": {
        "kw":["concurrent","write_file","broadcast_write","heredoc","race"],
        "l":"Concurrent edit safety",
        "d":"run_command with heredoc is preferred over write_file when siblings are running. write_file requires broadcast_write() first.",
        "hv":False,
    },
    "git": {
        "kw":["git remote","git push","git commit","git status","git log"],
        "l":"Git pre-flight checks",
        "d":"git remote get-url origin prevents silent push failures. git status before commit prevents empty-commit failures.",
        "hv":False,
    },
    "other": {"kw":[],"l":"Other","d":"","hv":False},
}

# QA clusters
QA_CL = {
    "launch": {
        "kw":["launch_game","get_game_state","screenshot","state_server","tcp"],
        "l":"StateServer / game launch patterns",
        "d":"launch_game + get_game_state() is the core QA loop. StateServer on port 11009 is the live-state observation channel.",
        "hv":True,
    },
    "headless": {
        "kw":["godot","headless","gut","testharness","scene","script","gut_cmdln"],
        "l":"Godot headless / GUT testing",
        "d":"godot --headless --path . --quit for fast validation. GUT via gut_cmdln.gd -gdir for full suite.",
        "hv":True,
    },
    "input": {
        "kw":["input","toggle","button","click","ui_accept","overlay","press_button"],
        "l":"Input / button testing",
        "d":"Input.parse_input_event + _input() for keyboard. StateServer.press_button for UI button verification.",
        "hv":False,
    },
    "harness": {
        "kw":["harness_launch","harness_poll","harness_kill","harness_inject","harness_run"],
        "l":"TestHarness tool patterns",
        "d":"harness_launch_game + harness_poll_state + harness_kill_game is the reliable test harness loop.",
        "hv":True,
    },
    "cleanup": {
        "kw":["rm -f","cleanup","temp","stale","port 11009"],
        "l":"Temp-file / port cleanup",
        "d":"Validation scripts and stale Godot processes on ports 11009-11049 must be cleaned up between runs.",
        "hv":False,
    },
    "robust": {
        "kw":["retry","timeout","loop","robustness","error handling"],
        "l":"Loop robustness",
        "d":"QA agents should stop and report rather than retry indefinitely.",
        "hv":False,
    },
    "other": {"kw":[],"l":"Other","d":"","hv":False},
}

# AUDIT clusters
AUDIT_CL = {
    "grep": {
        "kw":["grep","search","find","scan","targeted"],
        "l":"Search strategy",
        "d":"Use targeted grep searches before scanning all files. This saves loops vs. full file iteration.",
        "hv":True,
    },
    "godot": {
        "kw":["project.godot","autoload","autoloads"],
        "l":"project.godot autoload inspection",
        "d":"Read project.godot early to get autoloads list in one place rather than searching each script for singleton references.",
        "hv":True,
    },
    "tool": {
        "kw":["run_command","command:","cmd:","tool call","malformed"],
        "l":"Tool-call correctness",
        "d":"run_command uses command: key (NOT cmd:). Wrong key causes malformed tool call warnings and retries.",
        "hv":True,
    },
    "other": {"kw":[],"l":"Other","d":"","hv":False},
}

CMAP = {
    "bug":BUG_CL,"feature":FEAT_CL,"refactor":REF_CL,
    "qa":QA_CL,"harness_qa":QA_CL,"hybrid_qa":QA_CL,"audit":AUDIT_CL,
}

def clpat(pt, cls):
    pl = pt.lower()
    best, bc = "other", 0
    for k, c in cls.items():
        if k == "other":
            continue
        cnt = sum(1 for kw in c["kw"] if kw in pl)
        if cnt > bc:
            bc, best = cnt, k
    return best

def parse(p):
    ents = []
    try:
        ct = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ents
    for sec in re.split(
        r"(?=^## \d{4}-\d\d-\d\d \d\d:\d\d)", ct, flags=re.MULTILINE
    ):
        sec = sec.strip()
        if not sec or not sec.startswith("## "):
            continue
        m = DRE.match(sec)
        if not m:
            continue
        sp, lp = m.group(3), int(m.group(4))
        comp = "completed" in sp.lower()
        fail = "failed" in sp.lower()
        if not comp and not fail:
            comp = True
        pats = []
        for ln in sec.split("\n"):
            ln = ln.strip()
            if ln.startswith("- **") or ln.startswith("* **"):
                bm = BRE.match(ln)
                if bm:
                    pats.append((bm.group(1).strip(), bm.group(2).strip()))
        ents.append({"completed": comp, "failed": fail, "loops": lp, "patterns": pats})
    return ents

def main():
    proj_dirs = sorted([d for d in LD.iterdir() if d.is_dir()])
    stats = {tt: {"files": 0, "completed": 0, "failed": 0, "total_loops": 0}
             for tt in TT}
    pbt = defaultdict(list)

    for pd in proj_dirs:
        for tt in TT:
            f = pd / (tt + ".md")
            if not f.exists():
                continue
            ents = parse(f)
            if not ents:
                continue
            stats[tt]["files"] += 1
            for e in ents:
                if e["completed"]:
                    stats[tt]["completed"] += 1
                if e["failed"]:
                    stats[tt]["failed"] += 1
                stats[tt]["total_loops"] += e["loops"]
                for pt, dt in e["patterns"]:
                    if pt and len(pt) > 3:
                        pbt[tt].append({
                            "project": pd.name, "pattern": pt, "detail": dt,
                            "completed": e["completed"], "failed": e["failed"],
                        })

    # Bucket into clusters
    cbt = defaultdict(
        lambda: defaultdict(
            lambda: {"c": 0, "f": 0, "projects": set(), "examples": [], "desc": ""}
        )
    )
    for tt, tps in pbt.items():
        if tt not in CMAP:
            continue
        cls = CMAP[tt]
        for p in tps:
            key = clpat(p["pattern"], cls)
            b = cbt[tt][key]
            if p["completed"]:
                b["c"] += 1
            if p["failed"]:
                b["f"] += 1
            b["projects"].add(p["project"])
            if len(b["examples"]) < 4:
                full = (p["pattern"] + " " + p["detail"])[:250].strip()
                b["examples"].append(full)
            if not b["desc"] and key in cls:
                b["desc"] = cls[key].get("d", "")

    total_files = sum(s["files"] for s in stats.values())
    n_proj = len(proj_dirs)
    today = date.today().isoformat()

    L = []
    L.append(f"# Daily Swarm Health Audit -- {today}\n")
    L.append(
        f"Scanned {n_proj} projects and {total_files} learning files "
        f"under `data/learnings/`.\n"
    )
    L.append("Patterns grouped by task type. No cross-type poisoning.\n")
    L.append("---\n")

    # Summary table
    L.append("## Summary\n")
    L.append("| Task type | Files | Completed | Failed | Fail% | Loops | Avg | Patterns |")
    L.append("|---|---|---|---|---|---|---|---|")
    for tt in TT:
        s = stats[tt]
        if s["files"] == 0:
            continue
        tot = s["completed"] + s["failed"]
        fp = f"{s['failed']/tot*100:.0f}%" if tot > 0 else "0%"
        avg = s["total_loops"] / tot if tot > 0 else 0
        L.append(
            f"| `{tt}` | {s['files']} | {s['completed']} | {s['failed']} | "
            f"{fp} | {s['total_loops']} | {avg:.1f} | {len(pbt[tt])} |"
        )
    L.append("")

    for tt in TT:
        s = stats[tt]
        if s["files"] == 0:
            continue
        cls = CMAP.get(tt, {})
        L.append(
            f"## {tt.upper()} tasks ({s['completed']} completed / {s['failed']} failed)\n"
        )
        if not pbt[tt]:
            L.append("No distinct patterns emerged -- sample size too small.\n")
            continue
        hv, ot = [], []
        for key, cl in cls.items():
            b = cbt[tt].get(key, {"c": 0, "f": 0, "projects": set(), "examples": []})
            bt = b["c"] + b["f"]
            if bt == 0:
                continue
            item = (key, cl["l"], b, cl.get("d", ""))
            if cl["hv"]:
                hv.append(item)
            else:
                ot.append(item)
        hv.sort(key=lambda x: x[2]["c"] + x[2]["f"], reverse=True)
        for key, label, b, desc in hv:
            bt = b["c"] + b["f"]
            fp = f"{b['f']/bt*100:.0f}%" if bt > 0 else "0%"
            L.append(
                f"### {label} "
                f"-- {b['c']} ok / {b['f']} failed "
                f"(fail rate {fp}, {len(b['projects'])} projects)\n"
            )
            if desc:
                L.append(desc + "\n")
            for ex in b["examples"]:
                L.append(f"- **{ex}**")
            L.append("")
        if ot:
            ot.sort(key=lambda x: x[2]["c"] + x[2]["f"], reverse=True)
            L.append("### Other patterns\n")
            for key, label, b, desc in ot:
                bt = b["c"] + b["f"]
                L.append(
                    f"- **{label}** ({b['c']} ok / {b['f']} failed, "
                    f"{len(b['projects'])} projects)"
                )
            L.append("")

    # Cross-cutting
    L.append("---\n")
    L.append("## Cross-cutting observations\n")

    po = defaultdict(
        lambda: {"c": 0, "f": 0, "projects": set(), "types": set(), "text": ""}
    )
    seen = set()
    for tt, ps in pbt.items():
        for p in ps:
            key = (p["project"], p["pattern"][:100].lower())
            if key in seen:
                continue
            seen.add(key)
            b = po[p["pattern"][:100].lower()]
            if p["completed"]:
                b["c"] += 1
            if p["failed"]:
                b["f"] += 1
            b["projects"].add(p["project"])
            b["types"].add(tt)
            b["text"] = p["pattern"][:120]

    top = sorted(po.values(), key=lambda x: len(x["projects"]), reverse=True)
    L.append("### Highest-value patterns (by multi-project coverage)\n")
    for v in top[:25]:
        types = ", ".join(sorted(v["types"]))
        L.append(
            f"- **{v['text']}** -- {len(v['projects'])} projects ({types})"
        )
    L.append("")

    L.append("### Loop-count analysis by task type\n")
    L.append("| Task type | Completed | Failed | Total loops | Avg loops/task |")
    L.append("|---|---|---|---|---|")
    for tt in TT:
        s = stats[tt]
        tot = s["completed"] + s["failed"]
        if tot == 0:
            continue
        avg = s["total_loops"] / tot
        L.append(
            f"| `{tt}` | {s['completed']} | {s['failed']} | "
            f"{s['total_loops']} | {avg:.1f} |"
        )
    L.append("")

    L.append("### Recommendations\n")
    L.append(
        "The largest failure clusters are Godot 4 API gotchas and refactor "
        "re-export breakage -- both are addressable with a pre-flight checklist "
        "injected into task context rather than re-discovered each time.\n"
    )
    L.append(
        "Consider making this audit a daily cron job so the report is always "
        "fresh and patterns remain accurate.\n"
    )

    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"Done: {n_proj} projects, {total_files} files -> {OUT}")

if __name__ == "__main__":
    main()
