"""swarm.guard_log -- stable log lines for containment signals (L3).

Every guard rejection and every outbound host an agent contacts is written
to the agent's stdout (= its log file) as a single line with a fixed prefix,
so ``swarm.log_rotation.extract_signals`` can count them at finish time:

    [Agent] [GUARD] blocked kind=<kind> tool=<tool> detail=<repr>
    [Agent] [EGRESS] host=<host> tool=<tool> allowed=<0|1>

``allowed`` is the in-process guard's verdict; the seatbelt profile may still
refuse the connection at the OS level.

``kind`` is one of ``GUARD_KINDS``.  Dependency-free (stdlib only) so both
the tool layer and ``swarm.agent_token`` can import it.  Never raises.
"""

from __future__ import annotations

import re

GUARD_PREFIX = "[GUARD]"
EGRESS_PREFIX = "[EGRESS]"

# path      file tool path resolved outside the project root
# protected write to vendor / generated / guardrail paths
# shell     run_command blocked by the shell guard
# python    run_python snippet blocked by the AST guard
# fetch     fetch_url blocked by the egress allowlist
# api       swarm API refused the agent token (403)
GUARD_KINDS = ("path", "protected", "shell", "python", "fetch", "api")

# Long enough to show a whole one-liner (pipes, trailing `| head`) when a
# trip is reviewed; 200 cut off the part that explained most blocks.
_DETAIL_MAX = 2000

# scheme://[user@]host  and scp-style  user@host:path  (git@github.com:org/repo)
_RE_URL_HOST = re.compile(r"\b[a-z][a-z0-9+.-]*://(?:[^/\s@'\"]+@)?(\[[0-9a-f:]+\]|[^/\s:'\"?#]+)", re.IGNORECASE)
_RE_SCP_HOST = re.compile(r"(?<![\w/])[\w.-]+@([a-z0-9][a-z0-9.-]*\.[a-z]{2,}):(?=[\w~/.-])", re.IGNORECASE)


def _clean(value) -> str:
    text = " ".join(str(value or "").split())
    return text[:_DETAIL_MAX]


def outbound_hosts(text: str) -> list[str]:
    """Best-effort list of hosts named by URLs in a shell command (in order, deduped).

    Visibility only -- not a security boundary; a determined command can hide
    a host from any regex.  The sandbox profile is what actually blocks egress.
    """
    hosts: list[str] = []
    for rx in (_RE_URL_HOST, _RE_SCP_HOST):
        for m in rx.finditer(str(text or "")):
            host = m.group(1).lower().rstrip(".")
            if host and host not in hosts:
                hosts.append(host)
    return hosts


def log_blocked(kind: str, tool: str, detail="") -> None:
    """Record one guard rejection in the agent log."""
    try:
        print(f"[Agent] {GUARD_PREFIX} blocked kind={kind} tool={tool} detail={_clean(detail)!r}", flush=True)
    except Exception:
        pass


def log_outbound(host: str, tool: str, allowed: bool) -> None:
    """Record one outbound host contact (or attempt) in the agent log."""
    host = _clean(host).lower()
    if not host:
        return
    try:
        print(f"[Agent] {EGRESS_PREFIX} host={host} tool={tool} allowed={1 if allowed else 0}", flush=True)
    except Exception:
        pass
