"""Tests for swarm.runtime_config.RuntimeConfig and build_runtime_config()."""

import pytest
from pathlib import Path
from swarm.runtime_config import RuntimeConfig, build_runtime_config


class TestRuntimeConfigDefaults:
    def test_default_workspace_is_dot(self):
        cfg = RuntimeConfig()
        assert cfg.workspace == Path(".")

    def test_default_task_type_is_feature(self):
        cfg = RuntimeConfig()
        assert cfg.task_type == "feature"

    def test_default_task_id(self):
        cfg = RuntimeConfig()
        assert cfg.task_id == "unknown"

    def test_default_readonly_false(self):
        cfg = RuntimeConfig()
        assert cfg.readonly is False

    def test_default_managed_projects_empty(self):
        cfg = RuntimeConfig()
        assert cfg.managed_projects == []

    def test_default_ignore_dirs(self):
        cfg = RuntimeConfig()
        assert "addons" in cfg.ignore_dirs
        assert ".git" in cfg.ignore_dirs
        assert ".godot" in cfg.ignore_dirs

    def test_default_max_tool_loops(self):
        cfg = RuntimeConfig()
        assert cfg.max_tool_loops == 200

    def test_default_api_port(self):
        cfg = RuntimeConfig()
        assert cfg.api_port == 5001

    def test_default_llm_provider(self):
        cfg = RuntimeConfig()
        assert cfg.llm_provider == "minimax"

    def test_default_meta_investigation_enabled(self):
        cfg = RuntimeConfig()
        assert cfg.meta_investigation_enabled is True

    def test_mutable_fields_are_independent(self):
        """Each instance should have its own mutable defaults (not shared)."""
        a = RuntimeConfig()
        b = RuntimeConfig()
        a.managed_projects.append("proj-x")
        assert "proj-x" not in b.managed_projects
        a.task_metadata["key"] = "val"
        assert "key" not in b.task_metadata


class TestRuntimeConfigOverrides:
    def test_workspace_override(self):
        cfg = RuntimeConfig(workspace=Path("/tmp/ws"))
        assert cfg.workspace == Path("/tmp/ws")

    def test_project_identity(self):
        cfg = RuntimeConfig(project="my-game", task_id="task-123", task_type="bug")
        assert cfg.project == "my-game"
        assert cfg.task_id == "task-123"
        assert cfg.task_type == "bug"

    def test_readonly_flag(self):
        cfg = RuntimeConfig(readonly=True)
        assert cfg.readonly is True

    def test_custom_ignore_dirs(self):
        cfg = RuntimeConfig(ignore_dirs=frozenset({"vendor", "build"}))
        assert "vendor" in cfg.ignore_dirs
        assert "addons" not in cfg.ignore_dirs

    def test_max_tool_loops_override(self):
        cfg = RuntimeConfig(max_tool_loops=10)
        assert cfg.max_tool_loops == 10

    def test_worktree_fields(self):
        cfg = RuntimeConfig(
            project_path_override="/tmp/worktrees/branch-abc",
            worktree_branch="branch-abc",
        )
        assert cfg.is_worktree() is True
        assert cfg.worktree_branch == "branch-abc"

    def test_no_worktree_by_default(self):
        cfg = RuntimeConfig()
        assert cfg.is_worktree() is False


class TestRuntimeConfigHelpers:
    def test_project_root_without_override(self):
        cfg = RuntimeConfig(workspace=Path("/ws"), project="my-proj")
        assert cfg.project_root() == Path("/ws/my-proj")

    def test_project_root_with_override(self):
        cfg = RuntimeConfig(
            workspace=Path("/ws"),
            project="my-proj",
            project_path_override="/worktrees/branch",
        )
        assert cfg.project_root() == Path("/worktrees/branch")

    def test_is_unmanaged_empty_list_allows_all(self):
        cfg = RuntimeConfig(project="any-proj", managed_projects=[])
        assert cfg.is_unmanaged() is False

    def test_is_unmanaged_project_not_in_list(self):
        cfg = RuntimeConfig(project="test-proj", managed_projects=["real-proj"])
        assert cfg.is_unmanaged() is True

    def test_is_unmanaged_project_in_list(self):
        cfg = RuntimeConfig(project="real-proj", managed_projects=["real-proj"])
        assert cfg.is_unmanaged() is False


class TestBuildRuntimeConfig:
    def test_builds_from_agent_runtime_globals(self):
        import swarm.agent_runtime as rt
        # Save originals
        orig_project = rt.PROJECT
        orig_task_id = rt.TASK_ID
        orig_task_type = rt.TASK_TYPE
        orig_readonly = rt.READONLY
        try:
            rt.PROJECT = "test-game"
            rt.TASK_ID = "task-build-test"
            rt.TASK_TYPE = "bug"
            rt.READONLY = True

            cfg = build_runtime_config()

            assert cfg.project == "test-game"
            assert cfg.task_id == "task-build-test"
            assert cfg.task_type == "bug"
            assert cfg.readonly is True
        finally:
            rt.PROJECT = orig_project
            rt.TASK_ID = orig_task_id
            rt.TASK_TYPE = orig_task_type
            rt.READONLY = orig_readonly

    def test_build_produces_value_snapshot(self):
        """Modifying module globals after build does not affect the snapshot."""
        import swarm.agent_runtime as rt
        orig = rt.PROJECT
        try:
            rt.PROJECT = "snap-proj"
            cfg = build_runtime_config()
            rt.PROJECT = "changed-after-build"
            assert cfg.project == "snap-proj"
        finally:
            rt.PROJECT = orig

    def test_build_copies_mutable_fields(self):
        """Mutating a built config's lists/dicts does not affect the module globals."""
        import swarm.agent_runtime as rt
        orig = list(rt.MANAGED_PROJECTS)
        try:
            rt.MANAGED_PROJECTS = ["proj-a"]
            cfg = build_runtime_config()
            cfg.managed_projects.append("proj-b")
            assert "proj-b" not in rt.MANAGED_PROJECTS
        finally:
            rt.MANAGED_PROJECTS = orig

    def test_workspace_is_path(self):
        cfg = build_runtime_config()
        assert isinstance(cfg.workspace, Path)

    def test_ignore_dirs_is_frozenset(self):
        cfg = build_runtime_config()
        assert isinstance(cfg.ignore_dirs, frozenset)

    def test_ignore_extensions_is_frozenset(self):
        cfg = build_runtime_config()
        assert isinstance(cfg.ignore_extensions, frozenset)
