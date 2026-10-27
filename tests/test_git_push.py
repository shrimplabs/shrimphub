"""Controller-side git push for sandboxed agents (2026-10-26).

Agents run under seatbelt with no route to GitHub (443 denied) and no git
credentials, so the controller pushes for them.  Everything here pushes to a
local bare repository in tmp_path -- no network.
"""

import subprocess
from unittest.mock import patch

import pytest

from swarm.git_push import push_project
from tests.test_api_authz import OWN, OTHER, _make_app, _seed, _teardown

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=ENV).stdout


def _repo_with_origin(root):
    bare = root / "origin.git"
    _git(root, "init", "-q", "--bare", "-b", "main", str(bare))
    repo = root / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "remote", "add", "origin", str(bare))
    (repo / "a.txt").write_text("1")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "one")
    return repo, bare


def test_pushes_current_branch(tmp_path):
    repo, bare = _repo_with_origin(tmp_path)
    r = push_project(repo)
    assert r["ok"] is True and r["branch"] == "main"
    assert _git(bare, "rev-parse", "main") == _git(repo, "rev-parse", "HEAD")


def test_never_force_pushes(tmp_path):
    repo, bare = _repo_with_origin(tmp_path)
    push_project(repo)
    _git(repo, "commit", "-q", "--amend", "-m", "rewritten")   # diverges from origin
    r = push_project(repo)
    assert r["ok"] is False
    assert "rewritten" not in _git(bare, "log", "--format=%s", "main")


def test_skips_without_origin_and_never_raises(tmp_path):
    (tmp_path / "plain").mkdir()
    assert push_project(tmp_path / "plain")["skipped"] is True
    repo = tmp_path / "noremote"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    assert push_project(repo)["skipped"] is True


@pytest.fixture()
def app(tmp_path):
    flask_app, saved = _make_app(tmp_path)
    _seed()
    yield flask_app, tmp_path
    _teardown(saved)


def test_agent_may_push_only_its_own_project(app):
    flask_app, tmp_path = app
    ws = tmp_path / "workspace"
    repo, bare = _repo_with_origin(tmp_path)
    (ws / OWN).rmdir()            # _make_app creates it empty; make it a real repo
    (ws / OWN).symlink_to(repo)
    c = flask_app.test_client()
    r = c.post(f"/api/projects/{OWN}/push", json={}, headers={"X-Swarm-Agent-Token": "tok-own"})
    assert r.status_code == 200 and r.get_json()["ok"] is True, r.get_json()
    assert _git(bare, "rev-parse", "main") == _git(repo, "rev-parse", "HEAD")
    r = c.post(f"/api/projects/{OTHER}/push", json={}, headers={"X-Swarm-Agent-Token": "tok-own"})
    assert r.status_code == 403


def test_agent_git_push_in_worktree_defers_to_controller(monkeypatch):
    import swarm.tools.core as core
    from swarm.tools import shell
    monkeypatch.setenv("SWARM_AGENT_TOKEN", "x")
    monkeypatch.setattr(core, "WORKTREE_BRANCH", "agent/abc")
    monkeypatch.setattr(core, "READONLY", False)
    monkeypatch.setattr(shell, "git_root", lambda: ("/tmp", None))
    with patch.object(core, "_api_request") as api, patch.object(shell, "run") as run:
        r = shell.git_push()
    assert r["ok"] and "controller merges" in r["message"]
    api.assert_not_called(); run.assert_not_called()


def test_agent_git_push_outside_worktree_goes_through_controller(monkeypatch):
    import swarm.tools.core as core
    from swarm.tools import shell
    monkeypatch.setenv("SWARM_AGENT_TOKEN", "x")
    monkeypatch.setattr(core, "WORKTREE_BRANCH", "")
    monkeypatch.setattr(core, "READONLY", False)
    monkeypatch.setattr(core, "PROJECT", "game")
    monkeypatch.setattr(shell, "git_root", lambda: ("/tmp", None))
    with patch.object(core, "_api_request", return_value={"ok": True, "output": "done"}) as api, \
         patch.object(shell, "run") as run:
        r = shell.git_push()
    assert r["ok"] and "via controller" in r["message"]
    api.assert_called_once_with("POST", "/api/projects/game/push", payload={}, timeout=150)
    run.assert_not_called()


def test_push_after_merge_respects_config(tmp_path, monkeypatch):
    import swarm.agent_lifecycle as al
    from swarm import agent_finish
    repo, bare = _repo_with_origin(tmp_path)
    monkeypatch.setattr(al, "_CONFIG", {"push_after_merge": False})
    agent_finish._push_after_merge(repo)
    assert _git(bare, "branch") == ""
    monkeypatch.setattr(al, "_CONFIG", {})
    agent_finish._push_after_merge(repo)
    assert _git(bare, "rev-parse", "main") == _git(repo, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# Reconciliation with origin (2026-10-26): agents fetch/merge/resolve
# themselves; only the network hop goes through the controller.
# ---------------------------------------------------------------------------

from swarm.git_push import fetch_project, is_rejected_push


def _clone_and_commit(bare, root, name, fname, text):
    """Someone else pushes to origin."""
    other = root / name
    _git(root, "clone", "-q", str(bare), str(other))
    (other / fname).write_text(text)
    _git(other, "add", ".")
    _git(other, "commit", "-qm", f"{name} change")
    _git(other, "push", "-q", "origin", "main")


@pytest.fixture
def diverged(tmp_path):
    repo, bare = _repo_with_origin(tmp_path)
    push_project(repo)
    _clone_and_commit(bare, tmp_path, "human", "b.txt", "from origin\n")
    (repo / "c.txt").write_text("local\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "local change")
    return repo, bare


def test_fetch_reports_ahead_and_behind(diverged):
    repo, _ = diverged
    r = fetch_project(repo)
    assert r["ok"] and r["default_branch"] == "main" and r["ahead"] == 1 and r["behind"] == 1


def test_rejected_push_is_recognised(diverged):
    repo, _ = diverged
    r = push_project(repo)
    assert r["ok"] is False and is_rejected_push(r)


def _as_agent(monkeypatch, repo, worktree_branch=""):
    """Sandboxed-agent context whose controller calls are served in-process."""
    import swarm.tools.core as core
    from swarm.tools import shell
    monkeypatch.setenv("SWARM_AGENT_TOKEN", "x")
    monkeypatch.setattr(core, "PROJECT", "proj")
    monkeypatch.setattr(core, "READONLY", False)
    monkeypatch.setattr(core, "WORKTREE_BRANCH", worktree_branch)
    monkeypatch.setattr(shell, "git_root", lambda: (str(repo), None))
    calls = []

    def _api(method, path, payload=None, timeout=30):
        calls.append(path)
        project = Path(repo) if not worktree_branch else Path(repo).parent / "proj"
        return fetch_project(project) if path.endswith("/fetch") else push_project(project)
    monkeypatch.setattr(core, "_api_request", _api)
    return shell, calls


from pathlib import Path


def test_agent_git_sync_merges_origin_through_controller(diverged, monkeypatch):
    repo, bare = diverged
    shell, calls = _as_agent(monkeypatch, repo)
    monkeypatch.setattr(shell, "run", lambda cmd, cwd=None, timeout=60, env=None: _real_run(cmd, cwd or repo))
    r = shell.git_sync()
    assert r["ok"], r
    assert calls == ["/api/projects/proj/fetch"]
    assert (repo / "b.txt").read_text() == "from origin\n" and (repo / "c.txt").exists()
    assert push_project(repo)["ok"] is True          # now a fast-forward for origin


def _real_run(cmd, cwd):
    p = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, env={**ENV, "HOME": str(cwd)})
    return p.returncode, p.stdout, p.stderr


def test_agent_git_sync_leaves_conflicts_for_the_agent(tmp_path, monkeypatch):
    repo, bare = _repo_with_origin(tmp_path)
    push_project(repo)
    _clone_and_commit(bare, tmp_path, "human", "a.txt", "theirs\n")
    (repo / "a.txt").write_text("ours\n")
    _git(repo, "commit", "-qam", "ours")
    shell, _ = _as_agent(monkeypatch, repo)
    monkeypatch.setattr(shell, "run", lambda cmd, cwd=None, timeout=60, env=None: _real_run(cmd, cwd or repo))
    r = shell.git_sync()
    assert r["ok"] is False and r["conflicts"] == ["a.txt"] and "git_commit" in r["error"]
    assert "<<<<<<<" in (repo / "a.txt").read_text()            # merge left in progress
    # the agent resolves and commits; the push then succeeds without force
    (repo / "a.txt").write_text("ours and theirs\n")
    _git(repo, "commit", "-qam", "merge origin")
    assert push_project(repo)["ok"] is True


def test_worktree_sees_origin_after_controller_fetch(tmp_path, monkeypatch):
    repo, bare = _repo_with_origin(tmp_path)          # tmp_path/proj
    push_project(repo)
    _clone_and_commit(bare, tmp_path, "human", "b.txt", "from origin\n")
    wt = tmp_path / ".wt-proj-1111"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "agent/1111")
    shell, _ = _as_agent(monkeypatch, wt, worktree_branch="agent/1111")
    monkeypatch.setattr(shell, "run", lambda cmd, cwd=None, timeout=60, env=None: _real_run(cmd, cwd or wt))
    assert shell.git_sync()["ok"]
    assert (wt / "b.txt").read_text() == "from origin\n"


def test_typed_git_pull_and_push_are_rerouted(diverged, monkeypatch):
    repo, bare = diverged
    shell, calls = _as_agent(monkeypatch, repo)
    monkeypatch.setattr(shell, "run", lambda cmd, cwd=None, timeout=60, env=None: _real_run(cmd, cwd or repo))
    r = shell.run_command("git pull origin main --no-rebase && git push origin main")
    assert r["ok"], r
    assert calls == ["/api/projects/proj/fetch", "/api/projects/proj/push"]
    assert _git(bare, "rev-parse", "main") == _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("cmd", ["git push --force", "git push -f origin main", "git push origin +main",
                                 "git pull || rm -rf scripts"])
def test_dangerous_git_network_forms_are_not_rerouted(cmd, monkeypatch, tmp_path):
    from swarm.tools import shell
    monkeypatch.setenv("SWARM_AGENT_TOKEN", "x")
    assert shell._reroute_git_network(cmd, 30) is None


def test_rejected_push_after_merge_spawns_one_sync_task(diverged, monkeypatch, app):
    import swarm.agent_lifecycle as al
    from swarm import agent_finish, db
    repo, _ = diverged
    monkeypatch.setattr(al, "_CONFIG", {})
    agent_finish._push_after_merge(repo)
    agent_finish._push_after_merge(repo)                   # deduped while open
    tasks = [t for t in db.task_get_by_project(repo.name)
             if (t.get("metadata") or {}).get("created_by") == "origin_sync"]
    assert len(tasks) == 1
    assert tasks[0]["priority"] == 90 and "git_sync" in tasks[0]["description"]


def test_agent_may_fetch_only_its_own_project(app):
    flask_app, _ = app
    c = flask_app.test_client()
    assert c.post(f"/api/projects/{OTHER}/fetch", json={}, headers={"X-Swarm-Agent-Token": "tok-own"}).status_code == 403
    assert c.post(f"/api/projects/{OWN}/fetch", json={}, headers={"X-Swarm-Agent-Token": "tok-own"}).status_code == 200
