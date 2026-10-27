"""Tests for swarm.sandbox -- Layer 2 seatbelt containment.

Two kinds of test here:

  * Unit tests for the wrapper logic (argv shape, config gating, graceful
    degradation).  These run everywhere.
  * Behavioural tests that actually run sandbox-exec and assert the profile
    denies what it claims to deny.  These are the ones that would catch a
    profile edit that quietly opens a hole, so they are the point of the file.
    They skip off macOS.

The behavioural tests deliberately assert on *denials*.  A containment test
that only checks the happy path passes just as well with an empty profile.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from swarm import sandbox

REQUIRES_SEATBELT = pytest.mark.skipif(
    not sandbox.is_supported() or os.environ.get("SWARM_SANDBOXED") == "1",
    reason=(
        "sandbox-exec containment is macOS-only, and unavailable to a process "
        "already running under seatbelt -- nested sandbox-exec is kernel-denied "
        "(sandbox_apply: Operation not permitted) regardless of is_supported()"
    ),
)

CONTROLLER_ROOT = Path(__file__).resolve().parent.parent


def _run_sandboxed(
    shell_cmd: str,
    workspace: Path,
    data_dir: Path,
    timeout: int = 30,
    *,
    home: Path | None = None,
    profile: Path | None = None,
    params: dict | None = None,
    project: str | None = None,
    worktree: Path | None = None,
):
    """Run a /bin/sh snippet under the profile; return CompletedProcess.

    ``profile`` swaps in a mutated copy of the profile and ``params``
    overrides individual ``-D`` parameters -- both exist so the mutation
    tests below can prove a denial depends on the rule it claims to test.
    """
    argv = sandbox.build_command(
        ["/bin/sh", "-c", shell_cmd],
        workspace=workspace,
        data_dir=data_dir,
        home_dir=home,
        project=project,
        worktree=worktree,
    )
    if profile is not None:
        argv[argv.index("-f") + 1] = str(profile)
    for key, value in (params or {}).items():
        idx = next(i for i, a in enumerate(argv) if a.startswith(f"{key}="))
        argv[idx] = f"{key}={os.path.realpath(value)}"
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _allowed(shell_cmd: str, workspace: Path, data_dir: Path, **kw) -> bool:
    """True when the snippet ran successfully inside the sandbox."""
    return _run_sandboxed(shell_cmd, workspace, data_dir, **kw).returncode == 0


def _denied(shell_cmd: str, workspace: Path, data_dir: Path, **kw) -> bool:
    """True only when the sandbox itself refused the operation.

    A bare non-zero exit is not a denial: ``ls ~/.aws`` fails just as well
    with ENOENT when the directory does not exist, which is how the original
    credential tests passed on a machine without ~/.aws.  Denial probes must
    therefore leave stderr alone so the EPERM is visible here.
    """
    result = _run_sandboxed(shell_cmd, workspace, data_dir, **kw)
    return result.returncode != 0 and "Operation not permitted" in result.stderr


#: Every credential path the profile denies, relative to HOME_DIR.  Dirs end
#: in "/"; the fake_home fixture creates each one so a probe can only fail on
#: EPERM, never on a missing file.
CREDENTIAL_PATHS = [
    ".ssh/", ".aws/", ".gnupg/", ".config/gh/", ".docker/",
    "Library/Keychains/", ".local/share/keyrings/", ".netrc", ".git-credentials",
]


@pytest.fixture
def outside_root():
    """A scratch root that no profile rule already allows.

    pytest's tmp_path lives under $TMPDIR, i.e. inside VAR_FOLDERS, which the
    profile allows wholesale -- so a write-denial test built on tmp_path
    passes no matter what the rule under test says (swarm-controller-q8o1).
    ~/Library/Caches is likewise allowed, so this uses a plain home-directory
    path, which is denied by default.  test_scratch_root_is_not_already_writable
    proves that stays true.
    """
    import shutil
    import uuid

    root = Path.home() / f".swarm_sandbox_test_{uuid.uuid4().hex[:8]}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def fake_home(outside_root):
    """A HOME_DIR with every credential path present, outside all write roots.

    Probing the operator's real home made the tests depend on which secrets
    this particular machine happens to have.
    """
    home = outside_root / "home"
    for rel in CREDENTIAL_PATHS:
        target = home / rel
        if rel.endswith("/"):
            target.mkdir(parents=True)
            (target / "secret").write_text("s3cret\n")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("s3cret\n")
    return home


def _mutated_profile(dest: Path, old: str, new: str) -> Path:
    """Copy of the real profile with one rule rewritten.

    Asserts ``old`` is present so a profile edit breaks the mutation test
    loudly instead of turning it into a no-op.
    """
    text = sandbox.profile_path().read_text()
    assert old in text, f"mutation anchor not found in profile: {old!r}"
    out = dest / "mutated.sb"
    out.write_text(text.replace(old, new, 1))
    return out


def _require_network(url: str) -> None:
    """Skip, rather than pass vacuously, when the host itself is offline."""
    probe = subprocess.run(
        ["/usr/bin/curl", "-s", "-m", "8", "-o", "/dev/null", url],
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"{url} unreachable outside the sandbox; egress denial would be vacuous")


# ---------------------------------------------------------------------------
# Wrapper logic
# ---------------------------------------------------------------------------

class TestEnabledFlag:
    def test_disabled_by_default(self):
        assert sandbox.enabled({}) is False

    def test_disabled_for_none_config(self):
        assert sandbox.enabled(None) is False

    def test_enabled_when_set(self):
        assert sandbox.enabled({"sandbox_agents": True}) is True

    def test_falsy_values_do_not_enable(self):
        for value in (False, 0, "", None, []):
            assert sandbox.enabled({"sandbox_agents": value}) is False


class TestMaybeWrap:
    def test_passthrough_when_disabled(self, tmp_path):
        argv = [sys.executable, "agent.py"]
        wrapped, note = sandbox.maybe_wrap(
            argv, config={}, workspace=tmp_path, data_dir=tmp_path
        )
        assert wrapped == argv
        assert note is None

    def test_returns_a_copy_not_the_original_list(self, tmp_path):
        argv = [sys.executable, "agent.py"]
        wrapped, _ = sandbox.maybe_wrap(
            argv, config={}, workspace=tmp_path, data_dir=tmp_path
        )
        assert wrapped is not argv

    def test_degrades_gracefully_when_unsupported(self, tmp_path, monkeypatch):
        """An unavailable sandbox must not break spawning -- but must say so."""
        monkeypatch.setattr(sandbox, "is_supported", lambda: False)
        argv = [sys.executable, "agent.py"]
        wrapped, note = sandbox.maybe_wrap(
            argv,
            config={"sandbox_agents": True},
            workspace=tmp_path,
            data_dir=tmp_path,
        )
        assert wrapped == argv, "spawn must still proceed unsandboxed"
        assert note and "unavailable" in note

    @REQUIRES_SEATBELT
    def test_wraps_when_enabled(self, tmp_path):
        argv = [sys.executable, "agent.py"]
        wrapped, note = sandbox.maybe_wrap(
            argv,
            config={"sandbox_agents": True},
            workspace=tmp_path,
            data_dir=tmp_path,
        )
        assert wrapped[0] == sandbox.SANDBOX_EXEC
        assert wrapped[-2:] == argv
        assert note == "sandbox-exec containment active"


class TestBuildCommand:
    @REQUIRES_SEATBELT
    def test_passes_every_profile_parameter(self, tmp_path):
        cmd = sandbox.build_command(
            ["/bin/echo", "hi"], workspace=tmp_path, data_dir=tmp_path / "data"
        )
        joined = " ".join(cmd)
        for key in (
            "HOME_DIR",
            "WORKSPACE_DIR",
            "CONTROLLER_DIR",
            "DATA_DIR",
            "TMP_DIR",
            "VAR_FOLDERS",
        ):
            assert f"{key}=" in joined, f"profile parameter {key} not supplied"

    @REQUIRES_SEATBELT
    def test_paths_are_realpaths(self, tmp_path):
        """Seatbelt matches real paths; /tmp must be resolved or rules miss."""
        cmd = sandbox.build_command(
            ["/bin/echo"], workspace=tmp_path, data_dir=tmp_path
        )
        tmp_param = next(c for c in cmd if c.startswith("TMP_DIR="))
        assert tmp_param.split("=", 1)[1] == os.path.realpath("/tmp")

    @REQUIRES_SEATBELT
    def test_profile_is_checked_into_the_repo(self):
        assert sandbox.profile_path().is_file()
        assert sandbox.profile_path().is_relative_to(CONTROLLER_ROOT)


# ---------------------------------------------------------------------------
# Behaviour: the profile actually contains the agent
# ---------------------------------------------------------------------------

@REQUIRES_SEATBELT
class TestProfileCompiles:
    def test_profile_loads_without_error(self, tmp_path):
        result = _run_sandboxed("echo ok", tmp_path, tmp_path)
        assert result.returncode == 0, result.stderr
        assert "ok" in result.stdout


@REQUIRES_SEATBELT
class TestCredentialsAreUnreachable:
    """The whole point of Layer 2: an agent must not reach operator secrets.

    Probes a synthetic HOME_DIR that is the agent's own project (PROJECT_DIR),
    so every credential path is covered by a write root and readable by the broad read
    rule.  The only thing standing between the agent and the file is the
    credential deny itself -- which is what these tests claim to test.  The
    first version probed the operator's real home with tmp_path roots and
    ``2>/dev/null``: missing dirs (ENOENT) and default-deny both looked like
    containment, and it never noticed the deny was ordered before the write
    allows and so lost to them (swarm-controller-q8o1).
    """

    @staticmethod
    def _roots(fake_home, outside_root, tmp_path):
        return {"workspace": outside_root, "data_dir": tmp_path, "home": fake_home}

    def test_fake_home_is_otherwise_reachable(self, fake_home, outside_root, tmp_path):
        """Positive control: a non-credential file in the same home is writable."""
        r = self._roots(fake_home, outside_root, tmp_path)
        assert _allowed(
            f"echo x > {fake_home / '.zshrc'!s} && cat {fake_home / '.zshrc'!s}",
            r["workspace"], r["data_dir"], home=r["home"], project="home",
        )

    @pytest.mark.parametrize("rel", CREDENTIAL_PATHS)
    def test_credentials_cannot_be_read(self, rel, fake_home, outside_root, tmp_path):
        target = fake_home / rel / "secret" if rel.endswith("/") else fake_home / rel
        r = self._roots(fake_home, outside_root, tmp_path)
        assert _denied(
            f"cat {target!s}", r["workspace"], r["data_dir"], home=r["home"], project="home"
        ), f"agent could read {rel}"

    @pytest.mark.parametrize("rel", CREDENTIAL_PATHS)
    def test_credentials_cannot_be_written(self, rel, fake_home, outside_root, tmp_path):
        """~/.ssh/authorized_keys is persistence; ~/.netrc is credential injection."""
        target = fake_home / rel / "authorized_keys" if rel.endswith("/") else fake_home / rel
        before = target.read_text() if target.exists() else None
        r = self._roots(fake_home, outside_root, tmp_path)
        assert _denied(
            f"echo pwned > {target!s}", r["workspace"], r["data_dir"], home=r["home"], project="home"
        ), f"agent could write {rel}"
        assert (target.read_text() if target.exists() else None) == before

    def test_symlinked_project_resolving_to_home_does_not_open_credentials(
        self, fake_home, outside_root, tmp_path
    ):
        """PROJECT_DIR is re-allowed for read and write; the deny must still win."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "proj").symlink_to(fake_home)
        argv = sandbox.build_command(
            ["/bin/sh", "-c", f"cat {fake_home / '.ssh' / 'secret'!s}"],
            workspace=workspace, data_dir=tmp_path, home_dir=fake_home, project="proj",
        )
        assert f"PROJECT_DIR={os.path.realpath(fake_home)}" in argv
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        assert result.returncode != 0 and "Operation not permitted" in result.stderr

    def test_real_home_credentials_are_denied(self, tmp_path):
        """The shipped HOME_DIR wiring, for whichever stores this host has."""
        home = Path.home()
        present = [rel for rel in CREDENTIAL_PATHS if (home / rel.rstrip("/")).exists()]
        if not present:
            pytest.skip("no credential stores on this host")
        for rel in present:
            cmd = f"ls {home / rel!s}" if rel.endswith("/") else f"cat {home / rel!s}"
            assert _denied(cmd + " >/dev/null", tmp_path, tmp_path), f"agent could read ~/{rel}"

    def test_removing_the_deny_is_detected(self, fake_home, outside_root, tmp_path):
        """Mutation check: without the rule, the probes above must fail."""
        profile = _mutated_profile(
            tmp_path, "(deny file-read* file-write*\n", "(allow file-read-metadata\n"
        )
        r = self._roots(fake_home, outside_root, tmp_path)
        assert _allowed(
            f"cat {fake_home / '.ssh' / 'secret'!s} && echo x > {fake_home / '.netrc'!s}",
            r["workspace"], r["data_dir"], home=r["home"], project="home", profile=profile,
        )

    def test_deny_ordered_before_the_allows_is_detected(self, fake_home, outside_root, tmp_path):
        """Mutation check for the q8o1 bug itself: seatbelt is last-match-wins."""
        text = sandbox.profile_path().read_text()
        start = text.index("(deny file-read* file-write*\n")
        end = text.index("\n\n", start)
        deny = text[start:end]
        anchor = "(allow file-read* file-read-metadata)\n"
        assert anchor in text
        moved = text[:start] + text[end:]
        moved = moved.replace(anchor, anchor + deny + "\n", 1)
        profile = tmp_path / "early-deny.sb"
        profile.write_text(moved)
        target = fake_home / ".ssh" / "authorized_keys"
        r = self._roots(fake_home, outside_root, tmp_path)
        assert _allowed(
            f"echo pwned > {target!s}", r["workspace"], r["data_dir"],
            home=r["home"], project="home", profile=profile,
        ), "early-ordered deny no longer loses to the PROJECT_DIR allow -- update this test"


@REQUIRES_SEATBELT
class TestEgressIsRestricted:
    """Ports 80 and 443 are the broad exfiltration path; both must be shut.

    Each denial is checked against a host-level probe first: with the
    machine offline curl fails either way and the test would say nothing.
    """

    @pytest.mark.parametrize("url", ["https://example.com", "http://example.com"])
    def test_open_internet_is_blocked(self, url, tmp_path):
        _require_network(url)
        assert not _allowed(
            f"/usr/bin/curl -s -m 8 -o /dev/null {url}", tmp_path, tmp_path
        ), f"agent reached {url}"

    def test_model_vendor_endpoint_is_blocked_directly(self, tmp_path):
        """Agents reach models through the local proxy, never vendor APIs."""
        url = "https://api.minimax.io"
        probe = subprocess.run(
            ["/usr/bin/curl", "-s", "-m", "8", "-o", "/dev/null", url], capture_output=True
        )
        if probe.returncode in (6, 7, 28):  # DNS / connect / timeout: host offline
            pytest.skip(f"{url} unreachable outside the sandbox")
        assert not _allowed(f"/usr/bin/curl -s -m 8 -o /dev/null {url}", tmp_path, tmp_path)

    def test_opening_443_is_detected(self, tmp_path):
        """Mutation check: an allow for :443 must turn the denial above green."""
        url = "https://example.com"
        _require_network(url)
        anchor = "(allow network-outbound (literal \"/private/var/run/mDNSResponder\"))"
        profile = _mutated_profile(
            tmp_path, anchor, anchor + '\n(allow network-outbound (remote ip "*:443"))'
        )
        assert _allowed(
            f"/usr/bin/curl -s -m 8 -o /dev/null {url}", tmp_path, tmp_path, profile=profile
        )

    def test_localhost_allowlisted_port_is_reachable(self, tmp_path):
        """Positive control: negative checks alone pass with the network fully off.

        Serves on a port from the StateServer allowlist rather than hitting
        :5001, which may or may not be running.  The earlier version ended in
        ``|| true`` and so could not fail at all.
        """
        import http.server
        import threading

        port = 11131
        try:
            server = http.server.HTTPServer(("127.0.0.1", port), http.server.SimpleHTTPRequestHandler)
        except OSError:
            pytest.skip(f"localhost:{port} in use")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = _run_sandboxed(
                f"/usr/bin/curl -s -m 5 -o /dev/null http://localhost:{port}/",
                tmp_path, tmp_path,
            )
            assert result.returncode == 0, f"curl exit {result.returncode}: {result.stderr}"
        finally:
            server.shutdown()
            server.server_close()


@REQUIRES_SEATBELT
class TestWritesAreScoped:
    def test_workspace_is_writable(self, tmp_path):
        """Agents must be able to do their actual job."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        assert _allowed(
            f"echo x > {workspace / 'probe.txt'!s}", workspace, tmp_path
        )
        assert (workspace / "probe.txt").read_text().strip() == "x"

    def test_data_dir_is_writable(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        assert _allowed(
            f"echo x > {data_dir / 'agent_probe.log'!s}", tmp_path, data_dir
        )

    def test_data_dir_is_the_allow_under_test(self, outside_root, tmp_path):
        """Re-run the data-dir test where only DATA_DIR can grant the write."""
        data = outside_root / "data"
        data.mkdir()
        assert _allowed(f"echo x > {data / 'p'!s}", tmp_path, data)
        assert _denied(f"echo x > {outside_root / 'q'!s}", tmp_path, tmp_path)

    def test_scratch_root_is_not_already_writable(self, outside_root, tmp_path):
        """Guards the guard: every write-denial below relies on outside_root
        being outside every allow rule."""
        assert _denied(f"echo x > {outside_root / 'canary.txt'!s}", tmp_path, tmp_path), (
            "scratch root is writable with unrelated roots; denial tests built "
            "on it would pass vacuously"
        )

    def test_controller_checkout_is_read_only(self, tmp_path):
        """An agent must never be able to rewrite its own guardrails.

        The checkout is passed as the agent's own project, so the PROJECT_DIR
        allow covers it and only the CONTROLLER_DIR deny can refuse the write.
        Probes a path that does not exist rather than an existing source file:
        if the profile is ever broken, a failing containment test must not
        also corrupt the repo it is defending.  (It did exactly that once
        during development, which is why this note is here.)
        """
        probe = CONTROLLER_ROOT / ".swarm_sandbox_probe"
        try:
            assert _denied(
                f"echo x > {probe!s}", CONTROLLER_ROOT.parent, tmp_path,
                project=CONTROLLER_ROOT.name,
            ), "agent could write into the controller checkout"
            assert not probe.exists()
        finally:
            probe.unlink(missing_ok=True)

    def test_removing_controller_deny_is_detected(self, tmp_path):
        """Mutation check: the test above must depend on the CONTROLLER_DIR deny."""
        profile = _mutated_profile(
            tmp_path,
            '(deny file-write* (subpath (param "CONTROLLER_DIR")))',
            ";; controller deny removed by mutation test",
        )
        probe = CONTROLLER_ROOT / ".swarm_sandbox_probe"
        try:
            assert _allowed(
                f"echo x > {probe!s}", CONTROLLER_ROOT.parent, tmp_path, profile=profile,
                project=CONTROLLER_ROOT.name,
            )
        finally:
            probe.unlink(missing_ok=True)

    def test_guardrail_source_cannot_be_overwritten(self, tmp_path):
        """Named explicitly: these are the files Layer 1 depends on.

        Uses a copy into a sibling path and a byte-for-byte checksum of the
        real file, so a profile regression is detected without ever appending
        to the guardrail source.  Workspace covers the checkout, as above.
        """
        import hashlib

        for rel in ("swarm/tools/path_guard.py", "swarm/tools/shell_guard.py"):
            target = CONTROLLER_ROOT / rel
            if not target.is_file():
                continue
            before = hashlib.sha256(target.read_bytes()).hexdigest()
            denied = _denied(
                f"cp {target!s} {target!s}.sbprobe", CONTROLLER_ROOT.parent, tmp_path,
                project=CONTROLLER_ROOT.name,
            )
            after = hashlib.sha256(target.read_bytes()).hexdigest()
            Path(f"{target!s}.sbprobe").unlink(missing_ok=True)
            assert after == before, f"{rel} was modified by the probe"
            assert denied, f"agent could write beside {rel} in the checkout"

    def test_home_dotfiles_are_not_broadly_writable(self, tmp_path):
        """Writing ~/.zshrc would give an agent persistence on the host."""
        probe = Path.home() / ".swarm_sandbox_probe"
        try:
            assert _denied(f"echo x > {probe!s}", tmp_path, tmp_path)
            assert not probe.exists()
        finally:
            probe.unlink(missing_ok=True)


@REQUIRES_SEATBELT
class TestAgentWorkloadsStillFunction:
    """Containment that breaks the fleet is not containment, it is an outage."""

    def test_python_can_import_the_agent_runtime(self, tmp_path):
        venv_python = CONTROLLER_ROOT / ".venv" / "bin" / "python"
        if not venv_python.is_file():
            pytest.skip("no .venv in this checkout")
        result = _run_sandboxed(
            f"cd {CONTROLLER_ROOT!s} && {venv_python!s} -c "
            "'import swarm.agent_runtime; print(\"ok\")'",
            tmp_path,
            tmp_path,
            timeout=90,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        assert "ok" in result.stdout

    def test_git_can_read_a_repository(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo = workspace / "proj"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        result = _run_sandboxed(
            f"/usr/bin/git -C {repo!s} status --short", workspace, tmp_path
        )
        assert result.returncode == 0, result.stderr


class TestProjectDirParam:
    """Symlinked projects need their resolved path re-added as a write root.

    The workspace entry for most managed projects is a symlink into another
    tree.  Seatbelt checks the resolved path, so without PROJECT_DIR every
    write into such a project is denied -- the bug that made QA screenshots
    fail with EPERM while non-symlinked projects worked fine.
    """

    def test_sentinel_when_no_project(self, tmp_path):
        assert sandbox.project_dir_param(tmp_path, None).startswith("/dev/null")

    def test_project_inside_workspace_is_its_own_root(self, tmp_path):
        # The workspace is no longer a write root, so even a plain project
        # directory needs PROJECT_DIR (2026-10-26 narrowing).
        (tmp_path / "proj").mkdir()
        assert sandbox.project_dir_param(tmp_path, "proj") == os.path.realpath(tmp_path / "proj")

    @pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "../escape"])
    def test_never_the_workspace_or_a_parent(self, tmp_path, bad):
        assert sandbox.project_dir_param(tmp_path, bad).startswith("/dev/null")

    def test_resolves_symlinked_project_to_its_real_path(self, tmp_path):
        outside = tmp_path / "elsewhere" / "proj"
        outside.mkdir(parents=True)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "proj").symlink_to(outside)
        assert sandbox.project_dir_param(workspace, "proj") == os.path.realpath(outside)

    @REQUIRES_SEATBELT
    def test_param_is_passed_to_sandbox_exec(self, tmp_path):
        cmd = sandbox.build_command(
            ["/bin/echo"], workspace=tmp_path, data_dir=tmp_path, project="p"
        )
        assert "PROJECT_DIR=" in " ".join(cmd)


@REQUIRES_SEATBELT
class TestProjectDirDoesNotWidenContainment:
    """PROJECT_DIR adds exactly one project -- never its parent directory.

    The symlink targets share a parent that also holds the controller
    checkout, so a parent-level allow would hand agents write access to their
    own guardrails.  These tests exist to stop anyone "simplifying" the rule
    that way.
    """

    def _sandboxed(self, cmd, workspace, data_dir, project):
        argv = sandbox.build_command(
            ["/bin/sh", "-c", cmd],
            workspace=workspace,
            data_dir=data_dir,
            project=project,
        )
        return subprocess.run(argv, capture_output=True, text=True, timeout=30)

    def _layout(self, root):
        """workspace/ with two projects symlinked into a shared outside parent."""
        parent = root / "outside"
        parent.mkdir()
        workspace = root / "workspace"
        workspace.mkdir()
        data_dir = root / "data"
        data_dir.mkdir()
        for name in ("mine", "theirs"):
            (parent / name).mkdir()
            (workspace / name).symlink_to(parent / name)
        return workspace, parent, data_dir

    def test_symlinked_project_becomes_writable(self, outside_root):
        workspace, parent, data_dir = self._layout(outside_root)
        result = self._sandboxed(
            f"echo x > {parent / 'mine' / 'probe.txt'!s}", workspace, data_dir, "mine"
        )
        assert result.returncode == 0, result.stderr

    def test_sibling_project_stays_denied(self, outside_root):
        """One agent's root must not cover the project next to it."""
        workspace, parent, data_dir = self._layout(outside_root)
        result = self._sandboxed(
            f"echo x > {parent / 'theirs' / 'probe.txt'!s} 2>/dev/null",
            workspace,
            data_dir,
            "mine",
        )
        assert result.returncode != 0, "PROJECT_DIR leaked write access to a sibling"

    def test_controller_checkout_denied_even_when_named_as_project(self, tmp_path):
        """The deny must outrank the allow, whatever project is passed."""
        probe = CONTROLLER_ROOT / ".swarm_sandbox_probe"
        try:
            result = self._sandboxed(
                f"echo x > {probe!s} 2>/dev/null",
                CONTROLLER_ROOT.parent,
                tmp_path,
                CONTROLLER_ROOT.name,
            )
            assert result.returncode != 0, "agent could write the controller checkout"
            assert not probe.exists()
        finally:
            probe.unlink(missing_ok=True)


@REQUIRES_SEATBELT
class TestWorkspaceIsNotWritable:
    """2026-10-26: agents write only their own project, worktree and bare remote.

    The profile used to allow the whole WORKSPACE_DIR, so an agent -- or any
    script it ran -- could delete every sibling project.  outside_root is the
    workspace here, so no other rule (VAR_FOLDERS, ~/Library/Caches) can be
    what grants or refuses a write.  Probes only create files.
    """

    @pytest.fixture
    def ws(self, outside_root):
        for d in ("game-a", "game-b", ".wt-game-a-1111", ".wt-game-b-2222",
                  "_remotes/game-a.git", "_remotes/game-b.git"):
            (outside_root / d).mkdir(parents=True)
        return outside_root

    def _run(self, ws, tmp_path, target, **kw):
        return dict(workspace=ws, data_dir=tmp_path, project="game-a",
                    worktree=ws / ".wt-game-a-1111", **kw)

    @pytest.mark.parametrize("rel", ["game-a/f.txt", ".wt-game-a-1111/f.txt", "_remotes/game-a.git/f.txt"])
    def test_own_roots_are_writable(self, ws, tmp_path, rel):
        assert _allowed(f"echo x > {ws / rel!s}", **self._run(ws, tmp_path, rel))

    @pytest.mark.parametrize("rel", ["game-b/f.txt", ".wt-game-b-2222/f.txt", "_remotes/game-b.git/f.txt",
                                     "f.txt", "new-project/f.txt"])
    def test_everything_else_in_the_workspace_is_refused(self, ws, tmp_path, rel):
        cmd = f"mkdir -p {(ws / rel).parent!s} && echo x > {ws / rel!s}"
        assert _denied(cmd, **self._run(ws, tmp_path, rel)), f"agent could write {rel}"
        assert not (ws / rel).exists()

    def test_sibling_project_cannot_be_renamed_away(self, ws, tmp_path):
        assert _denied(f"mv {ws / 'game-b'!s} {ws / 'game-b-gone'!s}", **self._run(ws, tmp_path, ""))
        assert (ws / "game-b").is_dir()

    def test_restoring_the_workspace_allow_is_detected(self, ws, tmp_path):
        """Mutation check on a COPY of the profile with a harmless probe."""
        profile = _mutated_profile(
            tmp_path,
            '(allow file-write*\n  ;; Controller data dir',
            '(allow file-write*\n  (subpath (param "WORKSPACE_DIR"))\n  ;; Controller data dir',
        )
        assert _allowed(f"echo x > {ws / 'game-b' / 'f.txt'!s}",
                        **self._run(ws, tmp_path, "", profile=profile))

    def test_symlinked_project_writes_land_in_its_real_tree(self, outside_root, tmp_path):
        real = outside_root / "elsewhere" / "game-c"
        real.mkdir(parents=True)
        ws = outside_root / "ws"
        ws.mkdir()
        (ws / "game-c").symlink_to(real)
        assert _allowed(f"echo x > {ws / 'game-c' / 'f.txt'!s}", ws, tmp_path, project="game-c")
        assert _denied(f"echo x > {outside_root / 'elsewhere' / 'f.txt'!s}", ws, tmp_path, project="game-c")


@REQUIRES_SEATBELT
def test_agent_can_commit_in_its_worktree(outside_root, tmp_path):
    """A worktree commit writes into the project's .git (objects, refs,
    .git/worktrees/<name>) -- both roots must be writable, nothing else."""
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}
    ws = outside_root
    proj = ws / "game-a"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=proj, check=True, env=env)
    (proj / "a.txt").write_text("1")
    subprocess.run(["git", "add", "."], cwd=proj, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True, env=env)
    wt = ws / ".wt-game-a-1111"
    subprocess.run(["git", "worktree", "add", "-q", str(wt), "-b", "agent/1111"], cwd=proj, check=True, env=env)
    cmd = (f"cd {wt!s} && echo 2 > b.txt && git add b.txt && "
           f"GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t "
           f"git commit -qm agent-work")
    r = _run_sandboxed(cmd, ws, tmp_path, project="game-a", worktree=wt)
    assert r.returncode == 0, r.stderr
    log = subprocess.run(["git", "log", "--format=%s", "agent/1111"], cwd=proj,
                         capture_output=True, text=True, env=env).stdout
    assert "agent-work" in log
