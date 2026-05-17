"""
Pytest fixtures for dashboard Playwright tests.

Fixtures defined here (conftest.py) are automatically discovered by pytest.
Helper functions and mock data live in conftest_dashboard.py.
"""
import os
import sys
import time

import pytest
from playwright.sync_api import sync_playwright


def pytest_addoption(parser):
    parser.addoption(
        "--trace-tests",
        action="store_true",
        default=False,
        help="Print test start/end timing to help isolate hangs.",
    )


def _trace_enabled(config) -> bool:
    return bool(config.getoption("--trace-tests")) or os.environ.get("SWARM_TEST_TRACE") == "1"


def _trace_write(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if _trace_enabled(item.config):
        item._trace_started_at = time.time()
        _trace_write(f"[TEST START] {item.nodeid}")
    outcome = yield
    if _trace_enabled(item.config):
        started = getattr(item, "_trace_started_at", None)
        elapsed = f"{time.time() - started:.2f}s" if isinstance(started, float) else "?"
        _trace_write(f"[TEST END] {item.nodeid} elapsed={elapsed}")
    return outcome


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not _trace_enabled(item.config):
        return
    if report.when != "call":
        return
    status = "PASSED" if report.passed else "FAILED" if report.failed else "SKIPPED"
    duration = f"{report.duration:.2f}s"
    _trace_write(f"[TEST {status}] {item.nodeid} duration={duration}")


@pytest.fixture(scope="function")
def browser():
    """Launch a headless Chromium browser for the test duration."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture(scope="function")
def page(browser):
    """Create a new browser page within the test browser context."""
    context = browser.new_context()
    page = context.new_page()
    yield page
    context.close()
