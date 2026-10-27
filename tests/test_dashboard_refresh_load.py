"""Dashboard refresh must not flood the API (2026-09-30).

loadData ran every 5s and refetched /health, /closure and /notes (twice) for
every project card -- ~600 requests per refresh with ~200 projects.  The
server fell behind, refreshes took ~40s and overlapped, each adding its own
burst.  Per-project detail is now cached for PROJECT_DETAIL_TTL_MS, fetched a
few at a time, and refreshes never overlap.
"""

import re

from playwright.sync_api import Page

from tests.conftest_dashboard import PROJECT_DATA, load, page  # noqa: F401

N = 30
PROJECTS = {f"game-{i:02d}": dict(PROJECT_DATA["my-game"]) for i in range(N)}


def _count(requests, suffix):
    return sum(1 for u in requests if re.search(rf"/api/projects/[^/]+/{suffix}(\?|$)", u))


def _recording(page: Page):
    seen = []
    page.on("request", lambda r: seen.append(r.url))
    return seen


def test_project_detail_is_fetched_once_per_ttl(page: Page):
    seen = _recording(page)
    load(page, {"/api/projects": {"projects": PROJECTS}})
    page.evaluate("() => loadData()")  # settle the first refresh
    first = {s: _count(seen, s) for s in ("health", "closure", "notes")}
    assert first["health"] == N and first["closure"] == N
    assert first["notes"] <= N + 1  # at most once per card (+ focus banner)

    for _ in range(3):
        page.evaluate("() => loadData()")
    assert {s: _count(seen, s) for s in ("health", "closure", "notes")} == first

    # Once the TTL has passed, each project is refetched exactly once more.
    page.evaluate("() => { for (const k in _projectDetailFetchedAt) _projectDetailFetchedAt[k] -= PROJECT_DETAIL_TTL_MS + 1; }")
    page.evaluate("() => loadData()")
    assert _count(seen, "health") == 2 * N and _count(seen, "closure") == 2 * N


def test_refreshes_never_overlap(page: Page):
    seen = _recording(page)
    load(page, {"/api/projects": {"projects": PROJECTS}})
    page.evaluate("() => loadData()")
    before = sum(1 for u in seen if u.endswith("/api/projects"))
    same = page.evaluate("async () => { const a = loadData(); const b = loadData(); await a; return a === b; }")
    after = sum(1 for u in seen if u.endswith("/api/projects"))
    assert same is True
    assert after - before == 1  # the overlapping call joined the first
    page.evaluate("() => loadData()")  # a later refresh runs normally
    assert sum(1 for u in seen if u.endswith("/api/projects")) - before == 2


def test_concurrency_is_capped(page: Page):
    load(page, {"/api/projects": {"projects": PROJECTS}})
    peak = page.evaluate("""async () => {
        let live = 0, peak = 0, done = 0;
        await runLimited(Array.from({length: 40}, (_, i) => i), PROJECT_DETAIL_CONCURRENCY, async () => {
            live++; peak = Math.max(peak, live);
            await new Promise(r => setTimeout(r, 5));
            live--; done++;
        });
        return [peak, done, PROJECT_DETAIL_CONCURRENCY];
    }""")
    assert peak[1] == 40 and 1 < peak[0] <= peak[2]
