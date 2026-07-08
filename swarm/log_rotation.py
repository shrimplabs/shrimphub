"""
swarm.log_rotation -- log file rotation and signal extraction.

Signals are extracted at agent-finish time (identity and log are both fresh).
Rotation is a parse-free delete/compress pass that runs hourly via the monitor.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------

_RE_LOOP_FLAT      = re.compile(r"\[Agent\] Calling LLM\.\.\. \(loop (\d+)/(\d+)\)")
_RE_TOOL           = re.compile(r"\[Agent\] Executing tool: (\S+)")
_RE_CACHE          = re.compile(r"\[Agent\] \[LLM\] (?:cache )?read=(\d+) write=(\d+)")
_RE_WARNING        = re.compile(r"\[Agent\] WARNING: (.+)")
_RE_COMPACTION     = re.compile(r"\[Agent\] \[Compaction\]")
_RE_PIPELINE_HDR   = re.compile(r"\[Agent\] \[Pipeline\] Starting:")
_RE_PIPELINE_DONE  = re.compile(r"\[Agent\] \[Pipeline\] Done\. (OK|FAILED)")
_RE_PIPELINE_PHASE = re.compile(r"\[Agent\]   ✓ ([A-Z]+) complete")
_RE_LOOP_LIMIT     = re.compile(r"hit loop limit", re.IGNORECASE)
_RE_TASK_COMPLETE  = re.compile(r"\[Agent\] Task complete!")
_RE_RESULT_FAIL    = re.compile(r"\[Agent\] Result: \{'ok': False.*?'error': '(.{0,200}?)'")
_RE_MECH_SUMMARY   = re.compile(r"\[Agent\] \[MechanismSummary\] (\{.*\})")


def extract_signals(log_path: str) -> dict:
    """Parse a single agent log file and return a signals dict.

    Identity fields (agent_id, task_id, project, task_type, extracted_at,
    log_size_bytes, log_path) are NOT set here — the caller adds them.
    """
    signals: dict = {
        "terminal_status":   "unknown",
        "loop_count":        0,
        "total_loops":       0,
        "tool_sequence":     "[]",
        "unique_tools":      "[]",
        "tool_call_count":   0,
        "cache_read_total":  0,
        "cache_write_total": 0,
        "error_count":       0,
        "error_snippets":    "[]",
        "warning_count":     0,
        "warning_types":     "[]",
        "is_pipeline":       0,
        "phases_completed":  "[]",
        "phase_failed":      None,
        "compaction_count":  0,
        "mechanism_fires":   "{}",
    }

    tool_sequence: list[str] = []
    error_snippets: list[str] = []
    warning_types: list[str] = []
    phases_completed: list[str] = []

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                # Loop counter (flat agents)
                m = _RE_LOOP_FLAT.search(line)
                if m:
                    signals["loop_count"]  = int(m.group(1))
                    signals["total_loops"] = int(m.group(2))
                    continue

                # Tool calls
                m = _RE_TOOL.search(line)
                if m:
                    tool_sequence.append(m.group(1))
                    continue

                # Cache tokens (both "cache read=N write=N" and "read=N write=N")
                m = _RE_CACHE.search(line)
                if m:
                    signals["cache_read_total"]  += int(m.group(1))
                    signals["cache_write_total"] += int(m.group(2))
                    continue

                # Loop limit (check before WARNING so "hit loop limit" in warning text is caught)
                if _RE_LOOP_LIMIT.search(line):
                    signals["terminal_status"] = "loop_limit"
                    # still fall through to record as a warning if applicable

                # Warnings
                m = _RE_WARNING.search(line)
                if m:
                    signals["warning_count"] += 1
                    txt = m.group(1).strip()[:120]
                    if txt not in warning_types:
                        warning_types.append(txt)
                    continue

                # Recovery-mechanism summary (single authoritative JSON line
                # emitted by the agent at exit; overrides any partial count).
                m = _RE_MECH_SUMMARY.search(line)
                if m:
                    try:
                        signals["mechanism_fires"] = json.dumps(json.loads(m.group(1)), sort_keys=True)
                    except Exception:
                        pass
                    continue

                # Compaction
                if _RE_COMPACTION.search(line):
                    signals["compaction_count"] += 1
                    continue

                # Pipeline header
                if _RE_PIPELINE_HDR.search(line):
                    signals["is_pipeline"] = 1
                    continue

                # Pipeline done (overrides loop_limit if pipeline also marks FAILED/OK)
                m = _RE_PIPELINE_DONE.search(line)
                if m:
                    signals["terminal_status"] = "complete" if m.group(1) == "OK" else "failed"
                    continue

                # Pipeline phase complete marker
                m = _RE_PIPELINE_PHASE.search(line)
                if m:
                    phases_completed.append(m.group(1).lower())
                    continue

                # Flat task complete
                if _RE_TASK_COMPLETE.search(line):
                    signals["terminal_status"] = "complete"
                    continue

                # Result errors (Python repr — use regex not json.loads)
                m = _RE_RESULT_FAIL.search(line)
                if m:
                    signals["error_count"] += 1
                    if len(error_snippets) < 5:
                        error_snippets.append(m.group(1).strip())

    except Exception as e:
        signals["terminal_status"] = f"parse_error:{e}"

    signals["tool_call_count"] = len(tool_sequence)
    signals["tool_sequence"]   = json.dumps(tool_sequence)
    signals["unique_tools"]    = json.dumps(list(dict.fromkeys(tool_sequence)))
    signals["error_snippets"]  = json.dumps(error_snippets)
    signals["warning_types"]   = json.dumps(warning_types)
    signals["phases_completed"] = json.dumps(phases_completed)
    return signals


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

_rotation_lock = threading.Lock()


def rotate_logs(data_dir: str, db) -> dict:
    """Delete or compress agent_*.log files older than LOG_RETENTION_DAYS.

    Reads config from orchestrator module globals:
        LOG_RETENTION_DAYS  (int, 0 = disabled)
        LOG_ROTATION_ACTION ("delete" | "compress")

    Never touches logs belonging to currently active agents.
    Returns a stats dict.
    """
    if not _rotation_lock.acquire(blocking=False):
        return {"skipped": "already_running"}

    try:
        return _rotate_logs_inner(data_dir, db)
    finally:
        _rotation_lock.release()


def _rotate_logs_inner(data_dir: str, db) -> dict:
    try:
        import swarm.orchestrator as orc
        retention_days = int(getattr(orc, "LOG_RETENTION_DAYS", 0))
        action         = str(getattr(orc, "LOG_ROTATION_ACTION", "delete"))
    except Exception:
        return {"error": "could not read orchestrator config"}

    if retention_days <= 0:
        return {"skipped": "disabled", "retention_days": retention_days}

    cutoff = time.time() - retention_days * 86400
    data_path = Path(data_dir)

    # Build set of active agent IDs to protect their logs
    active_ids: set[str] = set()
    try:
        for a in db.agent_get_active():
            active_ids.add(a.get("id", ""))
    except Exception:
        pass
    try:
        import swarm.agent_lifecycle as _al
        for aid in _al._active_handles:
            active_ids.add(aid)
    except Exception:
        pass

    stats = {"checked": 0, "skipped_active": 0, "rotated": 0, "errors": 0}

    for log_file in data_path.glob("agent_*.log"):
        stats["checked"] += 1
        try:
            mtime = log_file.stat().st_mtime
        except FileNotFoundError:
            continue

        if mtime >= cutoff:
            continue

        # Extract agent UUID from filename (agent_<uuid>.log)
        stem = log_file.stem  # "agent_<uuid>"
        agent_uuid = stem[len("agent_"):]
        if agent_uuid in active_ids:
            stats["skipped_active"] += 1
            continue

        try:
            if action == "compress":
                _compress_log(log_file)
            else:
                log_file.unlink()
            stats["rotated"] += 1
        except Exception as e:
            print(f"[LogRotation] Error rotating {log_file.name}: {e}")
            stats["errors"] += 1

    stats["action"] = action
    stats["retention_days"] = retention_days
    return stats


def _compress_log(log_file: Path):
    """Compress a log file into data/archives/YYYY-MM/ and delete the original."""
    mtime = log_file.stat().st_mtime
    month = datetime.fromtimestamp(mtime).strftime("%Y-%m")
    archive_dir = log_file.parent / "archives" / month
    archive_dir.mkdir(parents=True, exist_ok=True)
    gz_path = archive_dir / (log_file.name + ".gz")

    # Write compressed, then delete original (atomic enough for our use case)
    with open(log_file, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    log_file.unlink()


# ---------------------------------------------------------------------------
# Analytics query helper
# ---------------------------------------------------------------------------

def get_signals_summary(db, project: Optional[str] = None) -> dict:
    """Return aggregate analytics from agent_signals."""
    rows = db.agent_signals_query(project=project, limit=10000)
    if not rows:
        return {"count": 0}

    import collections

    status_counts: dict[str, int] = collections.Counter()
    loop_counts: list[int] = []
    tool_freq: dict[str, int] = collections.Counter()
    error_counts: list[int] = []
    cache_reads: list[int] = []
    cache_writes: list[int] = []
    compactions: list[int] = []

    for row in rows:
        status_counts[row.get("terminal_status") or "unknown"] += 1

        lc = row.get("loop_count") or 0
        if lc:
            loop_counts.append(lc)

        for tool in json.loads(row.get("tool_sequence") or "[]"):
            tool_freq[tool] += 1

        ec = row.get("error_count") or 0
        error_counts.append(ec)

        cr = row.get("cache_read_total") or 0
        cw = row.get("cache_write_total") or 0
        cache_reads.append(cr)
        cache_writes.append(cw)

        cc = row.get("compaction_count") or 0
        compactions.append(cc)

    total_cache = sum(cache_reads) + sum(cache_writes)
    cache_hit_rate = sum(cache_reads) / total_cache if total_cache else 0.0

    return {
        "count": len(rows),
        "terminal_status": dict(status_counts),
        "avg_loop_count": round(sum(loop_counts) / len(loop_counts), 1) if loop_counts else 0,
        "top_tools": dict(tool_freq.most_common(10)),
        "avg_error_count": round(sum(error_counts) / len(error_counts), 2) if error_counts else 0,
        "cache_hit_rate": round(cache_hit_rate, 3),
        "total_compactions": sum(compactions),
        "agents_with_compaction": sum(1 for c in compactions if c > 0),
    }
