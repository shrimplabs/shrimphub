"""Tests for swarm/tasks.py — Task dataclass and task sources."""
import pytest
from swarm.tasks import Task, MemoryTaskSource


# ---------------------------------------------------------------------------
# Task dataclass
# ---------------------------------------------------------------------------

def make_task(**kw):
    defaults = dict(id="t1", project="proj", type="feature", description="do stuff")
    return Task(**{**defaults, **kw})


def test_task_defaults():
    t = make_task()
    assert t.priority == 50
    assert t.status == "pending"
    assert t.dependencies == []
    assert t.metadata == {}
    assert t.attempts == 0
    assert t.max_attempts == 3
    assert t.created is not None


def test_task_to_dict_round_trip():
    t = make_task(id="abc", priority=80, dependencies=["dep1"], metadata={"k": "v"})
    d = t.to_dict()
    t2 = Task.from_dict(d)
    assert t2.id == "abc"
    assert t2.priority == 80
    assert t2.dependencies == ["dep1"]
    assert t2.metadata == {"k": "v"}


def test_task_from_dict_missing_optionals():
    """from_dict must work with only the required 'project' field."""
    t = Task.from_dict({"project": "myproj"})
    assert t.project == "myproj"
    assert t.type == "feature"
    assert t.priority == 50
    assert t.status == "pending"
    assert t.id  # auto-generated


def test_task_from_dict_preserves_attempts():
    t = Task.from_dict({"project": "p", "attempts": 2, "max_attempts": 5})
    assert t.attempts == 2
    assert t.max_attempts == 5


# ---------------------------------------------------------------------------
# MemoryTaskSource
# ---------------------------------------------------------------------------

@pytest.fixture
def source():
    return MemoryTaskSource()


def _t(id, project="proj", status="pending", priority=50, type="feature", deps=None):
    return Task(
        id=id, project=project, type=type, description="x",
        priority=priority, status=status, dependencies=deps or [],
    )


def test_add_and_get(source):
    t = _t("t1")
    source.add_task(t)
    assert source.get_task("t1") is t


def test_get_missing_returns_none(source):
    assert source.get_task("ghost") is None


def test_get_all_tasks(source):
    source.add_task(_t("t1"))
    source.add_task(_t("t2"))
    assert len(source.get_all_tasks()) == 2


def test_get_pending_filters_status(source):
    source.add_task(_t("t1", status="pending"))
    source.add_task(_t("t2", status="completed"))
    pending = source.get_pending_tasks()
    assert len(pending) == 1
    assert pending[0].id == "t1"


def test_remove_task(source):
    source.add_task(_t("t1"))
    assert source.remove_task("t1") is True
    assert source.get_task("t1") is None


def test_remove_missing_returns_false(source):
    assert source.remove_task("ghost") is False


def test_update_task(source):
    t = _t("t1", priority=50)
    source.add_task(t)
    t.priority = 99
    source.update_task(t)
    assert source.get_task("t1").priority == 99


def test_get_tasks_for_project(source):
    source.add_task(_t("t1", project="alpha"))
    source.add_task(_t("t2", project="beta"))
    result = source.get_tasks_for_project("alpha")
    assert len(result) == 1
    assert result[0].id == "t1"


def test_get_tasks_by_status(source):
    source.add_task(_t("t1", status="pending"))
    source.add_task(_t("t2", status="failed"))
    assert len(source.get_tasks_by_status("failed")) == 1


def test_add_task_auto_generates_id(source):
    t = Task(id="", project="proj", type="feature", description="x")
    source.add_task(t)
    assert t.id  # should have been set


def test_get_next_task_priority_order(source):
    source.add_task(_t("lo", priority=10))
    source.add_task(_t("hi", priority=90))
    source.add_task(_t("mid", priority=50))
    next_task = source.get_next_task()
    assert next_task.id == "hi"


def test_get_next_task_skips_unmet_deps(source):
    source.add_task(_t("dep", priority=100, status="pending"))
    source.add_task(_t("child", priority=100, deps=["dep"]))
    # dep is pending (not completed), so child should be blocked
    # dep itself has no deps, so it should be returned
    next_task = source.get_next_task()
    assert next_task.id == "dep"


def test_get_next_task_returns_none_when_all_blocked(source):
    source.add_task(_t("child", deps=["missing-dep"]))
    assert source.get_next_task() is None


def test_get_next_task_returns_none_when_empty(source):
    assert source.get_next_task() is None


def test_get_completed_tasks(source):
    source.add_task(_t("t1", status="completed"))
    source.add_task(_t("t2", status="pending"))
    assert len(source.get_completed_tasks()) == 1


def test_get_failed_tasks(source):
    source.add_task(_t("t1", status="failed"))
    source.add_task(_t("t2", status="pending"))
    assert len(source.get_failed_tasks()) == 1
