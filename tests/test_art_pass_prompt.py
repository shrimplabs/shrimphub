from pathlib import Path


def test_art_pass_prompt_matches_run_command_schema():
    prompt = Path("prompts/art_pass.yaml").read_text()

    assert "run_command(command)" in prompt
    assert "run_command(cmd)" not in prompt


def test_art_pass_prompt_respects_task_scoped_completion():
    prompt = Path("prompts/art_pass.yaml").read_text()

    assert "git_push(), TASK_COMPLETE" not in prompt
    assert "at least 3 meaningful visual improvements" not in prompt
    assert "visual improvement scope requested by the task" in prompt
