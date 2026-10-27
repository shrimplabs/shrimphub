"""Regression tests for swarm-controller-5dnz.

1. git_commit()/git_push() must refuse when the pinned project root is not
   the top level of a git repository -- and must never fall back to the
   process cwd (pre-fix: run() -> pinned_cwd(None) -> _safe_cwd() ->
   os.getcwd(), so an agent for a missing/non-repo project committed and
   pushed whatever repo the controller was running from).
2. The generated agent wrapper refuses to start from a pytest temp path.
3. The conftest Popen guard fakes agent_*.py launches.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import swarm.tools._shared as _shared
import swarm.tools.core as _core
from swarm.tools import shell
from swarm.tools.shell import git_commit, git_push, git_root
from swarm.tools.shell_guard import pinned_cwd


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def _init_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("initial\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _commit_count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD").stdout.strip() or 0)


@pytest.fixture
def writable_task(monkeypatch):
    """git_commit is gated on core.READONLY / TASK_TYPE -- make it a normal feature task."""
    monkeypatch.setattr(_core, "READONLY", False)
    monkeypatch.setattr(_core, "TASK_TYPE", "feature")
    monkeypatch.setattr(_shared, "TASK_TYPE", "feature")
    monkeypatch.setattr(_core, "PROJECT", "some-project")
    monkeypatch.setattr(_shared, "PROJECT", "")
    shell.invalidate_shell_cache()
    yield


def _point_root_at(monkeypatch, root: Path, workspace: Path):
    monkeypatch.setattr(_shared, "WORKSPACE", workspace)
    monkeypatch.setattr(_shared, "PROJECT_PATH_OVERRIDE", str(root))
    monkeypatch.setattr(_core, "WORKSPACE", workspace)
    monkeypatch.setattr(_core, "PROJECT_PATH_OVERRIDE", str(root))


class TestGitCommitNeverTargetsCwd:

    def test_missing_project_root_refuses_and_leaves_cwd_repo_untouched(self, tmp_path, monkeypatch, writable_task):
        """Pre-fix reproduction: cwd is a git repo, the configured project root
        does not exist (and neither does WORKSPACE). pinned_cwd() therefore
        resolves to os.getcwd() -- which is exactly where the old git_commit
        ran `git add -A && git commit`. The guarded git_commit must refuse and
        the cwd repo must have no new commit and nothing staged."""
        cwd_repo = _init_repo(tmp_path / "controller-checkout")
        (cwd_repo / "stray.txt").write_text("would have been committed\n")
        monkeypatch.chdir(cwd_repo)

        missing_root = tmp_path / "workspace" / "ghost-project"   # never created
        _point_root_at(monkeypatch, missing_root, tmp_path / "no-such-workspace")

        # Prove the hazard: the shell cwd pin falls back to the process cwd.
        assert Path(pinned_cwd(None)).resolve() == cwd_repo.resolve()

        before = _commit_count(cwd_repo)
        result = git_commit("feat: should never land")

        assert result["ok"] is False
        assert result["error"].startswith("git refused:")
        assert str(missing_root) in result["error"]
        assert "not the top level of a git repository" in result["error"]
        assert _commit_count(cwd_repo) == before, "cwd repo gained a commit"
        assert _git(cwd_repo, "diff", "--cached", "--name-only").stdout.strip() == "", "cwd repo had files staged"
        assert "?? stray.txt" in _git(cwd_repo, "status", "--porcelain").stdout

    def test_existing_non_git_project_root_refuses(self, tmp_path, monkeypatch, writable_task):
        cwd_repo = _init_repo(tmp_path / "controller-checkout")
        (cwd_repo / "stray.txt").write_text("x\n")
        monkeypatch.chdir(cwd_repo)

        plain_dir = tmp_path / "workspace" / "plain-project"
        plain_dir.mkdir(parents=True)
        (plain_dir / "main.gd").write_text("extends Node\n")
        _point_root_at(monkeypatch, plain_dir, tmp_path / "workspace")

        before = _commit_count(cwd_repo)
        result = git_commit("feat: nope")
        assert result["ok"] is False and result["error"].startswith("git refused:")
        assert _commit_count(cwd_repo) == before
        assert (plain_dir / ".git").exists() is False

    def test_subdirectory_of_a_repo_is_not_a_top_level(self, tmp_path, monkeypatch, writable_task):
        """A root *inside* a repo would let `git -C root` succeed while the
        realpath differs from --show-toplevel -- must still be refused."""
        repo = _init_repo(tmp_path / "repo")
        sub = repo / "nested" / "project"
        sub.mkdir(parents=True)
        (sub / "file.txt").write_text("x\n")
        _point_root_at(monkeypatch, sub, tmp_path)

        root, err = git_root()
        assert root is None and "not the top level" in err and str(repo) in err
        before = _commit_count(repo)
        assert git_commit("feat: nested")["ok"] is False
        assert _commit_count(repo) == before

    def test_git_push_refuses_outside_a_repo(self, tmp_path, monkeypatch, writable_task):
        cwd_repo = _init_repo(tmp_path / "controller-checkout")
        monkeypatch.chdir(cwd_repo)
        _point_root_at(monkeypatch, tmp_path / "missing", tmp_path / "missing-ws")
        result = git_push()
        assert result["ok"] is False and result["error"].startswith("git refused:")

    def test_git_commit_in_real_project_repo_still_works(self, tmp_path, monkeypatch, writable_task):
        """A proper tmp git repo as the pinned project root commits normally,
        even when the process cwd is some *other* repo."""
        other_repo = _init_repo(tmp_path / "other-checkout")
        (other_repo / "stray.txt").write_text("x\n")
        monkeypatch.chdir(other_repo)

        project = _init_repo(tmp_path / "workspace" / "real-project")
        (project / "feature.gd").write_text("extends Node\n")
        _point_root_at(monkeypatch, project, tmp_path / "workspace")

        root, err = git_root()
        assert err is None and Path(root).resolve() == project.resolve()

        before = _commit_count(project)
        result = git_commit("feat: add feature.gd")
        assert result["ok"] is True, result
        assert _commit_count(project) == before + 1
        assert _git(project, "log", "-1", "--pretty=%s").stdout.strip() == "feat: add feature.gd"
        assert _git(project, "status", "--porcelain").stdout.strip() == ""
        # the other repo (cwd) is untouched
        assert _commit_count(other_repo) == 1
        assert "?? stray.txt" in _git(other_repo, "status", "--porcelain").stdout

    def test_linked_worktree_root_is_accepted(self, tmp_path, monkeypatch, writable_task):
        """PROJECT_PATH_OVERRIDE points at a linked worktree during validation
        runs; git reports the worktree itself as top level, so it must pass."""
        main = _init_repo(tmp_path / "main-repo")
        wt = tmp_path / "wt"
        r = _git(main, "worktree", "add", "-q", "-b", "agent/wt", str(wt))
        assert r.returncode == 0, r.stderr
        _point_root_at(monkeypatch, wt, tmp_path)
        root, err = git_root()
        assert err is None and Path(root).resolve() == wt.resolve()
        (wt / "new.gd").write_text("extends Node\n")
        assert git_commit("feat: in worktree")["ok"] is True
        assert _commit_count(wt) == 2


class TestWrapperRefusesPytestPaths:

    def _generate(self, tmp_path):
        import swarm_runner as sr
        from swarm_runner import generate_task_script
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        orig = (sr.WORKSPACE, sr.DATA_DIR)
        sr.WORKSPACE = tmp_path / "workspace"
        sr.DATA_DIR = data_dir
        try:
            return generate_task_script({
                "id": "feature-refuse-001", "project": "spawn-test-proj",
                "type": "feature", "description": "Do something", "metadata": {},
            })
        finally:
            sr.WORKSPACE, sr.DATA_DIR = orig

    def test_wrapper_exits_before_importing_agent_runtime(self, tmp_path):
        assert "pytest-" in str(tmp_path), "this test relies on pytest's tmp_path naming"
        script = tmp_path / "wrapper_under_test.py"
        script.write_text(self._generate(tmp_path), encoding="utf-8")

        env = {k: v for k, v in os.environ.items() if k != "SWARM_ALLOW_TEST_SPAWN"}
        # A bogus swarm-controller checkout on sys.path would still be importable
        # (the wrapper hardcodes the real one) -- so prove the refusal happens
        # *before* the import by checking the output and exit code only.
        proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=60)
        assert proc.returncode == 0
        assert "REFUSING TO START" in proc.stdout
        assert "pytest temp path" in proc.stdout
        assert "[Agent]" not in proc.stdout and "[LLM]" not in proc.stdout
        assert not list(tmp_path.glob("**/*_tokens.json"))
        assert not list(tmp_path.glob("**/*.exit"))


class TestPopenGuard:

    def test_agent_script_popen_is_faked(self, tmp_path, fake_agent_spawn):
        from tests.conftest import FakePopen
        script = tmp_path / "data" / "agent_00000000-dead-beef.py"
        script.parent.mkdir()
        # Looks like a real generated wrapper (imports the agent runtime).
        script.write_text("import swarm.agent_runtime as rt\nraise SystemExit('REAL SPAWN HAPPENED')\n")
        p = subprocess.Popen([sys.executable, str(script)])
        assert isinstance(p, FakePopen)
        assert p.poll() is None
        p.terminate()
        assert p.wait(1) == -15
        assert fake_agent_spawn["count"]() == 1
        assert str(script) in fake_agent_spawn["argv"]()[0]

    def test_non_agent_popen_is_real(self):
        from tests.conftest import FakePopen
        r = subprocess.run([sys.executable, "-c", "print('hi')"], capture_output=True, text=True)
        assert r.stdout.strip() == "hi"
        with subprocess.Popen([sys.executable, "-c", "pass"]) as real:
            real.wait(5)
        assert not isinstance(real, FakePopen)

    def test_missing_agent_script_is_faked_fail_closed(self, tmp_path, fake_agent_spawn):
        from tests.conftest import FakePopen
        p = subprocess.Popen([sys.executable, str(tmp_path / "data" / "agent_missing.py")])
        assert isinstance(p, FakePopen)
        p.kill()

    def test_harmless_stub_agent_script_runs_for_real(self, tmp_path, fake_agent_spawn):
        """tests/test_lifecycle.py spawns `sys.exit(0)`-style stubs named
        agent_<id>.py and needs them to really run; they never import the
        agent runtime, so the guard lets them through (and records them)."""
        from tests.conftest import FakePopen
        script = tmp_path / "data" / "agent_stub.py"
        script.parent.mkdir()
        script.write_text("import sys\nsys.exit(3)\n")
        p = subprocess.Popen([sys.executable, str(script)])
        assert not isinstance(p, FakePopen)
        assert p.wait(30) == 3
        assert str(script) in fake_agent_spawn["stats"]["stub_spawns"]
        assert fake_agent_spawn["count"]() == 0
