"""
Project Registry Module

Manages project state including:
- Project status (active, paused, locked)
- File-level locking for parallel work
- File scanning and line counts
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import json
import os


_LOCK_OWNER_UNSET = object()


@dataclass
class FileLock:
    """Represents a lock on a specific file"""
    file_path: str
    locked_by: str  # agent_id
    locked_at: str
    task_id: Optional[str] = None


@dataclass
class Project:
    """Represents a project in the registry"""
    name: str
    status: str = "active"  # active, paused, refactoring
    managed: bool = True
    locked: bool = False  # Legacy project-level lock
    locked_at: Optional[str] = None
    unlocked_at: Optional[str] = None
    last_update: Optional[str] = None
    files: Dict[str, int] = field(default_factory=dict)  # file_path -> line_count
    sprint: int = 0
    recent_commits: List[Dict[str, str]] = field(default_factory=list)
    last_commit: Optional[str] = None
    last_commit_msg: Optional[str] = None
    file_locks: Dict[str, FileLock] = field(default_factory=dict)  # file_path -> FileLock
    profile: Optional[str] = None  # profile name to use
    head_task_id: Optional[str] = None  # id of the most recently created/active task
    closure_mode: str = "build"
    closure_status: str = "yellow"
    closure_spec: Dict[str, Any] = field(default_factory=dict)
    last_verification_at: Optional[str] = None
    last_verification_status: Optional[str] = None
    open_regression_count: int = 0
    stall_count: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "managed": self.managed,
            "locked": self.locked,
            "locked_at": self.locked_at,
            "unlocked_at": self.unlocked_at,
            "last_update": self.last_update,
            "files": self.files,
            "sprint": self.sprint,
            "recent_commits": self.recent_commits,
            "last_commit": self.last_commit,
            "last_commit_msg": self.last_commit_msg,
            "file_locks": {
                k: {
                    "file_path": v.file_path,
                    "locked_by": v.locked_by,
                    "locked_at": v.locked_at,
                    "task_id": v.task_id
                }
                for k, v in self.file_locks.items()
            },
            "profile": self.profile,
            "head_task_id": self.head_task_id,
            "closure_mode": self.closure_mode,
            "closure_status": self.closure_status,
            "closure_spec": self.closure_spec,
            "last_verification_at": self.last_verification_at,
            "last_verification_status": self.last_verification_status,
            "open_regression_count": self.open_regression_count,
            "stall_count": self.stall_count,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Project":
        file_locks = {}
        for k, v in data.get("file_locks", {}).items():
            file_locks[k] = FileLock(
                file_path=v["file_path"],
                locked_by=v["locked_by"],
                locked_at=v["locked_at"],
                task_id=v.get("task_id")
            )
        return cls(
            name=data["name"],
            status=data.get("status", "active"),
            managed=data.get("managed", True),
            locked=data.get("locked", False),
            locked_at=data.get("locked_at"),
            unlocked_at=data.get("unlocked_at"),
            last_update=data.get("last_update"),
            files=data.get("files", {}),
            sprint=data.get("sprint", 0),
            recent_commits=data.get("recent_commits", []),
            last_commit=data.get("last_commit"),
            last_commit_msg=data.get("last_commit_msg"),
            file_locks=file_locks,
            profile=data.get("profile"),
            head_task_id=data.get("head_task_id"),
            closure_mode=data.get("closure_mode", "build"),
            closure_status=data.get("closure_status", "yellow"),
            closure_spec=data.get("closure_spec", {}),
            last_verification_at=data.get("last_verification_at"),
            last_verification_status=data.get("last_verification_status"),
            open_regression_count=data.get("open_regression_count", 0),
            stall_count=data.get("stall_count", 0),
        )
    
    def get_largest_file(self) -> tuple[str, int]:
        """Get the largest file (name, line_count)"""
        if not self.files:
            return ("", 0)
        return max(self.files.items(), key=lambda x: x[1])
    
    def has_oversized_files(self, max_lines: int = 5000) -> bool:
        """Check if any file exceeds max_lines"""
        return any(lines > max_lines for lines in self.files.values())
    
    def get_oversized_files(self, max_lines: int = 5000) -> List[tuple[str, int]]:
        """Get list of oversized files"""
        return [(f, l) for f, l in self.files.items() if l > max_lines]


class ProjectRegistry:
    """Manages the project registry"""
    
    def __init__(self, projects_file: Optional[Path] = None, workspace: Optional[Path] = None):
        if workspace is None:
            workspace = Path(os.path.expanduser("~/workspace"))
        self.workspace = workspace
        
        if projects_file is None:
            projects_file = workspace / "swarm-controller" / "data" / "projects.json"
        self.projects_file = Path(projects_file)
        
        self._projects: Dict[str, Project] = {}
        self._load()
    
    def _load(self):
        """Load projects from file"""
        if self.projects_file.exists():
            try:
                data = json.loads(self.projects_file.read_text())
                projects_data = data.get("projects", {})
                self._projects = {k: Project.from_dict(v) for k, v in projects_data.items()}
            except Exception as e:
                print(f"[ProjectRegistry] Error loading: {e}")
                self._projects = {}
        else:
            self._projects = {}
    
    def _save(self):
        """Save projects to file"""
        self.projects_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "projects": {k: v.to_dict() for k, v in self._projects.items()}
        }
        self.projects_file.write_text(json.dumps(data, indent=2))
    
    def get_all(self) -> Dict[str, Project]:
        return self._projects.copy()
    
    def get(self, name: str) -> Optional[Project]:
        return self._projects.get(name)
    
    def add_project(self, name: str, **kwargs) -> Project:
        """Add or update a project"""
        if name in self._projects:
            project = self._projects[name]
            for key, value in kwargs.items():
                if hasattr(project, key):
                    setattr(project, key, value)
        else:
            project = Project(name=name, **kwargs)
            self._projects[name] = project
        self._save()
        return project
    
    def remove_project(self, name: str) -> bool:
        """Remove a project"""
        if name in self._projects:
            del self._projects[name]
            self._save()
            return True
        return False
    
    # Status management
    def set_status(self, name: str, status: str):
        """Set project status"""
        if name in self._projects:
            self._projects[name].status = status
            self._save()

    def set_managed(self, name: str, managed: bool):
        if name in self._projects:
            self._projects[name].managed = bool(managed)
            self._save()

    def set_head_task_id(self, name: str, head_task_id):
        """Set the head task id for a project"""
        if name in self._projects:
            self._projects[name].head_task_id = head_task_id
            self._save()

    def is_paused(self, name: str) -> bool:
        """Check if project is paused"""
        project = self._projects.get(name)
        return project is not None and project.status == "paused"
    
    def is_available(self, name: str) -> bool:
        """Check if project is available for work"""
        project = self._projects.get(name)
        if project is None:
            return True
        return project.managed and project.status == "active" and not project.locked
    
    # Legacy project-level locking
    def lock(self, name: str) -> bool:
        """Lock a project (legacy - use file locks instead)"""
        if name in self._projects:
            self._projects[name].locked = True
            self._projects[name].locked_at = datetime.now().isoformat()
            self._save()
            return True
        return False
    
    def unlock(self, name: str) -> bool:
        """Unlock a project"""
        if name in self._projects:
            self._projects[name].locked = False
            self._projects[name].unlocked_at = datetime.now().isoformat()
            self._save()
            return True
        return False
    
    def is_locked(self, name: str) -> bool:
        """Check if project is locked"""
        project = self._projects.get(name)
        return project is not None and project.locked
    
    # File-level locking (new parallel work support)
    def lock_file(self, project_name: str, file_path: str, agent_id: str, task_id: Optional[str] = None) -> bool:
        """Lock a specific file for an agent"""
        project = self._projects.get(project_name)
        if project is None:
            return False
        
        # Check if already locked
        if file_path in project.file_locks:
            lock = project.file_locks[file_path]
            if lock.locked_by != agent_id:
                return False  # Already locked by someone else
        
        project.file_locks[file_path] = FileLock(
            file_path=file_path,
            locked_by=agent_id,
            locked_at=datetime.now().isoformat(),
            task_id=task_id
        )
        self._save()
        return True

    def replace_file_lock(self, project_name: str, file_path: str, agent_id: str,
                          task_id: Optional[str] = None, *,
                          previous_locked_by: Optional[str] = None,
                          previous_task_id: Optional[str] = None) -> bool:
        """Replace a lock only if it is still held by the expected prior owner."""
        project = self._projects.get(project_name)
        if project is None:
            return False

        lock = project.file_locks.get(file_path)
        if lock:
            if lock.locked_by != previous_locked_by or lock.task_id != previous_task_id:
                return False

        project.file_locks[file_path] = FileLock(
            file_path=file_path,
            locked_by=agent_id,
            locked_at=datetime.now().isoformat(),
            task_id=task_id,
        )
        self._save()
        return True
    
    def unlock_file(self, project_name: str, file_path: str, agent_id: str) -> bool:
        """Unlock a specific file"""
        project = self._projects.get(project_name)
        if project is None:
            return False
        
        lock = project.file_locks.get(file_path)
        if lock and lock.locked_by == agent_id:
            del project.file_locks[file_path]
            self._save()
            return True
        return False

    def remove_file_lock(self, project_name: str, file_path: str, *,
                         previous_locked_by: Any = _LOCK_OWNER_UNSET,
                         previous_task_id: Any = _LOCK_OWNER_UNSET) -> bool:
        """Remove a lock only if it is still held by the expected prior owner."""
        project = self._projects.get(project_name)
        if project is None:
            return False

        lock = project.file_locks.get(file_path)
        if not lock:
            return False
        if previous_locked_by is not _LOCK_OWNER_UNSET and lock.locked_by != previous_locked_by:
            return False
        if previous_task_id is not _LOCK_OWNER_UNSET and lock.task_id != previous_task_id:
            return False

        del project.file_locks[file_path]
        self._save()
        return True
    
    def unlock_all_for_agent(self, project_name: str, agent_id: str) -> List[str]:
        """Unlock all files locked by an agent"""
        project = self._projects.get(project_name)
        if project is None:
            return []
        
        unlocked = []
        for file_path, lock in list(project.file_locks.items()):
            if lock.locked_by == agent_id:
                del project.file_locks[file_path]
                unlocked.append(file_path)
        
        if unlocked:
            self._save()
        return unlocked
    
    def get_locked_files(self, project_name: str) -> Dict[str, FileLock]:
        """Get all locked files for a project"""
        project = self._projects.get(project_name)
        if project is None:
            return {}
        return project.file_locks.copy()
    
    def is_file_locked(self, project_name: str, file_path: str) -> bool:
        """Check if a specific file is locked"""
        project = self._projects.get(project_name)
        if project is None:
            return False
        return file_path in project.file_locks
    
    def get_file_lock(self, project_name: str, file_path: str) -> Optional[FileLock]:
        """Get the lock for a specific file"""
        project = self._projects.get(project_name)
        if project is None:
            return None
        return project.file_locks.get(file_path)
    
    # File scanning
    def scan_project_files(self, project_name: str, extensions: List[str], ignore_dirs: Set[str]) -> Dict[str, int]:
        """Scan a project for files with given extensions"""
        project_path = self.workspace / project_name
        if not project_path.exists():
            return {}
        
        files = {}
        for ext in extensions:
            for file_path in project_path.rglob(f"*{ext}"):
                # Skip ignored directories
                if any(ignored in file_path.parts for ignored in ignore_dirs):
                    continue
                try:
                    lines = len(file_path.read_text().splitlines())
                    rel_path = str(file_path.relative_to(project_path))
                    files[rel_path] = lines
                except Exception:
                    pass
        return files
    
    def update_file_counts(self, project_name: str, files: Dict[str, int]):
        """Update file counts for a project"""
        if project_name not in self._projects:
            self._projects[project_name] = Project(name=project_name)
        
        project = self._projects[project_name]
        project.files = files
        project.last_update = datetime.now().isoformat()
        self._save()
    
    # Git integration
    def get_commits(self, project_name: str, count: int = 5) -> List[Dict[str, str]]:
        """Get recent git commits for a project"""
        project_path = self.workspace / project_name
        if not project_path.exists():
            return []
        
        try:
            import subprocess as sp
            result = sp.run(
                ["git", "log", f"--pretty=format:%h|%s|%ar|%ct", f"-{count}"],
                cwd=str(project_path), capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return []

            commits = []
            for line in result.stdout.strip().splitlines():
                parts = line.split("|", 3)
                if len(parts) >= 3:
                    commit = {"hash": parts[0], "message": parts[1], "age": parts[2]}
                    if len(parts) == 4:
                        try:
                            commit["timestamp"] = int(parts[3]) * 1000  # ms for JS
                        except ValueError:
                            pass
                    commits.append(commit)
            return commits
        except Exception:
            return []

    def update_commits(self, project_name: str):
        """Update recent commits for a project"""
        commits = self.get_commits(project_name)
        if project_name in self._projects:
            project = self._projects[project_name]
            project.recent_commits = commits
            if commits:
                project.last_commit = commits[0]["hash"]
                project.last_commit_msg = commits[0]["message"]
            self._save()
    
    # Bulk operations
    def get_active_projects(self) -> List[str]:
        """Get list of active (non-paused) project names"""
        return [name for name, p in self._projects.items() if p.status == "active"]
    
    def get_paused_projects(self) -> List[str]:
        """Get list of paused project names"""
        return [name for name, p in self._projects.items() if p.status == "paused"]
    
    def get_oversized_projects(self, max_lines: int = 5000) -> List[tuple[str, List[tuple[str, int]]]]:
        """Get projects with oversized files"""
        result = []
        for name, project in self._projects.items():
            oversized = project.get_oversized_files(max_lines)
            if oversized:
                result.append((name, oversized))
        return result


class SQLiteProjectRegistry:
    """
    Project registry backed by SQLite via swarm.db.
    Drop-in replacement for ProjectRegistry with the same public interface.
    """

    def __init__(self, workspace: Optional[Path] = None):
        from swarm import db as _db
        self._db = _db
        if workspace is None:
            workspace = Path(os.path.expanduser("~/workspace"))
        self.workspace = workspace

    # ---- read ----

    def get_all(self) -> Dict[str, Project]:
        rows = self._db.project_get_all()
        return {name: Project.from_dict({**d, "name": name}) for name, d in rows.items()}

    def get(self, name: str) -> Optional[Project]:
        d = self._db.project_get(name)
        return Project.from_dict(d) if d else None

    # ---- write ----

    def add_project(self, name: str, **kwargs) -> Project:
        existing = self._db.project_get(name)
        if existing:
            proj = Project.from_dict(existing)
            for k, v in kwargs.items():
                if hasattr(proj, k):
                    setattr(proj, k, v)
        else:
            proj = Project(name=name, **kwargs)
        self._db.project_upsert(proj.to_dict())
        return proj

    def remove_project(self, name: str) -> bool:
        conn = self._db._connect()
        cur = conn.execute("DELETE FROM projects WHERE name=?", (name,))
        conn.commit()
        return cur.rowcount > 0

    def set_status(self, name: str, status: str):
        conn = self._db._connect()
        conn.execute("UPDATE projects SET status=? WHERE name=?", (status, name))
        conn.commit()

    def set_managed(self, name: str, managed: bool):
        conn = self._db._connect()
        conn.execute("UPDATE projects SET managed=? WHERE name=?", (1 if managed else 0, name))
        conn.commit()

    def set_head_task_id(self, name: str, head_task_id: Optional[str]):
        from swarm.task_chains import set_project_head
        set_project_head(self._db, name, head_task_id, repair_if_missing=(head_task_id is None))

    def is_paused(self, name: str) -> bool:
        d = self._db.project_get(name)
        return d is not None and d.get("status") == "paused"

    def is_available(self, name: str) -> bool:
        d = self._db.project_get(name)
        if d is None:
            return True
        return d.get("managed", True) and d.get("status") == "active" and not d.get("locked", False)

    def lock(self, name: str) -> bool:
        self._db.project_set_locked(name, True)
        return True

    def unlock(self, name: str) -> bool:
        self._db.project_set_locked(name, False)
        return True

    def is_locked(self, name: str) -> bool:
        d = self._db.project_get(name)
        return d is not None and bool(d.get("locked", False))

    # ---- file locks ----

    def _get_file_locks(self, project_name: str) -> Dict:
        d = self._db.project_get(project_name)
        return d.get("file_locks", {}) if d else {}

    def _normalize_file_path(self, project_name: str, file_path: str) -> str:
        raw = (file_path or "").strip().replace("\\", "/")
        if not raw:
            return ""

        project_root = (self.workspace / project_name).resolve()
        project_root_str = project_root.as_posix()
        prefixed_root = project_root_str.lstrip("/")

        if raw.startswith(prefixed_root + "/"):
            return raw[len(prefixed_root) + 1 :].lstrip("/")
        if raw.startswith(project_root_str + "/"):
            return raw[len(project_root_str) + 1 :].lstrip("/")

        raw_path = Path(raw)
        if raw_path.is_absolute():
            candidate = raw_path.resolve()
        else:
            candidate = (project_root / raw_path).resolve()

        try:
            if os.path.commonpath([project_root_str, candidate.as_posix()]) == project_root_str:
                return candidate.relative_to(project_root).as_posix()
        except Exception:
            pass
        return raw.lstrip("./").lstrip("/")

    def _canonicalize_file_locks(self, project_name: str, locks: Dict) -> Dict:
        canonical: Dict[str, Dict] = {}
        for raw_key, lock in (locks or {}).items():
            source_path = lock.get("file_path") or raw_key
            normalized = self._normalize_file_path(project_name, source_path)
            if not normalized:
                continue
            existing = canonical.get(normalized)
            normalized_lock = dict(lock)
            normalized_lock["file_path"] = normalized
            if existing is None:
                canonical[normalized] = normalized_lock
                continue
            existing_at = existing.get("locked_at") or ""
            incoming_at = normalized_lock.get("locked_at") or ""
            if incoming_at >= existing_at:
                canonical[normalized] = normalized_lock
        return canonical

    def _set_file_locks(self, project_name: str, file_locks: Dict):
        conn = self._db._connect()
        import json as _json
        conn.execute(
            "UPDATE projects SET file_locks=? WHERE name=?",
            (_json.dumps(file_locks), project_name),
        )
        conn.commit()

    def lock_file(self, project_name: str, file_path: str, agent_id: str,
                  task_id: Optional[str] = None) -> bool:
        normalized_path = self._normalize_file_path(project_name, file_path)
        if not normalized_path:
            return False
        locks = self._canonicalize_file_locks(project_name, self._get_file_locks(project_name))
        if normalized_path in locks and locks[normalized_path]["locked_by"] != agent_id:
            self._set_file_locks(project_name, locks)
            return False
        locks[normalized_path] = {
            "file_path": normalized_path,
            "locked_by": agent_id,
            "locked_at": datetime.now().isoformat(),
            "task_id": task_id,
        }
        self._set_file_locks(project_name, locks)
        return True

    def replace_file_lock(self, project_name: str, file_path: str, agent_id: str,
                          task_id: Optional[str] = None, *,
                          previous_locked_by: Optional[str] = None,
                          previous_task_id: Optional[str] = None) -> bool:
        """Replace a lock only if it is still held by the expected prior owner."""
        normalized_path = self._normalize_file_path(project_name, file_path)
        if not normalized_path:
            return False
        locks = self._canonicalize_file_locks(project_name, self._get_file_locks(project_name))
        lock = locks.get(normalized_path)
        if lock:
            if lock.get("locked_by") != previous_locked_by or lock.get("task_id") != previous_task_id:
                self._set_file_locks(project_name, locks)
                return False
        locks[normalized_path] = {
            "file_path": normalized_path,
            "locked_by": agent_id,
            "locked_at": datetime.now().isoformat(),
            "task_id": task_id,
        }
        self._set_file_locks(project_name, locks)
        return True

    def unlock_file(self, project_name: str, file_path: str, agent_id: str) -> bool:
        normalized_path = self._normalize_file_path(project_name, file_path)
        if not normalized_path:
            return False
        locks = self._canonicalize_file_locks(project_name, self._get_file_locks(project_name))
        lock = locks.get(normalized_path)
        if lock and lock["locked_by"] == agent_id:
            del locks[normalized_path]
            self._set_file_locks(project_name, locks)
            return True
        return False

    def remove_file_lock(self, project_name: str, file_path: str, *,
                         previous_locked_by: Any = _LOCK_OWNER_UNSET,
                         previous_task_id: Any = _LOCK_OWNER_UNSET) -> bool:
        """Remove a lock only if it is still held by the expected prior owner."""
        normalized_path = self._normalize_file_path(project_name, file_path)
        if not normalized_path:
            return False
        locks = self._canonicalize_file_locks(project_name, self._get_file_locks(project_name))
        lock = locks.get(normalized_path)
        if not lock:
            self._set_file_locks(project_name, locks)
            return False
        if previous_locked_by is not _LOCK_OWNER_UNSET and lock.get("locked_by") != previous_locked_by:
            self._set_file_locks(project_name, locks)
            return False
        if previous_task_id is not _LOCK_OWNER_UNSET and lock.get("task_id") != previous_task_id:
            self._set_file_locks(project_name, locks)
            return False

        del locks[normalized_path]
        self._set_file_locks(project_name, locks)
        return True

    def unlock_all_for_agent(self, project_name: str, agent_id: str) -> List[str]:
        locks = self._canonicalize_file_locks(project_name, self._get_file_locks(project_name))
        removed = [fp for fp, l in locks.items() if l["locked_by"] == agent_id]
        for fp in removed:
            del locks[fp]
        if removed:
            self._set_file_locks(project_name, locks)
        return removed

    def get_locked_files(self, project_name: str) -> Dict[str, FileLock]:
        locks = self._canonicalize_file_locks(project_name, self._get_file_locks(project_name))
        self._set_file_locks(project_name, locks)
        return {
            fp: FileLock(
                file_path=l["file_path"],
                locked_by=l["locked_by"],
                locked_at=l["locked_at"],
                task_id=l.get("task_id"),
            )
            for fp, l in locks.items()
        }

    def is_file_locked(self, project_name: str, file_path: str) -> bool:
        normalized_path = self._normalize_file_path(project_name, file_path)
        return normalized_path in self._canonicalize_file_locks(project_name, self._get_file_locks(project_name))

    def get_file_lock(self, project_name: str, file_path: str) -> Optional[FileLock]:
        normalized_path = self._normalize_file_path(project_name, file_path)
        locks = self._canonicalize_file_locks(project_name, self._get_file_locks(project_name))
        l = locks.get(normalized_path)
        if l is None:
            return None
        return FileLock(
            file_path=l["file_path"],
            locked_by=l["locked_by"],
            locked_at=l["locked_at"],
            task_id=l.get("task_id"),
        )

    # ---- file scanning ----

    def scan_project_files(self, project_name: str, extensions: List[str],
                           ignore_dirs: Set[str]) -> Dict[str, int]:
        project_path = self.workspace / project_name
        if not project_path.exists():
            return {}
        files = {}
        for ext in extensions:
            for fp in project_path.rglob(f"*{ext}"):
                if any(ig in fp.parts for ig in ignore_dirs):
                    continue
                try:
                    lines = len(fp.read_text().splitlines())
                    files[str(fp.relative_to(project_path))] = lines
                except Exception:
                    pass
        return files

    def update_file_counts(self, project_name: str, files: Dict[str, int]):
        import json as _json
        conn = self._db._connect()
        conn.execute(
            "UPDATE projects SET files=?, last_update=? WHERE name=?",
            (_json.dumps(files), datetime.now().isoformat(), project_name),
        )
        conn.commit()

    def get_commits(self, project_name: str, count: int = 5) -> List[Dict[str, str]]:
        project_path = self.workspace / project_name
        if not project_path.exists():
            return []
        try:
            import subprocess as sp
            result = sp.run(
                ["git", "log", f"--pretty=format:%h|%s|%ar|%ct", f"-{count}"],
                cwd=str(project_path), capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return []
            commits = []
            for line in result.stdout.strip().splitlines():
                parts = line.split("|", 3)
                if len(parts) >= 3:
                    commit = {"hash": parts[0], "message": parts[1], "age": parts[2]}
                    if len(parts) == 4:
                        try:
                            commit["timestamp"] = int(parts[3]) * 1000
                        except ValueError:
                            pass
                    commits.append(commit)
            return commits
        except Exception:
            return []

    def update_commits(self, project_name: str):
        import json as _json
        commits = self.get_commits(project_name)
        conn = self._db._connect()
        conn.execute(
            """UPDATE projects SET recent_commits=?, last_commit=?, last_commit_msg=?
               WHERE name=?""",
            (
                _json.dumps(commits),
                commits[0]["hash"] if commits else None,
                commits[0]["message"] if commits else None,
                project_name,
            ),
        )
        conn.commit()

    def get_active_projects(self) -> List[str]:
        rows = self._db._connect().execute(
            "SELECT name FROM projects WHERE status='active'"
        ).fetchall()
        return [r["name"] for r in rows]

    def get_paused_projects(self) -> List[str]:
        rows = self._db._connect().execute(
            "SELECT name FROM projects WHERE status='paused'"
        ).fetchall()
        return [r["name"] for r in rows]

    def get_oversized_projects(self, max_lines: int = 5000) -> List[tuple]:
        result = []
        for name, proj in self.get_all().items():
            oversized = proj.get_oversized_files(max_lines)
            if oversized:
                result.append((name, oversized))
        return result


# Global instance
_registry: Optional[ProjectRegistry] = None


def get_project_registry(projects_file: Optional[Path] = None, workspace: Optional[Path] = None) -> ProjectRegistry:
    """Get the global project registry instance"""
    global _registry
    if _registry is None:
        _registry = ProjectRegistry(projects_file, workspace)
    return _registry


def set_project_registry(registry: ProjectRegistry):
    """Set a custom project registry (useful for testing)"""
    global _registry
    _registry = registry
