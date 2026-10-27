from pathlib import Path

from tools.harness_qa_probe import HarnessQAProbeContext, build_probe


def test_build_probe_uses_expected_steps(tmp_path):
    runner = build_probe("demo", tmp_path, {})

    assert [step.name for step in runner.steps] == [
        "1-launch",
        "2-port",
        "3-checkpoint",
        "4-pass",
        "5-fail",
    ]


def test_harness_probe_setup_creates_canonical_project():
    ctx = HarnessQAProbeContext("demo", Path("/tmp/demo"), {})
    ctx.setup()
    try:
        assert (ctx.project_path / "project.godot").exists()
        assert (ctx.project_path / "autoload" / "test_harness.gd").exists()
        assert (ctx.project_path / "scripts" / "main.gd").exists()

        project_text = (ctx.project_path / "project.godot").read_text()
        script_text = (ctx.project_path / "scripts" / "main.gd").read_text()
        assert 'TestHarness="*res://autoload/test_harness.gd"' in project_text
        assert "create_timer(5.0)" in script_text
        assert "await TestHarness.checkpoint" in script_text
        assert "HARNESS_PROBE_FAIL_RECEIVED" in script_text
    finally:
        ctx.cleanup()
