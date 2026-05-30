AGENT KNOWLEDGE: At session start, PROJECT KNOWLEDGE and VALIDATION STATE (if any) have been injected into your task description above.

At session end, save what you learned using the right tool:

- **update_knowledge(content)** — structural facts that accumulate over time: key file locations, class names, autoload singletons, API patterns, system architecture, recurring gotchas. These entries are appended and periodically compacted by the system.

- **update_validation_state(content)** — current validation status, exclusion lists, and validation commands. This OVERWRITES the previous state (no accumulation). Use it for: validation pass/fail results, _swarm_check.gd exclusion patterns, scene skip lists, godot --headless command invocations, recurring ghost file warnings. Always overwrite with the full current state so the next agent sees one authoritative snapshot.

Only call these at session end when you have genuinely new facts. Do not repeat information already present in the injected context above.
