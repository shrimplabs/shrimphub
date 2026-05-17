"""Tests for swarm/dependencies.py — DAG and dependency resolution."""
import pytest
from swarm.dependencies import DependencyGraph, build_graph_from_tasks
from swarm.tasks import Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def graph_with(*tasks):
    """Build a graph from (id, project, deps, status) tuples."""
    g = DependencyGraph()
    for item in tasks:
        if len(item) == 2:
            tid, proj = item; deps = []; status = "pending"
        elif len(item) == 3:
            tid, proj, deps = item; status = "pending"
        else:
            tid, proj, deps, status = item
        g.add_task(tid, proj, dependencies=deps, status=status)
    return g


# ---------------------------------------------------------------------------
# add_task / get_task
# ---------------------------------------------------------------------------

def test_add_and_get_task():
    g = DependencyGraph()
    g.add_task("t1", "proj")
    node = g.get_task("t1")
    assert node is not None
    assert node.task_id == "t1"
    assert node.project == "proj"


def test_get_missing_task_returns_none():
    g = DependencyGraph()
    assert g.get_task("ghost") is None


def test_add_task_links_dependents():
    g = graph_with(("t1", "p"), ("t2", "p", ["t1"]))
    assert "t2" in g.get_task("t1").dependents


def test_add_task_with_unknown_dep_does_not_crash():
    """If dep is not yet in graph, it's fine — no link yet."""
    g = DependencyGraph()
    g.add_task("t1", "proj", dependencies=["not-yet-added"])
    # no crash, dep just isn't linked yet


# ---------------------------------------------------------------------------
# remove_task
# ---------------------------------------------------------------------------

def test_remove_task():
    g = graph_with(("t1", "p"))
    assert g.remove_task("t1") is True
    assert g.get_task("t1") is None


def test_remove_missing_task_returns_false():
    g = DependencyGraph()
    assert g.remove_task("ghost") is False


def test_remove_task_cleans_up_dependents():
    g = graph_with(("t1", "p"), ("t2", "p", ["t1"]))
    g.remove_task("t2")
    # t1's dependents list should no longer contain t2
    assert "t2" not in g.get_task("t1").dependents


def test_remove_task_cleans_up_dependency_refs():
    g = graph_with(("t1", "p"), ("t2", "p", ["t1"]))
    g.remove_task("t1")
    # t2's dependencies list should no longer contain t1
    assert "t1" not in g.get_task("t2").dependencies


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

def test_no_cycle_in_dag():
    g = graph_with(("t1", "p"), ("t2", "p", ["t1"]), ("t3", "p", ["t2"]))
    assert g.has_cycle() is False


def test_direct_cycle_detected():
    g = DependencyGraph()
    g.add_task("t1", "p", dependencies=["t2"])
    g.add_task("t2", "p", dependencies=["t1"])
    assert g.has_cycle() is True


def test_self_cycle_detected():
    g = DependencyGraph()
    g.add_task("t1", "p", dependencies=["t1"])
    assert g.has_cycle() is True


def test_three_node_cycle():
    g = DependencyGraph()
    g.add_task("a", "p", dependencies=["c"])
    g.add_task("b", "p", dependencies=["a"])
    g.add_task("c", "p", dependencies=["b"])
    assert g.has_cycle() is True


def test_empty_graph_no_cycle():
    assert DependencyGraph().has_cycle() is False


# ---------------------------------------------------------------------------
# Execution order (levels)
# ---------------------------------------------------------------------------

def test_execution_order_linear_chain():
    g = graph_with(("t1", "p"), ("t2", "p", ["t1"]), ("t3", "p", ["t2"]))
    levels = g.get_execution_order()
    assert len(levels) == 3
    assert "t1" in levels[0]
    assert "t2" in levels[1]
    assert "t3" in levels[2]


def test_execution_order_parallel():
    g = graph_with(("t1", "p"), ("t2", "p"), ("t3", "p", ["t1", "t2"]))
    levels = g.get_execution_order()
    assert len(levels) == 2
    assert set(levels[0]) == {"t1", "t2"}
    assert levels[1] == ["t3"]


def test_execution_order_empty_graph():
    assert DependencyGraph().get_execution_order() == []


def test_execution_order_single_node():
    g = graph_with(("t1", "p"))
    levels = g.get_execution_order()
    assert levels == [["t1"]]


# ---------------------------------------------------------------------------
# Ready tasks
# ---------------------------------------------------------------------------

def test_get_ready_tasks_no_deps():
    g = graph_with(("t1", "p"), ("t2", "p"))
    ready = g.get_ready_tasks(completed=set())
    assert set(ready) == {"t1", "t2"}


def test_get_ready_tasks_dep_not_completed():
    g = graph_with(("t1", "p"), ("t2", "p", ["t1"]))
    ready = g.get_ready_tasks(completed=set())
    assert ready == ["t1"]


def test_get_ready_tasks_dep_completed():
    g = graph_with(("t1", "p"), ("t2", "p", ["t1"]))
    ready = g.get_ready_tasks(completed={"t1"})
    assert "t2" in ready


def test_get_ready_tasks_skips_non_pending():
    g = DependencyGraph()
    g.add_task("t1", "p", status="completed")
    g.add_task("t2", "p", status="in_progress")
    ready = g.get_ready_tasks(completed=set())
    assert ready == []


# ---------------------------------------------------------------------------
# Transitive dependencies
# ---------------------------------------------------------------------------

def test_get_all_dependencies():
    g = graph_with(("t1", "p"), ("t2", "p", ["t1"]), ("t3", "p", ["t2"]))
    deps = g.get_all_dependencies("t3")
    assert deps == {"t1", "t2"}


def test_get_all_dependencies_no_deps():
    g = graph_with(("t1", "p"))
    assert g.get_all_dependencies("t1") == set()


def test_get_all_dependents():
    g = graph_with(("t1", "p"), ("t2", "p", ["t1"]), ("t3", "p", ["t2"]))
    dependents = g.get_all_dependents("t1")
    assert dependents == {"t2", "t3"}


# ---------------------------------------------------------------------------
# can_execute
# ---------------------------------------------------------------------------

def test_can_execute_no_deps():
    g = graph_with(("t1", "p"))
    assert g.can_execute("t1", completed=set()) is True


def test_can_execute_dep_not_done():
    g = graph_with(("t1", "p"), ("t2", "p", ["t1"]))
    assert g.can_execute("t2", completed=set()) is False


def test_can_execute_dep_done():
    g = graph_with(("t1", "p"), ("t2", "p", ["t1"]))
    assert g.can_execute("t2", completed={"t1"}) is True


def test_can_execute_missing_task_returns_false():
    g = DependencyGraph()
    assert g.can_execute("ghost", completed=set()) is False


# ---------------------------------------------------------------------------
# get_tasks_for_project
# ---------------------------------------------------------------------------

def test_get_tasks_for_project():
    g = graph_with(("t1", "alpha"), ("t2", "beta"), ("t3", "alpha"))
    result = g.get_tasks_for_project("alpha")
    assert set(result) == {"t1", "t3"}


# ---------------------------------------------------------------------------
# build_graph_from_tasks
# ---------------------------------------------------------------------------

def test_build_graph_from_task_objects():
    tasks = [
        Task(id="t1", project="p", type="feature", description="x"),
        Task(id="t2", project="p", type="feature", description="x", dependencies=["t1"]),
    ]
    g = build_graph_from_tasks(tasks)
    assert g.get_task("t1") is not None
    assert g.get_task("t2") is not None
    assert g.can_execute("t2", completed={"t1"}) is True


def test_build_graph_from_dicts():
    tasks = [
        {"id": "t1", "project": "p", "dependencies": [], "status": "pending"},
        {"id": "t2", "project": "p", "dependencies": ["t1"], "status": "pending"},
    ]
    g = build_graph_from_tasks(tasks)
    assert not g.has_cycle()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def test_get_stats():
    g = graph_with(("t1", "p"), ("t2", "p", ["t1"]))
    stats = g.get_stats()
    assert stats["total_tasks"] == 2
    assert stats["has_cycles"] is False
    assert stats["execution_levels"] == 2


# ---------------------------------------------------------------------------
# DOT output
# ---------------------------------------------------------------------------

def test_to_dot_contains_task_ids():
    g = graph_with(("t1", "p"), ("t2", "p", ["t1"]))
    dot = g.to_dot()
    assert "t1" in dot
    assert "t2" in dot
    assert "digraph" in dot
