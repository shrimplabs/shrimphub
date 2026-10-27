TOOL CALL FORMAT (required -- a reply without a tool call ends the task as failed):
Call exactly one tool per reply, in this form, with no markdown fence around it:
[TOOL_CALL]{"tool": "read_file", "args": {"path": "GAME_DESIGN.md"}}[/TOOL_CALL]
Do not describe what you are about to do -- make the call. When the task is fully done, reply with TASK_COMPLETE.
