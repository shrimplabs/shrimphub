<% set _intent_variant = prompt_intent_variant|default("exploratory") %>
Before making edits, take a reconnaissance pass so you understand both intent and reality:
- Check for `GAME_DESIGN.md`, `PROJECT_CLOSURE.md`, `README.md`, and other nearby spec/design docs in `<< project_path >>`.
- Read the relevant sections first. If the docs are short, read them fully. If they are long, extract the overview, the current feature/bug/refactor scope, acceptance criteria, constraints, and any closure expectations.
- Treat these docs as the product intent anchor. Compare them against the task description and the current code before deciding what to change.
<% if _intent_variant == "baseline" %>
- Look for nearby systems, tests, configs, scenes/routes, and existing patterns that might change how the task should be implemented.
- Check whether the first obvious file is actually the owner of the behavior, or whether the real logic lives in a neighboring subsystem.
<% else %>
- Investigate broadly and deliberately. Actively look for nearby systems, tests, configs, scenes/routes, related UI, data flow, and existing patterns that might change how the task should be implemented.
- Assume the first obvious file may not be the whole story. Follow references into the owning subsystem until you understand the surrounding behavior well enough to explain the change before editing.
<% endif %>
- Before writing code, identify the specific files and subsystems that appear to own the behavior you are changing.
