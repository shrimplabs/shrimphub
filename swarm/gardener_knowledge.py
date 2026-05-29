"""
Gardener knowledge store -- shared pattern memory for the swarm.

Tracks confirmed/suspected/disputed patterns across projects with
confidence, TTL, and evidence links. Written by the gardener agent;
read by all agents via the knowledge tool chain.

File layout (one JSON object per line, never a full JSON array):
  data/swarm_knowledge.jsonl

Auto-generated markdown view:
  data/SWARM_KNOWLEDGE.md
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).parent.parent / "data"
JSONL_PATH = _DATA_DIR / "swarm_knowledge.jsonl"
MARKDOWN_PATH = _DATA_DIR / "SWARM_KNOWLEDGE.md"


def _jsonl_path(data_dir: Path | None = None) -> Path:
    if data_dir is not None:
        return data_dir / "swarm_knowledge.jsonl"
    return JSONL_PATH

# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

VALID_CONFIDENCE: set[str] = {"confirmed", "suspected", "disputed"}
VALID_STATUS: set[str] = {"active", "expired"}
DEFAULT_TTL_DAYS: int = 90


def _now_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(entry: dict[str, Any]) -> bool:
    """Return True when an entry's TTL has passed and it is still active."""
    if entry.get("status") != "active":
        return False
    raw = entry.get("last_seen", "")
    try:
        last = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    ttl = entry.get("ttl_days", DEFAULT_TTL_DAYS)
    return _now_dt() > last + timedelta(days=ttl)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load(data_dir: Path | None = None) -> list[dict[str, Any]]:
    """Read all entries from the JSONL store."""
    path = _jsonl_path(data_dir)
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def append_entry(entry: dict[str, Any], data_dir: Path | None = None) -> str:
    """
    Append one entry to the JSONL store.

    Required fields: pattern_signature, godot_version
    Optional: confidence, ttl_days, affected_projects, evidence_task_ids,
              fix_summary, created_by, data_dir

    Returns the generated id.
    """
    target_dir = data_dir or _DATA_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    now = _now_date()
    record: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "pattern_signature": entry["pattern_signature"],
        "confidence": entry.get("confidence", "suspected"),
        "godot_version": entry.get("godot_version", "unknown"),
        "first_seen": now,
        "last_seen": now,
        "ttl_days": entry.get("ttl_days", DEFAULT_TTL_DAYS),
        "affected_projects": entry.get("affected_projects", []),
        "evidence_task_ids": entry.get("evidence_task_ids", []),
        "fix_summary": entry.get("fix_summary", ""),
        "status": "active",
        "created_by": entry.get("created_by", "gardener"),
    }

    with _jsonl_path(data_dir).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    return record["id"]


def update_confidence(pattern_id: str, confidence: str, data_dir: Path | None = None) -> bool:
    """
    Update the confidence level of an existing entry by its id.

    Returns True when the entry was found and updated.
    """
    if confidence not in VALID_CONFIDENCE:
        raise ValueError(f"confidence must be one of {VALID_CONFIDENCE!r}")

    entries = load(data_dir)
    updated = False
    for entry in entries:
        if entry.get("id") == pattern_id:
            entry["confidence"] = confidence
            entry["last_seen"] = _now_date()
            updated = True
            break

    if updated:
        _write_all(entries, data_dir)
    return updated


def expire_stale(data_dir: Path | None = None) -> int:
    """
    Mark all active entries whose TTL has passed as status=expired.

    Returns the number of entries expired.
    """
    entries = load(data_dir)
    expired = 0
    for entry in entries:
        if _is_expired(entry):
            entry["status"] = "expired"
            expired += 1

    if expired:
        _write_all(entries, data_dir)
    return expired


def render_markdown(data_dir: Path | None = None) -> str:
    """
    Generate a readable markdown document from all non-expired entries,
    grouped by confidence level.
    """
    by_conf = {"confirmed": [], "suspected": [], "disputed": []}
    for entry in load(data_dir):
        if entry.get("status") == "expired":
            continue
        conf = entry.get("confidence", "suspected")
        if conf in by_conf:
            by_conf[conf].append(entry)

    lines = [
        "<!-- ============================================================",
        "     AUTO-GENERATED FILE -- do not edit directly.",
        "     Regenerated by the gardener agent after each knowledge update.",
        "     Source: data/swarm_knowledge.jsonl",
        "     ============================================================ -->",
        "",
        "# SWARM Knowledge Base",
        "",
        "Shared pattern memory for all swarm agents.  Patterns are grouped by",
        "confidence level:",
        "",
        "| Level      | Meaning                                           |",
        "|------------|---------------------------------------------------|",
        "| **confirmed** | Verified across 3+ independent projects         |",
        "| **suspected** | Seen 1-2 times, plausible but unverified       |",
        "| **disputed**  | Contradicted by later evidence                   |",
        "",
    ]

    for level in ["confirmed", "suspected", "disputed"]:
        group = by_conf[level]
        if not group:
            continue
        label = level.capitalize()
        lines += [
            f"## {label}",
            "",
        ]
        for e in group:
            sig = e.get("pattern_signature", "")
            fix = e.get("fix_summary", "")
            projects = e.get("affected_projects", [])
            tasks = e.get("evidence_task_ids", [])
            ver = e.get("godot_version", "")
            seen = e.get("last_seen", "")
            lines.append(f"### {sig}")
            lines.append("")
            if fix:
                lines.append(f"**Fix:** {fix}")
                lines.append("")
            if ver:
                lines.append(f"**Version:** {ver}")
                lines.append("")
            if projects:
                lines.append(f"**Projects:** {', '.join(projects)}")
                lines.append("")
            if tasks:
                lines.append(f"**Evidence tasks:** {', '.join(tasks)}")
                lines.append("")
            lines.append(f"_Last updated: {seen} | Confidence: {level}_  ")
            lines.append("")

    lines.append("---")
    lines.append(f"_Auto-generated: {_now_date()}_")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _write_all(entries: list[dict[str, Any]], data_dir: Path | None = None) -> None:
    target_dir = data_dir or _DATA_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    with _jsonl_path(data_dir).open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
