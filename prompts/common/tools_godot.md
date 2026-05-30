AVAILABLE TOOLS (use EXACTLY these names):
- list_files(path): List files and directories (paths are relative to project root)
- read_file(path): Read file contents
- get_file_outline(path): Get functions/classes defined in a file with line numbers — use before editing large files
- read_file_range(path, start_line, end_line): Read a specific line range (max 300 lines)
- search_code(query): Search for pattern across .gd files
- get_file_stats(path): Get line count and size
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
- update_knowledge(content): Save persistent structural facts (autoloads, class names, patterns, gotchas) — appended and compacted over time
- update_validation_state(content): Overwrite current validation status, exclusion lists, and validation commands — always replaces, never appends
- get_task_context(): Get active agents, recent completed tasks, and last 5 commits for this project
- read_shared_knowledge(topic): Read cross-project knowledge base (optional topic filter)
- update_shared_knowledge(content, topic): Save a cross-project fact for all future agents
- rag_query(question, top_k): Query Godot 4 documentation via RAG — call this FIRST before writing any code
- web_search(query, max_results=3): Search for GDScript APIs, Godot answers, or error solutions
- fetch_url(url, extract_text): Fetch and extract text from a documentation URL
- mcp_call_tool(server, tool, args): Call an MCP server tool
- mcp_list_tools(server): List tools available on an MCP server
- launch_game(project_path): Launch the Godot game headlessly via the StateServer. Returns when the game is ready. Use after making changes to verify the game actually runs.
- get_game_state(): Read live structured state from the running game (score, positions, scene tree, any fields from get_game_state()). Use to verify game logic is correct after launch_game().
- wait(seconds): Wait N seconds (use after launch_game to let the game initialise before reading state).

GODOT UI ANCHORING RULES (violations cause elements stuck in top-left corner):
- NEVER set `position` directly on a Control node without also setting anchors. Raw position with default anchors (0,0,0,0) always places elements at the top-left corner.
- Always set an anchor preset in `_ready()` for any Control node you create:
    $MyControl.set_anchors_and_offsets_preset(Control.PRESET_CENTER)           # centered
    $MyControl.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)        # fills parent
    $MyControl.set_anchors_and_offsets_preset(Control.PRESET_CENTER_TOP)       # top-center
- For full-screen UI (menus, HUDs), structure scenes as: CanvasLayer → root Control with PRESET_FULL_RECT → children inside.
- For centered popups/labels: use a CenterContainer or set PRESET_CENTER on the Control.
- When writing .tscn files directly, always include anchor values:
    [node name="MyLabel" type="Label"]
    anchors_preset = 8   # 8 = PRESET_CENTER
    anchor_left = 0.5
    anchor_top = 0.5
    anchor_right = 0.5
    anchor_bottom = 0.5
    offset_left = -50.0
    offset_top = -20.0
    offset_right = 50.0
    offset_bottom = 20.0
- After adding any UI node, verify position with get_game_state() or a screenshot — do not assume it is correctly placed.

ADAPTIVE GRAPH TOOLS — call these before TASK_COMPLETE to improve downstream work:
- list_tasks(project): List all tasks for the project with their IDs and status — use to see what's downstream
- annotate_downstream_tasks(findings, task_ids): Prepend a context block to downstream pending tasks sharing what you learned (API shapes, constraints, architectural decisions, gotchas)
- split_task(task_id, replacement_tasks): Replace a pending downstream task with multiple smaller tasks; deps are rewired automatically
- prune_task(task_id, reason): Mark a downstream task completed if your work made it redundant
- insert_dependency(from_task_id, to_task_id): Add an ordering constraint between two downstream tasks you discovered must be sequenced
- set_task_complexity(task_id, complexity, reason): Tag a downstream pending task as 'simple' or 'complex'; complex tasks get extra max_attempts and scheduler priority boost
