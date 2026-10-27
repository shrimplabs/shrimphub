"""Targeted tests for swarm/db.py dependency validation and write atomicity.

Tests the three acceptance criteria from the atomic-write scope:
  1) Concurrent writes cannot slip a cycle past validation.
  2) task_update_status(..., dependencies=...) cannot bypass graph validation.
  3) Existing API tests still pass.
"""
import threading

import pytest

from swarm import db


# ---------------------------------------------------------------------------
# isolated_db fixture  (same pattern as test_db.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Give each test a fresh in-memory-like DB by pointing to a temp file."""
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm_test.db")
    yield
    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task(id="t1", project="proj", **kw):
    return {
        "id": id, "project": project, "type": "feature",
        "description": "test", "priority": 50, "status": "pending",
        "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 3,
        **kw,
    }


# ---------------------------------------------------------------------------
# Criterion 1: Concurrent writes cannot slip a cycle past validation.
# ---------------------------------------------------------------------------

def test_concurrent_writes_no_cycles_in_serially_submitted_graphs():
    """
    Two threads simultaneously add tasks that together would form a cycle
    if either were allowed to commit before the other's deps were validated.
    Neither write must succeed with a cycle.
    """
    errors = []
    barrier = threading.Barrier(2)

    def thread_a():
        try:
            barrier.wait()  # sync start
            db.task_upsert(_task("ta", project="p", dependencies=["tb"]))
        except ValueError as e:
            errors.append(("ta", str(e)))

    def thread_b():
        try:
            barrier.wait()  # sync start
            db.task_upsert(_task("tb", project="p", dependencies=["ta"]))
        except ValueError as e:
            errors.append(("tb", str(e)))

    t1 = threading.Thread(target=thread_a)
    t2 = threading.Thread(target=thread_b)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # At least one thread must have raised a cycle error
    cycle_errors = [(tid, msg) for tid, msg in errors if "cycle" in msg.lower()]
    assert len(cycle_errors) >= 1, (
        f"Expected at least one cycle-detection error, got: {errors}"
    )
    # DB must not contain a cycle
    all_tasks = db.task_get_all()
    dep_graph = {t["id"]: set(t["dependencies"]) for t in all_tasks}
    # Check for direct cycle: ta->tb and tb->ta
    assert not ("ta" in dep_graph.get("tb", set()) and "tb" in dep_graph.get("ta", set())), \
        "Cycle exists in DB despite concurrent writes"


def test_concurrent_writes_distinct_tasks_all_succeed():
    """Concurrent distinct writes must all succeed without race conditions."""
    errors = []

    def write(i):
        try:
            db.task_upsert(_task(f"t{i}", project=f"proj{i}"))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent writes raised errors: {errors}"
    assert len(db.task_get_all()) == 20


def test_concurrent_upsert_updates_are_not_lost():
    """
    Two threads updating the same task's priority concurrently.
    One write must not corrupt the row or silently fail.
    """
    db.task_upsert(_task("t1", priority=10))
    barrier = threading.Barrier(2)
    errors = []

    def set_priority(priority):
        def fn():
            barrier.wait()
            try:
                db.task_update("t1", {"priority": priority})
            except Exception as e:
                errors.append(e)
        return fn

    t1 = threading.Thread(target=set_priority(20))
    t2 = threading.Thread(target=set_priority(30))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], f"Update errors: {errors}"
    t = db.task_get("t1")
    assert t["priority"] in (20, 30)  # last writer wins; no corruption


# ---------------------------------------------------------------------------
# Criterion 2: task_update_status(..., dependencies=...) cannot bypass
#              graph validation.
# ---------------------------------------------------------------------------

def test_task_update_status_rejects_self_dependency():
    """Passing dependencies=[task_id] to task_update_status raises ValueError."""
    db.task_upsert(_task("t1"))
    with pytest.raises(ValueError, match="cannot depend on itself"):
        db.task_update_status("t1", "pending", dependencies=["t1"])


def test_task_update_status_rejects_cycle():
    """
    t1 --depends on--> t2
    t2 --depends on--> t1
    Updating t1's deps to [t2] (while t2 already depends on t1) raises.
    This is the same scenario as the existing API test but at the db layer.
    """
    db.task_upsert(_task("t1"))
    db.task_upsert(_task("t2", dependencies=["t1"]))  # t2 depends on t1

    with pytest.raises(ValueError, match="cycle"):
        db.task_update_status("t1", "pending", dependencies=["t2"])  # cycle!

    # Verify no partial write happened — t1's deps are still empty
    t1 = db.task_get("t1")
    assert t1["dependencies"] == []


def test_task_update_status_normalizes_dependencies():
    """task_update_status normalises dep inputs (dedup, strip, type coercion)."""
    db.task_upsert(_task("t1"))
    db.task_upsert(_task("t2"))

    # String list input (comma-separated, quoted) — must be normalized
    db.task_update_status("t1", "pending", dependencies=' [ "t2" , "t2" ] ')
    t1 = db.task_get("t1")
    assert t1["dependencies"] == ["t2"]  # deduped, parsed from string


def test_task_update_status_without_deps_bypasses_validation():
    """
    Plain status update (no deps kwarg) must NOT go through validation —
    this is existing safe behaviour preserved.
    """
    db.task_upsert(_task("t1", status="pending"))
    db.task_update_status("t1", "in_progress")
    t = db.task_get("t1")
    assert t["status"] == "in_progress"


def test_task_update_status_sets_extra_columns():
    """
    When kwargs includes non-dependency columns they are written correctly.
    """
    db.task_upsert(_task("t1"))
    db.task_update_status("t1", "completed", completed="2026-01-01T00:00:00", agent_id="a99")
    t = db.task_get("t1")
    assert t["status"] == "completed"
    assert t["completed"] == "2026-01-01T00:00:00"
    assert t["agent_id"] == "a99"


# ---------------------------------------------------------------------------
# Criterion 3: Existing API tests still pass.
# (Tested by running the full test suite — this module validates the logic
#  that API tests depend on.)
# ---------------------------------------------------------------------------

def test_cycle_via_task_update_rejected():
    """
    Same scenario as test_api.py::test_update_task_rejects_cycle but at db layer:
    cycle-parent (no deps) and cycle-child (deps=[cycle-parent]).
    Attempting to add cycle-child as a dep of cycle-parent must raise.
    """
    db.task_upsert(_task("cycle-parent"))
    db.task_upsert(_task("cycle-child", dependencies=["cycle-parent"]))

    with pytest.raises(ValueError, match="cycle"):
        db.task_update("cycle-parent", {"dependencies": ["cycle-child"]})


def test_self_dependency_via_task_update_rejected():
    """Self-dependency via task_update must raise."""
    db.task_upsert(_task("t1"))
    with pytest.raises(ValueError, match="cannot depend on itself"):
        db.task_update("t1", {"dependencies": ["t1"]})


def test_task_update_normalizes_dependencies():
    """task_update normalises dep inputs."""
    db.task_upsert(_task("t1"))
    db.task_upsert(_task("t2"))
    db.task_update("t1", {"dependencies": ' [ "t2" , "t2" ] '})
    t1 = db.task_get("t1")
    assert t1["dependencies"] == ["t2"]


def test_task_upsert_validates_dependencies():
    """task_upsert rejects cycles and self-deps at insert time."""
    db.task_upsert(_task("a"))
    db.task_upsert(_task("b", dependencies=["a"]))
    with pytest.raises(ValueError, match="cycle"):
        db.task_upsert(_task("a", dependencies=["b"]))  # a already deps b


def test_task_upsert_self_dependency_rejected():
    """task_upsert rejects tasks that depend on themselves."""
    with pytest.raises(ValueError, match="cannot depend on itself"):
        db.task_upsert(_task("self", dependencies=["self"]))


def test_task_update_status_concurrent_with_task_update():
    """
    One thread calls task_update_status(..., dependencies=[...]) and another
    calls task_update({...}). Both must be atomic — no interleaving that
    bypasses validation.
    """
    db.task_upsert(_task("x", project="p"))
    db.task_upsert(_task("y", project="p", dependencies=["x"]))

    barrier = threading.Barrier(2)
    status_error = []
    update_error = []

    def try_update_status():
        barrier.wait()
        try:
            # This would create x->y but y already has y->x, creating a cycle
            db.task_update_status("x", "pending", dependencies=["y"])
        except ValueError as e:
            status_error.append(str(e))

    def try_update_deps():
        barrier.wait()
        try:
            # y already depends on x; adding x->y would create a cycle
            db.task_update("x", {"dependencies": ["y"]})
        except ValueError as e:
            update_error.append(str(e))

    t1 = threading.Thread(target=try_update_status)
    t2 = threading.Thread(target=try_update_deps)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # At least one must have caught a cycle
    all_errors = status_error + update_error
    assert any("cycle" in e.lower() for e in all_errors), \
        f"Expected cycle error from concurrent writers, got: {all_errors}"
    # No partial state corruption
    x = db.task_get("x")
    y = db.task_get("y")
    # The DB must not contain a cycle between x and y
    assert "x" not in y.get("dependencies", []) or "y" not in x.get("dependencies", []), \
        "Cycle exists in DB after concurrent writes"
