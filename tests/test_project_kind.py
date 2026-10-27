"""One detector for prompts, validation and the baseline diff (2026-10-02).

They used to disagree: bare-scripts projects got generic prompts but Python
validation; a Godot project with a test-only requirements.txt got Python
prompts but Godot validation.
"""

import pytest

from swarm import db
from swarm import project_kind as pk
from swarm import validation


@pytest.fixture
def fresh_db(tmp_path):
    db._initialized = False
    db._local = __import__("threading").local()
    db.init(tmp_path / "t.db")
    yield
    db._initialized = False


def _make(tmp_path, *files):
    for rel in files:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n")
    return tmp_path


@pytest.mark.parametrize("files,expected", [
    (["pyproject.toml"], True),
    (["requirements.txt"], True),
    (["setup.py"], True),
    (["main.py"], True),                       # bare scripts (monopoly-test)
    (["src/app.py"], True),
    (["tests/test_x.py"], True),
    (["project.godot", "requirements.txt"], False),   # brick-blitz: Godot + pytest helper
    (["project.godot", "tools/gen.py", "x.py"], False),
    (["package.json", "tsconfig.json", "build.py"], False),  # TS with a helper script
    (["package.json", "tsconfig.json", "pyproject.toml"], True),  # explicit manifest wins
    (["README.md"], False),
    (["deep/nested/x.py"], False),
])
def test_is_python(tmp_path, files, expected):
    assert pk.is_python(_make(tmp_path, *files)) is expected


def test_bare_scripts_get_python_validation(tmp_path, fresh_db):
    """Validation runs its Python branch exactly when prompts pick Python."""
    root = _make(tmp_path / "bare", "ok.py")
    (root / "broken.py").write_text("def f(:\n")
    assert pk.is_python(root)
    failed, out = validation._post_task_validation_in_worktree("bare", "t1", worktree_path=root)
    assert failed is True and "broken.py" in out


def test_godot_with_requirements_is_not_validated_as_python(tmp_path, fresh_db):
    root = _make(tmp_path / "mixed", "project.godot", "requirements.txt")
    (root / "broken.py").write_text("def f(:\n")
    assert not pk.is_python(root)
    failed, out = validation._post_task_validation_in_worktree("mixed", "t1", worktree_path=root)
    assert "broken.py" not in out


def test_pytest_ignores_only_for_swarm_controller():
    assert validation._swarm_controller_pytest_ignores("swarm-controller") == [
        "--ignore=tests/test_dashboard.py", "--ignore=tests/test_agent_runtime.py"]
    assert validation._swarm_controller_pytest_ignores("aquarium-monitor") == []
