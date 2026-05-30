#!/usr/bin/env python3
"""
Swarm Controller - Entry Point

Thin entry point that delegates to swarm.api (Flask) or swarm.orchestrator.
Also exposes generate_task_script which swarm.api imports to build agent wrappers.
"""

import json
import os

# Force unbuffered stdout so monitor thread prints appear immediately in logs
# even when running under nohup or other output-redirecting wrappers.
os.environ.setdefault("PYTHONUNBUFFERED", "1")

import sys
sys.stdout.reconfigure(line_buffering=True)
from pathlib import Path
import re
from collections.abc import Iterable

from swarm.platform import godot_binary_available, resolve_godot_binary, shell_quote_command_arg

# ---------------------------------------------------------------------------
# Config -- loaded once at module level; swarm.api syncs these vars at startup
# ---------------------------------------------------------------------------

_CONFIG_FILE = Path(__file__).parent / "config.json"
_config: dict = {}
if _CONFIG_FILE.exists():
    try:
        _config = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass

_configured_godot_path = str(_config.get("godot_path") or "").strip()
if _configured_godot_path and not os.environ.get("GODOT_PATH"):
    os.environ["GODOT_PATH"] = _configured_godot_path

# Load .env if present
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").strip().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ[_k.strip()] = _v.strip()

WORKSPACE: Path = Path(os.path.expanduser(_config.get("workspace", "~/workspace")))
API_PORT: int = _config.get("port", 5001)
DATA_DIR: Path = Path(__file__).parent / "data"  # synced by api.py at startup
MAX_LINES: int = _config.get("max_lines", 5000)
IGNORE_DIRS: set = set(_config.get("ignore_dirs", ["addons", ".git", ".godot"]))
IGNORE_EXTENSIONS: set = set(_config.get("ignore_extensions", [
    ".fbx", ".obj", ".glb", ".gltf", ".blend",
    ".png", ".jpg", ".jpeg", ".webp", ".svg",
    ".wav", ".mp3", ".ogg", ".opus",
    ".ttf", ".otf", ".woff",
    ".zip", ".tar", ".gz",
    ".import", ".uid",
]))
MCP_SERVERS: dict = _config.get("mcp_servers", {})
PROJECT_INTENT_PROMPT_VARIANT: str = _config.get("project_intent_prompt_variant", "exploratory")

# Prompt template warning collector -- appended to by _WarnUndefined when a prompt
# variable is missing; exposed via /api/health so ops can see broken prompts.
_prompt_warnings: list = []

# LLM provider config
LLM_PROVIDER: str = _config.get("llm_provider", "minimax")
LLM_PROVIDERS: dict = {
    "minimax": {
        "base_url": "https://api.minimax.io/anthropic/v1",
        "model": "MiniMax-M2.7",
        "api_key_env": "MINIMAX_API_KEY",
        "format": "anthropic",
        "context_window": 204800,
        "max_tokens": 32768,
    },
    "claude": {
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-6",
        "api_key_env": "ANTHROPIC_API_KEY",
        "format": "anthropic_native",
        "max_tokens": 8096,
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "anthropic/claude-3.5-sonnet",
        "api_key_env": "OPENROUTER_API_KEY",
        "format": "openai",
        "max_tokens": 8096,
    },
    "kimi": {
        "base_url": "https://api.kimi.com/coding/v1",
        "model": "k2p5",
        "api_key_env": "KIMI_API_KEY",
        "format": "anthropic",
        "max_tokens": 32768,
    },
}
# Merge any user-defined provider overrides from config
for _name, _cfg in _config.get("llm_providers", {}).items():
    if _name in LLM_PROVIDERS:
        LLM_PROVIDERS[_name].update(_cfg)
    else:
        LLM_PROVIDERS[_name] = _cfg


def _sanitize_learnings_text(learnings_text: str) -> str:
    if not learnings_text:
        return ""
    banned_patterns = (
        r"rm\s+-f\s+.*\.gd\.uid",
        r"rm\s+-f\s+.*uid_cache\.bin",
        r"delete\s+.*\.gd\.uid",
        r"delete\s+.*uid_cache\.bin",
        r"avoid deleting .*\.godot",
    )
    kept: list[str] = []
    for line in learnings_text.splitlines():
        lowered = line.lower()
        if any(re.search(pattern, lowered) for pattern in banned_patterns):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


_LEARNINGS_STOPWORDS = {
    "this", "that", "with", "from", "into", "only", "before", "after", "while",
    "when", "then", "than", "they", "them", "their", "there", "would", "could",
    "should", "about", "because", "using", "used", "use", "task", "project",
    "agent", "agents", "repo", "file", "files", "tests", "test", "python",
    "feature", "bug", "refactor", "polish", "work", "current", "needs", "need",
    "adding", "implement", "implementation", "follow", "same", "first", "next",
    "check", "run", "runs", "path", "paths", "write", "edits", "edit",
    "src", "utils", "helper", "helpers", "module",
}

_GENERIC_LEARNINGS_HINTS = (
    "broadcast_",
    "lock",
    "claim",
    "sibling",
    "validation",
    "git status",
    "working tree",
    "already implemented",
    "already in head",
    "commit",
    "retry",
    "timeout",
)


def _description_terms(description: str) -> set[str]:
    if not description:
        return set()
    parts = re.split(r"[^a-zA-Z0-9_/.\-]+", description.lower())
    terms: set[str] = set()
    for part in parts:
        if not part:
            continue
        for token in re.split(r"[/_.\-]", part):
            token = token.strip()
            if len(token) < 4 or token.isdigit() or token in _LEARNINGS_STOPWORDS:
                continue
            terms.add(token)
    return terms


def _fileish_terms(text: str) -> set[str]:
    hits: set[str] = set()
    for match in re.findall(r"[a-zA-Z0-9_/.\-]+\.(?:py|gd|tscn|tres|md|json)", text.lower()):
        for token in re.split(r"[/_.\-]", match):
            token = token.strip()
            if len(token) >= 4 and token not in _LEARNINGS_STOPWORDS:
                hits.add(token)
    return hits


def _iter_learning_entries(learnings_text: str) -> Iterable[tuple[str, list[str]]]:
    if not learnings_text:
        return []
    parts = [
        part.strip()
        for part in re.split(r"(?=^## \d{4}-\d{2}-\d{2})", learnings_text, flags=re.MULTILINE)
        if part.strip()
    ]
    entries: list[tuple[str, list[str]]] = []
    for part in parts:
        lines = [line.rstrip() for line in part.splitlines() if line.strip()]
        if not lines:
            continue
        header = lines[0]
        bullets = [line for line in lines[1:] if line.lstrip().startswith("- ")]
        if bullets:
            entries.append((header, bullets))
    return entries


def _filter_learnings_text(learnings_text: str, description: str) -> str:
    """Keep only recent and relevant learnings for the current task description."""
    entries = list(_iter_learning_entries(learnings_text))
    if not entries:
        return ""

    task_terms = _description_terms(description)
    task_file_terms = _fileish_terms(description)
    kept_entries: list[str] = []

    # File is written newest-first; keep only the freshest few entries.
    for header, bullets in entries[:3]:
        kept_bullets: list[str] = []
        for bullet in bullets:
            lowered = bullet.lower()
            if any(hint in lowered for hint in _GENERIC_LEARNINGS_HINTS):
                kept_bullets.append(bullet)
                continue
            bullet_file_terms = _fileish_terms(bullet)
            if bullet_file_terms:
                if task_file_terms and task_file_terms.intersection(bullet_file_terms):
                    kept_bullets.append(bullet)
                continue
            bullet_terms = _description_terms(bullet)
            if task_terms and task_terms.intersection(bullet_terms):
                kept_bullets.append(bullet)
        if kept_bullets:
            kept_entries.append("\n".join([header, *kept_bullets]))

    return "\n\n".join(kept_entries).strip()


def _compact_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _truncate_text(text: str, limit: int = 220) -> str:
    compact = _compact_whitespace(text)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."


def _extract_markdown_section(text: str, headings: tuple[str, ...]) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    capture = False
    collected: list[str] = []
    heading_set = {h.strip().lower() for h in headings}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip().lower()
            if capture:
                break
            capture = heading in heading_set
            continue
        if not capture:
            continue
        if stripped:
            collected.append(stripped)
        elif collected:
            break
    return "\n".join(collected).strip()


def _extract_first_markdown_paragraph(text: str) -> str:
    if not text:
        return ""
    paragraph: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            if paragraph:
                break
            continue
        paragraph.append(line)
    return " ".join(paragraph).strip()


def _extract_markdown_bullets(text: str, heading: str) -> list[str]:
    section = _extract_markdown_section(text, (heading,))
    bullets: list[str] = []
    for line in section.splitlines():
        cleaned = re.sub(r"^\s*[-*]\s*", "", line).strip()
        if cleaned:
            bullets.append(cleaned)
    return bullets


def _doc_summary(project_path: Path) -> dict:
    found_docs: list[str] = []
    goal = ""
    quality_gates: list[str] = []
    for name in ("GAME_DESIGN.md", "README.md", "PROJECT_CLOSURE.md"):
        path = project_path / name
        if not path.exists():
            continue
        found_docs.append(name)
        try:
            text = path.read_text()
        except Exception:
            continue
        if name == "GAME_DESIGN.md":
            if not goal:
                goal = _extract_markdown_section(text, ("Overview", "Summary")) or _extract_first_markdown_paragraph(text)
            if not quality_gates:
                quality_gates = _extract_markdown_bullets(text, "Quality Gates")
        elif name == "README.md" and not goal:
            goal = _extract_first_markdown_paragraph(text)
    return {
        "found_docs": found_docs,
        "goal": _truncate_text(goal, 240),
        "quality_gates": [_truncate_text(g, 120) for g in quality_gates[:3]],
    }


def _task_memory_label(task: dict | None) -> str:
    if not task:
        return ""
    metadata = task.get("metadata") or {}
    for candidate in (metadata.get("title"), metadata.get("story_title"), task.get("title")):
        if isinstance(candidate, str) and candidate.strip():
            return _truncate_text(candidate.strip(), 90)
    desc = str(task.get("description") or "")
    for raw_line in desc.splitlines():
        cleaned = raw_line.strip().lstrip("#").strip()
        if not cleaned:
            continue
        lower = cleaned.lower()
        if lower.startswith(("acceptance criteria", "depends-on", "original task", "validation failed after")):
            continue
        if cleaned.startswith("-"):
            cleaned = cleaned.lstrip("-").strip()
        if cleaned:
            return _truncate_text(cleaned, 90)
    return task.get("id", "task")


def _task_memory_is_low_signal(label: str) -> bool:
    lowered = (label or "").strip().lower()
    if not lowered:
        return True
    noisy_prefixes = (
        "validation bug task",
        "closure repair task",
        "terminal recovery continuation",
        "recovery task:",
        "continuation of task",
    )
    return any(lowered.startswith(prefix) for prefix in noisy_prefixes)


def _read_task_history_tail(project: str, limit: int = 3, tail_lines: int = 400) -> list[dict]:
    history_path = DATA_DIR / "task-history.jsonl"
    if not history_path.exists():
        return []

    try:
        with history_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            remaining = handle.tell()
            chunk_size = 8192
            buffer = b""
            while remaining > 0 and buffer.count(b"\n") < tail_lines:
                read_size = min(chunk_size, remaining)
                remaining -= read_size
                handle.seek(remaining)
                buffer = handle.read(read_size) + buffer
        lines = buffer.decode("utf-8", errors="ignore").splitlines()
    except Exception:
        return []

    results: list[dict] = []
    seen: set[str] = set()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if record.get("project") != project or record.get("status") != "completed":
            continue
        task_id = str(record.get("id") or "").strip()
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        results.append(record)
        if len(results) >= limit:
            break
    return results


def _recent_completed_task_memory(project: str, live_tasks: list[dict], limit: int = 3) -> list[str]:
    memories: list[str] = []
    seen: set[str] = set()

    live_completed = sorted(
        (t for t in live_tasks if t.get("status") == "completed"),
        key=lambda t: t.get("completed") or t.get("created") or "",
        reverse=True,
    )
    for task in live_completed:
        task_id = task.get("id")
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        label = _task_memory_label(task)
        if _task_memory_is_low_signal(label):
            continue
        memories.append(f"{task_id}: {label}")
        if len(memories) >= limit:
            return memories

    for task in _read_task_history_tail(project, limit=limit):
        task_id = task.get("id")
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        label = _task_memory_label(task)
        if _task_memory_is_low_signal(label):
            continue
        memories.append(f"{task_id}: {label}")
        if len(memories) >= limit:
            break
    return memories


def _latest_verification_summary(project: str) -> list[str]:
    try:
        from swarm import db as _db
        runs = _db.verification_run_list_by_project(project, limit=1)
    except Exception:
        return []
    if not runs:
        return []

    run = runs[0]
    results = dict(run.get("results_json") or {})
    summary: list[str] = []
    for key in ("boot_ok", "tests_ok", "smoke_ok"):
        if key in results and results.get(key) is not None:
            summary.append(f"{key}={results.get(key)}")
    critical_flows = results.get("critical_flows") or {}
    if isinstance(critical_flows, dict) and critical_flows:
        passed = sum(1 for value in critical_flows.values() if value)
        summary.append(f"critical_flows_passed={passed}/{len(critical_flows)}")
    errors = results.get("errors") or []
    if errors:
        summary.append(f"errors={len(errors)}")
    return summary[:4]


def _project_context_packet(task: dict, project_path: Path) -> str:
    try:
        from swarm import db as _db
        from swarm.closure.specs import normalize_project_spec
    except Exception:
        return ""

    try:
        project_row = _db.project_get(task["project"]) or {}
        live_tasks = _db.task_get_by_project(task["project"]) or []
    except Exception:
        project_row = {}
        live_tasks = []

    task_id = task.get("id", "unknown")
    live_task_map = {t.get("id"): t for t in live_tasks if t.get("id")}
    current_task = live_task_map.get(task_id, task)
    doc_info = _doc_summary(project_path)

    depends_on = []
    for dep_id in list(current_task.get("dependencies") or [])[:3]:
        dep_task = live_task_map.get(dep_id)
        depends_on.append(f"{dep_id} ({_task_memory_label(dep_task)})" if dep_task else dep_id)

    downstream = [
        t for t in live_tasks
        if task_id in set(t.get("dependencies") or [])
    ]
    unlocks = [f"{t.get('id')}: {_task_memory_label(t)}" for t in downstream[:3]]

    try:
        profile = project_row.get("profile")
        closure_spec = normalize_project_spec(project_row.get("closure_spec"), profile)
    except Exception:
        closure_spec = project_row.get("closure_spec") or {}

    critical_flows = list(closure_spec.get("critical_flows") or [])
    flow_labels = []
    for flow in critical_flows[:3]:
        if isinstance(flow, dict):
            label = flow.get("description") or flow.get("id")
        else:
            label = str(flow)
        if label:
            flow_labels.append(_truncate_text(label, 80))

    verification_lines = _latest_verification_summary(task["project"])
    invariant_lines: list[str] = list(verification_lines)
    for gate in doc_info.get("quality_gates", []):
        if len(invariant_lines) >= 5:
            break
        invariant_lines.append(gate)
    invariant_lines = [_truncate_text(line, 110) for line in invariant_lines[:5]]

    packet_lines = ["## PROJECT CONTEXT PACKET"]
    if doc_info.get("goal"):
        packet_lines.append(f"- Project goal: {doc_info['goal']}")
    if doc_info.get("found_docs"):
        packet_lines.append(f"- Intent docs: {', '.join(doc_info['found_docs'])}")

    position = [f"`{task_id}` ({current_task.get('type', task.get('type', 'task'))})"]
    if depends_on:
        position.append("depends on " + "; ".join(depends_on))
    if unlocks:
        position.append("unlocks " + "; ".join(unlocks))
    packet_lines.append("- Task position: " + " | ".join(position))

    closure_mode = project_row.get("closure_mode") or closure_spec.get("mode")
    closure_status = project_row.get("closure_status")
    closure_bits = []
    if closure_mode:
        closure_bits.append(f"mode `{closure_mode}`")
    if closure_status:
        closure_bits.append(f"status `{closure_status}`")
    if flow_labels:
        closure_bits.append("critical flows: " + "; ".join(flow_labels))
    if closure_bits:
        packet_lines.append("- Current closure target: " + " | ".join(closure_bits))

    if invariant_lines:
        packet_lines.append("- Invariants / quality gates:")
        for line in invariant_lines:
            packet_lines.append(f"  - {line}")

    recent_memory = _recent_completed_task_memory(task["project"], live_tasks, limit=3)
    if recent_memory:
        packet_lines.append("- Recent completed tasks:")
        for line in recent_memory:
            packet_lines.append(f"  - {line}")

    if len(packet_lines) <= 1:
        return ""
    return "\n".join(packet_lines) + "\n\n"


# ---------------------------------------------------------------------------
# Godot validation prompt block -- shared across feature/bug/polish prompts
# ---------------------------------------------------------------------------

def _godot_status_block(godot_bin: str, godot_command: str) -> str:
    if godot_binary_available(godot_bin):
        return (
            f"Godot executable configured by the swarm controller: {godot_bin}\n"
            f"Use this shell command prefix for Godot validation: {godot_command}"
        )
    return (
        "Godot executable is NOT configured or discoverable by the swarm controller.\n"
        "Use the controller configuration as the source of truth. Report this as a "
        "configuration blocker and ask the operator to set config.json `godot_path` "
        "or the GODOT_PATH environment variable."
    )


def _godot_validation_block(
    project_path_str: str,
    godot_bin: str,
    godot_command: str,
    godot_status: str,
    include_gut: bool = False,
) -> str:
    """Return the numbered validation steps for Godot agent prompts."""
    if not godot_binary_available(godot_bin):
        return f"""{godot_status}

BEFORE committing, do not attempt Godot validation until the controller provides a valid executable path.
You may still run non-Godot checks, but if this task requires Godot execution, stop and report the blocker."""

    project_arg = shell_quote_command_arg(project_path_str)
    gut_step = ""
    if include_gut:
        gut_step = f"""
   d) GUT unit tests (only if addons/gut/ exists):
      run_command("{godot_command} --headless --path {project_arg} --script res://addons/gut/gut_cmdln.gd -- -gdir=res://tests -gexit 2>&1", timeout=300)
      ALL tests must pass (exit 0, no FAILED lines). Fix failures before committing.
      NOTE: GUT runs take 2-3 minutes with many tests -- always pass timeout=300."""

    return f"""{godot_status}

BEFORE committing, run the full validation suite in order:

   a) Script parse check -- write res://_swarm_check.gd then run it:
      Content:
        extends SceneTree
        func _init():
            var errors = []
            _scan("res://", errors)
            if errors.size() > 0:
                for e in errors: print("SCRIPT ERROR: " + e)
                quit(1)
            else: print("All scripts OK"); quit(0)
        func _scan(path: String, errors: Array) -> void:
            var dir = DirAccess.open(path)
            if dir == null: return
            dir.list_dir_begin()
            var f = dir.get_next()
            while f != "":
                if dir.current_is_dir() and not f.begins_with(".") and f != "addons" and f != "tests":
                    _scan(path + f + "/", errors)
                elif f.ends_with(".gd"):
                    var s = load(path + f)
                    if s == null: errors.append("Failed to load: " + path + f)
                f = dir.get_next()
      Run: run_command("{godot_command} --headless --path {project_arg} --script res://_swarm_check.gd --quit 2>&1")

   b) Scene load check -- write res://_swarm_scene_check.gd then run it:
      Content:
        extends SceneTree
        func _init():
            var errors = []
            _scan("res://", errors)
            if errors.size() > 0:
                for e in errors: print("SCENE ERROR: " + e)
                quit(1)
            else: print("All scenes OK"); quit(0)
        func _scan(path: String, errors: Array) -> void:
            var dir = DirAccess.open(path)
            if dir == null: return
            dir.list_dir_begin()
            var f = dir.get_next()
            while f != "":
                if dir.current_is_dir() and not f.begins_with(".") and f != "addons":
                    _scan(path + f + "/", errors)
                elif f.ends_with(".tscn"):
                    var s = load(path + f)
                    if s == null: errors.append("Failed to load scene: " + path + f)
                f = dir.get_next()
      Run: run_command("{godot_command} --headless --path {project_arg} --script res://_swarm_scene_check.gd --quit 2>&1")

   c) Main scene instantiate check:
      - Read project.godot using read_file("{project_path_str}/project.godot") and find run/main_scene.
      - Write res://_swarm_main_check.gd using the path found (replace MAIN_SCENE_PATH below):
        extends SceneTree
        func _init():
            var packed = load("MAIN_SCENE_PATH")
            if packed == null: print("SCENE ERROR: Cannot load main scene"); quit(1)
            var instance = packed.instantiate()
            if instance == null: print("SCENE ERROR: Cannot instantiate main scene"); quit(1)
            instance.free(); print("Main scene OK"); quit(0)
      Run: run_command("{godot_command} --headless --path {project_arg} --script res://_swarm_main_check.gd --quit 2>&1")
{gut_step}
   z) Delete all temp files with delete_file/write-safe tooling or Python pathlib; do not use platform-specific rm.
   Fix ALL errors before committing. Only commit after every check above passes."""


# ---------------------------------------------------------------------------
# Prompt loader -- Jinja2 + YAML with << >> delimiters (no {} escaping needed)
# ---------------------------------------------------------------------------

def _load_prompt(name: str, **vars) -> tuple:
    """Load prompts/<name>.yaml and render system+user with Jinja2.

    Templates may use `<% include 'common/foo.md' %>` to pull in shared blocks
    from prompts/common/. The FileSystemLoader makes includes work even when the
    parent template is loaded via from_string().

    Missing template variables are replaced with an empty string and logged as
    warnings rather than raising -- a broken prompt should never crash fill_slots
    for all other projects.
    """
    import yaml
    from jinja2 import Environment, FileSystemLoader, Undefined

    class _WarnUndefined(Undefined):
        """Renders as empty string but prints a warning so missing vars are visible."""
        def __str__(self):
            msg = f"prompt '{name}': undefined variable '{self._undefined_name}'"
            print(f"[Prompt] WARNING: {msg} -- rendered as empty string")
            import swarm_runner as _sr
            if msg not in _sr._prompt_warnings:
                _sr._prompt_warnings.append(msg)
            return ""
        def __iter__(self):
            return iter([])
        def __bool__(self):
            return False

    prompts_dir = Path(__file__).parent / "prompts"
    parts = name.split("/")
    prompt_file = prompts_dir / "/".join(parts[:-1]) / (parts[-1] + ".yaml")
    data = yaml.safe_load(prompt_file.read_text(encoding="utf-8"))

    env = Environment(
        loader=FileSystemLoader(str(prompts_dir)),
        variable_start_string="<<",
        variable_end_string=">>",
        block_start_string="<%",
        block_end_string="%>",
        undefined=_WarnUndefined,
        keep_trailing_newline=True,
    )
    system = env.from_string(data["system"]).render(**vars).strip()
    user   = env.from_string(data["user"]).render(**vars).strip()
    return system, user


# ---------------------------------------------------------------------------
# generate_task_script -- imported by swarm.api and swarm.orchestrator
# ---------------------------------------------------------------------------

def generate_task_script(task: dict) -> str:
    """Generate a thin wrapper script that imports swarm.agent_runtime and sets config."""
    # Re-fetch the task from DB so any description update that arrived after the
    # task was selected (race condition) is picked up before the script is baked.
    try:
        from swarm import db as _db
        fresh = _db.task_get(task.get("id", ""))
        if fresh:
            task = fresh
    except Exception:
        pass

    project     = task["project"]
    task_type   = task["type"]
    description = task.get("description", "")
    task_id     = task.get("id", "unknown")

    # Use worktree path if the orchestrator created one (parallel agent isolation)
    metadata = task.get("metadata", {}) or {}
    worktree_path_str = metadata.get("worktree_path")
    worktree_branch_str = metadata.get("worktree_branch", "")
    project_path = Path(worktree_path_str) if worktree_path_str else WORKSPACE / project

    # Detect project type (check the actual project dir, not worktree, for config files)
    _base_project_path = WORKSPACE / project
    is_python = (_base_project_path / "requirements.txt").exists() or (_base_project_path / "pyproject.toml").exists()
    is_typescript = (_base_project_path / "package.json").exists() and (
        (_base_project_path / "tsconfig.json").exists()
        or (_base_project_path / "tsconfig.app.json").exists()
    )

    # Tiered retry context: richer context on each successive attempt.
    # Research feeder results always take precedence (they contain a targeted diagnosis).
    #
    # Attempt 1: description only (no prefix)
    # Attempt 2: description + last_failure excerpt
    # Attempt 3+: description + last_failure + git diff stat + "try a fundamentally different approach"
    # Post-research: description + research_context diagnosis (overrides all of the above)
    retry_prefix = ""
    attempt_number = metadata.get("failure_attempt", 0)

    if metadata.get("research_context"):
        # Post-research tier: targeted diagnosis from a research feeder
        retry_prefix = (
            "\n```\n// RESEARCH DIAGNOSIS (read before anything else)\n"
            "A dedicated research agent investigated why previous attempts failed.\n"
            "Apply this diagnosis -- do not repeat the same approaches that failed.\n\n"
            + metadata["research_context"][:2000]
            + "\n```\n\n"
        )
    elif attempt_number >= 3 and metadata.get("last_failure"):
        # Attempt 3+: last failure + git diff of what attempt 2 changed + directive
        git_log_lines = []
        git_diff_stat = ""
        try:
            import subprocess as _sp
            _r = _sp.run(["git", "log", "--oneline", "-5"],
                         capture_output=True, text=True, cwd=str(project_path), timeout=5)
            if _r.returncode == 0:
                git_log_lines = _r.stdout.strip().splitlines()
            _d = _sp.run(["git", "diff", "HEAD~1", "--stat"],
                         capture_output=True, text=True, cwd=str(project_path), timeout=5)
            if _d.returncode == 0 and _d.stdout.strip():
                git_diff_stat = _d.stdout.strip()
        except Exception:
            pass
        retry_context = {
            "attempt": attempt_number,
            "max_attempts": task.get("max_attempts", 3),
            "previous_error": metadata["last_failure"],
            "what_changed_last_attempt": git_diff_stat or "(no committed changes)",
            "recent_commits": git_log_lines,
            "directive": "Previous approaches have not worked. Try a fundamentally different approach.",
        }
        retry_prefix = (
            "\n```json\n// RETRY CONTEXT (attempt %d -- try a different approach)\n" % attempt_number
            + json.dumps(retry_context, indent=2)
            + "\n```\n\n"
        )
    elif attempt_number >= 1 and metadata.get("last_failure"):
        # Attempt 2: last failure excerpt only
        retry_context = {
            "attempt": attempt_number,
            "max_attempts": task.get("max_attempts", 3),
            "previous_error": metadata["last_failure"],
        }
        retry_prefix = (
            "\n```json\n// RETRY CONTEXT\n"
            + json.dumps(retry_context, indent=2)
            + "\n```\n\n"
        )

    description = retry_prefix + description

    try:
        context_packet = _project_context_packet(task, project_path)
        if context_packet:
            description = context_packet + description
    except Exception:
        pass

    # Project notes: inject context set by the user (E)
    try:
        from swarm import db as _db
        _notes = _db.project_get_notes(project)
        if _notes and _notes.strip():
            description = f"PROJECT CONTEXT:\n{_notes.strip()}\n\n{description}"
    except Exception:
        pass

    # Recent git commits: prepend for all tasks
    try:
        import subprocess as _subp
        _git = _subp.run(["git", "log", "--oneline", "-5"],
                         capture_output=True, text=True, cwd=str(project_path), timeout=5)
        if _git.returncode == 0 and _git.stdout.strip():
            description = f"RECENT COMMITS:\n{_git.stdout.strip()}\n\n{description}"
    except Exception:
        pass

    # Learnings: inject observations from previous runs of same (project, task_type)
    # Keep bug/recovery prompts much tighter; stale broad notes cause more harm than help there.
    try:
        from swarm.learnings import get_learnings
        _learnings_text = get_learnings(project, task_type, str(DATA_DIR))
        _learnings_text = _sanitize_learnings_text(_learnings_text)
        _learnings_text = _filter_learnings_text(_learnings_text, description)
        if task_type in ("bug", "qa", "hybrid_qa", "harness_qa"):
            _learnings_text = ""
        if _learnings_text:
            description = f"NOTES FROM PREVIOUS RUNS:\n{_learnings_text}\n\n{description}"
    except Exception:
        pass

    # Agent Knowledge: prepend any persistent structural facts from AGENT_KNOWLEDGE.md
    try:
        from swarm.agent_runtime import read_agent_knowledge
        _knowledge_text = read_agent_knowledge(str(project_path))
        if _knowledge_text:
            description = f"PROJECT KNOWLEDGE:\n{_knowledge_text}\n\n{description}"
    except Exception:
        pass

    # Validation State: inject current validation status from VALIDATION_STATE.md
    # This is a separate overwrite-only file so it never grows unboundedly.
    try:
        from swarm.tools.knowledge import read_validation_state
        _validation_state = read_validation_state(str(project_path))
        if _validation_state:
            description = f"VALIDATION STATE:\n{_validation_state}\n\n{description}"
    except Exception:
        pass

    # Broadcast log: inject recent entries from other agents working on this project
    try:
        _broadcast_path = DATA_DIR / "broadcast" / f"{project.replace('/', '_').replace(chr(92), '_')}.log"
        if _broadcast_path.exists():
            _lines = _broadcast_path.read_text().splitlines()
            if _lines:
                _tail = "\n".join(_lines[-30:])
                description = (
                    f"## Recent Project Broadcast\n"
                    f"The following observations were left by recent agents working on this project:\n"
                    f"{_tail}\n\n"
                    f"{description}"
                )
    except Exception:
        pass

    # ---- Common render vars ----
    _godot_bin = resolve_godot_binary()
    _godot_command = shell_quote_command_arg(_godot_bin)
    _godot_status = _godot_status_block(_godot_bin, _godot_command)
    _validation_with_gut = _godot_validation_block(
        str(project_path),
        godot_bin=_godot_bin,
        godot_command=_godot_command,
        godot_status=_godot_status,
        include_gut=True,
    )
    _validation_without_gut = _godot_validation_block(
        str(project_path),
        godot_bin=_godot_bin,
        godot_command=_godot_command,
        godot_status=_godot_status,
        include_gut=False,
    )
    _common = dict(
        project=project,
        description=description,
        project_path=str(project_path),
        project_path_arg=shell_quote_command_arg(str(project_path)),
        godot_bin=_godot_bin,
        godot_command=_godot_command,
        godot_status=_godot_status,
        prompt_intent_variant=PROJECT_INTENT_PROMPT_VARIANT,
    )

    # ---- Godot prompts ----
    feature_system, feature_user = _load_prompt(
        "feature", **_common, validation_block=_validation_with_gut,
    )
    bug_system, bug_user = _load_prompt(
        "bug", **_common, validation_block=_validation_without_gut,
    )
    polish_system, polish_user = _load_prompt(
        "polish", **_common, validation_block=_validation_without_gut,
    )
    audit_system, audit_user = _load_prompt("audit", **_common)
    audit_learnings_system, audit_learnings_user = _load_prompt(
        "audit_learnings", **_common, data_dir=str(DATA_DIR)
    )
    triage_system, triage_user = _load_prompt("triage", **_common)
    qa_cycle = int((metadata.get("qa_cycle") or 0))
    qa_max_cycles = int(_config.get("qa_max_cycles", 3))
    meta_investigation_enabled = bool(_config.get("meta_investigation", True))
    meta_investigation_provider = str(_config.get("meta_investigation_provider", ""))
    qa_system, qa_user = _load_prompt("qa", **_common, qa_cycle=qa_cycle, qa_max_cycles=qa_max_cycles)
    harness_available = (project_path / "autoload" / "test_harness.gd").exists()
    hybrid_qa_system, hybrid_qa_user = _load_prompt(
        "hybrid_qa",
        **_common,
        qa_cycle=qa_cycle,
        qa_max_cycles=qa_max_cycles,
        harness_available=harness_available,
        harness_support=("available" if harness_available else "unavailable"),
    )
    project_plan_system, project_plan_user = _load_prompt(
        "project_plan", **_common, task_id=task_id,
    )
    art_pass_system, art_pass_user = _load_prompt("art_pass", **_common)
    research_system, research_user = _load_prompt("research", **_common)
    gardener_system, gardener_user = _load_prompt("gardener", **_common, task_id=task_id, swarm_data_dir=str(DATA_DIR))
    librarian_system, librarian_user = _load_prompt(
        "librarian", **_common, task_id=task_id, swarm_data_dir=str(DATA_DIR),
        librarian_max_prompt_tasks=_config.get("librarian_max_prompt_tasks", 3),
        librarian_autonomous_edits=_config.get("librarian_autonomous_edits", False),
    )
    auditor_system, auditor_user = _load_prompt(
        "auditor", **_common, task_id=task_id, swarm_data_dir=str(DATA_DIR),
        meta_auditor_max_tasks=_config.get("meta_auditor_max_tasks", 20),
        managed_projects_list=", ".join(_config.get("managed_projects", [])),
        managed_projects=", ".join(_config.get("managed_projects", [])),
        template_checksums="",   # populated at runtime by the auditor task description
        previous_report="",      # populated at runtime by the auditor task description
        swarm_knowledge="",      # populated at runtime by the auditor task description
    )
    cartographer_system, cartographer_user = _load_prompt(
        "cartographer", **_common, task_id=task_id, swarm_data_dir=str(DATA_DIR),
    )
    archaeologist_system, archaeologist_user = _load_prompt(
        "archaeologist", **_common, task_id=task_id, swarm_data_dir=str(DATA_DIR),
        stall_reason="",         # populated at runtime by the archaeologist task description
        recent_failures="",      # populated at runtime by the archaeologist task description
        git_log_summary="",      # populated at runtime by the archaeologist task description
    )
    scheduler_system, scheduler_user = _load_prompt(
        "scheduler", **_common, task_id=task_id, swarm_data_dir=str(DATA_DIR),
        scheduler_allow_pause=_config.get("scheduler_allow_pause", True),
        scheduler_allow_agent_ceiling_adjust=_config.get(
            "scheduler_allow_agent_ceiling_adjust", True
        ),
        scheduler_off_peak_hours=_config.get("scheduler_off_peak_hours", [0, 6]),
    )

    # ---- Python prompts ----
    python_feature_system, python_feature_user = _load_prompt("python/feature", **_common)
    python_bug_system, python_bug_user = _load_prompt("python/bug", **_common)
    python_refactor_system, python_refactor_user = _load_prompt("python/refactor", **_common)
    python_plan_system, python_plan_user = _load_prompt(
        "python/plan", **_common, task_id=task_id,
    )
    typescript_feature_system, typescript_feature_user = _load_prompt(
        "typescript/feature", **_common
    )
    typescript_bug_system, typescript_bug_user = _load_prompt(
        "typescript/bug", **_common
    )
    typescript_refactor_system, typescript_refactor_user = _load_prompt(
        "typescript/refactor", **_common
    )
    plan_system, plan_user = _load_prompt(
        "plan", **_common, task_id=task_id,
    )

    # ---- Manager prompt ----
    managed_projects_list = ", ".join(
        _config.get("managed_projects", [project])
    ) or project
    # ---- Harness QA prompts ----
    harness_qa_system, harness_qa_user = _load_prompt(
        "harness_qa",
        project_name=project,
        project_path=str(project_path),
        task_description=description,
    )
    # ---- Scenario QA prompts ----
    scenario_qa_system, scenario_qa_user = _load_prompt(
        "scenario_qa",
        **_common,
    )

    # ---- Plugin prompt + context injection ----
    # If a plugin is registered for this task_type:
    #   1. Use the plugin's prompt (overrides all built-in prompt vars below).
    #   2. Resolve plugin context providers and prepend to description.
    # This runs before the wrapper is generated so baked prompt vars are correct.
    try:
        from swarm.plugins import get_plugin, load_plugin_prompt, resolve_context as _plugin_ctx
        _plugin = get_plugin(task_type)
        if _plugin:
            _plugin_prompts = load_plugin_prompt(_plugin, Path(__file__).parent, **_common)
            if _plugin_prompts:
                # Store in dedicated rt vars so agent_runtime can use them
                _plugin_system, _plugin_user = _plugin_prompts
                print(f"[Swarm] Plugin '{_plugin.plugin_id}' prompt loaded for task_type='{task_type}'")
            else:
                _plugin_system, _plugin_user = "", ""
            _ctx_block = _plugin_ctx(_plugin, project_path)
            if _ctx_block:
                description = _ctx_block + "\n\n" + description
        else:
            _plugin_system, _plugin_user = "", ""
    except Exception as _plugin_exc:
        print(f"[Swarm] Plugin resolution error: {_plugin_exc}")
        _plugin_system, _plugin_user = "", ""

    # ---- Vision provider config (passed to QA agent for vision model dispatch) ----
    qa_config = {
        "vision_provider": _config.get("vision_provider", "minimax-mcp"),
        "vision_provider_fast": _config.get("vision_provider_fast", ""),
        "vision_providers": _config.get("vision_providers", {}),
    }

    # ---- Per-task-type thinking budget ----
    # If this task's type is listed in thinking_task_types, enable thinking for
    # the active provider (overrides the global thinking_budget setting).
    thinking_task_types: list = _config.get("thinking_task_types", [])
    thinking_task_budget: int = _config.get("thinking_task_budget", 10000)
    effective_llm_providers = {k: dict(v) for k, v in LLM_PROVIDERS.items()}
    if task_type in thinking_task_types and thinking_task_budget > 0:
        if LLM_PROVIDER in effective_llm_providers:
            effective_llm_providers[LLM_PROVIDER]["thinking_budget"] = thinking_task_budget

    # Override with Python prompts when appropriate
    if is_python:
        if task_type == "feature":
            feature_system, feature_user = python_feature_system, python_feature_user
        elif task_type == "bug":
            bug_system, bug_user = python_bug_system, python_bug_user
        elif task_type == "refactor":
            feature_system, feature_user = python_refactor_system, python_refactor_user
    elif is_typescript:
        if task_type == "feature":
            feature_system, feature_user = typescript_feature_system, typescript_feature_user
        elif task_type == "bug":
            bug_system, bug_user = typescript_bug_system, typescript_bug_user
        elif task_type == "refactor":
            feature_system, feature_user = typescript_refactor_system, typescript_refactor_user

    # ---- Generate thin wrapper that imports agent_runtime ----
    swarm_controller_dir = str(Path(__file__).parent)
    # Compute the venv site-packages path relative to swarm_runner.py so agents
    # can always find installed packages even if the server was launched with a
    # system/Homebrew Python instead of the venv interpreter.
    _venv_site = str(Path(__file__).parent / ".venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages")
    # Also collect any site-packages from the server's own sys.path (covers the case
    # where the server IS running in a venv or has additional packages installed).
    _extra_paths = list(dict.fromkeys(
        [_venv_site] + [p for p in sys.path if "site-packages" in p or "dist-packages" in p]
    ))
    wrapper = f'#!/usr/bin/env python3\n"""\nAgent wrapper -- {project} ({task_type})\nTask ID: {task_id}\n"""\n'
    wrapper += f"""import sys
import os
from pathlib import Path

# Add swarm-controller to path so agent_runtime can be imported
sys.path.insert(0, {repr(swarm_controller_dir)})
# Inject site-packages so dependencies (pyyaml, httpx, etc.) are always available
# regardless of which Python interpreter runs this script.
for _p in {repr(_extra_paths)}:
    if _p not in sys.path:
        sys.path.insert(1, _p)

import swarm.agent_runtime as rt

# Config
rt.WORKSPACE        = Path({repr(str(WORKSPACE))})
rt.DATA_DIR         = {repr(str(DATA_DIR))}
rt.PROJECT          = {repr(project)}
rt.PROJECT_PATH_OVERRIDE = {repr(str(project_path) if worktree_path_str else "")}
rt.WORKTREE_BRANCH      = {repr(worktree_branch_str)}
rt.TASK_TYPE        = {repr(task_type)}
rt.TASK_DESC        = {repr(description)}
rt.TASK_ID          = {repr(task_id)}
rt.TASK_PRIORITY    = {repr(task.get("priority", 50))}
rt.API_PORT         = {API_PORT}
rt.MAX_TOOL_LOOPS   = 200
rt.MAX_LINES        = {MAX_LINES}
rt.IGNORE_DIRS      = {repr(IGNORE_DIRS)}
rt.IGNORE_EXTENSIONS = {repr(IGNORE_EXTENSIONS)}
rt.MCP_SERVERS      = {json.dumps(MCP_SERVERS)}
rt.GODOT_PATH       = {repr(_godot_bin if godot_binary_available(_godot_bin) else "")}
rt.LLM_PROVIDER     = {repr(LLM_PROVIDER)}
rt.LLM_PROVIDERS    = {json.dumps(effective_llm_providers)}
rt.READONLY         = {repr(bool(metadata.get("readonly", False)))}
rt.MANAGED_PROJECTS = {json.dumps(_config.get("managed_projects", []))}
if rt.GODOT_PATH:
    os.environ.setdefault("GODOT_PATH", rt.GODOT_PATH)

# Prompts
rt.FEATURE_SYSTEM        = {repr(feature_system)}
rt.FEATURE_USER          = {repr(feature_user)}
rt.BUG_SYSTEM            = {repr(bug_system)}
rt.BUG_USER              = {repr(bug_user)}
rt.POLISH_SYSTEM         = {repr(polish_system)}
rt.POLISH_USER           = {repr(polish_user)}
rt.PYTHON_FEATURE_SYSTEM = {repr(python_feature_system)}
rt.PYTHON_FEATURE_USER   = {repr(python_feature_user)}
rt.PYTHON_BUG_SYSTEM        = {repr(python_bug_system)}
rt.PYTHON_BUG_USER          = {repr(python_bug_user)}
rt.PYTHON_REFACTOR_SYSTEM   = {repr(python_refactor_system)}
rt.PYTHON_REFACTOR_USER     = {repr(python_refactor_user)}
rt.PYTHON_PLAN_SYSTEM       = {repr(python_plan_system)}
rt.PYTHON_PLAN_USER         = {repr(python_plan_user)}
rt.PLAN_SYSTEM              = {repr(plan_system)}
rt.PLAN_USER                = {repr(plan_user)}
rt.MANAGER_SYSTEM           = ""
rt.MANAGER_USER             = ""
rt.PROJECT_CREATE_SYSTEM    = ""
rt.PROJECT_CREATE_USER      = ""
rt.QA_SYSTEM                = {repr(qa_system)}
rt.QA_USER                  = {repr(qa_user)}
rt.HYBRID_QA_SYSTEM         = {repr(hybrid_qa_system)}
rt.HYBRID_QA_USER           = {repr(hybrid_qa_user)}
rt.QA_CONFIG                = {repr(qa_config)}
rt.QA_CYCLE                 = {repr(qa_cycle)}
rt.QA_MAX_CYCLES            = {repr(qa_max_cycles)}
rt.META_INVESTIGATION_ENABLED  = {repr(meta_investigation_enabled)}
rt.META_INVESTIGATION_PROVIDER = {repr(meta_investigation_provider)}
rt.AUDIT_SYSTEM             = {repr(audit_system)}
rt.AUDIT_USER               = {repr(audit_user)}
rt.AUDIT_LEARNINGS_SYSTEM   = {repr(audit_learnings_system)}
rt.AUDIT_LEARNINGS_USER     = {repr(audit_learnings_user)}
rt.TRIAGE_SYSTEM            = {repr(triage_system)}
rt.TRIAGE_USER              = {repr(triage_user)}
rt.PROJECT_PLAN_SYSTEM      = {repr(project_plan_system)}
rt.PROJECT_PLAN_USER        = {repr(project_plan_user)}
rt.ART_PASS_SYSTEM          = {repr(art_pass_system)}
rt.ART_PASS_USER            = {repr(art_pass_user)}
rt.RESEARCH_SYSTEM          = {repr(research_system)}
rt.RESEARCH_USER            = {repr(research_user)}
rt.GARDENER_SYSTEM          = {repr(gardener_system)}
rt.GARDENER_USER            = {repr(gardener_user)}
rt.LIBRARIAN_SYSTEM         = {repr(librarian_system)}
rt.LIBRARIAN_USER           = {repr(librarian_user)}
rt.AUDITOR_SYSTEM           = {repr(auditor_system)}
rt.AUDITOR_USER             = {repr(auditor_user)}
rt.CARTOGRAPHER_SYSTEM      = {repr(cartographer_system)}
rt.CARTOGRAPHER_USER        = {repr(cartographer_user)}
rt.ARCHAEOLOGIST_SYSTEM     = {repr(archaeologist_system)}
rt.ARCHAEOLOGIST_USER       = {repr(archaeologist_user)}
rt.SCHEDULER_SYSTEM         = {repr(scheduler_system)}
rt.SCHEDULER_USER           = {repr(scheduler_user)}
rt.HARNESS_QA_SYSTEM        = {repr(harness_qa_system)}
rt.HARNESS_QA_USER          = {repr(harness_qa_user)}
rt.SCENARIO_QA_SYSTEM       = {repr(scenario_qa_system)}
rt.SCENARIO_QA_USER         = {repr(scenario_qa_user)}
rt.PLUGIN_SYSTEM            = {repr(_plugin_system)}
rt.PLUGIN_USER              = {repr(_plugin_user)}

if __name__ == "__main__":
    exit_code = rt.main()
    try:
        Path(__file__).with_suffix(".exit").write_text(str(exit_code))
    except Exception:
        pass
    sys.exit(exit_code)
"""
    return wrapper


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "api":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 5001
        from swarm.api import run_app
        run_app(port=port)
    else:
        print("Usage: python swarm_runner.py api [port]")
        sys.exit(1)
