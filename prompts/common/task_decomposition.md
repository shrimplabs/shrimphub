TASK DECOMPOSITION: If this task has 3+ independent parts touching different files, decompose:
1. Prefer delegate_task_batch(children, mode) for durable delegated work because it records file ownership and parent/child lifecycle metadata.
2. Each delegated child must declare the files it owns. Parallel children must have disjoint write scopes.
3. Use delegate_helper(question, files, scope) for read-only analysis only. Do not treat it as an MCP server.
4. Only fall back to create_task(description, type, priority, dependencies, parent_task_id=TASK_ID) when the task explicitly needs manual graph shaping and delegate_task_batch is not a fit.
5. Call TASK_COMPLETE — delegated children or successor tasks run automatically.
Do NOT decompose tasks with fewer than 3 independent parts or where parts share the same file.

COORDINATION HYGIENE FOR PARALLEL WORK:
1. Call broadcast_read() near the start of the task, before touching shared files, and before running broad validation if other agents may be active.
2. Use broadcast_write("message") as a bounded checkpoint system, not a live progress feed. At most 3 writes per task:
   - early checkpoint: after initial inspection, if you will touch a risky shared file or shared subsystem
   - finding checkpoint: when you discover a blocker, root cause, or "already implemented" result that should change sibling behaviour
   - handoff checkpoint: when you complete, create a bug/recovery continuation, or abandon a risky approach that siblings should avoid
3. Only broadcast coordination-relevant facts:
   - shared blockers or known bad validation paths
   - root causes that affect multiple tasks
   - intent to modify a risky shared file such as project config, shared tests, or global resources
   - confirmation that a shared subsystem is already implemented or has moved in a specific commit
   - bug/recovery continuation created, with the new canonical task id when relevant
4. Keep broadcast messages to one concise actionable line. Do not broadcast routine progress, local scratch thoughts, or repeated status updates.

WORK PHASES:
1. Explore
   - Read the task, inspect relevant files, and call broadcast_read() early.
   - Do not edit yet.
2. Plan
   - Decide intended files, likely validation path, and whether the task is already implemented, blocked, or needs delegation.
   - Do not edit yet.
3. Discuss (only if there are active sibling tasks or nearby pending tasks that could be affected)
   - Emit a bounded checkpoint only if your plan changes sibling behaviour:
     shared-file claim, blocker/root cause, already-implemented result, risky validation path, or canonical bug/recovery continuation.
   - If there are no other relevant tasks, skip this phase and continue.
4. Execute
   - Make the smallest coherent edits needed for the task.
5. Discuss Again (only if something changed that siblings need to know)
   - Use a finding or handoff checkpoint when a blocker/root cause appears, when you create bug/recovery follow-up, or when your result changes downstream assumptions.
6. Validate
   - Prefer targeted validation before broad validation.
7. Complete
   - Finish with TASK_COMPLETE only after the task outcome is stable.
