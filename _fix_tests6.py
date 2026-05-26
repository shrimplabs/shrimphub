"""Patch TestCreateSubtask 3 failing dispatch test cases."""
import pathlib, importlib

f = pathlib.Path('tests/test_agent_runtime.py')
lines = f.read_text().split('\n')

# Find and replace the 3 dispatch tests
def find_test(name):
    for i, l in enumerate(lines):
        if f'def {name}(' in l:
            return i
    return -1

def get_test_body(start):
    depth = 0
    end = start
    for i in range(start, len(lines)):
        l = lines[i]
        if l.strip().startswith('def ') and i > start:
            break
        end = i
    return lines[start:end+1]

# Fix test 1: test_dispatch_rejects_missing_description
idx = find_test('test_dispatch_rejects_missing_description')
old_body = '\n'.join(get_test_body(idx))
print(f"test_dispatch_rejects_missing_description at line {idx+1}, {len(old_body)} chars")

new_body = '''def test_dispatch_rejects_missing_description(self):
        # Must set core.TASK_ID AND patch _read_core so the dispatch
        # layer does not try to reach a real API after the required-args check.
        from swarm.tools import core as _core
        _orig_vals = (_core.TASK_ID, _core.PROJECT, _core.TASK_TYPE,
                      _core.TASK_PRIORITY, _core.API_PORT)
        try:
            _core.TASK_ID = "parent-task"
            _core.PROJECT = "test-proj"
            _core.TASK_TYPE = "feature"
            _core.TASK_PRIORITY = 50
            _core.API_PORT = 19999
            with patch.object(
                _core, "_read_core",
                return_value=("test-proj", "feature", "parent-task", 50, 19999)
            ), patch.object(urllib.request, "urlopen",
                            side_effect=Exception("should not be called")):
                result = rt.execute_tool({"tool": "create_subtask", "args": {}})
        finally:
            _core.TASK_ID, _core.PROJECT, _core.TASK_TYPE, _core.TASK_PRIORITY, _core.API_PORT = _orig_vals
        assert result["ok"] is False
        assert "description" in result["error"]'''

lines = lines[:idx] + [new_body] + lines[idx+len(get_test_body(idx)):]  
f.write_text('\n'.join(lines))
print("Patched test_dispatch_rejects_missing_description")
