from pathlib import Path

from tools.art_pass_probe import ArtPassProbeContext, build_probe
from tools.task_probe import Result


def test_build_probe_uses_expected_steps(tmp_path):
    runner = build_probe("demo", tmp_path, {})

    assert [step.name for step in runner.steps] == [
        "1-launch",
        "2-screenshot",
        "3-vision",
        "4-asset-list",
        "5-file-copy",
        "6-commit",
        "7-cleanup",
    ]


def test_art_probe_setup_creates_scratch_project_and_fallback_asset():
    ctx = ArtPassProbeContext("demo", Path("/tmp/demo"), {})
    ctx.setup()
    try:
        assert (ctx.project_path / "project.godot").exists()
        assert (ctx.project_path / "autoload" / "state_server.gd").exists()
        assert (ctx.project_path / "scripts" / "main.gd").exists()
        assert ctx.asset_library.exists()
        assert ctx.source_asset.exists()
        assert ctx.source_asset.suffix == ".png"
    finally:
        ctx.cleanup()


def test_asset_list_warns_without_configured_library():
    ctx = ArtPassProbeContext("demo", Path("/tmp/demo"), {})
    ctx.setup()
    try:
        outcome = ctx.step_asset_list()
        assert outcome.status == Result.WARN
        assert "generated fallback" in outcome.detail
    finally:
        ctx.cleanup()
