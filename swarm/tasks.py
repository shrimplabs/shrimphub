"""
Task Source Abstraction Layer

Provides a pluggable interface for different task sources:
- FileTaskSource: reads from task-queue.json (current implementation)
- MemoryTaskSource: in-memory task storage
- Future: LinearTaskSource, GitHubTaskSource, etc.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import os
import uuid


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskType(Enum):
    REFACTOR = "refactor"
    FEATURE = "feature"
    BUG = "bug"
    POLISH = "polish"
    MANAGER = "manager"
    PROJECT_CREATE = "project_create"


@dataclass
class Task:
    """Represents a task in the system"""
    id: str
    project: str
    type: str
    description: str
    priority: int = 50
    status: str = "pending"
    created: str = field(default_factory=lambda: datetime.now().isoformat())
    started: Optional[str] = None
    completed: Optional[str] = None
    agent_id: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    acceptance_test: Optional[Dict[str, Any]] = None  # {"lang": "gdscript"|"python", "code": "..."}
    attempts: int = 0
    max_attempts: int = 3
    run_after: Optional[str] = None  # ISO timestamp -- skip until this time passes
    plan_id: Optional[str] = None  # References a plan in the plans table

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "type": self.type,
            "description": self.description,
            "priority": self.priority,
            "status": self.status,
            "created": self.created,
            "started": self.started,
            "completed": self.completed,
            "agent_id": self.agent_id,
            "dependencies": self.dependencies,
            "metadata":        self.metadata,
            "acceptance_test":  self.acceptance_test,
            "attempts":         self.attempts,
            "max_attempts":     self.max_attempts,
            "run_after":        self.run_after,
            "plan_id":          self.plan_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            project=data["project"],
            type=data.get("type", "feature"),
            description=data.get("description", ""),
            priority=data.get("priority", 50),
            status=data.get("status", "pending"),
            created=data.get("created", datetime.now().isoformat()),
            started=data.get("started"),
            completed=data.get("completed"),
            agent_id=data.get("agent_id"),
            dependencies=data.get("dependencies", []),
            metadata=data.get("metadata", {}),
            acceptance_test=data.get("acceptance_test"),
            attempts=data.get("attempts", 0),
            max_attempts=data.get("max_attempts", 3),
            run_after=data.get("run_after"),
            plan_id=data.get("plan_id"),
        )


class TaskSource(ABC):
    """Abstract base class for task sources"""
    
    @abstractmethod
    def get_all_tasks(self) -> List[Task]:
        """Get all tasks"""
        pass
    
    @abstractmethod
    def get_pending_tasks(self) -> List[Task]:
        """Get all pending tasks"""
        pass
    
    @abstractmethod
    def get_task(self, task_id: str) -> Optional[Task]:
        """Get a specific task by ID"""
        pass
    
    @abstractmethod
    def add_task(self, task: Task) -> str:
        """Add a new task, returns task ID"""
        pass
    
    @abstractmethod
    def update_task(self, task: Task):
        """Update an existing task"""
        pass
    
    @abstractmethod
    def remove_task(self, task_id: str) -> bool:
        """Remove a task"""
        pass
    
    @abstractmethod
    def get_tasks_for_project(self, project: str) -> List[Task]:
        """Get all tasks for a specific project"""
        pass
    
    @abstractmethod
    def get_tasks_by_status(self, status: str) -> List[Task]:
        """Get all tasks with a specific status"""
        pass
    
    def get_tasks_by_type(self, task_type: str) -> List[Task]:
        """Get all tasks of a specific type"""
        return [t for t in self.get_all_tasks() if t.type == task_type]
    
    def get_next_task(self, project_filter: Optional[List[str]] = None) -> Optional[Task]:
        """
        Get the next task to work on.
        Override this for custom prioritization logic.
        """
        pending = self.get_pending_tasks()
        
        # Filter by project if specified
        if project_filter:
            pending = [t for t in pending if t.project in project_filter]
        
        # Filter out tasks with unmet dependencies
        ready = []
        for task in pending:
            deps_met = all(dep in [t.id for t in self.get_tasks_by_status("completed")] 
                         for dep in task.dependencies)
            if deps_met:
                ready.append(task)
        
        # Sort by priority (highest first), then by creation time
        ready.sort(key=lambda t: (-t.priority, t.created))
        
        return ready[0] if ready else None
    
    def get_completed_tasks(self) -> List[Task]:
        """Get all completed tasks"""
        return self.get_tasks_by_status("completed")
    
    def get_failed_tasks(self) -> List[Task]:
        """Get all failed tasks"""
        return self.get_tasks_by_status("failed")


class FileTaskSource(TaskSource):
    """Task source that reads/writes to a JSON file"""
    
    def __init__(self, tasks_file: Path):
        self.tasks_file = Path(tasks_file)
        self._tasks: Dict[str, Task] = {}
        self._load()
    
    def _load(self):
        """Load tasks from file"""
        if self.tasks_file.exists():
            try:
                data = json.loads(self.tasks_file.read_text())
                tasks_list = data.get("tasks", [])
                self._tasks = {t["id"]: Task.from_dict(t) for t in tasks_list}
            except Exception as e:
                print(f"[TaskSource] Error loading tasks: {e}")
                self._tasks = {}
        else:
            self._tasks = {}
    
    def _save(self):
        """Save tasks to file"""
        self.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "tasks": [t.to_dict() for t in self._tasks.values()]
        }
        self.tasks_file.write_text(json.dumps(data, indent=2))
    
    def get_all_tasks(self) -> List[Task]:
        return list(self._tasks.values())
    
    def get_pending_tasks(self) -> List[Task]:
        return [t for t in self._tasks.values() if t.status == "pending"]
    
    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)
    
    def add_task(self, task: Task) -> str:
        if not task.id:
            task.id = str(uuid.uuid4())
        self._tasks[task.id] = task
        self._save()
        return task.id
    
    def update_task(self, task: Task):
        self._tasks[task.id] = task
        self._save()
    
    def remove_task(self, task_id: str) -> bool:
        if task_id in self._tasks:
            del self._tasks[task_id]
            self._save()
            return True
        return False
    
    def get_tasks_for_project(self, project: str) -> List[Task]:
        return [t for t in self._tasks.values() if t.project == project]
    
    def get_tasks_by_status(self, status: str) -> List[Task]:
        return [t for t in self._tasks.values() if t.status == status]


class SQLiteTaskSource(TaskSource):
    """Task source backed by SQLite via swarm.db."""

    def __init__(self):
        from swarm import db as _db
        self._db = _db

    def get_all_tasks(self, exclude_statuses: tuple = ()) -> List[Task]:
        return [Task.from_dict(t) for t in self._db.task_get_all(exclude_statuses=exclude_statuses)]

    def get_pending_tasks(self) -> List[Task]:
        return [Task.from_dict(t) for t in self._db.task_get_by_status("pending")]

    def get_task(self, task_id: str) -> Optional[Task]:
        d = self._db.task_get(task_id)
        return Task.from_dict(d) if d else None

    def add_task(self, task: Task) -> str:
        if not task.id:
            task.id = str(uuid.uuid4())
        self._db.task_upsert(task.to_dict())
        return task.id

    def update_task(self, task: Task):
        self._db.task_upsert(task.to_dict())

    def remove_task(self, task_id: str) -> bool:
        return self._db.task_delete(task_id)

    def get_tasks_for_project(self, project: str) -> List[Task]:
        return [Task.from_dict(t) for t in self._db.task_get_by_project(project)]

    def get_tasks_by_status(self, status: str) -> List[Task]:
        return [Task.from_dict(t) for t in self._db.task_get_by_status(status)]


class MemoryTaskSource(TaskSource):
    """In-memory task source (useful for testing)"""
    
    def __init__(self):
        self._tasks: Dict[str, Task] = {}
    
    def get_all_tasks(self) -> List[Task]:
        return list(self._tasks.values())
    
    def get_pending_tasks(self) -> List[Task]:
        return [t for t in self._tasks.values() if t.status == "pending"]
    
    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)
    
    def add_task(self, task: Task) -> str:
        if not task.id:
            task.id = str(uuid.uuid4())
        self._tasks[task.id] = task
        return task.id
    
    def update_task(self, task: Task):
        self._tasks[task.id] = task
    
    def remove_task(self, task_id: str) -> bool:
        if task_id in self._tasks:
            del self._tasks[task_id]
            return True
        return False
    
    def get_tasks_for_project(self, project: str) -> List[Task]:
        return [t for t in self._tasks.values() if t.project == project]
    
    def get_tasks_by_status(self, status: str) -> List[Task]:
        return [t for t in self._tasks.values() if t.status == status]


# Task generation helpers
def create_refactor_task(project: str, description: str, priority: int = 100) -> Task:
    """Create a refactor task"""
    return Task(
        id=f"refactor-{project}-{int(datetime.now().timestamp())}",
        project=project,
        type="refactor",
        description=description,
        priority=priority,
        status="pending",
        created=datetime.now().isoformat()
    )


def create_feature_task(project: str, description: str, priority: int = 50) -> Task:
    """Create a feature task"""
    return Task(
        id=f"feature-{project}-{int(datetime.now().timestamp())}",
        project=project,
        type="feature",
        description=description,
        priority=priority,
        status="pending",
        created=datetime.now().isoformat()
    )


def create_bug_task(project: str, description: str, priority: int = 80) -> Task:
    """Create a bug fix task"""
    return Task(
        id=f"bug-{project}-{int(datetime.now().timestamp())}",
        project=project,
        type="bug",
        description=description,
        priority=priority,
        status="pending",
        created=datetime.now().isoformat()
    )


def create_polish_task(project: str, description: str, priority: int = 50) -> Task:
    """Create a polish task"""
    return Task(
        id=f"polish-{project}-{int(datetime.now().timestamp())}",
        project=project,
        type="polish",
        description=description,
        priority=priority,
        status="pending",
        created=datetime.now().isoformat()
    )


# Global instance factory
_task_source: Optional[TaskSource] = None


def get_task_source(tasks_file: Optional[Path] = None) -> TaskSource:
    """Get the global task source instance"""
    global _task_source
    if _task_source is None:
        if tasks_file is None:
            # Default location
            workspace = Path(os.path.expanduser("~/workspace"))
            tasks_file = workspace / "swarm-controller" / "data" / "task-queue.json"
        _task_source = FileTaskSource(tasks_file)
    return _task_source


def set_task_source(source: TaskSource):
    """Set a custom task source (useful for testing)"""
    global _task_source
    _task_source = source
