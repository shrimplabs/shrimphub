"""swarm.api_authz -- request classification + scope enforcement for the API.

Background: the API on :5001 is open by default (``login_required=false``)
and reachable from agent subprocesses on the same host.  Before this module
an agent could POST ``/api/managed-projects``, switch the provider, delete
tasks in other projects, kill agents or create whole projects through the
wizard (see ``docs/spawn-test-proj-incident.md``).

Every request is classified by ``before_request``:

* **AGENT** -- carries ``X-Swarm-Agent-Token`` (minted per spawn by
  ``agent_lifecycle.spawn_agent`` and stored in the ``agent_tokens`` table).
  An invalid / revoked token is a 403.  A valid token is scoped to the
  agent's own project: read-only routes + task mutations whose every touched
  task belongs to that project + a few own-project / own-agent routes.
  Everything else is a 403 with ``{"error": "agent token not permitted for
  <METHOD> <path>"}`` and a ``[Authz] agent <id> denied ...`` log line.

* **OPERATOR** -- no agent token (dashboard, tools/swarm-code.py, MCP server).
  Behaviour depends on ``config["agent_api_enforcement"]``:

  - ``"header"`` (default): unchanged from before -- open when
    ``login_required`` is false.  Protects against the tool layer and against
    prompt-injected misuse of tool calls (the tool layer always sends the
    token; ``run_command`` curl/wget to :5001 with a non-GET method is blocked
    by ``swarm.tools.shell_guard``).
  - ``"strict"``: every mutating request (anything but GET/HEAD/OPTIONS) must
    carry either a valid agent token or an operator credential --
    ``X-Swarm-Operator-Token`` matching ``config["operator_token"]`` or a
    valid login session (``Authorization: Bearer``).  Otherwise 401.

Agents reading ``GET /api/config`` get a scrubbed copy (no password hashes,
salts, tokens, or API keys).
"""

from __future__ import annotations

import hmac
import re
from typing import Callable, Optional, Tuple

from flask import g, jsonify, request

from swarm.agent_token import AGENT_TOKEN_HEADER, OPERATOR_TOKEN_HEADER

ENFORCEMENT_MODES = ("header", "strict")
DEFAULT_ENFORCEMENT = "header"

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Operator routes that must stay reachable without any credential even in
# strict mode: login bootstrap + game telemetry ingestion (games post without
# credentials).
_STRICT_EXEMPT_PATHS = frozenset({"/api/login", "/api/logout", "/api/session", "/api/telemetry"})

# Config keys an agent must never see in GET /api/config.
_SECRET_KEY_RE = re.compile(r"(token|secret|password|passwd|api_key|apikey|_key$|salt|hash|credential)", re.I)

# ---------------------------------------------------------------------------
# Agent allow-table (read-only)
# ---------------------------------------------------------------------------
# Prefixes an agent may GET regardless of project.
_AGENT_GET_PREFIXES: tuple = (
    "/api/tasks",           # list, get, dependencies, dependents, delegation, export
    "/api/task-lookup/",
    "/api/task-history",
    "/api/dependencies",    # graph, dot, ready, execution-order, integrity (GET), ...
    "/api/broadcast",
    "/api/knowledge",
    "/api/learnings",
    "/api/history",
    "/api/agents",          # list / active / <id> -- own-agent check applied below
    "/api/agent/",
)
_AGENT_GET_EXACT: frozenset = frozenset({
    "/api/health", "/api/ping", "/api/quota", "/api/config", "/api/projects", "/api/authz",
})

_RE_TASK = re.compile(r"^/api/tasks/(?P<tid>[^/]+)(?P<rest>/.*)?$")
_RE_PROJECT = re.compile(r"^/api/projects/(?P<name>[^/]+)(?P<rest>/.*)?$")
_RE_AGENT = re.compile(r"^/api/(?:agents|agent)/(?P<aid>[^/]+)(?P<rest>/.*)?$")
_RE_KILL = re.compile(r"^/api/kill/(?P<aid>[^/]+)$")
_RE_PLANS = re.compile(r"^/api/plans/(?P<name>[^/]+)(?P<rest>/.*)?$")
_RE_BROADCAST = re.compile(r"^/api/broadcast/(?P<name>[^/]+)$")

# Own-project POST sub-routes under /api/projects/<own>/...
# NOTE: these are the *file*-lock endpoints the agent runtime depends on
# (runtime_helpers._claim_file_lock / _unlock_claimed_files / lock-conflict
# handoff); the project-level lock used with ``lock_project`` is internal.
_AGENT_PROJECT_POST_SUBPATHS: frozenset = frozenset({
    "/lock", "/unlock", "/lock-conflict-handoff", "/notes",
    # Controller-side git push of the agent's own project (swarm.git_push):
    # takes no arguments, so an agent cannot pick the remote, ref or --force.
    "/push", "/fetch",
})
# Task sub-routes an agent may mutate when the task is in its project.
_AGENT_TASK_MUTATION_SUBPATHS: frozenset = frozenset({
    "", "/dependencies", "/reset", "/insert-after", "/insert-before-gate",
})
# Task routes that are NOT per-task ids (must be matched before _RE_TASK).
_TASK_COLLECTION_ROUTES: frozenset = frozenset({"batch", "batch-status", "import", "export"})


def _norm_mode(config: dict) -> str:
    mode = str((config or {}).get("agent_api_enforcement") or DEFAULT_ENFORCEMENT).strip().lower()
    return mode if mode in ENFORCEMENT_MODES else DEFAULT_ENFORCEMENT


def _json_body() -> dict:
    try:
        data = request.get_json(silent=True)
    except Exception:
        data = None
    return data if isinstance(data, dict) else {}


def _ids_from(value) -> list:
    """Coerce a dependency-ish payload value into a list of id strings."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if isinstance(v, (str, int))]
    return []


class _Scope:
    """Project-scope checks for one agent request."""

    def __init__(self, db, project: str):
        self.db = db
        self.project = project

    def task_in_project(self, task_id: str) -> bool:
        """True when *task_id* is absent (route will 404) or lives in our project."""
        if not task_id:
            return False
        try:
            task = self.db.task_get(task_id)
        except Exception:
            return False
        if task is None:
            return True
        return (task.get("project") or "") == self.project

    def all_in_project(self, task_ids) -> bool:
        return all(self.task_in_project(t) for t in task_ids)

    def body_deps_ok(self, body: dict) -> bool:
        return self.all_in_project(_ids_from(body.get("dependencies")))

    def containment_meta_unchanged(self, task_id: str, body: dict) -> bool:
        """False when *body* would set or clear a containment-breaker metadata key.

        PATCH replaces metadata wholesale, so dropping a key counts as a change.
        """
        if "metadata" not in body:
            return True
        new_meta = body.get("metadata")
        if not isinstance(new_meta, dict):
            new_meta = {}
        try:
            task = self.db.task_get(task_id) or {}
        except Exception:
            return False
        old_meta = task.get("metadata") or {}
        if not isinstance(old_meta, dict):
            old_meta = {}
        from swarm.containment_breaker import PROTECTED_META_KEYS
        return all(new_meta.get(k) == old_meta.get(k) for k in PROTECTED_META_KEYS)

    def task_item_ok(self, item: dict, require_project: bool) -> bool:
        proj = item.get("project")
        if proj is not None and proj != "" and proj != self.project:
            return False
        if require_project and not proj:
            return False
        if not self.body_deps_ok(item):
            return False
        return True


def agent_request_allowed(method: str, path: str, principal: dict, db,
                          body_getter: Callable[[], dict] = _json_body) -> Tuple[bool, str]:
    """Decide whether an AGENT request is inside its scope.

    Returns ``(allowed, reason)``.  ``principal`` is the token row
    (``agent_id``, ``project``, ``task_id``).
    """
    method = (method or "GET").upper()
    project = principal.get("project") or ""
    agent_id = principal.get("agent_id") or ""
    scope = _Scope(db, project)

    if not path.startswith("/api/"):
        return True, "non-api"

    # ---- read-only -------------------------------------------------------
    if method in _READ_METHODS:
        if path in _AGENT_GET_EXACT:
            return True, "read"
        m = _RE_PROJECT.match(path)
        if m:
            return (m.group("name") == project), "own-project read"
        m = _RE_PLANS.match(path)
        if m:
            return (m.group("name") == project), "own-project plans"
        m = _RE_AGENT.match(path)
        if m and m.group("aid") not in ("active",):
            return (m.group("aid") == agent_id), "own-agent read"
        if any(path == p.rstrip("/") or path.startswith(p) for p in _AGENT_GET_PREFIXES):
            return True, "read"
        return False, "read route not in agent allow-list"

    # ---- mutations -------------------------------------------------------
    if method == "POST" and path == "/api/tools/web_search":
        return True, "web_search"
    if method == "POST" and path == "/api/tools/fetch_url":
        return True, "fetch_url (controller-side egress guard)"

    m = _RE_BROADCAST.match(path)
    if m and method == "POST":
        return (m.group("name") == project), "own-project broadcast"

    if path == "/api/tasks" and method == "POST":
        body = body_getter()
        return scope.task_item_ok(body, require_project=True), "create task in own project"

    if path == "/api/tasks/batch" and method == "POST":
        body = body_getter()
        default_project = body.get("project") or ""
        if default_project and default_project != project:
            return False, "batch default project is not own project"
        items = body.get("tasks") or []
        if not isinstance(items, list):
            return False, "batch tasks must be a list"
        for item in items:
            if not isinstance(item, dict):
                continue
            if not scope.task_item_ok(item, require_project=not default_project):
                return False, "batch item outside own project"
        return True, "batch create in own project"

    if path == "/api/tasks/batch-status" and method == "POST":
        body = body_getter()
        return scope.all_in_project(_ids_from(body.get("ids"))), "batch-status in own project"

    if path == "/api/dependencies/bulk" and method == "POST":
        body = body_getter()
        ops = body.get("ops") or []
        if not isinstance(ops, list):
            return False, "bulk ops must be a list"
        for op in ops:
            if not isinstance(op, dict):
                continue
            if not scope.all_in_project([op.get("task_id") or "", op.get("dep_id") or ""]):
                return False, "bulk op outside own project"
        return True, "bulk deps in own project"

    m = _RE_TASK.match(path)
    if m and m.group("tid") not in _TASK_COLLECTION_ROUTES:
        tid = m.group("tid")
        rest = m.group("rest") or ""
        if method == "DELETE":
            # DELETE /api/tasks/<id> is operator-only; DELETE of a single dep
            # edge is a dependency edit and is allowed inside the project.
            dm = re.match(r"^/dependencies/(?P<dep>[^/]+)$", rest)
            if dm:
                ok = scope.task_in_project(tid) and scope.task_in_project(dm.group("dep"))
                return ok, "remove dep edge in own project"
            return False, "task deletion is operator-only"
        if method in ("PATCH", "PUT", "POST") and rest in _AGENT_TASK_MUTATION_SUBPATHS:
            if not scope.task_in_project(tid):
                return False, "task outside own project"
            body = body_getter()
            new_proj = body.get("project")
            if rest == "" and new_proj not in (None, "", project):
                return False, "cannot move task to another project"
            if rest == "/dependencies":
                deps = _ids_from(body.get("dependencies")) + _ids_from(body.get("dependency"))
                if not scope.all_in_project(deps):
                    return False, "dependency outside own project"
            elif not scope.body_deps_ok(body):
                return False, "dependency outside own project"
            if rest == "" and not scope.containment_meta_unchanged(tid, body):
                return False, "containment hold/review metadata is operator-only"
            return True, "task mutation in own project"
        return False, "task route not in agent allow-list"

    m = _RE_PROJECT.match(path)
    if m and method == "POST":
        rest = m.group("rest") or ""
        if m.group("name") == project and rest in _AGENT_PROJECT_POST_SUBPATHS:
            return True, "own-project route"
        return False, "project route not permitted for agents"

    m = _RE_AGENT.match(path) or _RE_KILL.match(path)
    if m and method == "POST":
        return (m.group("aid") == agent_id), "own-agent action"

    return False, "route not in agent allow-list"


# ---------------------------------------------------------------------------
# Operator credential check (strict mode)
# ---------------------------------------------------------------------------

def _operator_authenticated(config: dict) -> bool:
    expected = str((config or {}).get("operator_token") or "")
    presented = request.headers.get(OPERATOR_TOKEN_HEADER, "") or ""
    if expected and presented and hmac.compare_digest(expected, presented):
        return True
    session_token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if session_token:
        try:
            from swarm.login import verify_session
            if verify_session(session_token):
                return True
        except Exception:
            pass
    return False


def _scrub_config(data: dict) -> dict:
    return {k: v for k, v in data.items() if not _SECRET_KEY_RE.search(str(k))}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(app, config: dict, db) -> None:
    """Install the before/after request hooks and ``GET /api/authz``."""
    mode = _norm_mode(config)
    if mode == "strict" and not (config.get("operator_token") or config.get("login_required")):
        print("[Authz] WARNING: agent_api_enforcement=strict but neither operator_token nor "
              "login_required is configured -- every operator mutation will be rejected (401)")

    @app.before_request
    def _swarm_authz():
        path = request.path or ""
        if not path.startswith("/api/"):
            return None
        method = (request.method or "GET").upper()
        token = (request.headers.get(AGENT_TOKEN_HEADER) or "").strip()

        if token:
            principal = None
            try:
                principal = db.agent_token_lookup(token)
            except Exception as e:
                print(f"[Authz] token lookup error: {e}")
            if not principal:
                print(f"[Authz] agent <unknown> denied {method} {path} (invalid or revoked token)")
                return jsonify({"error": "invalid or revoked agent token"}), 403
            g.swarm_principal = {
                "kind": "agent",
                "agent_id": principal.get("agent_id"),
                "task_id": principal.get("task_id"),
                "project": principal.get("project"),
            }
            allowed, _reason = agent_request_allowed(method, path, principal, db)
            if not allowed:
                aid = (principal.get("agent_id") or "")[:8]
                print(f"[Authz] agent {aid} denied {method} {path} ({_reason})")
                return jsonify({"error": f"agent token not permitted for {method} {path}"}), 403
            return None

        # OPERATOR request (no agent token)
        g.swarm_principal = {"kind": "operator"}
        current_mode = _norm_mode(config)
        if current_mode != "strict" or method in _READ_METHODS or path in _STRICT_EXEMPT_PATHS:
            return None
        if _operator_authenticated(config):
            return None
        print(f"[Authz] operator request denied {method} {path} (strict mode, no credential)")
        return jsonify({
            "error": f"operator token required for {method} {path} (agent_api_enforcement=strict)"
        }), 401

    @app.after_request
    def _swarm_authz_scrub(response):
        principal = getattr(g, "swarm_principal", None) or {}
        if principal.get("kind") == "agent" and request.path == "/api/config" \
                and request.method == "GET" and response.mimetype == "application/json":
            try:
                data = response.get_json(silent=True)
                if isinstance(data, dict):
                    response.set_data(jsonify(_scrub_config(data)).get_data())
            except Exception as e:
                print(f"[Authz] config scrub error: {e}")
        return response

    @app.route("/api/authz", methods=["GET"])
    def authz_status():
        principal = getattr(g, "swarm_principal", None) or {"kind": "operator"}
        return jsonify({
            "agent_api_enforcement": _norm_mode(config),
            "operator_token_configured": bool(config.get("operator_token")),
            "principal": principal,
        })
