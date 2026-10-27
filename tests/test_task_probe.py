import pytest

import tools.task_probe as task_probe
from tools.task_probe import ProbeOutcome, ProbeRunner, ProbeStep, Result, fail, pass_, warn


def test_probe_runner_continues_after_failure_and_reports_summary(capsys):
    calls = []

    def first():
        calls.append("first")
        return pass_("ready")

    def second():
        calls.append("second")
        return fail("broken")

    def third():
        calls.append("third")
        return warn("degraded")

    runner = ProbeRunner(
        [
            ProbeStep("1-first", first, title="First"),
            ProbeStep("2-second", second, title="Second"),
            ProbeStep("3-third", third, title="Third"),
        ],
        "demo",
        {},
    )

    assert runner.run_all() is False
    assert calls == ["first", "second", "third"]

    out = capsys.readouterr().out
    assert "Probe 1 — First" in out
    assert "✓ 1-first" in out
    assert "✗ 2-second" in out
    assert "⚠ 3-third" in out
    assert "1/3 passed, 1 warnings, 1 failures" in out


def test_probe_runner_treats_warnings_as_success(capsys):
    runner = ProbeRunner([ProbeStep("1-warn", lambda: warn("optional"))], "demo", {})

    assert runner.run_all() is True
    assert "0 failures" in capsys.readouterr().out


def test_probe_runner_normalizes_tuple_results():
    runner = ProbeRunner(
        [
            ProbeStep("1-bool-pass", lambda: (True, "ok")),
            ProbeStep("2-status-warn", lambda: ("WARN", "soft")),
            ProbeStep("3-outcome", lambda: ProbeOutcome(Result.PASS, "done")),
        ],
        "demo",
        {},
    )

    assert runner.run_all() is True
    assert runner.results.rows == [
        ("1-bool-pass", Result.PASS, "ok"),
        ("2-status-warn", Result.WARN, "soft"),
        ("3-outcome", Result.PASS, "done"),
    ]


def test_probe_runner_records_exceptions_as_failures():
    def explode():
        raise RuntimeError("nope")

    runner = ProbeRunner([ProbeStep("1-explode", explode)], "demo", {})

    assert runner.run_all() is False
    assert runner.results.rows[0][1] == Result.FAIL
    assert "RuntimeError: nope" in runner.results.rows[0][2]


def test_probe_runner_rejects_invalid_return_value():
    runner = ProbeRunner([ProbeStep("1-invalid", lambda: {"ok": True})], "demo", {})

    assert runner.run_all() is False
    assert runner.results.rows[0][1] == Result.FAIL
    assert "TypeError" in runner.results.rows[0][2]


def test_probe_runner_applies_step_limit():
    calls = []
    runner = ProbeRunner(
        [
            ProbeStep("1-one", lambda: calls.append("one") or pass_()),
            ProbeStep("2-two", lambda: calls.append("two") or pass_()),
        ],
        "demo",
        {},
        max_steps=1,
    )

    assert runner.run_all() is True
    assert calls == ["one"]


def test_probe_outcome_rejects_unknown_status():
    with pytest.raises(ValueError):
        ProbeOutcome("SKIP", "not supported")


def test_build_runner_accepts_plan_probe_runner(tmp_path):
    runner = task_probe.build_runner("plan", "demo", tmp_path, {}, max_steps=1)

    assert isinstance(runner, ProbeRunner)
    assert [step.name for step in runner.steps] == ["1-list-tasks"]
