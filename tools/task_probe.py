#!/usr/bin/env python3
"""Shared task probe runner and CLI dispatcher.

Usage:
    .venv/bin/python tools/task_probe.py qa anti-grav-rush
    .venv/bin/python tools/task_probe.py feature my-project

Probe modules are expected to live next to this file as ``<name>_probe.py`` and
export one of:
    - build_probe(project_name, project_path, config) -> ProbeRunner
    - build_steps(project_name, project_path, config) -> list[ProbeStep]
    - run_probes(project_name, project_path, config)  # legacy compatibility
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


SWARM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SWARM_ROOT))
if __name__ == "__main__":
    sys.modules.setdefault("tools.task_probe", sys.modules[__name__])


class Result:
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"

    ALL = {PASS, WARN, FAIL}


@dataclass(frozen=True)
class ProbeOutcome:
    status: str
    detail: str = ""

    def __post_init__(self):
        if self.status not in Result.ALL:
            raise ValueError(f"unknown probe status: {self.status}")


@dataclass(frozen=True)
class ProbeStep:
    name: str
    run: Callable[[], ProbeOutcome | tuple[bool, str] | tuple[str, str] | bool]
    title: str | None = None


def pass_(detail: str = "") -> ProbeOutcome:
    return ProbeOutcome(Result.PASS, detail)


def warn(detail: str = "") -> ProbeOutcome:
    return ProbeOutcome(Result.WARN, detail)


def fail(detail: str = "") -> ProbeOutcome:
    return ProbeOutcome(Result.FAIL, detail)


class ProbeResults:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def record(self, probe: str, status: str, note: str = ""):
        self.rows.append((probe, status, note))
        icon = {Result.PASS: "✓", Result.WARN: "⚠", Result.FAIL: "✗"}.get(status, "?")
        colour = {
            Result.PASS: "\033[32m",
            Result.WARN: "\033[33m",
            Result.FAIL: "\033[31m",
        }.get(status, "")
        reset = "\033[0m"
        print(f"  {colour}{icon} {status}{reset}  {note}")

    def counts(self) -> tuple[int, int, int, int]:
        total = len(self.rows)
        passed = sum(1 for _, status, _ in self.rows if status == Result.PASS)
        warned = sum(1 for _, status, _ in self.rows if status == Result.WARN)
        failed = sum(1 for _, status, _ in self.rows if status == Result.FAIL)
        return total, passed, warned, failed

    def summary(self):
        print()
        print("=" * 60)
        print("SUMMARY")
        print("=" * 60)
        for probe, status, note in self.rows:
            icon = {Result.PASS: "✓", Result.WARN: "⚠", Result.FAIL: "✗"}.get(status, "?")
            print(f"  {icon} {probe:<30}  {status}  {note[:60]}")
        total, passed, warned, failed = self.counts()
        print(f"\n  {passed}/{total} passed, {warned} warnings, {failed} failures")


class ProbeRunner:
    def __init__(
        self,
        steps: Iterable[ProbeStep],
        project: str,
        config: dict,
        *,
        cleanup: Callable[[], None] | None = None,
        max_steps: int | None = None,
    ):
        self.steps = list(steps)
        if max_steps is not None:
            self.steps = self.steps[:max_steps]
        self.project = project
        self.config = config
        self.cleanup = cleanup
        self.results = ProbeResults()

    def run_all(self) -> bool:
        try:
            for index, step in enumerate(self.steps, start=1):
                print()
                print(f"Probe {index} — {step.title or _title_from_name(step.name)}")
                outcome = self._run_step(step)
                self.results.record(step.name, outcome.status, outcome.detail)
        finally:
            if self.cleanup is not None:
                self.cleanup()

        self.results.summary()
        _, _, _, failed = self.results.counts()
        return failed == 0

    def _run_step(self, step: ProbeStep) -> ProbeOutcome:
        try:
            return _normalize_outcome(step.run())
        except Exception as exc:
            return fail(f"{type(exc).__name__}: {exc}")


def load_config() -> dict:
    cfg_path = SWARM_ROOT / "config.json"
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text())
        except Exception as exc:
            print(f"[warn] Could not read config.json: {exc}")
    return {}


def resolve_project_path(project_name: str, config: dict) -> Path:
    workspace = config.get("workspace", "~/workspace")
    return Path(os.path.expanduser(workspace)) / project_name


def build_runner(
    probe_name: str,
    project_name: str,
    project_path: Path,
    config: dict,
    *,
    max_steps: int | None = None,
) -> ProbeRunner | Callable[[], bool]:
    module = _load_probe_module(probe_name)

    if hasattr(module, "build_probe"):
        runner = module.build_probe(project_name, project_path, config)
        if not isinstance(runner, ProbeRunner):
            raise TypeError(f"{module.__name__}.build_probe() must return ProbeRunner")
        if max_steps is not None:
            runner.steps = runner.steps[:max_steps]
        return runner

    if hasattr(module, "build_steps"):
        steps = module.build_steps(project_name, project_path, config)
        return ProbeRunner(steps, project_name, config, max_steps=max_steps)

    if hasattr(module, "run_probes"):
        if max_steps is not None:
            raise SystemExit("--steps is only supported by probes using ProbeRunner")

        def _legacy() -> bool:
            module.run_probes(project_name, project_path, config)
            return True

        return _legacy

    raise AttributeError(
        f"{module.__name__} must expose build_probe(), build_steps(), or run_probes()"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a task-type diagnostic probe.")
    parser.add_argument("probe", help="Probe name, e.g. qa, feature, plan")
    parser.add_argument("project", help="Managed project name")
    parser.add_argument("--steps", type=int, default=None, help="Run only the first N steps")
    args = parser.parse_args(argv)

    config = load_config()
    project_path = resolve_project_path(args.project, config)

    if not project_path.exists():
        print(f"Error: project path does not exist: {project_path}")
        return 1

    print(f"{args.probe}_probe — {args.project}")
    print(f"  path: {project_path}")
    print(f"  config: {SWARM_ROOT / 'config.json'}")

    runner = build_runner(
        args.probe,
        args.project,
        project_path,
        config,
        max_steps=args.steps,
    )
    ok = runner() if callable(runner) and not isinstance(runner, ProbeRunner) else runner.run_all()
    return 0 if ok else 1


def _load_probe_module(probe_name: str):
    safe_name = probe_name.strip().replace("-", "_")
    if safe_name.endswith("_probe"):
        safe_name = safe_name[:-6]
    candidates = [f"tools.{safe_name}_probe", f"{safe_name}_probe"]
    if safe_name == "qa":
        candidates.append("tools.qa_probe")

    errors = []
    for name in dict.fromkeys(candidates):
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as exc:
            errors.append(f"{name}: {exc}")
    raise ModuleNotFoundError(
        f"No probe module found for {probe_name!r}. Tried: {', '.join(dict.fromkeys(candidates))}"
    )


def _normalize_outcome(value) -> ProbeOutcome:
    if isinstance(value, ProbeOutcome):
        return value
    if isinstance(value, bool):
        return pass_() if value else fail()
    if isinstance(value, tuple) and len(value) == 2:
        first, detail = value
        if isinstance(first, bool):
            return ProbeOutcome(Result.PASS if first else Result.FAIL, str(detail))
        if isinstance(first, str):
            status = first.upper()
            return ProbeOutcome(status, str(detail))
    raise TypeError(
        "probe step must return ProbeOutcome, bool, (passed, detail), or (status, detail)"
    )


def _title_from_name(name: str) -> str:
    parts = name.split("-", 1)
    raw = parts[1] if len(parts) == 2 and parts[0].isdigit() else name
    return raw.replace("-", " ").replace("_", " ").title()


if __name__ == "__main__":
    raise SystemExit(main())
