CRITICAL — READ-ONLY FILES:
Never modify files in tests/, test/, addons/, .godot/, .import/, any `*.uid` file, or any file matching test_*.py / *_test.py.
Tests describe the correct behaviour — if a test fails after your change, the problem is in your change.
Deleting, weakening, or rewriting tests to make them pass is STRICTLY FORBIDDEN.
Files under addons/ are third-party vendor code. You may read them to diagnose warnings, but you must not patch, rewrite, or commit changes to them unless the task explicitly says to update vendored code.
Files under .godot/, .import/, and *.uid are generated engine artifacts. Do not hand-edit them; regenerate them through normal project tooling instead.
