"""swarm.agent_token -- agent-side helpers for the per-spawn API token.

Every agent subprocess is launched with ``SWARM_AGENT_TOKEN`` in its
environment (generated in ``swarm.agent_lifecycle.spawn_agent``).  The
controller's ``swarm.api_authz`` hook classifies any request carrying that
token in ``X-Swarm-Agent-Token`` as an *agent* request and scopes it to the
agent's own project.

This module is deliberately dependency-free (no Flask, no swarm.db; only the
stdlib-only ``swarm.guard_log``) so it can
be imported from the agent process and from the controller alike.

Public API:

    agent_token()               -> str    token from the environment ("" if unset)
    agent_auth_headers()        -> dict   {"X-Swarm-Agent-Token": token} or {}
    install_urllib_opener()     -> None   global urllib opener that stamps the
                                          header on every request to the local
                                          swarm API (localhost:<API_PORT>)
"""

from __future__ import annotations

import os
import urllib.request
from urllib.parse import urlsplit

from swarm.guard_log import log_blocked

AGENT_TOKEN_ENV = "SWARM_AGENT_TOKEN"
AGENT_TOKEN_HEADER = "X-Swarm-Agent-Token"
OPERATOR_TOKEN_HEADER = "X-Swarm-Operator-Token"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"})


def agent_token() -> str:
    """Return the agent token from the environment, or ``""``."""
    return (os.environ.get(AGENT_TOKEN_ENV) or "").strip()


def agent_auth_headers() -> dict:
    """Headers to merge into any agent-side request to the swarm API."""
    tok = agent_token()
    return {AGENT_TOKEN_HEADER: tok} if tok else {}


def _api_port() -> int:
    try:
        return int(os.environ.get("API_PORT") or 5001)
    except (TypeError, ValueError):
        return 5001


def _is_local_api_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except Exception:
        return False
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return host in _LOOPBACK_HOSTS and port == _api_port()


class _AgentTokenHandler(urllib.request.BaseHandler):
    """urllib handler that adds ``X-Swarm-Agent-Token`` to local API calls."""

    handler_order = 100  # run before the default handlers

    def http_request(self, req):
        tok = agent_token()
        # urllib stores header names as str.capitalize() -- check both forms.
        already = req.has_header(AGENT_TOKEN_HEADER) or req.has_header(AGENT_TOKEN_HEADER.capitalize())
        if tok and not already and _is_local_api_url(req.full_url):
            req.add_unredirected_header(AGENT_TOKEN_HEADER, tok)
        return req

    https_request = http_request

    def http_response(self, req, response):
        # Runs before HTTPErrorProcessor turns the 403 into an HTTPError, so
        # every agent-side caller of the local API is covered in one place.
        if getattr(response, "status", None) == 403 and _is_local_api_url(req.full_url):
            log_blocked("api", "swarm_api", f"{req.get_method()} {urlsplit(req.full_url).path}")
        return response

    https_response = http_response


_installed = False


def install_urllib_opener() -> bool:
    """Install a global ``urllib`` opener that stamps the agent token on every
    request to the local swarm API.  Idempotent; no-op when no token is set.

    Returns True when a token is present (opener installed or already
    installed), False otherwise.
    """
    global _installed
    if not agent_token():
        return False
    if _installed:
        return True
    opener = urllib.request.build_opener(_AgentTokenHandler())
    urllib.request.install_opener(opener)
    _installed = True
    return True
