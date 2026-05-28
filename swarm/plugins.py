"""
swarm.plugins -- Agent profile plugin loader.

Plugins live in <swarm-root>/plugins/*.yaml and let operators add or override
agent behaviour for a task_type without touching swarm internals.

Schema (all fields optional except plugin_id and task_type):

    plugin_id: "my_plugin"
    task_type: "my_task_type"          # must be unique across all plugins
    display_name: "My Plugin"          # shown in logs / dashboard
    role: "implementation"             # implementation | qa | research | planning
    permission_profile: "full"         # full | read_write | read_only | qa_write
    prompt_file: "plugins/my_task.yaml"  # relative to swarm root; must have system+user keys
    tools_allowed: []                  # if non-empty: only these tools are available
    tools_blocked: []                  # always wins over tools_allowed
    context_providers:
      - type: file
        path: "GAME_DESIGN.md"         # relative to project root
        max_chars: 8000
      - type: command
        command: "python -m pytest --tb=no -q 2>&1 | tail -20"
        timeout_seconds: 10            # hard cap: 30s
        max_chars: 4000
      - type: http
        url: "http://localhost:8080/status"  # GET only
        max_chars: 2000

Plugins are loaded once at startup via load_plugins().
Hot-reload is intentionally not supported (restart the server to pick up changes).
"""

from __future__ import annotations

import subprocess
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

_VALID_ROLES = {"implementation", "qa", "research", "planning"}
_VALID_PROFILES = {"full", "read_write", "read_only", "qa_write"}
_VALID_PROVIDER_TYPES = {"file", "command", "http"}
_MAX_COMMAND_TIMEOUT = 30


@dataclass
class ContextProvider:
    type: str                          # file | command | http
    # file
    path: str = ""
    # command
    command: str = ""
    timeout_seconds: int = 10
    # http
    url: str = ""
    # shared
    max_chars: int = 4000


@dataclass
class AgentPlugin:
    plugin_id: str
    task_type: str
    display_name: str = ""
    role: str = "implementation"
    permission_profile: str = "full"
    prompt_file: str = ""              # relative to swarm root; empty = use built-in
    tools_allowed: list[str] = field(default_factory=list)
    tools_blocked: list[str] = field(default_factory=list)
    context_providers: list[ContextProvider] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_plugins: dict[str, AgentPlugin] = {}   # task_type -> AgentPlugin
_plugins_root: Path = Path()


def load_plugins(swarm_root: Optional[Path] = None) -> dict[str, AgentPlugin]:
    """Load all YAML plugin files from <swarm_root>/plugins/.

    Called once at startup by swarm.api.create_app().
    Returns the task_type -> AgentPlugin mapping (also stored in module-level registry).
    """
    global _plugins, _plugins_root

    if swarm_root is None:
        swarm_root = Path(__file__).parent.parent

    _plugins_root = swarm_root
    plugins_dir = swarm_root / "plugins"
    _plugins = {}

    if not plugins_dir.exists():
        print("[Plugins] No plugins/ directory found — skipping plugin load")
        return _plugins

    try:
        import yaml
    except ImportError:
        print("[Plugins] PyYAML not installed — cannot load plugins")
        return _plugins

    for yaml_file in sorted(plugins_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            plugin = _parse_plugin(data, yaml_file)
            if plugin:
                if plugin.task_type in _plugins:
                    print(
                        f"[Plugins] WARNING: duplicate task_type '{plugin.task_type}' "
                        f"in {yaml_file.name} — ignoring (existing: {_plugins[plugin.task_type].plugin_id})"
                    )
                else:
                    _plugins[plugin.task_type] = plugin
                    print(f"[Plugins] Loaded plugin '{plugin.plugin_id}' → task_type='{plugin.task_type}'")
        except Exception as exc:
            print(f"[Plugins] Error loading {yaml_file.name}: {exc}")

    return _plugins


def get_plugin(task_type: str) -> Optional[AgentPlugin]:
    """Return the plugin for *task_type*, or None if none is registered."""
    return _plugins.get(task_type)


def all_plugins() -> dict[str, AgentPlugin]:
    """Return the full task_type -> AgentPlugin mapping (read-only view)."""
    return dict(_plugins)


# ---------------------------------------------------------------------------
# Context resolution
# ---------------------------------------------------------------------------

def resolve_context(plugin: AgentPlugin, project_path: Path) -> str:
    """Run all context providers for *plugin* and return the rendered block.

    Each provider's output is logged (truncated) and concatenated under a
    labelled header.  Errors are caught per-provider and logged but do not
    abort the others.
    """
    if not plugin.context_providers:
        return ""

    sections: list[str] = []

    for i, provider in enumerate(plugin.context_providers, start=1):
        label = _provider_label(provider, i)
        try:
            raw = _run_provider(provider, project_path)
        except Exception as exc:
            print(f"[Plugins:{plugin.plugin_id}] context provider {i} ({provider.type}) error: {exc}")
            raw = f"(provider error: {exc})"

        if raw:
            capped = raw[: provider.max_chars]
            if len(raw) > provider.max_chars:
                capped += "\n...(truncated)"
            preview = capped[:120].replace("\n", " ")
            print(f"[Plugins:{plugin.plugin_id}] provider {i} '{label}': {len(capped)} chars — {preview!r}")
            sections.append(f"### {label}\n{capped}")

    if not sections:
        return ""

    return (
        f"\n## PLUGIN CONTEXT ({plugin.display_name or plugin.plugin_id})\n"
        + "\n\n".join(sections)
        + "\n"
    )


# ---------------------------------------------------------------------------
# Permission enforcement helper
# ---------------------------------------------------------------------------

# Maps permission_profile -> set of blocked tools.
# Tools NOT in this set are allowed.
# tools_blocked in the plugin spec always additionally blocks.
_PROFILE_BLOCKED: dict[str, frozenset[str]] = {
    "full": frozenset(),
    "read_write": frozenset({"run_command"}),
    "read_only": frozenset({
        "write_file", "patch_file", "append_file",
        "git_commit", "git_push", "run_command",
        "create_task", "create_tasks", "create_tasks_file_aware",
    }),
    "qa_write": frozenset({
        "patch_file", "append_file",
        "git_commit", "git_push", "run_command",
        "create_task", "create_tasks", "create_tasks_file_aware",
    }),
}


def is_tool_blocked_by_plugin(plugin: AgentPlugin, tool: str) -> bool:
    """Return True if *tool* is blocked by the plugin's permission profile or tools_blocked list.

    tools_blocked always wins. If tools_allowed is non-empty and the tool is not
    in it, it is also blocked (allowlist mode).
    """
    # Profile-level blocks
    profile_blocked = _PROFILE_BLOCKED.get(plugin.permission_profile, frozenset())
    if tool in profile_blocked:
        return True

    # Explicit blocklist (always wins)
    if tool in plugin.tools_blocked:
        return True

    # Allowlist mode: if tools_allowed is non-empty and tool is absent → blocked
    if plugin.tools_allowed and tool not in plugin.tools_allowed:
        return True

    return False


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

def load_plugin_prompt(plugin: AgentPlugin, swarm_root: Optional[Path] = None, **vars) -> Optional[tuple[str, str]]:
    """Load and render the plugin's prompt_file.

    Returns (system, user) strings, or None if no prompt_file is configured.
    """
    if not plugin.prompt_file:
        return None

    root = swarm_root or _plugins_root or Path(__file__).parent.parent
    prompt_path = root / plugin.prompt_file

    if not prompt_path.exists():
        print(f"[Plugins:{plugin.plugin_id}] prompt_file not found: {prompt_path}")
        return None

    try:
        import yaml
        from jinja2 import Environment, FileSystemLoader, StrictUndefined

        prompts_dir = root / "prompts"
        data = yaml.safe_load(prompt_path.read_text(encoding="utf-8"))

        env = Environment(
            loader=FileSystemLoader(str(prompts_dir)),
            variable_start_string="<<",
            variable_end_string=">>",
            block_start_string="<%",
            block_end_string="%>",
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        system = env.from_string(data.get("system", "")).render(**vars).strip()
        user = env.from_string(data.get("user", "")).render(**vars).strip()
        return system, user
    except Exception as exc:
        print(f"[Plugins:{plugin.plugin_id}] error rendering prompt: {exc}")
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_plugin(data: Any, source: Path) -> Optional[AgentPlugin]:
    if not isinstance(data, dict):
        print(f"[Plugins] {source.name}: top-level must be a mapping, got {type(data).__name__}")
        return None

    plugin_id = str(data.get("plugin_id") or "").strip()
    task_type = str(data.get("task_type") or "").strip()

    if not plugin_id:
        print(f"[Plugins] {source.name}: missing required field 'plugin_id'")
        return None
    if not task_type:
        print(f"[Plugins] {source.name}: missing required field 'task_type'")
        return None

    role = str(data.get("role") or "implementation").strip()
    if role not in _VALID_ROLES:
        print(f"[Plugins] {source.name}: unknown role '{role}' — defaulting to 'implementation'")
        role = "implementation"

    profile = str(data.get("permission_profile") or "full").strip()
    if profile not in _VALID_PROFILES:
        print(f"[Plugins] {source.name}: unknown permission_profile '{profile}' — defaulting to 'full'")
        profile = "full"

    tools_allowed = [str(t) for t in (data.get("tools_allowed") or [])]
    tools_blocked = [str(t) for t in (data.get("tools_blocked") or [])]

    providers: list[ContextProvider] = []
    for raw in (data.get("context_providers") or []):
        if not isinstance(raw, dict):
            continue
        ptype = str(raw.get("type") or "").strip()
        if ptype not in _VALID_PROVIDER_TYPES:
            print(f"[Plugins] {source.name}: unknown context_provider type '{ptype}' — skipping")
            continue
        timeout = min(int(raw.get("timeout_seconds") or 10), _MAX_COMMAND_TIMEOUT)
        providers.append(ContextProvider(
            type=ptype,
            path=str(raw.get("path") or ""),
            command=str(raw.get("command") or ""),
            timeout_seconds=timeout,
            url=str(raw.get("url") or ""),
            max_chars=int(raw.get("max_chars") or 4000),
        ))

    return AgentPlugin(
        plugin_id=plugin_id,
        task_type=task_type,
        display_name=str(data.get("display_name") or plugin_id),
        role=role,
        permission_profile=profile,
        prompt_file=str(data.get("prompt_file") or ""),
        tools_allowed=tools_allowed,
        tools_blocked=tools_blocked,
        context_providers=providers,
    )


def _provider_label(provider: ContextProvider, index: int) -> str:
    if provider.type == "file":
        return provider.path or f"file-{index}"
    if provider.type == "command":
        cmd_short = (provider.command or "")[:40]
        return f"command: {cmd_short}"
    if provider.type == "http":
        return provider.url or f"http-{index}"
    return f"provider-{index}"


def _run_provider(provider: ContextProvider, project_path: Path) -> str:
    if provider.type == "file":
        path = project_path / provider.path if not Path(provider.path).is_absolute() else Path(provider.path)
        if not path.exists():
            return f"(file not found: {provider.path})"
        return path.read_text(encoding="utf-8", errors="replace")

    if provider.type == "command":
        result = subprocess.run(
            provider.command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(project_path),
            timeout=provider.timeout_seconds,
            env=None,  # inherit env but run in project dir, not swarm root
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        return output.strip()

    if provider.type == "http":
        req = urllib.request.Request(provider.url, method="GET")
        with urllib.request.urlopen(req, timeout=provider.timeout_seconds) as resp:
            return resp.read().decode("utf-8", errors="replace")

    return ""
