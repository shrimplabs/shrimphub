"""Strategic model routing policy: task_type x phase x complexity -> tier.

This is the strategic layer for roadmap item #8 ("graduate model routing from
experiment to policy"). It sits above swarm.model_routing's tactical
tool-lookahead heuristic (choose_adaptive_flat_provider) and decides, for a
given project/task_type/phase/complexity combination:

- the tier ceiling ("fast" | "strong" | "either")
- whether intra-phase tool-lookahead adaptivity is even enabled
- which concrete fast/strong provider names to use

Backward compatibility is structural, not incidental: when config.json has no
`model_policy` section for a project, resolve_policy() falls through to the
project's existing legacy per-phase provider keys (`_work_provider` etc.),
reproducing today's single-fixed-provider-per-phase behavior exactly. Nothing
changes for any project until it opts in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Reproduces swarm.model_routing.STRONG_TASK_TYPES at complexity="normal" so that
# flat-loop routing decisions (tier_ceiling) are unchanged for every project
# that hasn't opted into a `model_policy` config section (see resolve_policy's
# fallback order).
#
# `adaptive` is ALWAYS False here, deliberately, regardless of task type or
# complexity. It gates whether a phase-loop caller (e.g. work.py, M4) attempts
# per-loop tool-lookahead routing instead of using one fixed provider for its
# whole internal loop -- a genuinely new capability that must be opt-in via an
# explicit `model_policy.projects.<project>` entry, not silently switched on
# for every project the moment M4 ships. It has no effect on the flat-loop
# path (M3), which never reads `adaptive` -- only `tier_ceiling`/`strong_bias`.
_STRONG_TASK_TYPE_ENTRY = {
    "simple": {"tier": "either", "adaptive": False},
    "normal": {"tier": "strong", "adaptive": False},
    "complex": {"tier": "strong", "adaptive": False},
}

DEFAULT_POLICY_TABLE: dict[str, Any] = {
    "*": {
        "*": {
            "simple": {"tier": "either", "adaptive": False},
            "normal": {"tier": "either", "adaptive": False},
            "complex": {"tier": "strong", "adaptive": False},
        }
    },
    "art_pass": {"*": dict(_STRONG_TASK_TYPE_ENTRY)},
    "polish": {"*": dict(_STRONG_TASK_TYPE_ENTRY)},
    "qa": {"*": dict(_STRONG_TASK_TYPE_ENTRY)},
    "harness_qa": {"*": dict(_STRONG_TASK_TYPE_ENTRY)},
    "hybrid_qa": {"*": dict(_STRONG_TASK_TYPE_ENTRY)},
    "scenario_qa": {"*": dict(_STRONG_TASK_TYPE_ENTRY)},
    "bug": {"*": dict(_STRONG_TASK_TYPE_ENTRY)},
}


@dataclass(frozen=True)
class PolicyDecision:
    tier_ceiling: str  # "fast" | "strong" | "either"
    adaptive_enabled: bool
    fast_provider: str
    strong_provider: str
    reason: str


def _normalize_complexity(complexity: str | None) -> str:
    c = str(complexity or "normal").strip().lower()
    return c if c in ("simple", "normal", "complex") else "normal"


def _lookup_table(table: dict[str, Any], task_type: str, phase: str, complexity: str) -> dict[str, Any] | None:
    """Most-specific-wins lookup: task_type -> '*' , phase -> '*' , complexity exact."""
    for tt_key in (task_type, "*"):
        tt_entry = table.get(tt_key)
        if not isinstance(tt_entry, dict):
            continue
        for phase_key in (phase, "*"):
            phase_entry = tt_entry.get(phase_key)
            if not isinstance(phase_entry, dict):
                continue
            entry = phase_entry.get(complexity)
            if isinstance(entry, dict):
                return entry
    return None


def legacy_fallback_provider(config: dict[str, Any] | None, project_pipeline_cfg: dict[str, Any] | None, phase: str) -> str | None:
    """Reproduce today's per-phase fixed-provider precedence.

    Mirrors swarm_runner.py's existing `<phase>_provider` resolution:
    project_pipelines.<project>._<phase>_provider -> config.<phase>_provider -> None.
    """
    cfg = config or {}
    proj_cfg = project_pipeline_cfg or {}
    phase_key = phase or "work"
    project_override = proj_cfg.get(f"_{phase_key}_provider")
    if project_override:
        return str(project_override)
    global_override = cfg.get(f"{phase_key}_provider")
    if global_override:
        return str(global_override)
    return None


def resolve_policy(
    config: dict[str, Any] | None,
    *,
    project: str = "",
    task_type: str = "",
    phase: str = "",
    complexity: str = "normal",
    default_provider: str = "",
) -> PolicyDecision:
    """Resolve a strategic routing policy for a task_type x phase x complexity slice.

    Lookup order (most-specific wins):
      model_policy.projects.<project>.<task_type>.<phase>.<complexity>
      model_policy.default.<task_type>.<phase>.<complexity>
      DEFAULT_POLICY_TABLE.<task_type>.<phase>.<complexity>

    If a project has a legacy per-phase provider key (`_work_provider` etc.) in
    project_pipelines but no `model_policy` entry at all, this collapses to
    "always use the legacy fixed provider, with no adaptive switching" --
    identical to today's phases/*.py behavior.
    """
    cfg = config or {}
    complexity = _normalize_complexity(complexity)
    phase = phase or "*"

    model_policy_cfg = cfg.get("model_policy") or {}
    project_table = (model_policy_cfg.get("projects") or {}).get(project) or {}
    default_table = model_policy_cfg.get("default") or {}

    entry = (
        _lookup_table(project_table, task_type, phase, complexity)
        or _lookup_table(default_table, task_type, phase, complexity)
        or _lookup_table(DEFAULT_POLICY_TABLE, task_type, phase, complexity)
    )

    project_pipelines = cfg.get("project_pipelines") or {}
    project_pipeline_cfg = project_pipelines.get(project) or {}
    loop_model_routing = {
        **(cfg.get("loop_model_routing") or {}),
        **(project_pipeline_cfg.get("_loop_model_routing") or {}),
    }
    fast_provider = str(loop_model_routing.get("fast_provider") or "")
    strong_provider = str(loop_model_routing.get("strong_provider") or "")

    # The flat/no-phase loop (pipeline_mode="adaptive_flat") is definitionally
    # already adaptive whenever it's running -- it always has an explicit
    # loop_model_routing config, so it must never collapse to the legacy
    # single-fixed-provider fallback the way a phase-loop caller can.
    legacy_provider = (
        None if phase == "flat"
        else legacy_fallback_provider(cfg, project_pipeline_cfg, phase if phase != "*" else "work")
    )

    if entry is None:
        # No policy entry anywhere (shouldn't normally happen since
        # DEFAULT_POLICY_TABLE has a "*"/"*" catch-all, but stay safe).
        entry = {"tier": "either", "adaptive": True}

    if legacy_provider and not (model_policy_cfg.get("projects") or {}).get(project):
        # Project has an old-style fixed-provider override and no explicit
        # model_policy entry of its own -- reproduce today's behavior exactly:
        # single fixed provider, no adaptivity, regardless of what the
        # (global/default) policy table would otherwise say.
        return PolicyDecision(
            tier_ceiling="either",
            adaptive_enabled=False,
            fast_provider=legacy_provider,
            strong_provider=legacy_provider,
            reason="legacy_fixed_provider",
        )

    resolved_tier_ceiling = str(entry.get("tier", "either"))
    reason = "policy_table"

    # Soft, safety-scoped quota enforcement (M5): only ever downgrades an
    # "either" ceiling (the ambiguous/ordinary-heuristic case) to "fast".
    # A ceiling of "strong" -- e.g. from complexity="complex" -- is a
    # deliberate quality decision and is NEVER touched by budget pressure.
    # This can only affect the "intent-setting"/"cheap-loop-cap"/
    # "no-prior-signal" branches in choose_adaptive_flat_provider -- it never
    # reaches the STRONG_TOOLS tool-lookahead branches, which don't consult
    # tier_ceiling/strong_bias at all.
    if resolved_tier_ceiling == "either":
        quota = check_project_quota_cap(project, cfg)
        if quota.over_cap:
            resolved_tier_ceiling = "fast"
            reason = "policy_table_quota_capped"

    return PolicyDecision(
        tier_ceiling=resolved_tier_ceiling,
        adaptive_enabled=bool(entry.get("adaptive", True)),
        fast_provider=fast_provider or strong_provider or default_provider,
        strong_provider=strong_provider or default_provider,
        reason=reason,
    )


@dataclass(frozen=True)
class ProjectQuotaStatus:
    over_cap: bool
    strong_loops_today: int
    cap: int | None
    pct_used: float


def check_project_quota_cap(project: str, config: dict[str, Any] | None) -> ProjectQuotaStatus:
    """Check whether a project has exceeded its configured strong-loop budget.

    Never raises; returns safe (uncapped) defaults on any error. Returns
    over_cap=False, cap=None when the project has no configured cap -- the
    default for every project until config.json's `project_quota_caps`
    section is explicitly populated for it (opt-in, like everything else in
    this module).
    """
    try:
        cfg = config or {}
        caps = (cfg.get("project_quota_caps") or {}).get(project) or {}
        cap = caps.get("max_strong_loops_per_day")
        if not cap:
            return ProjectQuotaStatus(over_cap=False, strong_loops_today=0, cap=None, pct_used=0.0)

        from datetime import datetime, timezone
        import swarm.db as db

        today = datetime.now(timezone.utc).date().isoformat()
        row = db.project_tier_usage_get(project, today)
        strong_loops_today = int((row or {}).get("strong_loops", 0))
        cap = int(cap)
        pct_used = (strong_loops_today / cap * 100.0) if cap > 0 else 0.0
        return ProjectQuotaStatus(
            over_cap=strong_loops_today >= cap,
            strong_loops_today=strong_loops_today,
            cap=cap,
            pct_used=pct_used,
        )
    except Exception:
        return ProjectQuotaStatus(over_cap=False, strong_loops_today=0, cap=None, pct_used=0.0)
