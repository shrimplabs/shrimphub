"""
Pytest fixtures for dashboard Playwright tests.

Fixtures defined here (conftest.py) are automatically discovered by pytest.
Helper functions and mock data live in conftest_dashboard.py.
"""
import copy
import functools
import inspect
import os
import re
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from swarm.tools.path_guard import _temp_roots as _REAL_TEMP_ROOTS

# ---------------------------------------------------------------------------
# agent_runtime globals isolation
# ---------------------------------------------------------------------------
# agent_runtime has many module-level config globals (LLM_PROVIDER, WORKSPACE,
# etc.) that tests mutate without teardown, causing ordering-dependent failures
# when the full suite runs. This autouse fixture snapshots them before each test
# and restores them after, so no test can leak state to a subsequent test.

_AGENT_RUNTIME_GLOBALS = [
    "LLM_PROVIDER", "LLM_PROVIDERS", "WORKSPACE", "DATA_DIR", "PROJECT",
    "PROJECT_PATH_OVERRIDE", "TASK_TYPE", "TASK_ID", "TASK_PRIORITY",
    "MAX_TOOL_LOOPS", "QA_MAX_CYCLES", "QA_CYCLE", "MANAGED_PROJECTS",
    "LOCK_CONFLICT_HANDOFF", "PROJECT_CREATE_SYSTEM", "PROJECT_CREATE_USER",
    "PROJECT_PLAN_SYSTEM", "PROJECT_PLAN_USER",
]


# ---------------------------------------------------------------------------
# Real config.json protection
# ---------------------------------------------------------------------------
# On 2026-09-14 the real config.json was wiped down to two keys by a
# read-modify-write race. Two layers of defence for the test suite:
#   1. a session-scoped fixture that snapshots the real file's bytes at start
#      and fails loudly at the end if any test changed it;
#   2. a function-scoped fixture that wraps swarm.api.create_app so a call
#      without config_file= is redirected to a tmp path instead of the real
#      project-root config.json.

_REAL_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def _read_real_config_bytes():
    try:
        return _REAL_CONFIG_PATH.read_bytes()
    except FileNotFoundError:
        return None


@pytest.fixture(autouse=True, scope="session")
def _guard_real_config_json():
    # Preventive layer: every sanctioned writer goes through
    # config_store.update_config_file, which refuses forbidden paths, so the
    # offending test fails at the write (with a traceback) instead of the
    # file being clobbered and only reported at session teardown.
    from swarm import config_store
    real = os.path.realpath(_REAL_CONFIG_PATH)
    config_store._WRITE_FORBIDDEN.add(real)
    before = _read_real_config_bytes()
    yield
    config_store._WRITE_FORBIDDEN.discard(real)
    after = _read_real_config_bytes()
    if before != after:
        pytest.fail(
            "a test wrote to the real config.json — pass config_file=tmp_path/... to create_app "
            f"(path: {_REAL_CONFIG_PATH}; before={len(before or b'')} bytes, "
            f"after={len(after or b'')} bytes)",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _redirect_unisolated_create_app(tmp_path, monkeypatch):
    """Redirect create_app(config_file=None) to a tmp path so no test can
    touch the real config.json even if it forgets the kwarg.

    Patching swarm.api.create_app alone is not enough: any test module that
    did ``from swarm.api import create_app`` at import time already holds its
    own reference to the *original* function object, bound before this
    fixture ever runs, so monkeypatch.setattr(swarm.api, ...) never reaches
    it. Every test file in this suite uses that import form. Walk
    sys.modules and rebind the name everywhere it was imported directly, not
    just on the defining module.
    """
    try:
        import swarm.api as _api
    except Exception:
        yield
        return
    _orig = _api.create_app
    _sig = inspect.signature(_orig)

    @functools.wraps(_orig)
    def _isolated_create_app(*args, **kwargs):
        bound = _sig.bind_partial(*args, **kwargs)
        if bound.arguments.get("config_file") is None:
            bound.arguments["config_file"] = tmp_path / "config.json"
        return _orig(*bound.args, **bound.kwargs)

    monkeypatch.setattr(_api, "create_app", _isolated_create_app)
    # Only test modules import create_app directly, and restricting the scan
    # to them (and reading __dict__ instead of getattr) avoids tripping
    # lazy-attribute __getattr__ hooks on unrelated third-party modules
    # (e.g. transformers) that happen to be sitting in sys.modules -- those
    # hooks can themselves raise (missing optional deps like torchvision),
    # which would otherwise blow up every test's setup.
    for _mod_name, _mod in list(sys.modules.items()):
        if _mod is None or _mod is _api or not _mod_name.startswith("tests."):
            continue
        if _mod.__dict__.get("create_app") is _orig:
            monkeypatch.setattr(_mod, "create_app", _isolated_create_app)
    yield


@pytest.fixture(autouse=True)
def _reset_agent_runtime_globals():
    try:
        import swarm.agent_runtime as rt
    except Exception:
        yield
        return
    snapshot = {k: copy.deepcopy(getattr(rt, k)) for k in _AGENT_RUNTIME_GLOBALS if hasattr(rt, k)}
    yield
    for k, v in snapshot.items():
        setattr(rt, k, v)


@pytest.fixture(autouse=True)
def _isolate_tools_shared_cwd(tmp_path):
    """Safety net: swarm.tools._shared owns its OWN WORKSPACE/PROJECT globals,
    separate from swarm.agent_runtime's (see module docstring in _shared.py --
    it exists to break a circular import, at the cost of duplicating this
    config). They default to WORKSPACE=Path(".")/PROJECT="", which makes
    _project_root() -> _safe_cwd() resolve to the REAL current working
    directory. Any test that exercises the real tool-dispatch path (e.g. a
    work-phase test that mocks call_llm but not execute_tool) can therefore
    cause git_commit()/git_push() to run `git add -A && git commit && git
    push` for real, against whatever repo pytest happens to be invoked from.

    This isolates every test's default to an empty tmp_path (not a git repo,
    so an unmocked git_commit fails harmlessly with "not a git repository")
    instead of the real checkout. A test that needs a real git repo should
    set up its own tmp_path git init and pass project_path explicitly --
    this fixture only changes the *default* when nothing else configures it.
    """
    import swarm.tools._shared as _shared
    snapshot = {
        "WORKSPACE": _shared.WORKSPACE,
        "PROJECT": _shared.PROJECT,
        "PROJECT_PATH_OVERRIDE": _shared.PROJECT_PATH_OVERRIDE,
    }
    _shared.WORKSPACE = tmp_path
    _shared.PROJECT = ""
    _shared.PROJECT_PATH_OVERRIDE = str(tmp_path)
    yield
    for k, v in snapshot.items():
        setattr(_shared, k, v)


_REAL_HOME = os.path.realpath(os.path.expanduser("~"))
_DESTRUCTIVE = re.compile(
    r"\brm\b|\brmdir\b|\bunlink\b|rmtree|os\.remove|\.unlink\(|shutil\.move|\bmv\b|"
    r"-delete\b|\btruncate\b|\bdd\b|\bsrm\b|open\([^)]*['\"][wa]"
)
_DANGEROUS_TARGET = re.compile(
    r"(?<![\w.-])(?:/|/Users|/System|/Applications|/Library|~|\$HOME|\$\{HOME\})(?=$|[\s'\")/;*])"
    r"|expanduser|Path\.home\(|getenv\(['\"]HOME|environ\[['\"]HOME|" + re.escape(_REAL_HOME)
)


def _live_fire_text(cmd) -> str:
    """The command plus the source of any script file it executes."""
    text = str(cmd)
    for tok in re.findall(r"[^\s'\"]+\.py\b", text):
        try:
            p = Path(tok)
            if p.is_file() and p.stat().st_size < 1_000_000:
                text += "\n" + p.read_text(errors="replace")
        except OSError:
            pass
    return text


@pytest.fixture(autouse=True)
def _refuse_live_destructive_commands(monkeypatch):
    """Last line of defence for the test suite (2026-10-05 incident).

    Tests run as the real user with no sandbox.  Guard tests pass dangerous
    commands to run_command/run_python and expect a refusal; if a guard
    regresses -- or is mutated on purpose -- the command really executes.
    A guard mutation once ran `rm -rf /Users` for 60s and deleted most of
    ~/workspace and parts of $HOME.  Every shell execution in tests goes
    through swarm.tools.shell.run; refuse (and fail the test) when the
    command or the script it runs combines a destructive operation with
    /, /Users, ~ or the real home directory.  pytest.fail raises a
    BaseException, so no `except Exception` in product code can swallow it.
    """
    import swarm.tools.shell as _shell
    real_run = _shell.run

    def _guarded_run(cmd, *args, **kwargs):
        text = _live_fire_text(cmd)
        if _DESTRUCTIVE.search(text) and _DANGEROUS_TARGET.search(text):
            pytest.fail(
                "TEST SAFETY NET: refused to execute a destructive command against a real "
                f"system path -- a guard let it through:\n{text[:500]}",
                pytrace=False,
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(_shell, "run", _guarded_run)
    yield


@pytest.fixture(autouse=True)
def _no_live_wizard_llm(request, monkeypatch):
    """Project-creation code asks an LLM for GAME_DESIGN.md.  In tests that
    would hit the real provider with a dummy key and wait for the timeout
    (~20-70s per test); fail fast so the documented fallback doc is used.
    Tests that need a specific doc patch _generate_game_design_doc."""
    if request.node.path.name == "test_wizard_llm.py":  # tests _llm_call itself (HTTP is mocked there)
        yield
        return
    import swarm.api_wizard as _wiz

    def _offline(*a, **k):
        raise RuntimeError("live LLM disabled in tests")

    monkeypatch.setattr(_wiz, "_llm_call", _offline)
    yield


@pytest.fixture(autouse=True)
def _pin_scratch_roots(tmp_path, monkeypatch):
    """Keep Layer 1 denial tests load-bearing.

    path_guard._temp_roots() trusts the per-user temp dir as agent scratch
    space, and tmp_path lives inside it -- so every "outside" dir or fake
    $HOME a test builds under tmp_path would be silently allowed and its
    denial assertion would pass vacuously.  Pin the scratch roots to a single
    dir under tmp_path that nothing else uses.  Tests that exercise the real
    roots request the ``real_temp_roots`` fixture.
    """
    from swarm.tools import path_guard
    root = Path(os.path.realpath(tmp_path)) / "_scratch_root"
    monkeypatch.setattr(path_guard, "_temp_roots", lambda: (root,))
    yield


@pytest.fixture
def real_temp_roots(monkeypatch):
    """Undo ``_pin_scratch_roots`` for tests of the real /tmp, $TMPDIR roots."""
    from swarm.tools import path_guard
    monkeypatch.setattr(path_guard, "_temp_roots", _REAL_TEMP_ROOTS)
    yield



@pytest.fixture(autouse=True)
def _reset_quota_cache():
    """Reset the orchestrator quota cache timestamp so mocked HTTP calls aren't skipped."""
    try:
        import swarm.orchestrator as orc
        orc._quota_cache_ts = 0.0
    except Exception:
        pass
    yield
    try:
        import swarm.orchestrator as orc
        orc._quota_cache_ts = 0.0
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _mock_llm_sleep(monkeypatch):
    """Prevent real time.sleep() in LLM retry backoff from slowing the suite.

    The retry loop in llm_utils sleeps [10, 30, 60, 120, 240]s between attempts.
    Without this mock a single failing LLM test takes ~15 minutes.
    """
    monkeypatch.setattr("swarm.llm_utils.time.sleep", lambda _: None)


# ---------------------------------------------------------------------------
# Agent subprocess guard
# ---------------------------------------------------------------------------
# swarm-controller-5dnz: several API/spawn tests drive the REAL spawn path
# (agent_lifecycle.spawn_agent / SQLiteAgentTracker.spawn_agent), which
# subprocess.Popen()s the generated data/agent_<id>.py wrapper. Left alone,
# that wrapper runs up to 200 live LLM loops and git commit/push -- from a
# test. This guard replaces subprocess.Popen for every test (unless the test
# is marked @pytest.mark.real_spawn) with a selective wrapper: argv that
# targets an agent_*.py script gets a FakePopen; everything else (git in tmp
# repos, godot, etc.) is delegated to the real Popen unchanged.
#
# A session-scoped recorder additionally wraps the real Popen so that, if a
# real agent_*.py process is ever launched (guard bypassed, real_spawn test),
# it is recorded -- and the session fails at teardown if any such launch
# happened outside a real_spawn-marked test.
#
# Note: the wrapper is installed on the `subprocess` module itself (not on
# swarm.agent_lifecycle.subprocess as a proxy) so existing tests that
# `patch("subprocess.Popen", ...)` keep working -- they simply override the
# guard for their scope.

import itertools as _itertools
import subprocess as _subprocess
import threading as _threading

_REAL_POPEN = _subprocess.Popen
_FAKE_PID_COUNTER = _itertools.count(900_000)
_SPAWN_GUARD_STATS = {
    "fake_spawns": 0,
    "fake_argv": [],           # argv of every faked agent spawn (for assertions)
    "real_agent_spawns": [],   # (nodeid, argv, allowed_by_marker)
    "stub_spawns": [],         # argv of harmless agent_*.py stubs allowed to run for real
}
_CURRENT_TEST = {"nodeid": None, "real_spawn": False}


_REAL_WRAPPER_MARKERS = ("swarm.agent_runtime", "_swarm_refuse_test_spawn", "rt.main(")


def _agent_script_path(args):
    """Return the agent_<id>.py path in argv (or None)."""
    if args is None:
        return None
    if isinstance(args, (str, bytes, os.PathLike)):
        args = [args]
    try:
        items = list(args)
    except TypeError:
        return None
    for a in items:
        if isinstance(a, os.PathLike):
            a = os.fspath(a)
        if isinstance(a, bytes):
            a = a.decode(errors="replace")
        if not isinstance(a, str):
            continue
        for token in a.split():
            base = os.path.basename(token)
            if base.startswith("agent_") and base.endswith(".py"):
                return token
    return None


def _is_real_agent_wrapper(path) -> bool:
    """True unless the script is a harmless test stub (e.g. tests/test_lifecycle.py's
    `sys.exit(0)` scripts). A missing/unreadable script is treated as REAL so
    the guard fails closed. Real generated wrappers import swarm.agent_runtime
    and run up to 200 LLM loops + git commit/push -- those are always faked."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return True
    return any(marker in text for marker in _REAL_WRAPPER_MARKERS)


def _is_agent_script_argv(args) -> bool:
    """True when argv launches a generated data/agent_<id>.py wrapper."""
    if args is None:
        return False
    if isinstance(args, (str, bytes, os.PathLike)):
        args = [args]
    try:
        items = list(args)
    except TypeError:
        return False
    for a in items:
        if isinstance(a, os.PathLike):
            a = os.fspath(a)
        if isinstance(a, bytes):
            a = a.decode(errors="replace")
        if not isinstance(a, str):
            continue
        for token in a.split():
            base = os.path.basename(token)
            if base.startswith("agent_") and base.endswith(".py"):
                return True
    return False


class FakePopen:
    """Stand-in for a spawned agent process.

    pid comes from a counter; poll() returns None until terminate()/kill()
    (or _finish()) is called; wait() blocks on an Event so the lifecycle
    waiter thread behaves like it does with a real process.
    """

    def __init__(self, args, *popen_args, **popen_kwargs):
        self.args = args
        self.popen_args = popen_args
        self.popen_kwargs = popen_kwargs
        self.pid = next(_FAKE_PID_COUNTER)
        self.returncode = None
        self.stdout = None
        self.stderr = None
        self.stdin = None
        self._done = _threading.Event()
        _SPAWN_GUARD_STATS["fake_spawns"] += 1
        _SPAWN_GUARD_STATS["fake_argv"].append(
            [os.fspath(a) if isinstance(a, os.PathLike) else a for a in (args if not isinstance(args, (str, bytes)) else [args])]
        )

    # -- lifecycle -------------------------------------------------------
    def _finish(self, returncode=0):
        if self.returncode is None:
            self.returncode = returncode
        self._done.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise _subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode

    def communicate(self, input=None, timeout=None):
        self.wait(timeout)
        return ("", "")

    def terminate(self):
        self._finish(-15)

    def kill(self):
        self._finish(-9)

    def send_signal(self, sig):
        self._finish(-int(sig))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._finish(0)
        return False

    def __repr__(self):
        return f"<FakePopen pid={self.pid} rc={self.returncode} args={self.args!r}>"


class _RecordingPopen(_REAL_POPEN):
    """Real Popen that records any launch of a data/agent_*.py wrapper."""

    def __init__(self, args, *popen_args, **popen_kwargs):
        script = _agent_script_path(args)
        if script is not None and _is_real_agent_wrapper(script):
            _SPAWN_GUARD_STATS["real_agent_spawns"].append(
                (_CURRENT_TEST["nodeid"], args, _CURRENT_TEST["real_spawn"])
            )
        super().__init__(args, *popen_args, **popen_kwargs)


class _GuardedPopen(_RecordingPopen):
    """subprocess.Popen replacement: FakePopen for agent scripts, real otherwise."""

    def __new__(cls, args=None, *popen_args, **popen_kwargs):
        script = _agent_script_path(args)
        if script is not None:
            if _is_real_agent_wrapper(script):
                # Returning a non-instance of cls means __init__ is NOT invoked.
                return FakePopen(args, *popen_args, **popen_kwargs)
            _SPAWN_GUARD_STATS["stub_spawns"].append(script)
        return super().__new__(cls)


@pytest.fixture(autouse=True, scope="session")
def _agent_spawn_recorder():
    """Wrap the real Popen for the whole session and fail at teardown if a
    real agent_*.py process was launched by a test not marked real_spawn."""
    _subprocess.Popen = _RecordingPopen
    try:
        yield _SPAWN_GUARD_STATS
    finally:
        _subprocess.Popen = _REAL_POPEN
    leaked = [r for r in _SPAWN_GUARD_STATS["real_agent_spawns"] if not r[2]]
    if leaked:
        lines = "\n".join(f"  {nodeid}: {argv!r}" for nodeid, argv, _ in leaked)
        pytest.fail(
            "REAL agent subprocess spawned during tests (LLM calls + git commit/push!). "
            "Mark the test @pytest.mark.real_spawn if this is intentional:\n" + lines,
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def fake_agent_spawn(request, monkeypatch):
    """Install the selective Popen guard for this test (skipped for
    @pytest.mark.real_spawn). Yields the guard stats dict so tests can assert
    on faked spawns via ``fake_agent_spawn["fake_argv"]``."""
    _CURRENT_TEST["nodeid"] = request.node.nodeid
    _CURRENT_TEST["real_spawn"] = request.node.get_closest_marker("real_spawn") is not None
    if not _CURRENT_TEST["real_spawn"]:
        monkeypatch.setattr(_subprocess, "Popen", _GuardedPopen)
    before = _SPAWN_GUARD_STATS["fake_spawns"]
    before_argv = len(_SPAWN_GUARD_STATS["fake_argv"])
    yield {
        "stats": _SPAWN_GUARD_STATS,
        "fake_argv": _SPAWN_GUARD_STATS["fake_argv"][before_argv:],
        "count": lambda: _SPAWN_GUARD_STATS["fake_spawns"] - before,
        "argv": lambda: _SPAWN_GUARD_STATS["fake_argv"][before_argv:],
    }
    _CURRENT_TEST["nodeid"] = None
    _CURRENT_TEST["real_spawn"] = False


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    st = _SPAWN_GUARD_STATS
    allowed = sum(1 for r in st["real_agent_spawns"] if r[2])
    leaked = len(st["real_agent_spawns"]) - allowed
    terminalreporter.write_line(
        f"agent spawn guard: {st['fake_spawns']} faked agent spawn(s), "
        f"{allowed} real (real_spawn-marked), {leaked} real UNMARKED, "
        f"{len(st['stub_spawns'])} harmless stub script(s) run for real"
    )


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
