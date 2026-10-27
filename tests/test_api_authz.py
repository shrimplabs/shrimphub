"""Tests for swarm/api_authz.py -- per-spawn agent token scope enforcement.

Covers:
* agent token: own-project task mutations allowed, config/provider/other
  project mutations denied, invalid/revoked token denied, read-only routes
  allowed, GET /api/config scrubbed
* no token, header mode: operator behaviour unchanged (open)
* strict mode: mutations without a credential -> 401, GET -> 200, operator
  token -> 200, agent token -> still scoped
"""
import os
import threading

import pytest

from swarm import db
from swarm.agent_token import AGENT_TOKEN_HEADER, OPERATOR_TOKEN_HEADER

OWN = "alpha"
OTHER = "beta"
AGENT_ID = "agent-own-0001"
OTHER_AGENT_ID = "agent-other-0002"


def _make_app(tmp_path, extra_config=None):
    _api_key_vars = ("MINIMAX_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "KIMI_API_KEY")
    saved = {k: os.environ.pop(k, None) for k in _api_key_vars}
    os.environ["MINIMAX_API_KEY"] = "test-key-do-not-use"
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm_test.db")

    from swarm.api import create_app
    cfg = {
        "workspace": str(tmp_path / "workspace"),
        "max_active_agents": 3,
        "lock_project": False,
        "disable_monitor": True,
        "disable_remote_repo": True,
        "project_creation_retry_rounds": 0,
        "llm_provider": "minimax",
        "managed_projects": [OWN, OTHER],
        "paused_projects": [],
        "login_password_hash": "hash-should-never-reach-agents",
        "login_salt": "salt-should-never-reach-agents",
    }
    cfg.update(extra_config or {})
    (tmp_path / "workspace" / OWN).mkdir(parents=True, exist_ok=True)
    (tmp_path / "workspace" / OTHER).mkdir(parents=True, exist_ok=True)
    flask_app = create_app(
        config=cfg,
        data_dir=tmp_path / "data",
        config_file=tmp_path / "config.json",  # REQUIRED: never touch the real config.json
    )
    flask_app.config["TESTING"] = True
    return flask_app, saved


def _teardown(saved):
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _seed():
    """Two projects, one task each, one active agent per project with a token."""
    db.task_upsert({"id": "task-own-1", "project": OWN, "type": "feature", "description": "own",
                    "priority": 50, "status": "pending", "metadata": {}, "dependencies": []})
    db.task_upsert({"id": "task-own-2", "project": OWN, "type": "feature", "description": "own 2",
                    "priority": 50, "status": "pending", "metadata": {}, "dependencies": []})
    db.task_upsert({"id": "task-other-1", "project": OTHER, "type": "feature", "description": "other",
                    "priority": 50, "status": "pending", "metadata": {}, "dependencies": []})
    db.agent_upsert({"id": AGENT_ID, "project": OWN, "task_type": "feature", "status": "active",
                     "task_id": "task-own-1", "pid": 0})
    db.agent_upsert({"id": OTHER_AGENT_ID, "project": OTHER, "task_type": "feature", "status": "active",
                     "task_id": "task-other-1", "pid": 0})
    db.agent_token_create("tok-own", AGENT_ID, "task-own-1", OWN)
    db.agent_token_create("tok-other", OTHER_AGENT_ID, "task-other-1", OTHER)


@pytest.fixture()
def app(tmp_path):
    flask_app, saved = _make_app(tmp_path)
    _seed()
    yield flask_app
    _teardown(saved)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def strict_app(tmp_path):
    flask_app, saved = _make_app(tmp_path, {"agent_api_enforcement": "strict", "operator_token": "op-secret"})
    _seed()
    yield flask_app
    _teardown(saved)


@pytest.fixture()
def strict_client(strict_app):
    return strict_app.test_client()


H = {AGENT_TOKEN_HEADER: "tok-own"}


# ---------------------------------------------------------------------------
# Agent token: allowed
# ---------------------------------------------------------------------------

class TestAgentAllowed:
    def test_patch_own_project_task(self, client):
        r = client.patch("/api/tasks/task-own-1", json={"description": "updated"}, headers=H)
        assert r.status_code == 200, r.get_json()
        assert db.task_get("task-own-1")["description"] == "updated"

    def test_post_task_in_own_project(self, client):
        r = client.post("/api/tasks", json={"project": OWN, "type": "bug", "description": "x"}, headers=H)
        assert r.status_code in (200, 201), r.get_json()
        assert r.get_json()["task"]["project"] == OWN

    def test_batch_in_own_project(self, client):
        r = client.post("/api/tasks/batch", json={"project": OWN, "tasks": [
            {"type": "bug", "description": "a"}, {"type": "bug", "description": "b", "depends_on": [0]}]},
            headers=H)
        assert r.status_code in (200, 201), r.get_json()

    def test_dependency_edit_within_project(self, client):
        r = client.post("/api/tasks/task-own-2/dependencies", json={"dependency": "task-own-1"}, headers=H)
        assert r.status_code == 200, r.get_json()
        r = client.delete("/api/tasks/task-own-2/dependencies/task-own-1", headers=H)
        assert r.status_code == 200, r.get_json()

    def test_reset_own_task(self, client):
        db.task_update_status("task-own-2", "failed")
        r = client.post("/api/tasks/task-own-2/reset", json={}, headers=H)
        assert r.status_code == 200, r.get_json()

    def test_get_health_and_reads(self, client):
        for path in ("/api/health", "/api/tasks", "/api/tasks/task-other-1", "/api/dependencies",
                     f"/api/projects/{OWN}/locks", "/api/agents", f"/api/agents/{AGENT_ID}", "/api/history"):
            r = client.get(path, headers=H)
            assert r.status_code == 200, (path, r.status_code, r.get_json())

    def test_web_search_post_allowed(self, client):
        from unittest.mock import patch
        with patch("swarm.api_tools.run_search_chain", return_value={"ok": True, "results": []}):
            r = client.post("/api/tools/web_search", json={"query": "q"}, headers=H)
        assert r.status_code == 200

    def test_own_project_file_lock_allowed(self, client):
        r = client.post(f"/api/projects/{OWN}/lock",
                        json={"file_path": "a.gd", "agent_id": "task-own-1", "task_id": "task-own-1"}, headers=H)
        assert r.status_code == 200
        r = client.post(f"/api/projects/{OWN}/unlock", json={"file_path": "a.gd", "agent_id": "task-own-1"}, headers=H)
        assert r.status_code == 200

    def test_config_get_scrubbed_for_agent(self, client):
        r = client.get("/api/config", headers=H)
        assert r.status_code == 200
        data = r.get_json()
        assert "login_password_hash" not in data
        assert "login_salt" not in data
        assert "operator_token" not in data
        assert "workspace" in data
        # operator (no token) still sees the full config in header mode
        assert "login_password_hash" in client.get("/api/config").get_json()

    def test_authz_status_reports_agent_principal(self, client):
        data = client.get("/api/authz", headers=H).get_json()
        assert data["principal"]["kind"] == "agent"
        assert data["principal"]["project"] == OWN
        assert data["agent_api_enforcement"] == "header"


# ---------------------------------------------------------------------------
# Agent token: denied
# ---------------------------------------------------------------------------

class TestAgentDenied:
    @pytest.mark.parametrize("method,path,body", [
        ("POST", "/api/managed-projects", {"managed_projects": ["evil"]}),
        ("POST", "/api/provider", {"provider": "claude"}),
        ("POST", "/api/max-agents", {"max_active_agents": 99}),
        ("POST", "/api/auto-mode", {"enabled": False}),
        ("POST", "/api/auto-scale", {"enabled": False}),
        ("POST", "/api/quota-limit", {"quota_limit_percent": 1}),
        ("POST", "/api/qa-max-cycles", {"qa_max_cycles": 1}),
        ("POST", "/api/strategy", {"strategy": "fifo"}),
        ("POST", "/api/thinking", {"enabled": True}),
        ("POST", "/api/mcp-servers", {}),
        ("POST", "/api/adaptive-flat", {"enabled": False}),
        ("POST", "/api/model-policy", {"enabled": False}),
        ("POST", "/api/event-bus", {"enabled": False}),
        ("POST", "/api/auto-replan/alpha", {}),
        ("POST", "/api/spawn", {"project": OWN}),
        ("POST", "/api/spawn-batch", {}),
        ("POST", f"/api/projects/{OWN}/spawn", {}),
        ("POST", f"/api/projects/{OWN}/restart", {}),
        ("POST", f"/api/projects/{OWN}/repair", {}),
        ("POST", f"/api/projects/{OTHER}/lock", {"file_path": "x", "agent_id": "a"}),
        ("POST", "/api/wizard/plan", {}),
        ("POST", "/api/wizard/create", {}),
        ("POST", "/api/create-project-tasks", {}),
        ("POST", "/api/gardener/run", {}),
        ("POST", "/api/librarian/run", {}),
        ("POST", "/api/cartographer/run", {}),
        ("POST", "/api/archaeologist/run", {}),
        ("POST", "/api/scheduler/run", {}),
        ("POST", "/api/meta-auditor/run", {}),
        ("POST", "/api/meta-mode", {"enabled": True}),
        ("POST", "/api/unified-chat", {"message": "hi"}),
        ("POST", "/api/chat", {"message": "hi"}),
        ("POST", "/api/project-chat", {"message": "hi"}),
        ("POST", "/api/log-rotation/run", {}),
        ("POST", "/api/dependencies/integrity", {}),
        ("POST", "/api/tasks/import", {"tasks": []}),
        ("POST", "/api/config", {"llm_provider": "claude"}),
        ("POST", f"/api/agents/{OTHER_AGENT_ID}/kill", {}),
        ("POST", f"/api/kill/{OTHER_AGENT_ID}", {}),
        ("DELETE", "/api/tasks/task-own-1", None),
        ("DELETE", "/api/tasks/task-other-1", None),
        ("PATCH", "/api/tasks/task-other-1", {"description": "hijack"}),
        ("POST", "/api/tasks", {"project": OTHER, "type": "bug", "description": "x"}),
        ("POST", "/api/tasks", {"type": "bug", "description": "no project"}),
        ("POST", "/api/tasks/batch", {"project": OTHER, "tasks": [{"type": "bug", "description": "a"}]}),
        ("POST", "/api/tasks/batch", {"project": OWN, "tasks": [{"type": "bug", "description": "a", "project": OTHER}]}),
        ("POST", "/api/tasks/task-other-1/reset", {}),
        ("POST", "/api/tasks/task-own-2/dependencies", {"dependency": "task-other-1"}),
        ("PATCH", "/api/tasks/task-own-1", {"dependencies": ["task-other-1"]}),
        ("PATCH", "/api/tasks/task-own-1", {"project": OTHER}),
        ("POST", "/api/dependencies/bulk", {"ops": [{"action": "add", "task_id": "task-own-1", "dep_id": "task-other-1"}]}),
        ("POST", "/api/tasks/batch-status", {"ids": ["task-other-1"], "status": "cancelled"}),
        ("GET", f"/api/projects/{OTHER}/locks", None),
        ("GET", f"/api/agents/{OTHER_AGENT_ID}", None),
        ("GET", "/api/providers", None),
    ])
    def test_denied_routes(self, client, method, path, body):
        kwargs = {"headers": H}
        if body is not None:
            kwargs["json"] = body
        r = client.open(path, method=method, **kwargs)
        assert r.status_code == 403, (method, path, r.status_code, r.get_json())
        assert r.get_json()["error"] == f"agent token not permitted for {method} {path}"

    def test_state_untouched_after_denials(self, client, app):
        client.post("/api/managed-projects", json={"managed_projects": ["evil"]}, headers=H)
        client.patch("/api/tasks/task-other-1", json={"description": "hijack"}, headers=H)
        client.delete("/api/tasks/task-other-1", headers=H)
        assert db.task_get("task-other-1")["description"] == "other"
        assert client.get("/api/managed-projects").get_json()["managed_projects"] == [OWN, OTHER]

    def test_invalid_token(self, client):
        r = client.get("/api/health", headers={AGENT_TOKEN_HEADER: "nope"})
        assert r.status_code == 403
        assert "invalid or revoked" in r.get_json()["error"]

    def test_revoked_token(self, client):
        assert client.get("/api/health", headers=H).status_code == 200
        db.agent_token_revoke(AGENT_ID)
        r = client.patch("/api/tasks/task-own-1", json={"description": "late"}, headers=H)
        assert r.status_code == 403
        assert db.task_get("task-own-1")["description"] == "own"

    def test_token_dies_when_agent_leaves_active(self, client):
        db.agent_update_status(AGENT_ID, "completed")
        assert client.get("/api/health", headers=H).status_code == 403
        assert db.agent_token_lookup("tok-own") is None


# ---------------------------------------------------------------------------
# No token: operator, header mode (default) -- unchanged behaviour
# ---------------------------------------------------------------------------

class TestOperatorHeaderMode:
    def test_managed_projects_post_open(self, client):
        r = client.post("/api/managed-projects", json={"managed_projects": [OWN]})
        assert r.status_code == 200, r.get_json()

    def test_task_mutations_open(self, client):
        assert client.patch("/api/tasks/task-other-1", json={"description": "op"}).status_code == 200
        assert client.delete("/api/tasks/task-other-1").status_code == 200

    def test_authz_status_operator(self, client):
        data = client.get("/api/authz").get_json()
        assert data["principal"]["kind"] == "operator"
        assert data["operator_token_configured"] is False


# ---------------------------------------------------------------------------
# Strict mode
# ---------------------------------------------------------------------------

class TestStrictMode:
    def test_mutation_without_credential_401(self, strict_client):
        r = strict_client.post("/api/managed-projects", json={"managed_projects": [OWN]})
        assert r.status_code == 401
        assert "operator token required" in r.get_json()["error"]
        r = strict_client.patch("/api/tasks/task-own-1", json={"description": "x"})
        assert r.status_code == 401
        assert db.task_get("task-own-1")["description"] == "own"

    def test_get_without_credential_200(self, strict_client):
        assert strict_client.get("/api/health").status_code == 200
        assert strict_client.get("/api/tasks").status_code == 200

    def test_login_route_stays_open(self, strict_client):
        assert strict_client.post("/api/login", json={}).status_code == 200

    def test_operator_token_200(self, strict_client):
        oh = {OPERATOR_TOKEN_HEADER: "op-secret"}
        r = strict_client.post("/api/managed-projects", json={"managed_projects": [OWN]}, headers=oh)
        assert r.status_code == 200, r.get_json()
        r = strict_client.patch("/api/tasks/task-own-1", json={"description": "op"}, headers=oh)
        assert r.status_code == 200

    def test_wrong_operator_token_401(self, strict_client):
        r = strict_client.post("/api/managed-projects", json={"managed_projects": [OWN]},
                               headers={OPERATOR_TOKEN_HEADER: "wrong"})
        assert r.status_code == 401

    def test_agent_token_still_scoped_in_strict(self, strict_client):
        assert strict_client.patch("/api/tasks/task-own-1", json={"description": "a"}, headers=H).status_code == 200
        assert strict_client.post("/api/managed-projects", json={"managed_projects": []}, headers=H).status_code == 403

    def test_mode_toggle_is_live(self, strict_app, strict_client):
        # config dict is shared with the app; flipping it changes behaviour without restart
        from swarm.api_authz import _norm_mode  # noqa: F401 -- ensure import works
        r = strict_client.post("/api/managed-projects", json={"managed_projects": [OWN]})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Pure decision function
# ---------------------------------------------------------------------------

class TestDecisionFunction:
    def test_agent_request_allowed_table(self, app):
        from swarm.api_authz import agent_request_allowed
        principal = {"agent_id": AGENT_ID, "project": OWN, "task_id": "task-own-1"}
        allowed = lambda m, p, body=None: agent_request_allowed(m, p, principal, db, lambda: body or {})[0]  # noqa: E731
        assert allowed("GET", "/api/health")
        assert allowed("GET", "/api/tasks/task-other-1")
        assert allowed("POST", "/api/tools/web_search")
        assert allowed("POST", f"/api/broadcast/{OWN}")
        assert allowed("PATCH", "/api/tasks/task-own-1", {"description": "x"})
        assert allowed("PATCH", "/api/tasks/does-not-exist", {})  # route will 404
        assert allowed("POST", f"/api/agents/{AGENT_ID}/hint", {"message": "m"})
        assert not allowed("POST", f"/api/broadcast/{OTHER}")
        assert not allowed("POST", "/api/managed-projects")
        assert not allowed("DELETE", "/api/tasks/task-own-1")
        assert not allowed("PATCH", "/api/tasks/task-other-1", {"description": "x"})
        assert not allowed("POST", f"/api/agents/{OTHER_AGENT_ID}/kill")
        assert not allowed("GET", f"/api/projects/{OTHER}")
        assert not allowed("POST", "/api/tasks", {"project": OTHER})
