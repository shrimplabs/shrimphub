from pathlib import Path

from swarm import learnings


def test_extract_learnings_writes_unicode_as_utf8(tmp_path, monkeypatch):
    log_path = tmp_path / "agent.log"
    log_path.write_text("[Agent] Calling LLM... (loop 7/200)\n", encoding="utf-8")

    monkeypatch.setattr(learnings, "_summarise_log", lambda *args, **kwargs: "- Follow arrows \u2192 safely")

    from swarm import orchestrator

    monkeypatch.setattr(orchestrator, "MINIMAX_API_KEY", "key")
    monkeypatch.setattr(orchestrator, "MINIMAX_BASE_URL", "https://example.invalid")

    learnings.extract_learnings(
        task_id="t1",
        task_type="bug",
        project="proj",
        log_path=str(log_path),
        exit_code=0,
        data_dir=str(tmp_path),
    )

    learnings_file = tmp_path / "learnings" / "proj" / "bug.md"
    assert "\u2192" in learnings_file.read_text(encoding="utf-8")
    assert "\u2192" in learnings.get_learnings("proj", "bug", str(tmp_path))
