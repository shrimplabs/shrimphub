"""
Prompt Template Loader and Renderer

Loads YAML prompt templates from the prompts/ directory.
Provides a unified interface for generating agent prompts based on task type.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import yaml


class PromptTemplate:
    """Represents a loaded prompt template"""
    
    def __init__(self, name: str, data: Dict[str, Any]):
        self.name = name
        self.description = data.get("description", "")
        self.priority = data.get("priority", 50)
        self.system_prompt = data.get("system_prompt", "")
        self.user_template = data.get("user_template", "")
        self.completion_check = data.get("completion_check", "TASK_COMPLETE").strip()
        self.auto_commit_template = data.get("auto_commit_template", "")
        
    def __repr__(self):
        return f"PromptTemplate({self.name}, priority={self.priority})"


class PromptLoader:
    """
    Loads and manages prompt templates from YAML files.
    
    Usage:
        loader = PromptLoader(Path("prompts"))
        template = loader.load("feature")
        system, user = loader.render(template, {"project_name": "myproject", ...})
    """
    
    def __init__(self, prompts_dir: Optional[Path] = None):
        if prompts_dir is None:
            prompts_dir = Path(__file__).parent.parent / "prompts"
        self.prompts_dir = Path(prompts_dir)
        self._templates: Dict[str, PromptTemplate] = {}
        self._load_templates()
    
    def _load_templates(self):
        """Load all YAML templates from the prompts directory"""
        if not self.prompts_dir.exists():
            print(f"[Prompts] Directory not found: {self.prompts_dir}")
            return
        
        for yaml_file in self.prompts_dir.glob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_file.read_text())
                if data and "name" in data:
                    template = PromptTemplate(data["name"], data)
                    self._templates[template.name] = template
                    print(f"[Prompts] Loaded template: {template.name}")
            except Exception as e:
                print(f"[Prompts] Error loading {yaml_file.name}: {e}")
    
    def load(self, task_type: str) -> Optional[PromptTemplate]:
        """
        Load a prompt template by task type.
        
        Args:
            task_type: The type of task (feature, bug, refactor, polish)
            
        Returns:
            PromptTemplate if found, None otherwise
        """
        return self._templates.get(task_type)
    
    def get(self, task_type: str) -> Optional[PromptTemplate]:
        """Alias for load()"""
        return self.load(task_type)
    
    def get_default(self) -> Optional[PromptTemplate]:
        """Get the default template (refactor)"""
        return self._templates.get("refactor")
    
    def list_types(self) -> list[str]:
        """List all available template names"""
        return list(self._templates.keys())
    
    def reload(self):
        """Reload all templates"""
        self._templates.clear()
        self._load_templates()
    
    def render(self, template: PromptTemplate, context: Dict[str, Any]) -> Tuple[str, str]:
        """
        Render a prompt template with the given context.
        
        Args:
            template: The prompt template to render
            context: Dictionary of variables to substitute
            
        Returns:
            Tuple of (system_prompt, user_prompt)
        """
        system = self._render_text(template.system_prompt, context)
        user = self._render_text(template.user_template, context)
        return system, user
    
    def _render_text(self, template: str, context: Dict[str, Any]) -> str:
        """
        Render template variables with context values.
        
        Supports {{variable}} syntax for simple substitution.
        """
        result = template
        
        # Replace all {{variable}} with context values
        for key, value in context.items():
            placeholder = "{{" + key + "}}"
            result = result.replace(placeholder, str(value))
        
        return result


# Global instance - lazily initialized
_prompt_loader: Optional[PromptLoader] = None


def get_prompt_loader(prompts_dir: Optional[Path] = None) -> PromptLoader:
    """Get the global prompt loader instance"""
    global _prompt_loader
    if _prompt_loader is None:
        _prompt_loader = PromptLoader(prompts_dir)
    return _prompt_loader


def load_prompt(task_type: str, context: Dict[str, Any], 
                prompts_dir: Optional[Path] = None) -> Tuple[str, str]:
    """
    Convenience function to load and render a prompt.
    
    Args:
        task_type: Type of task (feature, bug, refactor, polish)
        context: Variables to substitute in the prompt
        prompts_dir: Optional custom prompts directory
        
    Returns:
        Tuple of (system_prompt, user_prompt)
    """
    loader = get_prompt_loader(prompts_dir)
    template = loader.load(task_type)
    
    if template is None:
        # Fallback to refactor
        template = loader.get_default()
    
    if template is None:
        raise ValueError(f"No prompt template found for task type: {task_type}")
    
    return loader.render(template, context)


# Example usage when run directly
if __name__ == "__main__":
    loader = PromptLoader()
    print(f"Available templates: {loader.list_types()}")
    
    # Test rendering
    if loader.get("feature"):
        ctx = {
            "project_name": "test-project",
            "project_path": "/workspace/test-project",
            "task_description": "Add player movement",
            "language": "Godot",
        }
        system, user = loader.render(loader.get("feature"), ctx)
        print("\n=== System Prompt ===")
        print(system[:500])
        print("\n=== User Prompt ===")
        print(user)
