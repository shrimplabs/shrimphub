"""Helpers for preserving the original task objective across bug/recovery chains."""

from __future__ import annotations

from typing import Any


def _truncate(text: str, limit: int | None) -> str:
    text = (text or "").strip()
    if limit is None or limit <= 0:
        return text
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[... original objective truncated ...]"


def extract_branch_intent(task: dict[str, Any] | None) -> dict[str, str]:
    task = task or {}
    meta = task.get("metadata") or {}
    desc = (
        meta.get("branch_intent_full_description")
        or meta.get("branch_intent_description")
        or task.get("description")
        or ""
    ).strip()
    title = (
        meta.get("branch_intent_title")
        or next((line.strip() for line in desc.splitlines() if line.strip()), task.get("id", "task"))
    )
    return {
        "root_task_id": (
            meta.get("branch_intent_root_task_id")
            or meta.get("recovery_root_task_id")
            or meta.get("failed_task_id")
            or task.get("id", "")
        ),
        "title": title,
        "description": desc,
        "type": str(meta.get("branch_intent_type") or task.get("type") or "feature"),
    }


def branch_intent_metadata(task: dict[str, Any] | None, *, max_chars: int | None = None) -> dict[str, str]:
    intent = extract_branch_intent(task)
    return {
        "branch_intent_root_task_id": intent["root_task_id"],
        "branch_intent_title": intent["title"],
        "branch_intent_full_description": intent["description"],
        "branch_intent_description": _truncate(intent["description"], max_chars),
        "branch_intent_type": intent["type"],
    }


def format_branch_intent(task: dict[str, Any] | None, *, heading: str = "ORIGINAL TASK OBJECTIVE", max_chars: int | None = 4000) -> str:
    intent = extract_branch_intent(task)
    desc = _truncate(intent["description"], max_chars)
    root = intent["root_task_id"] or "unknown-root"
    return f"{heading} ({root}):\n{desc}"
