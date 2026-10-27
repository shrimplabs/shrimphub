"""
Prompt Template Loader and Plugin System

Loads YAML prompt templates from the prompts/ directory.
Provides a unified interface for generating agent prompts.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional
import yaml


class PromptTemplate:
    """Represents a loaded prompt template"""
    
    def __init__(self, name: str, data: Dict[str, Any]):
        self.name = name
        self.description = data.get("description", "")
        self.priority = data.get("priority", 50)
        self.system_prompt = data.get("system_prompt", "")
        self.user_template = data.get("user_template", "")
        self.tools = data.get("tools", [])
        self.completion_check = data.get("completion_check", "TASK_COMPLETE").strip()
        self.auto_commit_template = data.get("auto_commit_template", "")
        self.file_extensions = data.get("file_extensions", [])
        self.max_lines = data.get("max_lines", 5000)
        
        # Resume templates
        self.resume_exists = data.get("resume_template_exists", "")
        self.resume_all_done = data.get("resume_template_all_done", "")
        self.resume_oversized = data.get("resume_template_oversized", "")
        self.resume_none = data.get("resume_template_none", "")
    
    def render(self, context: Dict[str, Any]) -> tuple[str, str]:
        """
        Render the template with the given context.
        Returns (system_prompt, user_prompt) tuple.
        """
        system = self._render_text(self.system_prompt, context)
        user = self._render_text(self.user_template, context)
        return system, user
    
    def _render_text(self, template: str, context: Dict[str, Any]) -> str:
        """Simple template rendering with {{variable}} syntax"""
        result = template
        for key, value in context.items():
            # Handle simple variables
            result = result.replace(f"{{{{{key}}}}}", str(value))
            
            # Handle conditional blocks {{#if key}}...{{/if}}
            if_blocks = re.findall(r'\{\{#if\s+(\w+)\}\}(.*?)\{\{/if\}\}', result, re.DOTALL)
            for var_name, block_content in if_blocks:
                if context.get(var_name):
                    result = result.replace(
                        f'{{{{#if {var_name}}}}}{block_content}{{{{/if}}}}',
                        self._render_text(block_content, context)
                    )
                else:
                    result = result.replace(f'{{{{#if {var_name}}}}}{block_content}{{{{/if}}}}', '')
            
            # Handle loops {{#each key}}...{{/each}} - simplified
            # For now, just handle basic iteration if needed
        return result
    
    def get_resume_context(self, context: Dict[str, Any], resume_type: str) -> str:
        """Get the appropriate resume context based on project state"""
        if resume_type == "exists":
            return self._render_text(self.resume_exists, context)
        elif resume_type == "all_done":
            return self._render_text(self.resume_all_done, context)
        elif resume_type == "oversized":
            return self._render_text(self.resume_oversized, context)
        else:
            return self._render_text(self.resume_none, context)


class PromptLoader:
    """Loads and manages prompt templates"""
    
    def __init__(self, prompts_dir: Optional[Path] = None):
        if prompts_dir is None:
            # Default to prompts/ directory in the project root (parent of this file's directory)
            prompts_dir = Path(__file__).parent.parent / "prompts"
        self.prompts_dir = Path(prompts_dir)
        self._templates: Dict[str, PromptTemplate] = {}
        self._load_templates()
    
    def _load_templates(self):
        """Load all YAML templates from the prompts directory"""
        if not self.prompts_dir.exists():
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
    
    def get(self, name: str) -> Optional[PromptTemplate]:
        """Get a template by name"""
        return self._templates.get(name)
    
    def get_default(self) -> Optional[PromptTemplate]:
        """Get the default template (refactor)"""
        return self._templates.get("refactor")
    
    def list_templates(self) -> list[str]:
        """List all available template names"""
        return list(self._templates.keys())
    
    def reload(self):
        """Reload all templates"""
        self._templates.clear()
        self._load_templates()


class PromptContext:
    """Builder for prompt context - makes it easy to create context dicts"""
    
    def __init__(self):
        self._data: Dict[str, Any] = {}
    
    def project(self, name: str) -> "PromptContext":
        self._data["project_name"] = name
        return self
    
    def path(self, project_path: str) -> "PromptContext":
        self._data["project_path"] = project_path
        return self
    
    def task(self, description: str) -> "PromptContext":
        self._data["task_description"] = description
        return self
    
    def language(self, lang: str) -> "PromptContext":
        self._data["language"] = lang
        return self
    
    def max_lines(self, lines: int) -> "PromptContext":
        self._data["max_lines"] = lines
        return self
    
    def ignore_dirs(self, dirs: list[str]) -> "PromptContext":
        self._data["ignore_dirs"] = ", ".join(sorted(dirs))
        return self
    
    def ignore_extensions(self, exts: list[str]) -> "PromptContext":
        self._data["ignore_extensions"] = ", ".join(sorted(exts))
        return self
    
    def file_extension(self, ext: str) -> "PromptContext":
        self._data["file_extension"] = ext
        return self
    
    def largest_file(self, name: str, lines: int) -> "PromptContext":
        self._data["largest_file"] = name
        self._data["largest_lines"] = lines
        return self
    
    def resume_context(self, context: str) -> "PromptContext":
        self._data["resume_context"] = context
        return self
    
    def refactor_md_content(self, content: str) -> "PromptContext":
        self._data["refactor_md_content"] = content[:3000]
        return self
    
    def oversized_files(self, files: list[tuple[str, int]]) -> "PromptContext":
        self._data["oversized_files"] = "\n".join(f"  - {p} ({l} lines)" for p, l in files)
        return self
    
    def acceptance_criteria(self, criteria: str) -> "PromptContext":
        self._data["acceptance_criteria"] = criteria
        return self
    
    def dependencies(self, deps: str) -> "PromptContext":
        self._data["dependencies"] = deps
        return self
    
    def error_message(self, error: str) -> "PromptContext":
        self._data["error_message"] = error
        return self
    
    def stack_trace(self, trace: str) -> "PromptContext":
        self._data["stack_trace"] = trace
        return self
    
    def reproduce_steps(self, steps: str) -> "PromptContext":
        self._data["reproduce_steps"] = steps
        return self
    
    def focus_areas(self, areas: str) -> "PromptContext":
        self._data["focus_areas"] = areas
        return self
    
    def build(self) -> Dict[str, Any]:
        return self._data.copy()


# Global instance
_prompt_loader: Optional[PromptLoader] = None


def get_prompt_loader() -> PromptLoader:
    """Get the global prompt loader instance"""
    global _prompt_loader
    if _prompt_loader is None:
        _prompt_loader = PromptLoader()
    return _prompt_loader


def get_template(name: str) -> Optional[PromptTemplate]:
    """Convenience function to get a template"""
    return get_prompt_loader().get(name)


def render_prompt(template_name: str, context: Dict[str, Any]) -> tuple[str, str]:
    """Convenience function to render a prompt"""
    template = get_template(template_name)
    if template is None:
        # Fallback to refactor
        template = get_prompt_loader().get_default()
    if template is None:
        raise ValueError(f"No prompt template found: {template_name}")
    return template.render(context)


if __name__ == "__main__":
    # Test the prompt loader
    loader = PromptLoader()
    print(f"Loaded templates: {loader.list_templates()}")
    
    # Test rendering
    if loader.get("refactor"):
        ctx = PromptContext() \
            .project("test-project") \
            .path("/workspace/test-project") \
            .language("GDScript") \
            .max_lines(5000) \
            .ignore_dirs(["addons", ".git"]) \
            .ignore_extensions([".png", ".jpg"]) \
            .file_extension(".gd") \
            .largest_file("main.gd", 6000) \
            .build()
        
        system, user = loader.get("refactor").render(ctx)
        print("\n=== System Prompt ===")
        print(system[:500])
        print("\n=== User Prompt ===")
        print(user[:500])
