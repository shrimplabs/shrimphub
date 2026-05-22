"""
swarm.tools -- agent tool function modules.

Re-exports all public tool functions for backwards compatibility.
Config vars are set via _sync_core_globals() / _sync_knowledge_globals()
in agent_runtime.py (same pattern as _sync_qa_tools_globals).
"""

from swarm.tools.core import (  # noqa: F401
    log,
    _project_root,
    read_file,
    list_files,
    search_code,
    get_file_stats,
    get_file_outline,
    read_file_range,
    patch_file,
    write_file,
    append_file,
    run_command,
    git_commit,
    git_push,
    mcp_call_tool,
    mcp_list_tools,
    rag_query,
    web_search,
    fetch_url,
    broadcast_read,
    broadcast_write,
    delegate_helper,
)

from swarm.tools.tasks import (  # noqa: F401
    create_task,
    create_tasks_file_aware,
    create_tasks,
    delegate_task_batch,
    list_tasks,
    list_subtasks,
    annotate_downstream_tasks,
    split_task,
    prune_task,
    insert_dependency,
    set_task_complexity,
)

from swarm.tools.shell import _safe_cwd, run  # noqa: F401

from swarm.tools.knowledge import (  # noqa: F401
    scratchpad_write,
    scratchpad_read,
    read_agent_knowledge,
    update_knowledge,
    get_task_context,
    read_shared_knowledge,
    update_shared_knowledge,
)
