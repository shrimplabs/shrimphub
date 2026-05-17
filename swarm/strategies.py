"""
Task Selection Strategies

Implements different strategies for selecting the next task to work on.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from swarm.tasks import Task, TaskSource


class TaskSelectionStrategy(ABC):
    """Abstract base class for task selection strategies"""
    
    @abstractmethod
    def select_next(
        self,
        task_source: TaskSource,
        context: Dict[str, Any]
    ) -> Optional[Task]:
        """
        Select the next task to work on.
        
        Args:
            task_source: Source to query tasks from
            context: Additional context (available_projects, config, etc.)
        
        Returns:
            The selected task or None if no task available
        """
        pass
    
    def filter_available(
        self,
        tasks: List[Task],
        context: Dict[str, Any],
        task_source: Optional["TaskSource"] = None,
    ) -> List[Task]:
        """Filter tasks that are available (dependencies met, project not locked, etc.)"""
        available_projects = context.get("available_projects", set())
        locked_projects = context.get("locked_projects", set())

        # Get completed task IDs
        completed_ids: set = set()
        if task_source is not None:
            completed_ids = {t.id for t in task_source.get_all_tasks() if t.status == "completed"}
        
        available = []
        for task in tasks:
            # Skip if project is locked
            if task.project in locked_projects:
                continue
            
            # Skip if project not in available list
            if available_projects and task.project not in available_projects:
                continue
            
            # Check dependencies
            deps = task.dependencies
            if deps and not all(dep_id in completed_ids for dep_id in deps):
                continue
            
            available.append(task)
        
        return available


class PriorityStrategy(TaskSelectionStrategy):
    """Select highest priority task"""
    
    def select_next(
        self,
        task_source: TaskSource,
        context: Dict[str, Any]
    ) -> Optional[Task]:
        pending = task_source.get_pending_tasks()
        available = self.filter_available(pending, context, task_source)
        
        if not available:
            return None
        
        # Sort by priority (highest first), then by creation time
        available.sort(key=lambda t: (-t.priority, t.created))
        return available[0]


class RoundRobinStrategy(TaskSelectionStrategy):
    """Round-robin through projects"""
    
    def __init__(self):
        self._last_project: Optional[str] = None
    
    def select_next(
        self,
        task_source: TaskSource,
        context: Dict[str, Any]
    ) -> Optional[Task]:
        available_projects = context.get("available_projects", [])
        if not available_projects:
            return None
        
        # Get list of projects to cycle through
        projects = list(available_projects)
        
        # Find starting point
        start_idx = 0
        if self._last_project and self._last_project in projects:
            start_idx = projects.index(self._last_project) + 1
        
        # Try each project starting from last position
        for i in range(len(projects)):
            idx = (start_idx + i) % len(projects)
            project = projects[idx]
            
            # Find pending tasks for this project
            project_tasks = [t for t in task_source.get_pending_tasks() if t.project == project]
            available = self.filter_available(project_tasks, context, task_source)
            
            if available:
                available.sort(key=lambda t: (-t.priority, t.created))
                self._last_project = project
                return available[0]
        
        return None


class RefactorFirstStrategy(TaskSelectionStrategy):
    """Always prioritize refactor tasks, then others by priority"""
    
    def select_next(
        self,
        task_source: TaskSource,
        context: Dict[str, Any]
    ) -> Optional[Task]:
        pending = task_source.get_pending_tasks()
        
        # First, look for refactor tasks
        refactor_tasks = [t for t in pending if t.type == "refactor"]
        available_refactors = self.filter_available(refactor_tasks, context, task_source)
        
        if available_refactors:
            available_refactors.sort(key=lambda t: (-t.priority, t.created))
            return available_refactors[0]
        
        # Then other tasks
        other_tasks = [t for t in pending if t.type != "refactor"]
        available = self.filter_available(other_tasks, context, task_source)
        
        if available:
            available.sort(key=lambda t: (-t.priority, t.created))
            return available[0]
        
        return None


class SkillMatchStrategy(TaskSelectionStrategy):
    """Match task to agent skills (for future use with agent pools)"""
    
    def __init__(self, skills: Optional[Dict[str, List[str]]] = None):
        self.skills = skills or {}
    
    def select_next(
        self,
        task_source: TaskSource,
        context: Dict[str, Any]
    ) -> Optional[Task]:
        # Get available agent skills from context
        agent_skills = context.get("agent_skills", {})
        available_projects = context.get("available_projects", set())
        
        pending = task_source.get_pending_tasks()
        available = self.filter_available(pending, context, task_source)
        
        if not available:
            return None
        
        # Score tasks based on skill match
        scored = []
        for task in available:
            score = 0
            
            # Check if any agent has required skills for this task
            task_type = task.type
            for agent_id, skills in agent_skills.items():
                if task_type in skills:
                    score += 10
            
            # Add priority score
            score += task.priority
            
            scored.append((score, -task.priority, task))
        
        scored.sort(key=lambda x: (x[0], x[1]))
        return scored[0][2] if scored else None


class DependencyAwareStrategy(TaskSelectionStrategy):
    """Prioritize tasks whose dependencies are completed"""
    
    def select_next(
        self,
        task_source: TaskSource,
        context: Dict[str, Any]
    ) -> Optional[Task]:
        pending = task_source.get_pending_tasks()
        all_tasks = {t.id: t for t in task_source.get_all_tasks()}
        
        # Get completed task IDs
        completed_ids = {t.id for t in all_tasks.values() if t.status == "completed"}
        
        # Score tasks based on dependency readiness
        scored = []
        for task in pending:
            # Skip locked projects
            locked = context.get("locked_projects", set())
            if task.project in locked:
                continue
            
            available = context.get("available_projects", set())
            if available and task.project not in available:
                continue
            
            deps = task.dependencies
            if not deps:
                # No dependencies - ready immediately
                ready_score = 100
            else:
                # Score based on how many dependencies are met
                met = sum(1 for d in deps if d in completed_ids)
                ready_score = (met / len(deps)) * 100
            
            # Sort by readiness, then priority
            scored.append((ready_score, -task.priority, task))
        
        if not scored:
            return None
        
        scored.sort(key=lambda x: (x[0], x[1]))
        return scored[0][2]


class LeastRecentlyWorkedStrategy(TaskSelectionStrategy):
    """Pick the project that hasn't been worked on longest"""
    
    def __init__(self):
        self._project_last_worked: Dict[str, str] = {}  # project -> timestamp
    
    def select_next(
        self,
        task_source: TaskSource,
        context: Dict[str, Any]
    ) -> Optional[Task]:
        pending = task_source.get_pending_tasks()
        available = self.filter_available(pending, context, task_source)
        
        if not available:
            return None
        
        # Get project last worked times from context
        project_times = context.get("project_last_worked", {})
        
        # Find project that hasn't been worked on longest
        available_projects = set(t.project for t in available)
        
        oldest_project = None
        oldest_time = None
        
        for project in available_projects:
            # Use context time, or fall back to stored time, or use epoch
            time_val = project_times.get(project) or self._project_last_worked.get(project)
            
            if time_val is None:
                # Never worked - highest priority
                oldest_project = project
                oldest_time = ""
                break
            
            if oldest_time is None or time_val < oldest_time:
                oldest_time = time_val
                oldest_project = project
        
        if oldest_project is None:
            return None
        
        # Get task for this project
        project_tasks = [t for t in available if t.project == oldest_project]
        project_tasks.sort(key=lambda t: (-t.priority, t.created))
        
        return project_tasks[0]
    
    def mark_worked(self, project: str):
        """Mark that work was done on a project"""
        from datetime import datetime
        self._project_last_worked[project] = datetime.now().isoformat()


# Strategy registry
_strategies: Dict[str, type] = {
    "priority": PriorityStrategy,
    "round_robin": RoundRobinStrategy,
    "refactor_first": RefactorFirstStrategy,
    "skill_match": SkillMatchStrategy,
    "dependency_aware": DependencyAwareStrategy,
    "least_recently_worked": LeastRecentlyWorkedStrategy,
}


def get_strategy(name: str, **kwargs) -> TaskSelectionStrategy:
    """Get a strategy by name"""
    strategy_class = _strategies.get(name, PriorityStrategy)
    return strategy_class(**kwargs)


def register_strategy(name: str, strategy_class: type):
    """Register a custom strategy"""
    _strategies[name] = strategy_class


def list_strategies() -> List[str]:
    """List available strategy names"""
    return list(_strategies.keys())
