"""Experiment metadata propagation helpers."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Mapping


PIPELINE_PHASES = ["plan", "scout", "work", "validate"]


def classify_phase_order(phases: list[str] | None) -> dict[str, Any]:
    if not isinstance(phases, list):
        return {"is_valid_order": None, "invalidity_reason": ""}
    reasons: list[str] = []
    positions = {phase: i for i, phase in enumerate(phases)}
    if "validate" in positions and "work" in positions and positions["validate"] < positions["work"]:
        reasons.append("validate_before_work")
    if "synthesize" in positions and "scout" in positions and positions["synthesize"] < positions["scout"]:
        reasons.append("synthesize_before_scout")
    if "synthesize" in positions and "work" in positions and positions["synthesize"] > positions["work"]:
        reasons.append("synthesize_after_work")
    return {
        "is_valid_order": not reasons,
        "invalidity_reason": ",".join(reasons),
    }


def load_config(config: Mapping[str, Any] | None = None, config_file: str | Path | None = None) -> dict[str, Any]:
    if isinstance(config, Mapping):
        return dict(config)
    path = Path(config_file) if config_file else Path("config.json")
    try:
        if path.exists():
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                return loaded
    except Exception:
        return {}
    return {}


def experiment_defaults_for_project(
    project_name: str,
    *,
    config: Mapping[str, Any] | None = None,
    config_file: str | Path | None = None,
) -> dict[str, Any]:
    if not project_name:
        return {}
    cfg = load_config(config=config, config_file=config_file)
    project_cfg = (cfg.get("project_pipelines") or {}).get(project_name) or {}
    experiment = project_cfg.get("_experiment")
    return dict(experiment) if isinstance(experiment, Mapping) else {}


STABLE_RECOVERY_PIPELINE: list[str] = ["plan", "work", "validate"]


def stamp_experiment_metadata(
    project_name: str,
    metadata: Mapping[str, Any] | None,
    *,
    config: Mapping[str, Any] | None = None,
    config_file: str | Path | None = None,
    stable: bool = False,
) -> dict[str, Any]:
    """Apply project-level experiment defaults to a newly-created task.

    stable=True: record experiment labels for analysis but pin execution to
    STABLE_RECOVERY_PIPELINE regardless of the project's variant. Use for
    recovery tasks, validation bug tasks, auto-QA/polish, closure repair —
    any task that must not inherit chaos/experimental phase ordering.
    """
    meta = dict(metadata or {})
    if meta.get("experiment_variant"):
        return meta

    experiment = experiment_defaults_for_project(project_name, config=config, config_file=config_file)
    if not experiment:
        return meta

    variant = experiment.get("experiment_variant", "")
    pipeline_mode = experiment.get("pipeline_mode", "")
    meta["experiment_id"] = experiment.get("experiment_id", "")
    meta["experiment_arm"] = experiment.get("experiment_arm", "")
    meta["experiment_variant"] = variant

    if experiment.get("source_project") and not meta.get("source_project"):
        meta["source_project"] = experiment.get("source_project")
    if not meta.get("source_task_id"):
        meta["source_task_id"] = meta.get("parent_task_id") or meta.get("parent_task") or ""

    if stable:
        # Record the variant for analysis but don't inherit randomized ordering
        meta["pipeline"] = list(STABLE_RECOVERY_PIPELINE)
        meta["pipeline_variant"] = list(STABLE_RECOVERY_PIPELINE)
        meta["phase_order"] = list(STABLE_RECOVERY_PIPELINE)
        meta["recovery_pipeline_override"] = True
        meta["recovery_pipeline_reason"] = "stable_recovery"
        meta.pop("phase_random_seed", None)
        meta.update(classify_phase_order(STABLE_RECOVERY_PIPELINE))
    elif pipeline_mode == "random":
        phases = list(experiment.get("phase_pool") or PIPELINE_PHASES)
        seed = random.randint(0, 2**31 - 1)
        random.Random(seed).shuffle(phases)
        meta["pipeline"] = phases
        meta["pipeline_variant"] = phases
        meta["phase_order"] = phases
        meta["phase_random_seed"] = seed
        meta.update(classify_phase_order(phases))
    elif pipeline_mode == "flat":
        meta["pipeline"] = []
        meta["pipeline_variant"] = []
        meta["phase_order"] = []
        if experiment.get("flat_provider"):
            meta["flat_provider"] = experiment.get("flat_provider")
    elif isinstance(experiment.get("pipeline"), list):
        phases = list(experiment["pipeline"])
        meta["pipeline"] = phases
        meta["pipeline_variant"] = phases
        meta["phase_order"] = phases
        meta.update(classify_phase_order(phases))

    return meta
