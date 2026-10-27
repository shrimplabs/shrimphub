"""Tests for ToolContext and sync_tool_context() in swarm.tools._shared.

Verifies that the single entry-point sync propagates config into each tool
module correctly, covering file tools (core), task tools, knowledge tools,
and QA tools.
"""

from pathlib import Path

import pytest

from swarm.tools._shared import ToolContext, sync_tool_context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(**kwargs) -> ToolContext:
    defaults = dict(
        workspace=Path("/ws"),
        data_dir="data",
        project="my-proj",
        project_path_override="",
        worktree_branch="",
        task_id="task-123",
        task_type="feature",
        task_priority=50,
        max_lines=5000,
        ignore_dirs={"addons", ".git"},
        ignore_extensions=frozenset(),
        max_tool_loops=200,
        api_port=5001,
        mcp_servers={},
        managed_projects=[],
        readonly=False,
        qa_config={},
        qa_cycle=0,
        qa_max_cycles=3,
        mcp_client=None,
    )
    defaults.update(kwargs)
    return ToolContext(**defaults)


# ---------------------------------------------------------------------------
# ToolContext defaults
# ---------------------------------------------------------------------------

class TestToolContextDefaults:
    def test_default_workspace(self):
        ctx = ToolContext()
        assert ctx.workspace == Path(".")

    def test_default_task_type(self):
        ctx = ToolContext()
        assert ctx.task_type == "feature"

    def test_default_readonly(self):
        ctx = ToolContext()
        assert ctx.readonly is False

    def test_default_mcp_client_is_none(self):
        ctx = ToolContext()
        assert ctx.mcp_client is None

    def test_mutable_fields_are_independent(self):
        a = ToolContext()
        b = ToolContext()
        a.ignore_dirs.add("vendor")
        assert "vendor" not in b.ignore_dirs


# ---------------------------------------------------------------------------
# sync_tool_context → swarm.tools.core
# ---------------------------------------------------------------------------

class TestSyncToolContextCore:
    def test_workspace_propagates_to_core(self):
        import swarm.tools.core as _core
        ctx = _make_ctx(workspace=Path("/tmp/sync-test"))
        sync_tool_context(ctx)
        assert _core.WORKSPACE == Path("/tmp/sync-test")

    def test_project_propagates_to_core(self):
        import swarm.tools.core as _core
        ctx = _make_ctx(project="fire-game")
        sync_tool_context(ctx)
        assert _core.PROJECT == "fire-game"

    def test_readonly_propagates_to_core(self):
        import swarm.tools.core as _core
        ctx = _make_ctx(readonly=True)
        sync_tool_context(ctx)
        assert _core.READONLY is True

    def test_task_id_propagates_to_core(self):
        import swarm.tools.core as _core
        ctx = _make_ctx(task_id="xyz-999")
        sync_tool_context(ctx)
        assert _core.TASK_ID == "xyz-999"

    def test_ignore_dirs_propagates_to_core(self):
        import swarm.tools.core as _core
        ctx = _make_ctx(ignore_dirs={"vendor"})
        sync_tool_context(ctx)
        assert _core.IGNORE_DIRS == {"vendor"}


# ---------------------------------------------------------------------------
# sync_tool_context → swarm.tools.tasks
# ---------------------------------------------------------------------------

class TestSyncToolContextTasks:
    def test_project_propagates_to_tasks(self):
        import swarm.tools.tasks as _tasks
        ctx = _make_ctx(project="water-game")
        sync_tool_context(ctx)
        assert _tasks.PROJECT == "water-game"

    def test_task_id_propagates_to_tasks(self):
        import swarm.tools.tasks as _tasks
        ctx = _make_ctx(task_id="t-tasks-42")
        sync_tool_context(ctx)
        assert _tasks.TASK_ID == "t-tasks-42"

    def test_api_port_propagates_to_tasks(self):
        import swarm.tools.tasks as _tasks
        ctx = _make_ctx(api_port=9999)
        sync_tool_context(ctx)
        assert _tasks.API_PORT == 9999

    def test_task_priority_propagates_to_tasks(self):
        import swarm.tools.tasks as _tasks
        ctx = _make_ctx(task_priority=80)
        sync_tool_context(ctx)
        assert _tasks.TASK_PRIORITY == 80


# ---------------------------------------------------------------------------
# sync_tool_context → swarm.tools.knowledge
# ---------------------------------------------------------------------------

class TestSyncToolContextKnowledge:
    def test_project_propagates_to_knowledge(self):
        import swarm.tools.knowledge as _knowledge
        ctx = _make_ctx(project="earth-game")
        sync_tool_context(ctx)
        assert _knowledge.PROJECT == "earth-game"

    def test_readonly_propagates_to_knowledge(self):
        import swarm.tools.knowledge as _knowledge
        ctx = _make_ctx(readonly=True)
        sync_tool_context(ctx)
        assert _knowledge.READONLY is True

    def test_task_id_propagates_to_knowledge(self):
        import swarm.tools.knowledge as _knowledge
        ctx = _make_ctx(task_id="t-know-7")
        sync_tool_context(ctx)
        assert _knowledge.TASK_ID == "t-know-7"


# ---------------------------------------------------------------------------
# sync_tool_context → swarm.qa_tools
# ---------------------------------------------------------------------------

class TestSyncToolContextQA:
    def test_project_propagates_to_qa_tools(self):
        from swarm import qa_tools
        ctx = _make_ctx(project="sky-game")
        sync_tool_context(ctx)
        assert qa_tools.PROJECT == "sky-game"

    def test_api_port_propagates_to_qa_tools(self):
        from swarm import qa_tools
        ctx = _make_ctx(api_port=7777)
        sync_tool_context(ctx)
        assert qa_tools.API_PORT == 7777

    def test_qa_cycle_propagates_to_qa_tools(self):
        from swarm import qa_tools
        ctx = _make_ctx(qa_cycle=2)
        sync_tool_context(ctx)
        assert qa_tools.QA_CYCLE == 2

    def test_mcp_client_propagates_to_qa_tools(self):
        from swarm import qa_tools
        sentinel = object()
        ctx = _make_ctx(mcp_client=sentinel)
        sync_tool_context(ctx)
        assert qa_tools.mcp_client is sentinel


# ---------------------------------------------------------------------------
# sync_tool_context → _shared module vars
# ---------------------------------------------------------------------------

class TestSyncToolContextShared:
    def test_task_type_propagates_to_shared(self):
        import swarm.tools._shared as _shared
        ctx = _make_ctx(task_type="bug")
        sync_tool_context(ctx)
        assert _shared.TASK_TYPE == "bug"

    def test_workspace_propagates_to_shared(self):
        import swarm.tools._shared as _shared
        ctx = _make_ctx(workspace=Path("/shared-ws"))
        sync_tool_context(ctx)
        assert _shared.WORKSPACE == Path("/shared-ws")

    def test_project_path_override_propagates_to_shared(self):
        import swarm.tools._shared as _shared
        ctx = _make_ctx(project_path_override="/worktrees/branch-abc")
        sync_tool_context(ctx)
        assert _shared.PROJECT_PATH_OVERRIDE == "/worktrees/branch-abc"
