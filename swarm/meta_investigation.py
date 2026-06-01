"""
swarm.meta_investigation -- out-of-band LLM investigator for repeated errors.

Extracted from agent_runtime.py. Reads config from agent_runtime at call-time
via lazy import to avoid capturing stale values.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from swarm.llm_utils import call_llm


def _run_meta_investigation(repeated_error: str, loop_history: list[str], task_desc: str) -> str:
    """Out-of-band LLM call that investigates a repeatedly-seen error.

    Gets a compact view of what the agent has tried, can read files and run
    commands itself, then returns a short hint to inject into the main loop.

    loop_history: list of (tool, args_summary, error_snippet) strings from
    recent loops where the error appeared.
    """
    import swarm.agent_runtime as _rt

    # Prefer the real project path; fall back gracefully if override doesn't exist
    _override = Path(_rt.PROJECT_PATH_OVERRIDE) if _rt.PROJECT_PATH_OVERRIDE else None
    project_root = (_override if _override and _override.exists() else None) or (_rt.WORKSPACE / _rt.PROJECT)
    _rt.log(f"[Meta] project_root={project_root} (exists={project_root.exists()})")

    # Give the investigator a read_file and run_command it can use
    def _mini_read(path: str) -> str:
        try:
            p = Path(path) if Path(path).is_absolute() else project_root / path
            if not p.exists():
                return f"[not found: {p}]"
            return p.read_text(encoding="utf-8", errors="replace")[:3000]
        except Exception as e:
            return f"[read error: {e}]"

    def _mini_run(cmd: str) -> str:
        try:
            if sys.platform == "win32":
                args = ["cmd", "/c", cmd]
                r = subprocess.run(args, capture_output=True, timeout=20,
                                   encoding=sys.stdout.encoding or "utf-8", errors="replace")
            else:
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20)
            out = (r.stdout or "") + (r.stderr or "")
            return out[:2000] if out.strip() else "[no output]"
        except Exception as e:
            return f"[run error: {e}]"

    def _mini_list_dir(path: str, pattern: str = "**/*") -> str:
        """Python-native directory listing — no shell, works on all platforms."""
        try:
            p = Path(path) if Path(path).is_absolute() else project_root / path
            if not p.exists():
                return f"[not found: {p}]"
            entries = sorted(p.rglob(pattern) if "**" in pattern else p.glob(pattern))
            lines = [str(e.relative_to(p)) for e in entries[:200]]
            suffix = f"\n... ({len(entries) - 200} more)" if len(entries) > 200 else ""
            return "\n".join(lines) + suffix if lines else "[empty directory]"
        except Exception as e:
            return f"[list error: {e}]"

    # --- Seed context: last 30 lines of this agent's log ---
    _log_tail = ""
    try:
        _log_path = Path(_rt.DATA_DIR) / f"agent_{_rt.TASK_ID}.log"
        if _log_path.exists():
            _lines = _log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            _log_tail = "\n".join(_lines[-30:])
    except Exception:
        pass

    # --- Seed context: project file tree (always provided up-front) ---
    _dir_listing = _mini_list_dir(str(project_root))

    _is_windows = sys.platform == "win32"
    history_text = "\n".join(f"  - {h}" for h in loop_history[-8:])
    _cmd_note = (
        "\nIMPORTANT — this agent runs on Windows. For shell commands use simple "
        "cmd.exe syntax (no &&, no Unix pipes). Prefer [LIST_DIR] for directory "
        "listings and [READ_FILE] for file contents instead of shell commands.\n"
    ) if _is_windows else ""
    sys_prompt = (
        "You are a debugging investigator. An AI agent has been stuck on the same "
        "error for many loops. Your job is to figure out WHY the fix isn't working "
        "and produce a short, specific hint (2-5 sentences) to redirect the agent.\n\n"
        "You have access to three tools:\n"
        "[READ_FILE] <absolute_or_relative_path>\n"
        "[LIST_DIR] <absolute_or_relative_path>  — lists all files recursively (Python-native, always works)\n"
        "[RUN_CMD] <shell command>  — run a shell command\n"
        + _cmd_note +
        "\nUse them to probe the environment. Then write your hint starting with HINT:"
    )
    user_msg = (
        f"TASK: {task_desc[:500]}\n\n"
        f"REPEATED ERROR (seen 3+ times):\n{repeated_error}\n\n"
        f"WHAT THE AGENT HAS TRIED (recent loops):\n{history_text}\n\n"
        f"PROJECT ROOT: {project_root}\n"
        f"PROJECT FILE TREE:\n{_dir_listing[:3000]}\n\n"
        + (f"RECENT AGENT LOG (last 30 lines):\n{_log_tail}\n\n" if _log_tail else "")
        + "Investigate why the fix isn't working. Use [READ_FILE] to inspect specific files, "
        "[LIST_DIR] for subdirectories, [RUN_CMD] for commands. Then write your HINT."
    )

    _inv_provider = _rt.META_INVESTIGATION_PROVIDER or _rt.LLM_PROVIDER
    _rt.log(f"[Meta] using provider={_inv_provider}")

    conversation = [{"role": "user", "content": user_msg}]
    hint = ""

    for inv_loop in range(8):  # max 8 investigator loops
        response, _, _thinking = call_llm(sys_prompt, conversation, provider=_inv_provider)
        conversation.append({"role": "assistant", "content": response})

        tool_output_parts = []
        for m in re.finditer(r'\[READ_FILE\]\s*(.+)', response):
            path = m.group(1).strip()
            content = _mini_read(path)
            _rt.log(f"[Meta] inv-loop {inv_loop + 1} READ_FILE {path} → {len(content)} chars")
            tool_output_parts.append(f"[READ_FILE {path}]\n{content}")

        for m in re.finditer(r'\[LIST_DIR\]\s*(.+)', response):
            path = m.group(1).strip()
            listing = _mini_list_dir(path)
            _rt.log(f"[Meta] inv-loop {inv_loop + 1} LIST_DIR {path} → {len(listing)} chars")
            tool_output_parts.append(f"[LIST_DIR {path}]\n{listing}")

        for m in re.finditer(r'\[RUN_CMD\]\s*(.+)', response):
            cmd = m.group(1).strip()
            output = _mini_run(cmd)
            _rt.log(f"[Meta] inv-loop {inv_loop + 1} RUN_CMD {cmd[:80]} → {output[:120].strip()}")
            tool_output_parts.append(f"[RUN_CMD {cmd}]\n{output}")

        # Extract hint — accept HINT: label or any of these fallback prefixes
        for _marker in ("HINT:", "NOTE:", "DIAGNOSIS:", "The issue is", "The problem is", "The root cause"):
            if _marker in response:
                hint = response[response.index(_marker):response.index(_marker) + 800].strip()
                break

        if tool_output_parts and not hint:
            loops_used = inv_loop + 1
            loops_left = 8 - loops_used
            if loops_left <= 2:
                pressure = (
                    f"\n\n⚠️ FINAL {loops_left} LOOP(S) REMAINING out of 8. "
                    "Stop exploring — you have enough information. Write your HINT: now."
                )
            else:
                pressure = (
                    f"\n\n[Investigation loop {loops_used}/8 — {loops_left} remaining. "
                    "If you have enough to diagnose the issue, write your HINT: now instead of continuing.]"
                )
            conversation.append({"role": "user", "content": "\n\n".join(tool_output_parts) + pressure})
        else:
            # No more tool calls (or hint already found) — if still no hint, use last response
            if not hint and response.strip():
                hint = response.strip()[:800]
                _rt.log("[Meta] No HINT: marker — using full response as hint")
            break

    if hint:
        _rt.log(f"[Meta] Full hint:\n{hint}")
    else:
        _rt.log(f"[Meta] No hint produced after {inv_loop + 1} investigator loops")

    return hint or f"[Meta-investigator found no clear cause for repeated error: {repeated_error[:120]}]"
