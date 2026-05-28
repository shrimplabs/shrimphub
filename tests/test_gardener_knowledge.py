"""
Tests for swarm.gardener_knowledge.
"""

import json

import pytest

# Patch module-level path constants before importing the module under test.


@pytest.fixture(autouse=True)
def patched_paths(tmp_path, monkeypatch):
    """Redirect JSONL + markdown output to a temp directory."""
    import swarm.gardener_knowledge as gk

    monkeypatch.setattr(gk, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(gk, "JSONL_PATH", tmp_path / "swarm_knowledge.jsonl")
    monkeypatch.setattr(gk, "MARKDOWN_PATH", tmp_path / "SWARM_KNOWLEDGE.md")


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------

def test_load_returns_empty_list_when_file_missing(patched_paths):
    from swarm.gardener_knowledge import load

    assert load() == []


def test_load_returns_all_entries(patched_paths, tmp_path):
    from swarm.gardener_knowledge import load

    path = tmp_path / "swarm_knowledge.jsonl"
    entry1 = {"id": "a", "pattern_signature": "sig-1", "confidence": "confirmed",
               "godot_version": "4.3+", "first_seen": "2026-01-01",
               "last_seen": "2026-01-01", "ttl_days": 90,
               "affected_projects": [], "evidence_task_ids": [],
               "fix_summary": "", "status": "active", "created_by": "gardener"}
    entry2 = {"id": "b", "pattern_signature": "sig-2", "confidence": "suspected",
               "godot_version": "4.2", "first_seen": "2026-01-01",
               "last_seen": "2026-01-01", "ttl_days": 90,
               "affected_projects": [], "evidence_task_ids": [],
               "fix_summary": "", "status": "active", "created_by": "gardener"}
    path.write_text(json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n")

    entries = load()
    assert len(entries) == 2
    assert entries[0]["id"] == "a"
    assert entries[1]["id"] == "b"


def test_load_skips_blank_and_malformed_lines(patched_paths, tmp_path):
    from swarm.gardener_knowledge import load

    path = tmp_path / "swarm_knowledge.jsonl"
    path.write_text('{"id":"good"}\n\nnot-json\n{"id":"also-good"}\n')

    entries = load()
    assert [e["id"] for e in entries] == ["good", "also-good"]


# ---------------------------------------------------------------------------
# append_entry
# ---------------------------------------------------------------------------

def test_append_entry_generates_id_and_writes_jsonl(patched_paths):
    from swarm.gardener_knowledge import append_entry, load

    id1 = append_entry({"pattern_signature": "sig-a", "godot_version": "4.3+"})
    id2 = append_entry({"pattern_signature": "sig-b", "godot_version": "4.2"})

    assert id1 != id2
    entries = load()
    assert len(entries) == 2
    assert entries[0]["id"] == id1
    assert entries[0]["pattern_signature"] == "sig-a"
    assert entries[0]["confidence"] == "suspected"      # default
    assert entries[0]["ttl_days"] == 90
    assert entries[0]["status"] == "active"


def test_append_entry_preserves_optional_fields(patched_paths):
    from swarm.gardener_knowledge import append_entry, load

    _ = append_entry({
        "pattern_signature": "sig-x",
        "godot_version": "4.4",
        "confidence": "confirmed",
        "ttl_days": 30,
        "affected_projects": ["proj-1", "proj-2"],
        "evidence_task_ids": ["task-abc"],
        "fix_summary": "Copy file from templates/",
        "created_by": "test",
    })

    entry = load()[0]
    assert entry["confidence"] == "confirmed"
    assert entry["ttl_days"] == 30
    assert entry["affected_projects"] == ["proj-1", "proj-2"]
    assert entry["evidence_task_ids"] == ["task-abc"]
    assert entry["fix_summary"] == "Copy file from templates/"
    assert entry["created_by"] == "test"


# ---------------------------------------------------------------------------
# update_confidence
# ---------------------------------------------------------------------------

def test_update_confidence_returns_true_and_updates(patched_paths):
    from swarm.gardener_knowledge import append_entry, load, update_confidence

    id_ = append_entry({"pattern_signature": "sig", "godot_version": "4.3+"})
    ok = update_confidence(id_, "confirmed")

    assert ok is True
    entry = load()[0]
    assert entry["confidence"] == "confirmed"


def test_update_confidence_returns_false_for_unknown_id(patched_paths):
    from swarm.gardener_knowledge import update_confidence

    ok = update_confidence("nonexistent-id", "disputed")
    assert ok is False


def test_update_confidence_raises_on_bad_confidence(patched_paths):
    from swarm.gardener_knowledge import append_entry, update_confidence

    id_ = append_entry({"pattern_signature": "sig", "godot_version": "4.3+"})
    with pytest.raises(ValueError):
        update_confidence(id_, "invalid")


# ---------------------------------------------------------------------------
# expire_stale
# ---------------------------------------------------------------------------

def test_expire_stale_marks_expired_entries(patched_paths, tmp_path):
    from swarm.gardener_knowledge import append_entry, expire_stale, load

    # Fresh entry should not be expired.
    _ = append_entry({"pattern_signature": "sig-fresh", "godot_version": "4.3+", "ttl_days": 90})
    assert expire_stale() == 0
    assert load()[0]["status"] == "active"

    # Backdate last_seen so TTL is already past.
    path = tmp_path / "swarm_knowledge.jsonl"
    entries = load()
    entries[0]["last_seen"] = "2020-01-01"
    entries[0]["ttl_days"] = 1
    path.write_text(json.dumps(entries[0]) + "\n")

    assert expire_stale() == 1
    assert load()[0]["status"] == "expired"


def test_expire_stale_ignores_already_expired_entries(patched_paths, tmp_path):
    from swarm.gardener_knowledge import expire_stale, load

    path = tmp_path / "swarm_knowledge.jsonl"
    entry = {
        "id": "already-expired",
        "pattern_signature": "sig",
        "confidence": "confirmed",
        "godot_version": "4.3+",
        "first_seen": "2020-01-01",
        "last_seen": "2020-01-01",
        "ttl_days": 1,
        "affected_projects": [],
        "evidence_task_ids": [],
        "fix_summary": "",
        "status": "expired",       # already expired -- should stay expired
        "created_by": "test",
    }
    path.write_text(json.dumps(entry) + "\n")

    assert expire_stale() == 0
    assert load()[0]["status"] == "expired"


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------

def test_render_markdown_shows_auto_generated_header(patched_paths):
    from swarm.gardener_knowledge import render_markdown

    md = render_markdown()
    assert "AUTO-GENERATED FILE" in md
    assert "do not edit directly" in md
    assert "swarm_knowledge.jsonl" in md


def test_render_markdown_groups_by_confidence(patched_paths, tmp_path):
    from swarm.gardener_knowledge import render_markdown

    path = tmp_path / "swarm_knowledge.jsonl"
    confirmed = {
        "id": "c1", "pattern_signature": "confirmed-sig",
        "confidence": "confirmed", "godot_version": "4.3+",
        "first_seen": "2026-01-01", "last_seen": "2026-01-01",
        "ttl_days": 90, "affected_projects": ["proj-a"],
        "evidence_task_ids": [], "fix_summary": "Apply patch",
        "status": "active", "created_by": "test",
    }
    suspected = {
        "id": "s1", "pattern_signature": "suspected-sig",
        "confidence": "suspected", "godot_version": "4.2",
        "first_seen": "2026-01-01", "last_seen": "2026-01-01",
        "ttl_days": 90, "affected_projects": [],
        "evidence_task_ids": [], "fix_summary": "",
        "status": "active", "created_by": "test",
    }
    path.write_text(json.dumps(confirmed) + "\n" + json.dumps(suspected) + "\n")

    md = render_markdown()

    assert "## Confirmed" in md
    assert "confirmed-sig" in md
    assert "Apply patch" in md
    assert "## Suspected" in md
    assert "suspected-sig" in md


def test_render_markdown_excludes_expired(patched_paths, tmp_path):
    from swarm.gardener_knowledge import render_markdown

    path = tmp_path / "swarm_knowledge.jsonl"
    active = {
        "id": "a1", "pattern_signature": "active-sig",
        "confidence": "confirmed", "godot_version": "4.3+",
        "first_seen": "2026-01-01", "last_seen": "2026-01-01",
        "ttl_days": 90, "affected_projects": [],
        "evidence_task_ids": [], "fix_summary": "",
        "status": "active", "created_by": "test",
    }
    expired = {
        "id": "e1", "pattern_signature": "expired-sig",
        "confidence": "confirmed", "godot_version": "4.3+",
        "first_seen": "2020-01-01", "last_seen": "2020-01-01",
        "ttl_days": 1, "affected_projects": [],
        "evidence_task_ids": [], "fix_summary": "",
        "status": "expired", "created_by": "test",
    }
    path.write_text(json.dumps(active) + "\n" + json.dumps(expired) + "\n")

    md = render_markdown()
    assert "active-sig" in md
    assert "expired-sig" not in md
