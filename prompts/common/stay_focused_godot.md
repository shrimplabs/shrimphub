STAY FOCUSED ON THE CURRENT TASK:
- If you discover a related bug or improvement while working, use create_task to queue it rather than trying to fix everything in one session
- Focus on completing the task at hand — queue out-of-scope issues for later
- Valid task types: feature, bug, polish, refactor, qa
- Priority is capped at 90 (agents cannot create higher-priority tasks than their own)

GODOT AUTOLOAD RULES (critical — violations break the entire project):
- NEVER add `const SignalBus = preload(...)`, `const MissionManager = preload(...)`, or any `const X = preload(...)` for a script registered as an autoload in project.godot
- Autoloads (SignalBus, MissionManager, OperativeSwitcher, etc.) are globally accessible by their registered name — no import or preload needed
- Preloading an autoload script gives you the GDScript class resource, NOT the running singleton instance — signals and methods will fail with "cannot find member" errors
- Signal bus pattern: just write `SignalBus.my_signal.emit(value)` or `SignalBus.my_signal.connect(callback)` directly — no import required
