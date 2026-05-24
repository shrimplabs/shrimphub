"""
Task Dependency Graph

Provides dependency graph visualization and analysis for tasks.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set
from collections import defaultdict, deque


def _norm_dep_ids(raw: Optional[List[str]]) -> List[str]:
    """Normalize dependency IDs: keep non-empty unique strings in original order."""
    out: List[str] = []
    seen: Set[str] = set()
    for d in (raw or []):
        if not isinstance(d, str):
            continue
        dep = d.strip()
        if not dep or dep in seen:
            continue
        seen.add(dep)
        out.append(dep)
    return out


def _dot_quote(value: str) -> str:
    """Return a Graphviz-safe quoted string/ID."""
    s = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


@dataclass
class DependencyNode:
    """Represents a task in the dependency graph"""
    task_id: str
    project: str
    dependencies: List[str] = field(default_factory=list)
    dependents: List[str] = field(default_factory=list)  # tasks that depend on this
    status: str = "pending"
    metadata: Dict[str, Any] = field(default_factory=dict)


class DependencyGraph:
    """
    Manages task dependencies as a directed acyclic graph (DAG).
    """
    
    def __init__(self):
        self._nodes: Dict[str, DependencyNode] = {}
        self._project_tasks: Dict[str, List[str]] = defaultdict(list)
    
    def add_task(
        self,
        task_id: str,
        project: str,
        dependencies: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        status: str = "pending"
    ):
        """Add a task to the graph"""
        deps = _norm_dep_ids(dependencies)
        
        node = DependencyNode(
            task_id=task_id,
            project=project,
            dependencies=deps,
            status=status,
            metadata=metadata or {}
        )
        self._nodes[task_id] = node
        self._project_tasks[project].append(task_id)
        
        # Update dependents for dependencies
        for dep_id in deps:
            if dep_id in self._nodes:
                self._nodes[dep_id].dependents.append(task_id)
            else:
                # Dependency not yet added - will be linked when added
                pass
    
    def remove_task(self, task_id: str) -> bool:
        """Remove a task from the graph"""
        if task_id not in self._nodes:
            return False
        
        node = self._nodes[task_id]
        
        # Remove from dependents' dependency lists
        for dependent_id in node.dependents:
            if dependent_id in self._nodes:
                self._nodes[dependent_id].dependencies.remove(task_id)
        
        # Remove from dependencies' dependents lists
        for dep_id in node.dependencies:
            if dep_id in self._nodes:
                self._nodes[dep_id].dependents.remove(task_id)
        
        # Remove from project index
        self._project_tasks[node.project].remove(task_id)
        
        del self._nodes[task_id]
        return True
    
    def get_task(self, task_id: str) -> Optional[DependencyNode]:
        """Get a task node"""
        return self._nodes.get(task_id)
    
    def get_dependencies(self, task_id: str) -> List[str]:
        """Get direct dependencies of a task"""
        node = self._nodes.get(task_id)
        return node.dependencies.copy() if node else []
    
    def get_dependents(self, task_id: str) -> List[str]:
        """Get tasks that depend on this task"""
        node = self._nodes.get(task_id)
        return node.dependents.copy() if node else []
    
    def get_all_dependencies(self, task_id: str) -> Set[str]:
        """Get all transitive dependencies"""
        visited = set()
        queue = deque([task_id])
        
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            
            node = self._nodes.get(current)
            if node:
                for dep in node.dependencies:
                    if dep not in visited:
                        queue.append(dep)
        
        visited.discard(task_id)
        return visited
    
    def get_all_dependents(self, task_id: str) -> Set[str]:
        """Get all transitive dependents"""
        visited = set()
        queue = deque([task_id])
        
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            
            node = self._nodes.get(current)
            if node:
                for dependent in node.dependents:
                    if dependent not in visited:
                        queue.append(dependent)
        
        visited.discard(task_id)
        return visited
    
    def get_tasks_for_project(self, project: str) -> List[str]:
        """Get all task IDs for a project"""
        return self._project_tasks.get(project, []).copy()
    
    def has_cycle(self) -> bool:
        """Check if the graph has cycles"""
        # Use DFS to detect cycles
        visited = set()
        rec_stack = set()
        
        def visit(node_id: str) -> bool:
            visited.add(node_id)
            rec_stack.add(node_id)
            
            node = self._nodes.get(node_id)
            if node:
                for dep in node.dependencies:
                    if dep not in visited:
                        if visit(dep):
                            return True
                    elif dep in rec_stack:
                        return True
            
            rec_stack.remove(node_id)
            return False
        
        for task_id in self._nodes:
            if task_id not in visited:
                if visit(task_id):
                    return True
        
        return False
    
    def get_execution_order(self) -> List[List[str]]:
        """
        Get tasks in execution order (levels).
        Returns a list of lists, where each inner list contains tasks
        that can be executed in parallel (no dependencies between them).
        """
        # Calculate in-degree for each node
        in_degree = {task_id: 0 for task_id in self._nodes}
        for task_id, node in self._nodes.items():
            for dep in node.dependencies:
                if dep in self._nodes:
                    in_degree[task_id] += 1
        
        # Start with nodes that have no dependencies
        levels = []
        remaining = set(self._nodes.keys())
        
        while remaining:
            # Find all nodes with in-degree 0
            current_level = [tid for tid in remaining if in_degree[tid] == 0]
            
            if not current_level:
                # Cycle detected or error
                break
            
            levels.append(current_level)
            
            # Remove current level nodes and update in-degrees
            for task_id in current_level:
                remaining.remove(task_id)
                node = self._nodes[task_id]
                for dependent in node.dependents:
                    if dependent in remaining:
                        in_degree[dependent] -= 1
        
        return levels
    
    def get_ready_tasks(self, completed: Set[str]) -> List[str]:
        """
        Get tasks that are ready to execute (all dependencies completed).

        A dependency is considered met if:
        - It is in the completed set, OR
        - It is not present in the graph at all (pruned/absent tasks self-heal,
          matching the same rule used by _get_next_task in orchestrator.py)

        Args:
            completed: Set of completed task IDs

        Returns:
            List of task IDs that can be executed now
        """
        active_ids = set(self._nodes.keys())
        ready = []

        for task_id, node in self._nodes.items():
            if node.status != "pending":
                continue

            # Dep is met if completed OR absent from the graph (pruned/failed+pruned)
            if all(dep in completed or dep not in active_ids for dep in node.dependencies):
                ready.append(task_id)

        return ready
    
    def can_execute(self, task_id: str, completed: Set[str]) -> bool:
        """Check if a task can be executed (all dependencies met).

        Matches the self-heal rule in _get_next_task: a dep absent from the
        graph is treated as met (pruned completed/failed tasks don't block).
        """
        node = self._nodes.get(task_id)
        if not node:
            return False
        active_ids = set(self._nodes.keys())
        return all(dep in completed or dep not in active_ids for dep in node.dependencies)
    
    def get_blocked_tasks(self, completed: Set[str]) -> Dict[str, List[str]]:
        """
        Get tasks that are blocked, grouped by which dependency is waiting.
        
        Returns:
            Dict mapping dependency ID to list of blocked task IDs
        """
        blocked = defaultdict(list)
        
        for task_id, node in self._nodes.items():
            if node.status != "pending":
                continue
            
            for dep in node.dependencies:
                if dep not in completed:
                    blocked[dep].append(task_id)
        
        return dict(blocked)
    
    def to_dot(self) -> str:
        """Generate DOT graph representation for visualization"""
        # Assign a stable accent color per project
        project_palette = [
            "#1f6feb", "#388bfd", "#6e40c9", "#8957e5",
            "#f0883e", "#d29922", "#3fb950", "#238636",
        ]
        projects = sorted(set(n.project for n in self._nodes.values()))
        project_color = {p: project_palette[i % len(project_palette)] for i, p in enumerate(projects)}

        node_styles = {
            "pending":     ("\"#30363d\"", "\"#c9d1d9\""),
            "in_progress": ("\"#f0883e\"", "\"#0d1117\""),
            "completed":   ("\"#238636\"", "\"#ffffff\""),
            "failed":      ("\"#da3633\"", "\"#ffffff\""),
        }

        lines = [
            "digraph tasks {",
            "  rankdir=LR;",
            "  bgcolor=\"#0d1117\";",
            "  graph [fontname=\"-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif\" pad=0.4 nodesep=0.5 ranksep=0.8];",
            "  node  [fontname=\"-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif\" fontsize=11 shape=box style=filled penwidth=1.5 margin=\"0.15,0.1\"];",
            "  edge  [color=\"#8b949e\" arrowsize=0.8 penwidth=1.5];",
        ]

        # Cluster by project
        for proj in projects:
            accent = project_color[proj]
            lines.append(f"  subgraph {_dot_quote(f'cluster_{proj}')} {{")
            lines.append(f"    label={_dot_quote(proj)};")
            lines.append(f'    fontname="sans-serif"; fontsize=10; fontcolor="{accent}";')
            lines.append(f'    color="{accent}"; penwidth=1.5; style=rounded;')
            for task_id, node in self._nodes.items():
                if node.project != proj:
                    continue
                fill, font = node_styles.get(node.status, ("\"#30363d\"", "\"#c9d1d9\""))
                # Short label: strip project prefix if id starts with it
                short = task_id
                if short.startswith(proj + "-"):
                    short = short[len(proj) + 1:]
                lines.append(
                    f"    {_dot_quote(task_id)} [label={_dot_quote(short)}, fillcolor={fill}, fontcolor={font}, color=\"{accent}\"];"
                )
            lines.append("  }")

        # Edges — only between live nodes; deps not in the live set are
        # rendered by the ghost pass in api_deps.py, so skip them here to
        # avoid Graphviz auto-creating undefined grey nodes.
        live_ids = set(self._nodes.keys())
        for task_id, node in self._nodes.items():
            for dep in node.dependencies:
                if dep in live_ids:
                    lines.append(f"  {_dot_quote(dep)} -> {_dot_quote(task_id)};")

        lines.append("}")
        return "\n".join(lines)
    
    def get_subgraph(self, root_id: str, direction: str = "downstream", depth: int = 0) -> Set[str]:
        """Return the set of task IDs reachable from root_id.

        direction: "downstream" (dependents), "upstream" (dependencies), or "both"
        depth: max hops (0 = unlimited)
        """
        if root_id not in self._nodes:
            return set()
        result: Set[str] = {root_id}
        frontier = {root_id}
        hops = 0
        while frontier and (depth == 0 or hops < depth):
            next_frontier: Set[str] = set()
            for node_id in frontier:
                node = self._nodes.get(node_id)
                if not node:
                    continue
                if direction in ("downstream", "both"):
                    next_frontier.update(d for d in node.dependents if d not in result)
                if direction in ("upstream", "both"):
                    next_frontier.update(d for d in node.dependencies if d not in result)
            result.update(next_frontier)
            frontier = next_frontier
            hops += 1
        return result

    def get_critical_path(self, project: str = "") -> List[str]:
        """Return the longest chain of pending/in_progress tasks by hop count.

        Uses a longest-path algorithm on the DAG (works because it's acyclic).
        Returns the ordered list of task IDs on the critical path.
        Filters to the given project if specified.
        """
        # Build a filtered node set
        nodes = {
            nid: node for nid, node in self._nodes.items()
            if node.status in ("pending", "in_progress")
            and (not project or node.project == project)
        }
        if not nodes:
            return []

        # Longest path via memoised DFS
        memo: Dict[str, List[str]] = {}

        def longest_from(nid: str) -> List[str]:
            if nid in memo:
                return memo[nid]
            node = nodes.get(nid)
            if not node:
                memo[nid] = [nid]
                return [nid]
            best: List[str] = []
            for dep_id in node.dependents:
                if dep_id in nodes:
                    candidate = longest_from(dep_id)
                    if len(candidate) > len(best):
                        best = candidate
            memo[nid] = [nid] + best
            return memo[nid]

        # Find roots (no unmet pending dependencies)
        roots = [
            nid for nid, node in nodes.items()
            if not any(d in nodes for d in node.dependencies)
        ]
        critical: List[str] = []
        for root in roots:
            path = longest_from(root)
            if len(path) > len(critical):
                critical = path
        return critical

    def get_stats(self) -> Dict[str, Any]:
        """Get graph statistics"""
        status_counts = defaultdict(int)
        for node in self._nodes.values():
            status_counts[node.status] += 1
        
        return {
            "total_tasks": len(self._nodes),
            "by_status": dict(status_counts),
            "has_cycles": self.has_cycle(),
            "execution_levels": len(self.get_execution_order()),
            "projects": len(self._project_tasks)
        }


def build_graph_from_tasks(tasks: List[Any]) -> DependencyGraph:
    """Build a dependency graph from a list of task objects"""
    graph = DependencyGraph()
    
    for task in tasks:
        task_id = task.id if hasattr(task, 'id') else task.get('id')
        project = task.project if hasattr(task, 'project') else task.get('project')
        dependencies = task.dependencies if hasattr(task, 'dependencies') else task.get('dependencies', [])
        status = task.status if hasattr(task, 'status') else task.get('status', 'pending')
        
        graph.add_task(
            task_id=task_id,
            project=project,
            dependencies=dependencies,
            status=status
        )
    
    return graph
