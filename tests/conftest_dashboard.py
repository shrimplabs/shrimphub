"""
Shared fixtures and helpers for dashboard Playwright tests.

All API calls to http://localhost:5001 are intercepted via page.route()
so no live server is needed. Uses the sync Playwright API.
"""
import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

DASHBOARD_PATH = Path(__file__).parent.parent / "dashboard.html"
PROJECT_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Default mock payloads
# ---------------------------------------------------------------------------

EMPTY_STATE = {
    "/api/projects":        {"projects": {}},
    "/api/tasks":           {"tasks": []},
    "/api/agents":          {"agents": []},
    "/api/history":         {"agents": []},
    "/api/quota":           {"model_remains": []},
    "/api/auto-mode":       {"enabled": False},
    "/api/max-agents":      {"max_active_agents": 3, "max_lines": 2000},
    "/api/managed-projects": {"managed_projects": [], "paused_projects": []},
    "/api/providers": {
        "current": "minimax",
        "providers": {
            "minimax": {"model": "MiniMax-M2.5", "api_key_env": "MINIMAX_API_KEY", "api_key_set": True},
            "claude":  {"model": "claude-sonnet-4-6", "api_key_env": "ANTHROPIC_API_KEY", "api_key_set": False},
        }
    },
}

AGENT_ACTIVE = {
    "id": "aaaa-bbbb-cccc-dddd",
    "project": "my-game",
    "task_type": "feature",
    "status": "active",
    "spawned_at": "2026-03-06T10:00:00",
    "exit_code": None,
    "output": "",
    "metadata": {},
}

AGENT_COMPLETED = {
    "id": "1111-2222-3333-4444",
    "project": "my-game",
    "task_type": "bug",
    "status": "completed",
    "spawned_at": "2026-03-06T09:00:00",
    "completed_at": "2026-03-06T09:30:00",
    "exit_code": 0,
    "output": "[Agent] loop 5/120\nTASK_COMPLETE",
    "metadata": {"diff_stat": "main.gd | 15 +++++++++++++++\n 1 file changed, 15 insertions(+)"},
}

TASK_PENDING = {
    "id": "task-001",
    "project": "my-game",
    "type": "feature",
    "description": "Add player movement",
    "priority": 50,
    "status": "pending",
    "dependencies": [],
    "metadata": {},
    "attempts": 0,
    "max_attempts": 3,
}

TASK_IN_PROGRESS = {
    "id": "task-002",
    "project": "my-game",
    "type": "bug",
    "description": "Fix collision detection",
    "priority": 80,
    "status": "in_progress",
    "dependencies": [],
    "metadata": {},
    "attempts": 0,
    "max_attempts": 3,
}

PROJECT_DATA = {
    "my-game": {
        "status": "active",
        "locked": False,
        "files": {"scripts/main.gd": 500, "scripts/player.gd": 200},
        "recent_commits": [
            {"hash": "abc1234", "message": "Add player sprite", "age": "2 hours ago"},
        ],
        "sprint": 0,
    }
}

PROJECT_OVERFLOW = {
    "big-project": {
        "status": "active",
        "locked": True,
        "files": {"scripts/main.gd": 9000},
        "recent_commits": [],
        "sprint": 0,
    }
}

QUOTA_DATA = {
    "model_remains": [{
        "model_name": "MiniMax-M2.5",
        "current_interval_usage_count": 200,   # remaining=200, used=800, pct=80%
        "current_interval_total_count": 1000,
        "remains_time": 3661000,
    }]
}


# ---------------------------------------------------------------------------
# Route setup (sync)
# ---------------------------------------------------------------------------

def make_mock_routes(overrides: dict = None) -> dict:
    routes = dict(EMPTY_STATE)
    if overrides:
        routes.update(overrides)
    return routes


def _get_static_content(filename):
    """Read static file from project root."""
    fpath = PROJECT_ROOT / filename
    if fpath.exists():
        return fpath.read_bytes()
    return b""


def setup_routes(page, routes: dict):
    """Intercept all API calls and serve mock JSON responses (sync handler).

    The dashboard uses file:// in tests, so window.location.protocol === 'file:'
    makes it fall back to http://localhost:5001 as its API base — which means
    all requests land on localhost:5001 and are intercepted here.
    """
    def handle(route):
        url = route.request.url
        # Strip query strings for static-file matching (e.g. ?v=20260409d cache busters)
        url_path = url.split("?")[0]

        # Handle static JS/CSS requests (from file:// or http://)
        _static_map = {
            "dashboard.css": ("text/css", "dashboard.css"),
            "dashboard.js": ("application/javascript", "dashboard.js"),
            "dashboard_closure.js": ("application/javascript", "dashboard_closure.js"),
            "dashboard_deps_integrity.js": ("application/javascript", "dashboard_deps_integrity.js"),
        }
        for suffix, (content_type, filename) in _static_map.items():
            if url_path.endswith(f"/{suffix}"):
                route.fulfill(
                    status=200,
                    content_type=content_type,
                    body=_get_static_content(filename),
                )
                return

        # Handle http://localhost:5001 API requests
        if "localhost:5001" in url:
            path = "/" + url.split("localhost:5001/", 1)[-1].split("?")[0]

            # Exact match
            if path in routes:
                body = routes[path]
            else:
                # Prefix match for sub-paths (e.g. /api/projects/my-game/health)
                body = next(
                    (v for k, v in routes.items() if path.startswith(k) and k != "/"),
                    {"ok": True},
                )

            # For mutations default to a generic success body unless overridden
            if route.request.method in ("POST", "PUT", "DELETE") and path not in routes:
                body = {"success": True, "count": 0, "spawned": [], "skipped": []}

            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(body),
            )
            return

        # Fallback: continue the route (let browser handle normally)
        route.continue_()

    # Intercept static assets (with and without cache-buster query strings)
    page.route("**/dashboard.css*", handle)
    page.route("**/dashboard.js*", handle)
    page.route("**/dashboard_closure.js*", handle)
    page.route("**/dashboard_deps_integrity.js*", handle)
    page.route("http://localhost:5001/**", handle)


def load(page, overrides: dict = None):
    """Set routes, navigate, wait for first render."""
    setup_routes(page, make_mock_routes(overrides))
    page.goto(DASHBOARD_PATH.as_uri())
    # Wait until loadData() has run: lastUpdated shows a real timestamp (not the initial '-')
    page.wait_for_function("document.getElementById('lastUpdated') !== null && document.getElementById('lastUpdated').textContent !== '' && document.getElementById('lastUpdated').textContent !== '-'")


@pytest.fixture
def page():
    """Launch a headless Chromium browser and return a new page.

    The dashboard tests intercept ALL API calls via page.route() so no
    live server is needed. Static assets (dashboard.css, dashboard.js) are
    served from the project root via route handlers in setup_routes().
    """
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()

    yield page

    page.close()
    context.close()
    browser.close()
    pw.stop()
