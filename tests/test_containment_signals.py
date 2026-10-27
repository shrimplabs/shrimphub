"""Containment signals (swarm-controller-583t, L3).

Every guard rejection writes a ``[GUARD] blocked kind=...`` line and every
outbound host an ``[EGRESS] host=...`` line to the agent log (stdout);
``extract_signals`` aggregates them into ``agent_signals.blocked_calls`` /
``outbound_hosts``; ``get_signals_summary`` exposes counts and a 24h rate
for the dashboard Guard chip.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import pytest

import swarm.db as db
import swarm.tools.core as core
from swarm import agent_token
from swarm.guard_log import log_blocked, log_outbound, outbound_hosts
from swarm.log_rotation import extract_signals, get_signals_summary
from swarm.tools._shared import ToolContext, sync_tool_context
from swarm.tools.files import read_file, search_code, write_file
from swarm.tools.shell import run_command, run_python


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm_test.db")
    yield
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


@pytest.fixture
def proj(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    p = workspace / "test-proj"
    (p / "addons").mkdir(parents=True)
    (p / "main.py").write_text("x = 1\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("SECRET = 1\n")
    (p / "escape.py").symlink_to(outside / "secret.py")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    sync_tool_context(ToolContext(
        workspace=workspace, project="test-proj", project_path_override="",
        task_type="feature", readonly=False,
    ))
    return p


def _signals_from(capsys, tmp_path: Path) -> dict:
    log = tmp_path / "agent_x.log"
    log.write_text(capsys.readouterr().out, encoding="utf-8")
    return extract_signals(str(log))


# ---------------------------------------------------------------------------
# Guards emit stable lines that extract_signals counts
# ---------------------------------------------------------------------------

def test_every_guard_kind_is_counted(proj, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(core.FETCH_ALLOWED_HOSTS_ENV, raising=False)
    assert read_file("/etc/passwd")["ok"] is False                       # path
    assert write_file("addons/x.gd", "x")["ok"] is False                 # protected
    # Canary targets only: if a guard regresses these must not touch real files.
    outside = tmp_path / "outside_canary"
    outside.mkdir()
    assert run_command(f"rm -rf {outside}")["ok"] is False                # shell
    assert run_python(f'open("{outside}/x.txt", "w")')["ok"] is False      # python
    assert outside.exists() and not (outside / "x.txt").exists()
    assert core.fetch_url("https://evil.example.com/x")["ok"] is False    # fetch

    sig = _signals_from(capsys, tmp_path)
    assert json.loads(sig["blocked_calls"]) == {
        "path": 1, "protected": 1, "shell": 1, "python": 1, "fetch": 1,
    }
    assert json.loads(sig["outbound_hosts"]) == {"evil.example.com": 1}


def test_guard_lines_do_not_pollute_tool_or_warning_signals(tmp_path, capsys):
    log_blocked("shell", "run_command", "recursive rm: rm -rf /")
    log_outbound("github.com", "run_command", allowed=True)
    sig = _signals_from(capsys, tmp_path)
    assert sig["tool_call_count"] == 0
    assert sig["warning_count"] == 0
    assert sig["error_count"] == 0


def test_search_code_walk_does_not_log_symlink_filtering(proj, tmp_path, capsys):
    result = search_code("SECRET")
    assert result["ok"] and result["count"] == 0
    assert json.loads(_signals_from(capsys, tmp_path)["blocked_calls"]) == {}


def test_allowed_run_command_logs_outbound_hosts(proj, tmp_path, capsys):
    with mock.patch("swarm.tools.shell.subprocess.Popen", side_effect=OSError("no exec in test")), \
            pytest.raises(OSError):
        run_command("git clone git@github.com:org/repo.git && curl -s https://pypi.org/simple/")
    hosts = json.loads(_signals_from(capsys, tmp_path)["outbound_hosts"])
    assert hosts == {"github.com": 1, "pypi.org": 1}


def test_detail_is_single_line_and_bounded(capsys):
    from swarm import guard_log
    log_blocked("shell", "run_command", "line1\nline2 " + "x" * 10000)
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert len(out) < guard_log._DETAIL_MAX + 120


def test_detail_keeps_a_whole_one_liner(capsys):
    # Trip reviews need the tail of a command (`| head -50`), which the old
    # 200-char cap cut off.
    cmd = "cd /Users/x/workspace/game && python3 -c \"" + "y" * 400 + "\" 2>&1 | head -50"
    log_blocked("shell", "run_command", cmd)
    assert "| head -50" in capsys.readouterr().out


@pytest.mark.parametrize("cmd,expected", [
    ("curl -s https://user:pw@api.github.com/x", ["api.github.com"]),
    ("wget http://127.0.0.1:5001/api/config", ["127.0.0.1"]),
    ("git clone git@gitlab.com:a/b.git", ["gitlab.com"]),
    ("echo foo@bar.txt: done", []),
    ("ls -la", []),
])
def test_outbound_hosts(cmd, expected):
    assert outbound_hosts(cmd) == expected


# ---------------------------------------------------------------------------
# API scope: a 403 from the local swarm API is logged by the agent opener
# ---------------------------------------------------------------------------

class _Forbidden(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error": "agent token not permitted"}')

    def log_message(self, *args):
        pass


def test_api_403_is_logged_as_api_block(tmp_path, capsys, monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Forbidden)
    port = server.server_address[1]
    threading.Thread(target=server.handle_request, daemon=True).start()
    monkeypatch.setenv("API_PORT", str(port))
    monkeypatch.setenv(agent_token.AGENT_TOKEN_ENV, "tok")
    opener = urllib.request.build_opener(agent_token._AgentTokenHandler())
    req = urllib.request.Request(f"http://localhost:{port}/api/max-agents", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError):
        opener.open(req, timeout=5)
    server.server_close()
    sig = _signals_from(capsys, tmp_path)
    assert json.loads(sig["blocked_calls"]) == {"api": 1}


# ---------------------------------------------------------------------------
# Persistence + summary
# ---------------------------------------------------------------------------

def _row(agent_id: str, extracted_at: str, blocked: dict, hosts: dict) -> dict:
    return {
        "agent_id": agent_id, "task_id": "t", "project": "p", "task_type": "feature",
        "extracted_at": extracted_at, "terminal_status": "complete",
        "tool_sequence": "[]", "blocked_calls": json.dumps(blocked),
        "outbound_hosts": json.dumps(hosts),
    }


def test_columns_round_trip():
    db.agent_signals_upsert(_row("a1", "2026-09-28T00:00:00+00:00", {"shell": 2}, {"github.com": 1}))
    got = db.agent_signals_get("a1")
    assert json.loads(got["blocked_calls"]) == {"shell": 2}
    assert json.loads(got["outbound_hosts"]) == {"github.com": 1}


def test_summary_counts_and_24h_rate():
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(days=3)).isoformat()
    db.agent_signals_upsert(_row("a1", recent, {"shell": 2, "path": 1}, {"github.com": 2}))
    db.agent_signals_upsert(_row("a2", recent, {}, {"pypi.org": 1}))
    db.agent_signals_upsert(_row("a3", old, {"fetch": 5}, {"github.com": 1}))
    # legacy row from before the columns existed
    legacy = _row("a4", old, {}, {})
    legacy["blocked_calls"] = legacy["outbound_hosts"] = None
    db.agent_signals_upsert(legacy)

    s = get_signals_summary(db)
    assert s["blocked_calls"] == {"shell": 2, "path": 1, "fetch": 5}
    assert s["top_outbound_hosts"] == {"github.com": 3, "pypi.org": 1}
    assert s["containment_24h"] == {"agents": 2, "blocked_calls": 3, "blocked_per_agent": 1.5}


def test_guard_log_is_a_protected_guardrail_file(tmp_path, monkeypatch):
    from swarm.tools import path_guard
    target = path_guard._SWARM_CONTROLLER_ROOT / "swarm" / "guard_log.py"
    monkeypatch.setattr(path_guard, "_guardrail_active", lambda: True)
    assert path_guard._guardrail_reason(target) == "guardrail"
    assert path_guard._command_targets_protected_path("sed -i '' swarm/guard_log.py") is not None
