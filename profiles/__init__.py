"""
Project Profile Loader

Loads YAML configuration profiles for different project types.
Provides project-type detection and profile management.
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional, List
import yaml


class ProjectProfile:
    """Represents a loaded project profile"""
    
    def __init__(self, name: str, data: Dict[str, Any]):
        self.name = name
        self.description = data.get("description", "")
        self.language = data.get("language", "unknown")
        self.framework = data.get("framework", "")
        
        # File scanning
        self.file_extensions = data.get("file_extensions", [])
        self.max_lines = data.get("max_lines", 5000)
        self.ignore_dirs = set(data.get("ignore_dirs", []))
        self.ignore_extensions = set(data.get("ignore_extensions", []))
        
        # Commands
        self.test_command = data.get("test_command", "")
        self.build_command = data.get("build_command", "")
        self.lint_command = data.get("lint_command", "")
        
        # Prompt template
        self.prompt_template = data.get("prompt_template", "refactor")
        
        # Task types
        self.task_types = data.get("task_types", ["refactor", "feature", "bug", "polish"])
        self.priority_order = data.get("priority_order", ["refactor", "bug", "feature", "polish"])
    
    def should_ignore_dir(self, dirname: str) -> bool:
        """Check if a directory should be ignored"""
        return dirname in self.ignore_dirs
    
    def should_ignore_file(self, filename: str) -> bool:
        """Check if a file should be ignored based on extension"""
        return Path(filename).suffix in self.ignore_extensions
    
    def is_scannable_file(self, filepath: str) -> bool:
        """Check if a file should be scanned for line counts"""
        return Path(filepath).suffix in self.file_extensions


class ProfileLoader:
    """Loads and manages project profiles"""
    
    def __init__(self, profiles_dir: Optional[Path] = None):
        if profiles_dir is None:
            profiles_dir = Path(__file__).parent.parent / "profiles"
        self.profiles_dir = Path(profiles_dir)
        self._profiles: Dict[str, ProjectProfile] = {}
        self._load_profiles()
    
    def _load_profiles(self):
        """Load all YAML profiles from the profiles directory"""
        if not self.profiles_dir.exists():
            return
        
        for yaml_file in self.profiles_dir.glob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_file.read_text())
                if data and "name" in data:
                    profile = ProjectProfile(data["name"], data)
                    self._profiles[profile.name] = profile
                    print(f"[Profiles] Loaded profile: {profile.name}")
            except Exception as e:
                print(f"[Profiles] Error loading {yaml_file.name}: {e}")
    
    def get(self, name: str) -> Optional[ProjectProfile]:
        """Get a profile by name"""
        return self._profiles.get(name)
    
    def detect_profile(self, project_path: Path) -> Optional[ProjectProfile]:
        """
        Auto-detect the appropriate profile based on project files.
        Returns the best matching profile or None.
        """
        if not project_path.exists():
            return None
        
        # Scoring system
        scores: Dict[str, int] = {}
        
        for profile in self._profiles.values():
            score = 0
            
            # Check for indicator files
            if profile.name == "godot":
                if (project_path / "project.godot").exists():
                    score += 100
                if (project_path / "project.godot").exists():
                    score += 50
            elif profile.name == "python":
                if (project_path / "requirements.txt").exists():
                    score += 50
                if (project_path / "setup.py").exists():
                    score += 50
                if (project_path / "pyproject.toml").exists():
                    score += 50
                if (project_path / "poetry.lock").exists():
                    score += 50
            elif profile.name == "typescript":
                if (project_path / "package.json").exists():
                    score += 50
                if (project_path / "tsconfig.json").exists():
                    score += 100
            
            # Check for source files
            for ext in profile.file_extensions:
                matching_files = list(project_path.rglob(f"*{ext}"))
                score += len(matching_files)
            
            if score > 0:
                scores[profile.name] = score
        
        if not scores:
            return None
        
        # Return highest scoring profile
        best = max(scores.items(), key=lambda x: x[1])
        return self._profiles.get(best[0])
    
    def list_profiles(self) -> List[str]:
        """List all available profile names"""
        return list(self._profiles.keys())
    
    def reload(self):
        """Reload all profiles"""
        self._profiles.clear()
        self._load_profiles()


# Project profile context - for per-project overrides
class ProjectProfileContext:
    """Manages profile assignments for projects"""
    
    def __init__(self, config_file: Optional[Path] = None):
        if config_file is None:
            config_file = Path(__file__).parent.parent / "data" / "project-profiles.json"
        self.config_file = Path(config_file)
        self._assignments: Dict[str, str] = {}  # project_name -> profile_name
        self._load()
    
    def _load(self):
        """Load assignments from config file"""
        if self.config_file.exists():
            try:
                import json
                data = json.loads(self.config_file.read_text())
                self._assignments = data.get("profiles", {})
            except Exception as e:
                print(f"[Profiles] Error loading assignments: {e}")
    
    def _save(self):
        """Save assignments to config file"""
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        import json
        self.config_file.write_text(json.dumps({"profiles": self._assignments}, indent=2))
    
    def get_profile(self, project_name: str) -> Optional[str]:
        """Get assigned profile for a project"""
        return self._assignments.get(project_name)
    
    def set_profile(self, project_name: str, profile_name: str):
        """Set profile for a project"""
        self._assignments[project_name] = profile_name
        self._save()
    
    def remove_profile(self, project_name: str):
        """Remove profile assignment (will auto-detect)"""
        if project_name in self._assignments:
            del self._assignments[project_name]
            self._save()


# Global instances
_profile_loader: Optional[ProfileLoader] = None
_profile_context: Optional[ProjectProfileContext] = None


def get_profile_loader() -> ProfileLoader:
    """Get the global profile loader instance"""
    global _profile_loader
    if _profile_loader is None:
        _profile_loader = ProfileLoader()
    return _profile_loader


def get_profile_context() -> ProjectProfileContext:
    """Get the global profile context instance"""
    global _profile_context
    if _profile_context is None:
        _profile_context = ProjectProfileContext()
    return _profile_context


def get_project_profile(project_name: str, project_path: Optional[Path] = None) -> Optional[ProjectProfile]:
    """
    Get the profile for a project.
    First checks explicit assignment, then auto-detects.
    """
    loader = get_profile_loader()
    context = get_profile_context()
    
    # Check for explicit assignment
    profile_name = context.get_profile(project_name)
    if profile_name:
        return loader.get(profile_name)
    
    # Auto-detect
    if project_path:
        return loader.detect_profile(project_path)
    
    return None


if __name__ == "__main__":
    # Test the profile loader
    loader = ProfileLoader()
    print(f"Loaded profiles: {loader.list_profiles()}")
    
    # Test detection (would need an actual project)
    # profile = loader.detect_profile(Path("/some/project"))
    # print(f"Detected: {profile.name if profile else 'None'}")
