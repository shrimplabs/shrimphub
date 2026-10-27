"""
Agent learnings — capture observations from completed runs and inject them
into future agent prompts for the same (project, task_type).

Flow:
  _finish_agent() calls extract_learnings_async() in a daemon thread.
  generate_task_script() calls get_learnings() to inject into the user prompt.
"""

import re
import threading
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _summarise_log(log_tail: str, task_type: str, loops_used: int,
                   exit_code: int, api_key: str, base_url: str) -> str:
    """Call the LLM to extract learnings from a log tail."""
    import requests

    outcome = "completed successfully" if exit_code == 0 else f"failed (exit code {exit_code})"
    prompt = (
        f"An AI coding agent just finished a '{task_type}' task. "
        f"Outcome: {outcome}. Loops used: {loops_used}/200.\n\n"
        f"Log tail:\n<log>\n{log_tail}\n</log>\n\n"
        "Write 3-5 bullet points for the NEXT agent doing the same task type on "
        "this project. Focus on:\n"
        "- What approach worked (be specific)\n"
        "- What failed or needed retrying and why\n"
        "- Any gotchas or missing imports/deps discovered\n"
        "- Concrete shortcuts or patterns that saved loops\n\n"
        "Be terse and actionable. Each bullet starts with '- '."
    )

    try:
        resp = requests.post(
            f"{base_url}/messages",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "MiniMax-M3",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        result = resp.json()
        for item in result.get("content", []):
            if item.get("type") == "text":
                return item["text"].strip()
    except Exception as e:
        print(f"[Learnings] LLM call failed: {e}")

    return ""


def extract_learnings(task_id: str, task_type: str, project: str,
                      log_path: str, exit_code: int, data_dir: str) -> None:
    """Extract and store learnings. Designed to run in a daemon thread."""
    try:
        from swarm import orchestrator as _orch
        api_key = _orch.MINIMAX_API_KEY
        base_url = _orch.MINIMAX_BASE_URL
        if not api_key:
            return

        log_file = Path(log_path)
        if not log_file.exists():
            return

        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()

        # Count loops from log
        loop_nums = re.findall(r"loop (\d+)/\d+", "\n".join(lines))
        loops_used = max((int(n) for n in loop_nums), default=0)

        log_tail = "\n".join(lines[-120:])

        summary = _summarise_log(log_tail, task_type, loops_used, exit_code, api_key, base_url)
        if not summary:
            return

        outcome = "completed" if exit_code == 0 else f"failed (exit {exit_code})"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"## {timestamp} — {outcome} ({loops_used} loops)\n{summary}\n"

        learnings_dir = Path(data_dir) / "learnings" / project
        learnings_dir.mkdir(parents=True, exist_ok=True)
        learnings_file = learnings_dir / f"{task_type}.md"

        existing = learnings_file.read_text(encoding="utf-8", errors="replace") if learnings_file.exists() else ""

        # Split on entry headers, keep newest 5
        parts = re.split(r"(?=^## \d{4}-\d{2}-\d{2})", existing, flags=re.MULTILINE)
        kept = [p for p in parts if p.strip()][:4]  # keep 4 old + 1 new = 5 total
        learnings_file.write_text(entry + "\n" + "".join(kept), encoding="utf-8")
        print(f"[Learnings] Saved {project}/{task_type} ({loops_used} loops, {outcome})")

    except Exception as e:
        print(f"[Learnings] Error: {e}")


def extract_learnings_async(task_id: str, task_type: str, project: str,
                             log_path: str, exit_code: int, data_dir: str) -> None:
    """Fire-and-forget learnings extraction in a daemon thread."""
    t = threading.Thread(
        target=extract_learnings,
        args=(task_id, task_type, project, log_path, exit_code, data_dir),
        daemon=True,
    )
    t.start()


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

def get_learnings(project: str, task_type: str, data_dir: str) -> str:
    """Return learnings text to prepend to agent prompts, or empty string."""
    learnings_file = Path(data_dir) / "learnings" / project / f"{task_type}.md"
    if not learnings_file.exists():
        return ""
    try:
        content = learnings_file.read_text(encoding="utf-8", errors="replace").strip()
        if not content:
            return ""
        return content[:3000]  # cap to avoid bloating prompts
    except Exception:
        return ""
