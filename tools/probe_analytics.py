"""Analytics instrumentation for pipeline-probe.py.

Extracted as a module so it can be unit-tested independently of the
top-level script. Import and call ``install(analytics)`` before running
a pipeline to activate tracking.
"""
import re
import time
from typing import Any


# ---------------------------------------------------------------------------
# Data structure
# ---------------------------------------------------------------------------

def make_analytics() -> dict:
    """Return a fresh analytics accumulator."""
    return {
        "phases": {},         # phase_name → PhaseStats dict
        "_current_phase": None,
    }


def make_phase_stats() -> dict:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
        "tools": {},
        "elapsed_s": 0.0,
        "_t0": time.time(),
    }


# ---------------------------------------------------------------------------
# Tracking helpers
# ---------------------------------------------------------------------------

def track_phase(analytics: dict, name: str) -> None:
    """Mark *name* as the currently-active phase."""
    analytics["_current_phase"] = name
    if name not in analytics["phases"]:
        analytics["phases"][name] = make_phase_stats()


def record_tokens(analytics: dict, tokens: dict) -> None:
    """Add token counts from an LLM response to the current phase."""
    phase = analytics["_current_phase"]
    if not phase or phase not in analytics["phases"]:
        return
    p = analytics["phases"][phase]
    if not isinstance(tokens, dict):
        return
    p["calls"] += 1
    p["input_tokens"]  += tokens.get("input",  tokens.get("input_tokens", 0))
    p["output_tokens"] += tokens.get("output", tokens.get("output_tokens", 0))
    p["cache_read"]    += tokens.get("cache_read",  tokens.get("cache_read_input_tokens", 0))
    p["cache_write"]   += tokens.get("cache_write", tokens.get("cache_creation_input_tokens", 0))


def record_tool_calls(analytics: dict, phase_name: str, tools_str: str) -> None:
    """Parse a comma/space-separated tool list and increment counters."""
    p = analytics["phases"].get(phase_name)
    if not p:
        return
    for tool_name in re.split(r"[,\s]+", tools_str.strip()):
        tool_name = tool_name.strip()
        if tool_name:
            p["tools"][tool_name] = p["tools"].get(tool_name, 0) + 1


def handle_log_line(analytics: dict, msg: str) -> None:
    """Parse a pipeline log line and update analytics state.

    Handles:
    - ``  PHASE: PLAN  (1/4)``  → start tracking phase
    - ``  ✓ PLAN complete (12.3s)``  → record elapsed time
    - ``Tools: read_file, search_code``  → record tool calls (via Phase.log patch)
    """
    # Phase start
    m_start = re.search(r"PHASE:\s*([A-Z_]+)", msg)
    if m_start:
        track_phase(analytics, m_start.group(1).lower())

    # Phase complete — elapsed recorded by pipeline's log_fn
    m_done = re.search(r"✓\s+([A-Z_]+)\s+complete\s+\(([0-9.]+)s\)", msg)
    if m_done:
        phase = m_done.group(1).lower()
        elapsed_s = float(m_done.group(2))
        if phase in analytics["phases"]:
            analytics["phases"][phase]["elapsed_s"] = elapsed_s

    # Tool list from Phase.log (stripped of "[Pipeline:plan] " prefix)
    m_tools = re.search(r"^Tools:\s+(.+)", msg)
    if m_tools:
        phase = analytics["_current_phase"]
        if phase:
            record_tool_calls(analytics, phase, m_tools.group(1))


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

def totals(analytics: dict) -> dict:
    phases = analytics["phases"]
    return {
        "calls":        sum(p["calls"]         for p in phases.values()),
        "input_tokens": sum(p["input_tokens"]  for p in phases.values()),
        "output_tokens":sum(p["output_tokens"] for p in phases.values()),
        "cache_read":   sum(p["cache_read"]    for p in phases.values()),
    }


# ---------------------------------------------------------------------------
# Install patches
# ---------------------------------------------------------------------------

def install(analytics: dict) -> None:
    """Monkey-patch swarm internals to feed data into *analytics*.

    Safe to call multiple times — idempotent via sentinel attribute.
    """
    import swarm.llm_utils as _llm_mod
    import swarm.pipeline as _pipeline_mod
    from swarm.pipeline import Phase as _Phase

    if getattr(_llm_mod, "_probe_analytics_installed", False):
        return
    _llm_mod._probe_analytics_installed = True

    # 1. LLM call token tracking
    _orig_call_llm = _llm_mod.call_llm

    def _instrumented_call_llm(system, messages, **kwargs):
        text, tokens, thinking = _orig_call_llm(system, messages, **kwargs)
        record_tokens(analytics, tokens if isinstance(tokens, dict) else {})
        return text, tokens, thinking

    _llm_mod.call_llm = _instrumented_call_llm

    # 2. run_pipeline log_fn interception (phase start/complete from pipeline log)
    _orig_run_pipeline = _pipeline_mod.run_pipeline

    def _instrumented_run_pipeline(phases, state, provider_cfg, log_fn=None):
        _orig_log = log_fn or (lambda x: None)

        def _phase_tracking_log(msg):
            handle_log_line(analytics, msg)
            _orig_log(msg)

        return _orig_run_pipeline(phases, state, provider_cfg, log_fn=_phase_tracking_log)

    _pipeline_mod.run_pipeline = _instrumented_run_pipeline

    # 3. Phase.log patch — captures "Tools: ..." lines that go direct to stdout
    _orig_phase_log = _Phase.log

    def _analytics_phase_log(self, msg: str) -> None:
        _orig_phase_log(self, msg)
        if self.name not in analytics["phases"]:
            track_phase(analytics, self.name)
        handle_log_line(analytics, msg)

    _Phase.log = _analytics_phase_log


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def print_table(analytics: dict) -> None:
    """Print a human-readable per-phase analytics table to stdout."""
    if not analytics["phases"]:
        return
    t = totals(analytics)
    print(f"\n{'='*60}")
    print(f"  ANALYTICS")
    print(f"{'='*60}")
    print(f"  {'Phase':<16} {'Calls':>5}  {'In tok':>8}  {'Out tok':>8}  {'Cache':>7}  {'Time':>7}  Top tools")
    print(f"  {'-'*16} {'-'*5}  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*7}  ---------")
    for pname, ps in analytics["phases"].items():
        top = ", ".join(
            f"{tool}×{n}" for tool, n in sorted(ps["tools"].items(), key=lambda x: -x[1])[:3]
        )
        print(
            f"  {pname:<16} {ps['calls']:>5}  {ps['input_tokens']:>8,}  "
            f"{ps['output_tokens']:>8,}  {ps['cache_read']:>7,}  "
            f"{ps['elapsed_s']:>6.1f}s  {top}"
        )
    print(
        f"  {'TOTAL':<16} {t['calls']:>5}  {t['input_tokens']:>8,}  "
        f"{t['output_tokens']:>8,}  {t['cache_read']:>7,}"
    )
    if t["input_tokens"] > 0:
        pct = 100 * t["cache_read"] / t["input_tokens"]
        print(f"\n  Cache hit rate: {pct:.1f}%  (~{t['cache_read'] * 0.9:,.0f} tok discount)")
    print(f"{'='*60}")


def build_summary(analytics: dict, *, task_id: str, project: str, task_type: str,
                  pipeline: list, provider: str | None, elapsed_s: float,
                  final_state: Any) -> dict:
    """Return a JSON-serialisable summary dict."""
    t = totals(analytics)
    scout_report = getattr(final_state, "scout_report", None) or {}
    synthesis    = getattr(final_state, "synthesis", None) or {}
    work_report  = getattr(final_state, "work_report", None) or {}

    return {
        "task_id":                task_id,
        "project":                project,
        "task_type":              task_type,
        "pipeline":               pipeline,
        "provider":               provider,
        "elapsed_s":              elapsed_s,
        "failed":                 bool(getattr(final_state, "failed", False)),
        "errors":                 list(getattr(final_state, "errors", None) or []),
        "total_llm_calls":        t["calls"],
        "total_input_tokens":     t["input_tokens"],
        "total_output_tokens":    t["output_tokens"],
        "total_cache_read_tokens":t["cache_read"],
        "phases": {
            pname: {
                "calls":             ps["calls"],
                "input_tokens":      ps["input_tokens"],
                "output_tokens":     ps["output_tokens"],
                "cache_read_tokens": ps["cache_read"],
                "elapsed_s":         ps["elapsed_s"],
                "tools":             ps["tools"],
            }
            for pname, ps in analytics["phases"].items()
        },
        "work": work_report or None,
        "scout": {
            "files_inspected": len(scout_report.get("findings", [])),
            "findings_count":  len(scout_report.get("findings", [])),
        } if scout_report else None,
        "synthesis": {
            "tasks_proposed": len(synthesis.get("proposed_tasks", None) or []),
            "confidence":     synthesis.get("confidence"),
        } if synthesis else None,
    }
