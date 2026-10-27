"""Path confinement (swarm-controller-8sno) and guardrail protection (swarm-controller-2rr4).

Every agent file tool must stay inside the project root returned by
swarm.tools._shared._project_root() (which honours the worktree override).
Absolute paths, ``..`` traversal, ``~`` expansion and symlinks that point
outside the root are all refused with ``{"ok": False, "error": ...}``.
"""

import os
from pathlib import Path

import pytest

import swarm.tools._shared as _shared
import swarm.tools.core as _core
from swarm.tools._shared import ToolContext, sync_tool_context
from swarm.tools.files import (
    append_file,
    get_file_outline,
    get_file_stats,
    list_files,
    patch_file,
    read_file,
    read_file_range,
    search_code,
    write_file,
)
from swarm.tools import path_guard
from swarm.tools.path_guard import (
    PathConfinementError,
    _command_attempts_protected_write,
    _confine_project_path,
    _protected_path_reason,
    _resolve_project_path,
)


def _configure(workspace: Path, project: str, override: str = "", task_type: str = "feature"):
    sync_tool_context(ToolContext(
        workspace=workspace,
        project=project,
        project_path_override=override,
        task_type=task_type,
        readonly=False,
    ))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A project dir, an outside dir with a secret, a fake HOME, and symlinks
    inside the project that point outside it."""
    workspace = tmp_path / "workspace"
    proj = workspace / "test-proj"
    proj.mkdir(parents=True)
    (proj / "main.gd").write_text("extends Node\nfunc _ready():\n\tpass\n")
    (proj / "sub").mkdir()
    (proj / "sub" / "inner.py").write_text("x = 1\n")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET\n")

    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("export SECRET=1\n")
    monkeypatch.setenv("HOME", str(home))

    os.symlink(outside / "secret.txt", proj / "escape_file")
    os.symlink(outside, proj / "escape_dir")

    _configure(workspace, "test-proj")
    return {"workspace": workspace, "proj": proj, "outside": outside, "home": home}


ESCAPES = ["/etc/passwd", "../../.ssh/id_rsa", "~/.zshrc", "escape_file", "escape_dir/secret.txt"]


def _assert_refused(result, path_arg):
    assert result["ok"] is False, f"{path_arg!r} was not refused: {result}"
    assert "outside the project root" in result["error"], result["error"]


class TestReadToolsAreConfined:
    @pytest.mark.parametrize("path_arg", ESCAPES)
    def test_read_file(self, env, path_arg):
        _assert_refused(read_file(path_arg), path_arg)

    @pytest.mark.parametrize("path_arg", ESCAPES)
    def test_read_file_range(self, env, path_arg):
        _assert_refused(read_file_range(path_arg, 1, 5), path_arg)

    @pytest.mark.parametrize("path_arg", ESCAPES)
    def test_get_file_stats(self, env, path_arg):
        _assert_refused(get_file_stats(path_arg), path_arg)

    @pytest.mark.parametrize("path_arg", ESCAPES)
    def test_get_file_outline(self, env, path_arg):
        _assert_refused(get_file_outline(path_arg), path_arg)

    @pytest.mark.parametrize("path_arg", ["/etc", "../..", "~", "escape_dir"])
    def test_list_files(self, env, path_arg):
        _assert_refused(list_files(path_arg), path_arg)

    def test_symlinked_file_never_leaks_content(self, env):
        result = read_file("escape_file")
        assert result["ok"] is False
        assert "SECRET" not in str(result)

    def test_search_code_skips_symlinks_pointing_outside(self, env):
        (env["outside"] / "leak.py").write_text("NEEDLE = 1\n")
        os.symlink(env["outside"] / "leak.py", env["proj"] / "leak_link.py")
        result = search_code("NEEDLE")
        assert result["ok"] is True
        assert result["count"] == 0


class TestWriteToolsAreConfined:
    @pytest.mark.parametrize("path_arg", ESCAPES)
    def test_write_file(self, env, path_arg):
        _assert_refused(write_file(path_arg, "pwned\n"), path_arg)

    @pytest.mark.parametrize("path_arg", ESCAPES)
    def test_append_file(self, env, path_arg):
        _assert_refused(append_file(path_arg, "pwned\n"), path_arg)

    @pytest.mark.parametrize("path_arg", ESCAPES)
    def test_patch_file(self, env, path_arg):
        _assert_refused(patch_file(path_arg, "SECRET", "pwned"), path_arg)

    def test_nothing_outside_was_touched(self, env):
        write_file("escape_file", "pwned\n")
        append_file("escape_dir/secret.txt", "pwned\n")
        patch_file("~/.zshrc", "export", "pwned")
        assert (env["outside"] / "secret.txt").read_text() == "SECRET\n"
        assert (env["home"] / ".zshrc").read_text() == "export SECRET=1\n"
        assert not (env["proj"] / ".swarm_path_denied").exists()


class TestNormalPathsStillWork:
    def test_relative_read(self, env):
        result = read_file("main.gd")
        assert result["ok"] is True
        assert "extends Node" in result["content"]

    def test_relative_nested_read_and_outline(self, env):
        assert read_file("sub/inner.py")["ok"] is True
        assert get_file_outline("sub/inner.py")["ok"] is True
        assert get_file_stats("sub/inner.py") == {"ok": True, "lines": 1, "size": 6}
        assert read_file_range("main.gd", 1, 2)["ok"] is True

    def test_dot_dot_that_stays_inside_root(self, env):
        assert read_file("sub/../main.gd")["ok"] is True

    def test_relative_write_patch_append(self, env):
        assert write_file("new/file.txt", "hello\n")["ok"] is True
        assert (env["proj"] / "new" / "file.txt").read_text() == "hello\n"
        assert patch_file("new/file.txt", "hello", "bye")["ok"] is True
        assert append_file("new/file.txt", "more\n")["ok"] is True
        assert (env["proj"] / "new" / "file.txt").read_text() == "bye\nmore\n"

    def test_list_files_relative_and_default(self, env):
        assert "main.gd" in list_files()["files"]
        assert "inner.py" in list_files("sub")["files"]

    def test_absolute_path_inside_root(self, env):
        abs_path = str(env["proj"] / "main.gd")
        assert read_file(abs_path)["ok"] is True
        assert write_file(str(env["proj"] / "abs.txt"), "abs\n")["ok"] is True
        assert (env["proj"] / "abs.txt").read_text() == "abs\n"
        assert append_file(abs_path, "# tail\n")["ok"] is True
        assert patch_file(abs_path, "# tail", "# end")["ok"] is True
        assert "main.gd" in list_files(str(env["proj"]))["files"]

    def test_absolute_path_via_realpath_alias_of_root(self, env):
        # e.g. macOS /var -> /private/var: the realpath spelling must also be accepted
        real = Path(os.path.realpath(env["proj"])) / "main.gd"
        assert read_file(str(real))["ok"] is True

    def test_symlink_inside_root_pointing_inside_root_is_fine(self, env):
        os.symlink(env["proj"] / "main.gd", env["proj"] / "alias.gd")
        assert read_file("alias.gd")["ok"] is True

    def test_confine_returns_root_for_dot(self, env):
        path, err = _confine_project_path(".")
        assert err is None
        assert path == Path(_shared._project_root())

    def test_resolve_project_path_sentinel_and_strict(self, env):
        sentinel = _resolve_project_path("/etc/passwd")
        assert not sentinel.exists()
        assert Path(_shared._project_root()) in sentinel.parents
        with pytest.raises(PathConfinementError):
            _resolve_project_path("/etc/passwd", strict=True)
        assert _resolve_project_path("main.gd") == Path(_shared._project_root()) / "main.gd"

    def test_vendor_protection_still_applies(self, env):
        (env["proj"] / "addons").mkdir()
        result = write_file("addons/plugin.gd", "x")
        assert result["ok"] is False
        assert "vendor code" in result["error"]


class TestWorktreeOverride:
    def test_override_root_is_the_boundary(self, env, tmp_path):
        worktree = tmp_path / "worktrees" / "test-proj-wt"
        worktree.mkdir(parents=True)
        (worktree / "wt.gd").write_text("in worktree\n")
        _configure(env["workspace"], "test-proj", override=str(worktree))

        assert read_file("wt.gd")["ok"] is True
        assert write_file("out.txt", "x\n")["ok"] is True
        assert (worktree / "out.txt").exists()
        assert not (env["proj"] / "out.txt").exists()

        # the non-worktree project dir is now outside the boundary
        _assert_refused(read_file(str(env["proj"] / "main.gd")), "main project path")
        _assert_refused(write_file(str(env["proj"] / "x.txt"), "x"), "main project path")

    def test_virtual_project_without_directory_is_still_confined(self, env):
        _configure(env["workspace"], "ghost-proj")
        assert not (env["workspace"] / "ghost-proj").exists()
        _assert_refused(read_file("/etc/passwd"), "/etc/passwd")
        _assert_refused(write_file("../test-proj/hijack.txt", "x"), "../test-proj/hijack.txt")
        assert write_file("a.txt", "x\n")["ok"] is True
        assert (env["workspace"] / "ghost-proj" / "a.txt").read_text() == "x\n"


GUARDRAIL_PATHS = [
    "swarm/tools/path_guard.py",
    "swarm/tools/shell.py",
    "swarm/tool_dispatch.py",
    "swarm/agent_lifecycle.py",
    "swarm/agent_runtime.py",
    "prompts/feature.yaml",
    "prompts/common/tools.yaml",
    "config.json",
]


@pytest.fixture
def swarm_controller_env(tmp_path):
    workspace = tmp_path / "workspace"
    proj = workspace / "swarm-controller"
    for rel in GUARDRAIL_PATHS + ["swarm/db.py", "config.example.json", "README.md"]:
        f = proj / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("original\n")
    _configure(workspace, "swarm-controller")
    return proj


class TestGuardrailProtectedPaths:
    @pytest.mark.parametrize("rel", GUARDRAIL_PATHS)
    def test_write_patch_append_refused(self, swarm_controller_env, rel):
        for result in (
            write_file(rel, "hacked\n"),
            patch_file(rel, "original", "hacked"),
            append_file(rel, "hacked\n"),
        ):
            assert result["ok"] is False, (rel, result)
            assert "guardrail" in result["error"], result["error"]
        assert (swarm_controller_env / rel).read_text() == "original\n"

    @pytest.mark.parametrize("rel", GUARDRAIL_PATHS)
    def test_reason_label(self, swarm_controller_env, rel):
        assert _protected_path_reason(swarm_controller_env / rel) == "guardrail"

    def test_guardrail_files_are_still_readable(self, swarm_controller_env):
        assert read_file("swarm/tools/path_guard.py")["ok"] is True

    @pytest.mark.parametrize("rel", ["swarm/db.py", "config.example.json", "README.md", "swarm/tools/files.py"])
    def test_other_files_in_swarm_controller_are_writable(self, swarm_controller_env, rel):
        assert _protected_path_reason(swarm_controller_env / rel) is None
        assert write_file(rel, "edited\n")["ok"] is True

    @pytest.mark.parametrize("cmd", [
        "sed -i 's/a/b/' prompts/feature.yaml",
        "cp /tmp/x swarm/tools/path_guard.py",
        "echo '{}' > config.json",
        "rm swarm/agent_runtime.py",
        "python -c \"open('swarm/tool_dispatch.py','w').write('x')\"",
        "mv swarm/tools/shell.py swarm/tools/shell_old.py",
        "touch swarm/agent_lifecycle.py",
    ])
    def test_command_attempts_protected_write(self, swarm_controller_env, cmd):
        result = _command_attempts_protected_write(cmd)
        assert result is not None, cmd
        assert result[0] == "guardrail"

    @pytest.mark.parametrize("cmd", [
        "cat prompts/feature.yaml",
        "grep -n foo swarm/tools/path_guard.py",
        "sed -i 's/a/b/' swarm/db.py",
        "cp x config.example.json",
    ])
    def test_command_read_or_unprotected_is_allowed(self, swarm_controller_env, cmd):
        assert _command_attempts_protected_write(cmd) is None, cmd

    def test_guardrail_inactive_for_other_projects(self, env):
        proj = env["proj"]
        (proj / "prompts").mkdir()
        assert _protected_path_reason(proj / "prompts" / "feature.yaml") is None
        assert write_file("prompts/feature.yaml", "ok\n")["ok"] is True
        assert write_file("config.json", "{}\n")["ok"] is True
        assert _command_attempts_protected_write("sed -i 's/a/b/' prompts/feature.yaml") is None

    def test_guardrail_by_real_repo_location(self, env):
        # Even when PROJECT is something else, a path that resolves inside the
        # actual swarm-controller checkout is flagged.
        real_prompt = path_guard._SWARM_CONTROLLER_ROOT / "prompts" / "feature.yaml"
        assert path_guard._guardrail_reason(real_prompt) == "guardrail"
        assert path_guard._guardrail_reason(path_guard._SWARM_CONTROLLER_ROOT / "swarm" / "db.py") is None

    def test_guardrail_active_when_root_is_inside_repo(self, env):
        _configure(path_guard._SWARM_CONTROLLER_ROOT.parent, path_guard._SWARM_CONTROLLER_ROOT.name)
        assert path_guard._guardrail_active() is True
        assert _protected_path_reason(path_guard._SWARM_CONTROLLER_ROOT / "config.json") == "guardrail"


def test_allow_self_modification_defaults_to_false():
    import json
    from swarm import orchestrator
    assert orchestrator.ALLOW_SELF_MODIFICATION is False
    example = json.loads(Path(path_guard._SWARM_CONTROLLER_ROOT / "config.example.json").read_text())
    assert example.get("allow_self_modification") is False
