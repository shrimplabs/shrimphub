"""swarm.containment_breaker -- trip auto-mode on a blocked-call spike (L3).

Guard rejections are counted per agent from two sources: finished agents via
``agent_signals.blocked_calls`` (see ``swarm.guard_log`` /
``swarm.log_rotation.extract_signals``), and still-running agents by counting
``[GUARD] blocked`` lines in their live log files -- otherwise a misbehaving
agent would be invisible to the breaker until it exited.  The monitor thread
calls :func:`check` periodically; when the total across all agents in the
window reaches the threshold the breaker trips:

* the offending tasks are flagged ``needs_human_review`` and put on
  ``containment_hold`` -- the scheduler never picks a held task, and agent
  tokens may not mutate one (``swarm.api_authz``);
* offenders that are still running are listed in ``trip["running"]`` and
  the monitor freezes them with :func:`freeze_agent` (SIGSTOP of the agent's
  process group -- non-destructive, nothing is lost).  Releasing the task
  thaws its agent (:func:`thaw_agent`), which resumes exactly where it
  stopped; quota recovery never thaws a contained agent;
* the caller suspends auto-mode.  Unlike quota suspension this never lifts on
  its own: only an operator re-enabling auto-mode clears it, which records
  ``containment_ack_at`` so rows seen before the acknowledgement do not
  re-trip the breaker.

Held tasks are released one at a time with :func:`release`
(``POST /api/tasks/<id>/containment-release``, operator-only).

Config (all optional): ``containment_breaker_enabled`` (default false --
opt-in; it is a debugging aid for spotting guard/tool gaps, the guards
themselves always run),
``containment_spike_threshold`` (default 10 blocked calls),
``containment_spike_window_minutes`` (default 60), ``containment_ack_at``
(written by the auto-mode endpoint).
"""

from __future__ import annotations

import json
import os
import re
import signal
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

DEFAULT_THRESHOLD = 10
DEFAULT_WINDOW_MINUTES = 60

HOLD_KEY = "containment_hold"
# Metadata keys an agent token may never set or clear (api_authz).
PROTECTED_META_KEYS = frozenset({
    HOLD_KEY, "needs_human_review", "human_review_reason",
    "containment_blocked_calls", "containment_agent_id", "containment_tripped_at",
})


def settings(config: dict) -> tuple[bool, int, int]:
    """Return ``(enabled, threshold, window_minutes)`` from *config*."""
    cfg = config or {}
    enabled = bool(cfg.get("containment_breaker_enabled", False))
    try:
        threshold = max(1, int(cfg.get("containment_spike_threshold", DEFAULT_THRESHOLD)))
    except (TypeError, ValueError):
        threshold = DEFAULT_THRESHOLD
    try:
        window = max(1, int(cfg.get("containment_spike_window_minutes", DEFAULT_WINDOW_MINUTES)))
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW_MINUTES
    return enabled, threshold, window


def _counts(raw) -> dict:
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out[str(k)] = n
    return out


_RE_GUARD_BLOCKED = re.compile(r"\[Agent\] \[GUARD\] blocked kind=(\w+)")


def live_blocked_calls(log_path) -> dict:
    """Count guard rejections by kind in a running agent's log.  ``{}`` on any error."""
    counts: dict = {}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "[GUARD]" not in line:
                    continue
                m = _RE_GUARD_BLOCKED.search(line)
                if m:
                    counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    except OSError:
        return {}
    return counts


def _live_rows(db, finished_ids: set) -> list[dict]:
    """One row per active agent with guard rejections in its log so far.

    Log lines carry no timestamp, so a running agent's blocks count as "now";
    :func:`check` subtracts what the operator has already reviewed.
    """
    rows = []
    for agent in db.agent_get_active():
        agent_id = agent.get("id")
        if not agent_id or agent_id in finished_ids or not agent.get("log_path"):
            continue
        blocked = live_blocked_calls(agent["log_path"])
        if not blocked:
            continue
        rows.append({
            "agent_id": agent_id, "task_id": agent.get("task_id"),
            "project": agent.get("project"), "blocked_calls": json.dumps(blocked),
            "live": True,
        })
    return rows


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def evaluate(rows: list[dict], threshold: int) -> Optional[dict]:
    """Pure decision: return a trip record if blocked calls in *rows* reach *threshold*."""
    offenders = []
    total = 0
    for row in rows:
        blocked = _counts(row.get("blocked_calls"))
        n = sum(blocked.values())
        if not n:
            continue
        total += n
        offenders.append({
            "agent_id": row.get("agent_id"), "task_id": row.get("task_id"),
            "project": row.get("project"), "blocked": blocked, "count": n,
            "live": bool(row.get("live")),
            "blocked_total": _counts(row.get("blocked_total")) or blocked,
        })
    if total < threshold:
        return None
    offenders.sort(key=lambda o: -o["count"])
    return {"total": total, "threshold": threshold, "offenders": offenders}


def is_held(task: Optional[dict]) -> bool:
    meta = (task or {}).get("metadata") or {}
    return isinstance(meta, dict) and bool(meta.get(HOLD_KEY))


def flag_tasks(db, trip: dict) -> list[str]:
    """Put each offender's task on containment hold.  Idempotent; returns newly flagged ids."""
    flagged = []
    for off in trip.get("offenders", []):
        task_id = off.get("task_id")
        if not task_id:
            continue
        task = db.task_get(task_id)
        if not task:
            continue
        meta = dict(task.get("metadata") or {})
        # Already held for this agent run.  (A released run only reaches here
        # with blocks beyond what was reviewed, so it is held again.)
        if meta.get("containment_agent_id") == off.get("agent_id") and meta.get(HOLD_KEY):
            continue
        kinds = ", ".join(f"{k}={v}" for k, v in sorted(off["blocked"].items()))
        meta.update({
            HOLD_KEY: True,
            "needs_human_review": True,
            "human_review_reason": (
                f"Containment breaker: agent {str(off.get('agent_id'))[:8]} hit {off['count']} "
                f"blocked call(s) ({kinds}) during a spike of {trip['total']} in "
                f"{trip.get('window_minutes', '?')}m. Review the agent log, then release via "
                f"POST /api/tasks/{task_id}/containment-release."
            ),
            "containment_blocked_calls": off.get("blocked_total") or off["blocked"],
            "containment_agent_id": off.get("agent_id"),
            "containment_tripped_at": trip.get("tripped_at"),
        })
        db.task_update(task_id, {"metadata": meta})
        if task_id not in flagged:  # two offender runs can share a task
            flagged.append(task_id)
    return flagged


ACK_COUNTS_KEY = "containment_ack_counts"


def _subtract(counts: dict, seen: dict) -> dict:
    out = {}
    for kind, n in counts.items():
        left = n - int(seen.get(kind, 0) or 0)
        if left > 0:
            out[kind] = left
    return out


def _reviewed_counts(db, row: dict, ack_counts: dict) -> dict:
    """Blocks of this agent run the operator has already seen: the snapshot on
    its held/released task, and the counts acknowledged when auto mode was
    re-enabled.  Per kind, the larger of the two."""
    agent_id = row.get("agent_id")
    seen = dict(_counts((ack_counts or {}).get(agent_id)))
    task = db.task_get(row.get("task_id")) if row.get("task_id") else None
    meta = (task or {}).get("metadata") or {}
    if isinstance(meta, dict) and meta.get("containment_agent_id") == agent_id:
        for kind, n in _counts(meta.get("containment_blocked_calls")).items():
            seen[kind] = max(seen.get(kind, 0), n)
    return seen


def _unreviewed(db, rows: list, config: dict) -> list:
    """Rows reduced to blocks the operator has not reviewed yet.

    Without this an agent that was running when a trip was acknowledged had
    all its already-seen blocks counted again when it finished and its
    signals row was written after ``containment_ack_at``."""
    ack_counts = (config or {}).get(ACK_COUNTS_KEY) or {}
    out = []
    for row in rows:
        total = _counts(row.get("blocked_calls"))
        fresh = _subtract(total, _reviewed_counts(db, row, ack_counts))
        if fresh:
            out.append({**row, "blocked_calls": fresh, "blocked_total": total})
    return out


def ack_snapshot(db) -> dict:
    """Per running agent, the blocked calls in its log right now.

    Stored in config on acknowledgement so those blocks never count again,
    even after the agent finishes.  Finished agents need no entry: their
    signals rows predate the ack and fall outside the window."""
    snap = {}
    try:
        for row in _live_rows(db, set()):
            snap[row["agent_id"]] = _counts(row.get("blocked_calls"))
    except Exception as exc:
        print(f"[Containment] ack snapshot failed: {exc}")
    return snap


def check(db, config: dict, *, now: Optional[datetime] = None) -> Optional[dict]:
    """Evaluate the window and flag offenders.  Returns the trip record or ``None``.

    Never raises -- a breaker bug must not take down the monitor thread.
    """
    try:
        enabled, threshold, window = settings(config)
        if not enabled:
            return None
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=window)
        ack = _parse_iso((config or {}).get("containment_ack_at"))
        if ack and ack > cutoff:
            cutoff = ack
        rows = db.agent_signals_since(cutoff.isoformat())
        try:
            rows = rows + _live_rows(db, {r.get("agent_id") for r in rows})
        except Exception as exc:
            print(f"[Containment] live log scan failed: {exc}")
        trip = evaluate(_unreviewed(db, rows, config), threshold)
        if trip is None:
            return None
        trip["window_minutes"] = window
        trip["tripped_at"] = now.isoformat()
        trip["flagged"] = flag_tasks(db, trip)
        trip["running"] = [o["agent_id"] for o in trip["offenders"] if o["live"]]
        return trip
    except Exception as exc:
        print(f"[Containment] breaker check failed: {exc}")
        return None


def release(db, task_id: str) -> Optional[dict[str, Any]]:
    """Operator action: lift the hold on one task after review.  Returns the new metadata."""
    task = db.task_get(task_id)
    if not task:
        return None
    meta = dict(task.get("metadata") or {})
    meta.pop(HOLD_KEY, None)
    meta["needs_human_review"] = False
    meta["containment_released_at"] = datetime.now(timezone.utc).isoformat()
    db.task_update(task_id, {"metadata": meta})
    agent_id = meta.get("containment_agent_id")
    if agent_id:
        thaw_agent(db, agent_id)
    return meta


# ---------------------------------------------------------------------------
# Freeze / thaw a running offender
# ---------------------------------------------------------------------------

# Handle flag that marks a containment freeze (vs. a quota freeze).  The quota
# watcher skips flagged handles so a quota recovery cannot thaw them.
FROZEN_KEY = "containment_frozen"


def _signal_group(pid: int, sig: int) -> None:
    """Signal the agent's whole process group (agents are session leaders, so
    this also stops any shell command or tool subprocess it is running)."""
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        raise
    except OSError:
        os.kill(pid, sig)


def _pid_is_agent(pid: int, agent_id: str) -> bool:
    """True if ``pid`` is alive and really is this agent's wrapper process.

    After a server restart only the DB pid is known; the kernel may have
    reused it, so never signal it without checking the command line names the
    agent's wrapper script (``data/agent_<id>.py``)."""
    import subprocess
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    try:
        cmd = subprocess.run(
            ["ps", "-o", "command=", "-p", str(int(pid))],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return False
    return f"agent_{agent_id}.py" in cmd


def _handle(agent_id: str):
    try:
        from swarm.agent_lifecycle import _active_handles, _handle_lock
    except Exception:
        return None, None
    with _handle_lock:
        return _active_handles.get(agent_id), _handle_lock


def freeze_agent(db, agent_id: str) -> bool:
    """SIGSTOP a running agent.  Idempotent; never raises.  Returns True if frozen.

    Sets ``freeze_started`` on the in-memory handle so the watchdog's timeout
    clock stops while the agent is frozen (same mechanism as the quota freeze).
    """
    try:
        data, lock = _handle(agent_id)
        pid = None
        if data is not None:
            proc = data.get("process")
            if proc is not None and proc.poll() is not None:
                return False
            pid = proc.pid if proc is not None else data.get("pid")
        else:
            agent = db.agent_get(agent_id) or {}
            pid = agent.get("pid") if agent.get("status") == "active" else None
            if pid and not _pid_is_agent(pid, agent_id):
                return False
        if not pid:
            return False
        _signal_group(int(pid), signal.SIGSTOP)
        if data is not None:
            with lock:
                data[FROZEN_KEY] = True
                data.setdefault("freeze_started", time.time())
        print(f"[Containment] froze agent {str(agent_id)[:8]} (pid {pid})")
        return True
    except Exception as exc:
        print(f"[Containment] freeze {str(agent_id)[:8]} failed: {exc}")
        return False


def thaw_agent(db, agent_id: str) -> bool:
    """SIGCONT an agent frozen by :func:`freeze_agent`.  Idempotent; never raises."""
    try:
        data, lock = _handle(agent_id)
        pid = None
        if data is not None:
            proc = data.get("process")
            pid = proc.pid if proc is not None else data.get("pid")
        else:
            agent = db.agent_get(agent_id) or {}
            pid = agent.get("pid") if agent.get("status") == "active" else None
            if pid and not _pid_is_agent(pid, agent_id):
                return False
        if not pid:
            return False
        _signal_group(int(pid), signal.SIGCONT)
        if data is not None:
            with lock:
                data.pop(FROZEN_KEY, None)
                started = data.pop("freeze_started", None)
                if started:
                    data["started"] = data.get("started", time.time()) + (time.time() - started)
        print(f"[Containment] thawed agent {str(agent_id)[:8]} (pid {pid})")
        return True
    except Exception as exc:
        print(f"[Containment] thaw {str(agent_id)[:8]} failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Recovery after a server restart
# ---------------------------------------------------------------------------

def recover(db, config: dict) -> Optional[dict[str, Any]]:
    """Rebuild the trip record from durable task holds after a restart.

    The suspension and the in-memory freeze bookkeeping die with the server
    process, but every held task still carries the offending agent id, the
    trip time and its blocked-call counts.  Tasks tripped after the last
    ``containment_ack_at`` are an unacknowledged trip: the caller restores
    ``suspended_for_containment`` so the dashboard shows it (auto mode is
    already persisted off).  Offenders whose process survived the restart are
    re-frozen (idempotent SIGSTOP) and listed in ``running``; releasing the
    task thaws them through the DB pid as usual.  Returns ``None`` when there
    is nothing to recover.  Never raises.
    """
    try:
        enabled, threshold, window = settings(config)
        ack = _parse_iso((config or {}).get("containment_ack_at"))
        offenders, running, flagged, times = [], [], [], []
        for task in db.task_get_all():
            if not is_held(task):
                continue
            meta = task.get("metadata") or {}
            agent_id = meta.get("containment_agent_id")
            tripped = _parse_iso(meta.get("containment_tripped_at"))
            live = False
            if agent_id:
                agent = db.agent_get(agent_id) or {}
                pid = agent.get("pid")
                if agent.get("status") == "active" and pid and _pid_is_agent(pid, agent_id):
                    try:
                        _signal_group(int(pid), signal.SIGSTOP)
                        live = True
                        print(f"[Containment] recovered frozen agent {str(agent_id)[:8]} (pid {pid})")
                    except Exception as exc:
                        print(f"[Containment] could not re-freeze {str(agent_id)[:8]}: {exc}")
            unacked = tripped is not None and (ack is None or tripped > ack)
            if not (unacked or live):
                continue  # an old hold the operator already acknowledged
            blocked = _counts(meta.get("containment_blocked_calls"))
            offenders.append({
                "agent_id": agent_id, "task_id": task["id"], "project": task.get("project"),
                "blocked": blocked, "count": sum(blocked.values()), "live": live,
            })
            flagged.append(task["id"])
            if live:
                running.append(agent_id)
            if tripped:
                times.append(tripped)
        if not offenders:
            return None
        return {
            "offenders": offenders,
            "flagged": flagged,
            "running": running,
            "total": sum(o["count"] for o in offenders),
            "threshold": threshold,
            "window_minutes": window,
            "tripped_at": max(times).isoformat() if times else None,
            "recovered_after_restart": True,
        }
    except Exception as exc:
        print(f"[Containment] recovery after restart failed: {exc}")
        return None
