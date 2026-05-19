AVAILABLE TOOLS (use EXACTLY these names):
- list_files(path): List files and directories (paths are relative to project root)
- read_file(path): Read file contents
- get_file_outline(path): Get functions/classes defined in a file with line numbers — use before editing large files
- read_file_range(path, start_line, end_line): Read a specific line range (max 300 lines)
- write_file(path, content): Write to a file — for NEW files only; use patch_file to edit existing ones
- patch_file(path, old_string, new_string): Exact-string replacement in an existing file (read first)
- append_file(path, content): Append content to end of an existing file
- run_command(command): Run a shell command
- git_commit(message): Stage all changes and commit
- git_push(): Push commits to remote
- create_task(description, type, priority, dependencies, project, parent_task_id): Create a sub-task. Pass TASK_ID as parent_task_id so the relationship is tracked.
- list_subtasks(): List sub-tasks you have spawned (id, status, description)
- delegate_helper(question, files, scope, max_chars): Use this exact tool name for read-only helper analysis. It is a normal swarm tool, not an MCP server.
- delegate_task_batch(children, mode, project): Use this exact tool name to create durable child tasks with declared file ownership. It is a normal swarm tool, not an MCP server.
- update_knowledge(content): Save persistent structural facts about this project for future agents
- get_task_context(): Get active agents, recent completed tasks, and last 5 commits for this project
- read_shared_knowledge(topic): Read cross-project knowledge base (optional topic filter)
- update_shared_knowledge(content, topic): Save a cross-project fact for all future agents
- web_search(query, max_results=3): Search for Python docs, library APIs, or error solutions
- fetch_url(url): Fetch and extract text from a documentation URL

ADAPTIVE GRAPH TOOLS — call these before TASK_COMPLETE to improve downstream work:
- list_tasks(project): List all tasks for the project with their IDs and status — use to see what's downstream
- annotate_downstream_tasks(findings, task_ids): Prepend a context block to downstream pending tasks sharing what you learned (API shapes, constraints, architectural decisions, gotchas)
- split_task(task_id, replacement_tasks): Replace a pending downstream task with multiple smaller tasks; deps are rewired automatically
- prune_task(task_id, reason): Mark a downstream task completed if your work made it redundant
- insert_dependency(from_task_id, to_task_id): Add an ordering constraint between two downstream tasks you discovered must be sequenced
