"""
Swarm Controller - Modular Core Package

A modular agent orchestration system that can manage tasks across multiple projects
with support for different project types, task sources, and parallel work.
"""

from swarm.tasks import (
    Task,
    TaskStatus,
    TaskType,
    TaskSource,
    FileTaskSource,
    MemoryTaskSource,
    create_refactor_task,
    create_feature_task,
    create_bug_task,
    create_polish_task,
    get_task_source,
    set_task_source
)

from swarm.projects import (
    Project,
    ProjectRegistry,
    FileLock,
    get_project_registry,
    set_project_registry
)

from swarm.agents import (
    Agent,
    AgentTracker,
    AgentFactory,
    AgentSpawner,
    get_agent_tracker,
    get_agent_spawner
)

from swarm.strategies import (
    TaskSelectionStrategy,
    PriorityStrategy,
    RoundRobinStrategy,
    RefactorFirstStrategy,
    SkillMatchStrategy,
    DependencyAwareStrategy,
    LeastRecentlyWorkedStrategy,
    get_strategy,
    register_strategy,
    list_strategies
)

from swarm.dependencies import (
    DependencyGraph,
    DependencyNode,
    build_graph_from_tasks
)

# Import prompt and profile loaders
from prompts import (
    PromptLoader,
    PromptTemplate,
    PromptContext,
    get_prompt_loader,
    get_template,
    render_prompt
)

from profiles import (
    ProfileLoader,
    ProjectProfile,
    ProjectProfileContext,
    get_profile_loader,
    get_profile_context,
    get_project_profile
)

__all__ = [
    # Tasks
    "Task",
    "TaskStatus", 
    "TaskType",
    "TaskSource",
    "FileTaskSource",
    "MemoryTaskSource",
    "create_refactor_task",
    "create_feature_task",
    "create_bug_task",
    "create_polish_task",
    "get_task_source",
    "set_task_source",
    
    # Projects
    "Project",
    "ProjectRegistry",
    "FileLock",
    "get_project_registry",
    "set_project_registry",
    
    # Agents
    "Agent",
    "AgentTracker",
    "AgentFactory",
    "AgentSpawner",
    "get_agent_tracker",
    "get_agent_spawner",
    
    # Strategies
    "TaskSelectionStrategy",
    "PriorityStrategy",
    "RoundRobinStrategy",
    "RefactorFirstStrategy",
    "SkillMatchStrategy",
    "DependencyAwareStrategy",
    "LeastRecentlyWorkedStrategy",
    "get_strategy",
    "register_strategy",
    "list_strategies",
    
    # Dependencies
    "DependencyGraph",
    "DependencyNode",
    "build_graph_from_tasks",
    
    # Prompts
    "PromptLoader",
    "PromptTemplate",
    "PromptContext",
    "get_prompt_loader",
    "get_template",
    "render_prompt",
    
    # Profiles
    "ProfileLoader",
    "ProjectProfile",
    "ProjectProfileContext",
    "get_profile_loader",
    "get_profile_context",
    "get_project_profile",
]

__version__ = "0.3.0"
