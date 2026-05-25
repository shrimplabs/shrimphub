from pathlib import Path

from tools.feature_probe import FeatureProbeContext, build_probe


def test_build_probe_uses_expected_steps(tmp_path):
    runner = build_probe("demo", tmp_path, {})

    assert [step.name for step in runner.steps] == [
        "1-read",
        "2-write",
        "3-commit",
        "4-validation-fires",
        "5-pass-no-bug",
        "6-fail-spawns-bug",
    ]


def test_feature_probe_cleanup_removes_temp_project(monkeypatch):
    ctx = FeatureProbeContext("demo", Path("/tmp/demo"), {})
    monkeypatch.setattr(ctx, "_create_task", lambda *args, **kwargs: None)
    ctx.setup()
    project_path = ctx.project_path

    monkeypatch.setattr(ctx, "request", lambda *args, **kwargs: {})
    ctx.cleanup()

    assert project_path is not None
    assert not project_path.exists()
