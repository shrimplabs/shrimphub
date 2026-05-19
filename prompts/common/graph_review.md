## Before you say TASK_COMPLETE — review the downstream graph

Real development produces information the planner didn't have. Before finishing,
call list_tasks() and look at what tasks depend on yours. Then ask:

1. **Did I learn something that changes how a downstream task should be done?**
   → Call annotate_downstream_tasks(findings) to prepend that context.
   Examples: the API you built has a different shape than the task description assumed;
   you chose a specific library, file structure, or pattern they need to follow;
   you found a constraint or gotcha they'll hit.

2. **Is a downstream task now too big, or clearly the wrong granularity?**
   → Call split_task(task_id, replacement_tasks) to break it into better pieces.
   The dep graph rewires automatically.

3. **Did your work make a downstream task redundant?**
   → Call prune_task(task_id, reason) to mark it completed so it doesn't waste an agent.
   Examples: you implemented something that was going to be a separate task;
   you discovered the task described work that no longer makes sense.

4. **Did you discover that two downstream tasks must run in a specific order?**
   → Call insert_dependency(from_task_id, to_task_id) to enforce the ordering.

You don't have to do all of these — only act when you genuinely learned something
useful. If the downstream tasks look correct as planned, do nothing and proceed.
