"""
Agent Module

Handles agent spawning, tracking, and the modular agent factory.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid

from swarm.platform import popen_session_kwargs


@dataclass
class Agent:
    """Represents an agent process"""
    id: str
    project: str
    task_type: str
    status: str = "spawning"  # spawning, active, completed, failed
    spawned_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    task_id: Optional[str] = None
    log_path: Optional[str] = None
    script_path: Optional[str] = None
    output: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "task_type": self.task_type,
            "status": self.status,
            "spawned_at": self.spawned_at,
            "completed_at": self.completed_at,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "task_id": self.task_id,
            "log_path": self.log_path,
            "script_path": self.script_path,
            "output": self.output[-1000:] if self.output else "",
            "metadata": self.metadata,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Agent":
        return cls(
            id=data["id"],
            project=data["project"],
            task_type=data.get("task_type", "refactor"),
            status=data.get("status", "active"),
            spawned_at=data.get("spawned_at", datetime.now().isoformat()),
            completed_at=data.get("completed_at"),
            pid=data.get("pid"),
            exit_code=data.get("exit_code"),
            task_id=data.get("task_id"),
            log_path=data.get("log_path"),
            script_path=data.get("script_path"),
            output=data.get("output", ""),
            metadata=data.get("metadata", {}),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
        )


class AgentTracker:
    """Tracks active agents"""
    
    def __init__(self, agents_file: Optional[Path] = None):
        if agents_file is None:
            workspace = Path(os.path.expanduser("~/workspace"))
            agents_file = workspace / "swarm-controller" / "data" / "agents.json"
        self.agents_file = Path(agents_file)
        self._agents: Dict[str, Agent] = {}
        self._active_handles: Dict[str, subprocess.Popen] = {}  # agent_id -> process handle
        self._lock = threading.Lock()
        self._load()
    
    def _load(self):
        """Load agents from file"""
        if self.agents_file.exists():
            try:
                data = json.loads(self.agents_file.read_text())
                agents_list = data.get("agents", [])
                self._agents = {a["id"]: Agent.from_dict(a) for a in agents_list}
            except Exception as e:
                print(f"[AgentTracker] Error loading: {e}")
                self._agents = {}
    
    def _save(self):
        """Save agents to file"""
        self.agents_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "agents": [a.to_dict() for a in self._agents.values()]
        }
        self.agents_file.write_text(json.dumps(data, indent=2))
    
    def add(self, agent: Agent):
        """Add an agent"""
        self._agents[agent.id] = agent
        self._save()
    
    def get(self, agent_id: str) -> Optional[Agent]:
        """Get an agent by ID"""
        return self._agents.get(agent_id)
    
    def get_all(self) -> List[Agent]:
        """Get all agents"""
        return list(self._agents.values())
    
    def get_active(self) -> List[Agent]:
        """Get all active agents"""
        return [a for a in self._agents.values() if a.status == "active"]
    
    def get_active_ids(self) -> set:
        """Get IDs of active agents"""
        return {a.id for a in self._agents.values() if a.status == "active"}
    
    def update(self, agent: Agent):
        """Update an agent"""
        self._agents[agent.id] = agent
        self._save()
    
    def register_handle(self, agent_id: str, handle: subprocess.Popen):
        """Register a process handle for an agent"""
        with self._lock:
            self._active_handles[agent_id] = handle
    
    def unregister_handle(self, agent_id: str) -> Optional[subprocess.Popen]:
        """Unregister a process handle"""
        with self._lock:
            return self._active_handles.pop(agent_id, None)
    
    def check_active_handles(self) -> List[str]:
        """Check which agent processes have finished, update their status"""
        finished = []
        with self._lock:
            for agent_id, handle in list(self._active_handles.items()):
                exit_code = handle.poll()
                if exit_code is not None:
                    finished.append(agent_id)
                    agent = self._agents.get(agent_id)
                    if agent:
                        agent.status = "completed" if exit_code == 0 else "failed"
                        agent.exit_code = exit_code
                        agent.completed_at = datetime.now().isoformat()
        if finished:
            self._save()
        return finished
    
    def prune_completed(self, keep_count: int = 50):
        """Remove old completed agents, keeping only the most recent"""
        completed = sorted(
            [a for a in self._agents.values() if a.status != "active"],
            key=lambda a: a.spawned_at,
            reverse=True
        )
        for agent in completed[keep_count:]:
            del self._agents[agent.id]
        self._save()


class SQLiteAgentTracker:
    """
    Agent tracker backed by SQLite via swarm.db.
    Drop-in replacement for AgentTracker with the same public interface.
    """

    def __init__(self):
        from swarm import db as _db
        self._db = _db
        self._active_handles: Dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def add(self, agent: Agent):
        self._db.agent_upsert(agent.to_dict())

    def get(self, agent_id: str) -> Optional[Agent]:
        d = self._db.agent_get(agent_id)
        return Agent.from_dict(d) if d else None

    def get_all(self) -> List[Agent]:
        return [Agent.from_dict(d) for d in self._db.agent_get_all()]

    def get_active(self) -> List[Agent]:
        return [Agent.from_dict(d) for d in self._db.agent_get_active()]

    def get_active_ids(self) -> set:
        return {a.id for a in self.get_active()}

    def update(self, agent: Agent):
        self._db.agent_upsert(agent.to_dict())

    def register_handle(self, agent_id: str, handle: subprocess.Popen):
        with self._lock:
            self._active_handles[agent_id] = handle

    def unregister_handle(self, agent_id: str) -> Optional[subprocess.Popen]:
        with self._lock:
            return self._active_handles.pop(agent_id, None)

    def check_active_handles(self) -> List[str]:
        """Check which processes have finished and update their status in SQLite."""
        finished = []
        with self._lock:
            for agent_id, handle in list(self._active_handles.items()):
                exit_code = handle.poll()
                if exit_code is not None:
                    finished.append(agent_id)
                    self._db.agent_update_status(
                        agent_id,
                        "completed" if exit_code == 0 else "failed",
                        exit_code=exit_code,
                        completed_at=datetime.now().isoformat(),
                    )
        for agent_id in finished:
            with self._lock:
                self._active_handles.pop(agent_id, None)
        return finished

    def prune_completed(self, keep_count: int = 50):
        conn = self._db._connect()
        conn.execute(
            """DELETE FROM agents WHERE status != 'active'
               AND id NOT IN (
                   SELECT id FROM agents WHERE status != 'active'
                   ORDER BY spawned_at DESC LIMIT ?
               )""",
            (keep_count,),
        )
        conn.commit()


class AgentFactory:
    """
    Modular agent script factory.
    Generates agent scripts from templates.
    """
    
    def __init__(self, prompts_loader=None, profile_loader=None):
        # Lazy imports to avoid circular dependencies
        self._prompts_loader = prompts_loader
        self._profile_loader = profile_loader
    
    @property
    def prompts_loader(self):
        if self._prompts_loader is None:
            from prompts import get_prompt_loader
            self._prompts_loader = get_prompt_loader()
        return self._prompts_loader
    
    @property
    def profile_loader(self):
        if self._profile_loader is None:
            from profiles import get_profile_loader
            self._profile_loader = get_profile_loader()
        return self._profile_loader
    
    def generate_script(
        self,
        task,
        workspace: Path,
        project_name: str,
        config: Dict[str, Any]
    ) -> str:
        """
        Generate an agent script for a task.
        
        Args:
            task: Task object or dict with task details
            workspace: Workspace Path
            project_name: Name of the project
            config: Configuration dict with max_lines, ignore_dirs, etc.
        
        Returns:
            Complete Python script as string
        """
        project_path = workspace / project_name
        task_type = task.type if hasattr(task, 'type') else task.get('type', 'refactor')
        description = task.description if hasattr(task, 'description') else task.get('description', '')
        task_id = task.id if hasattr(task, 'id') else task.get('id', 'unknown')
        
        # Get prompt template
        template = self.prompts_loader.get(task_type)
        if template is None:
            template = self.prompts_loader.get_default()
        if template is None:
            raise ValueError(f"No prompt template found for task type: {task_type}")
        
        # Build context for prompt rendering
        from prompts import PromptContext
        ctx = PromptContext() \
            .project(project_name) \
            .path(str(project_path)) \
            .language(config.get("language", "GDScript")) \
            .max_lines(config.get("max_lines", 5000)) \
            .ignore_dirs(config.get("ignore_dirs", [])) \
            .ignore_extensions(config.get("ignore_extensions", [])) \
            .task(description)
        
        # Add largest file info
        if hasattr(task, 'metadata') and task.metadata:
            largest = task.metadata.get('largest_file')
            if largest:
                ctx = ctx.largest_file(largest[0], largest[1])
        
        # Check for existing refactor.md
        refactor_md = project_path / "refactor.md"
        if refactor_md.exists():
            content = refactor_md.read_text()
            has_pending = "- [ ]" in content
            if has_pending:
                ctx = ctx.resume_context("exists").refactor_md_content(content)
            else:
                # Check if still oversized
                oversized = []
                for root, dirs, files in os.walk(project_path):
                    dirs[:] = [d for d in dirs if d not in config.get("ignore_dirs", [])]
                    for f in files:
                        if f.endswith(config.get("file_extension", ".gd")):
                            try:
                                lc = len(open(os.path.join(root, f)).readlines())
                                if lc > config.get("max_lines", 5000):
                                    oversized.append((os.path.relpath(os.path.join(root, f), project_path), lc))
                            except:
                                pass
                if oversized:
                    ctx = ctx.resume_context("oversized").refactor_md_content(content).oversized_files(oversized)
                else:
                    ctx = ctx.resume_context("all_done")
        else:
            ctx = ctx.resume_context("none")
        
        context = ctx.build()
        system_prompt, user_prompt = template.render(context)
        
        # Generate the script
        return self._build_script(
            task_id=task_id,
            project_name=project_name,
            task_type=task_type,
            description=description,
            workspace=workspace,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            config=config,
            tool_definitions=template.tools,
            max_tool_loops=config.get("max_tool_loops", 45)
        )
    
    def _build_script(
        self,
        task_id: str,
        project_name: str,
        task_type: str,
        description: str,
        workspace: Path,
        system_prompt: str,
        user_prompt: str,
        config: Dict[str, Any],
        tool_definitions: List[Dict],
        max_tool_loops: int = 45
    ) -> str:
        """Build the actual Python script content"""
        
        ignore_dirs_repr = repr(set(config.get("ignore_dirs", [])))
        ignore_exts_repr = repr(set(config.get("ignore_extensions", [])))
        system_prompt_json = json.dumps(system_prompt)
        user_prompt_json = json.dumps(user_prompt)
        
        script = f'''#!/usr/bin/env python3
"""
Agent Task - {project_name} ({task_type})
Task ID: {task_id}
Generated by AgentFactory
"""
import subprocess
import json
import sys
import os
import re
import requests
import time
from pathlib import Path
from datetime import datetime

WORKSPACE = Path("{workspace}")
PROJECT = "{project_name}"
TASK_TYPE = "{task_type}"
TASK_DESC = """{description}"""
TASK_ID = "{task_id}"
MAX_TOOL_LOOPS = {max_tool_loops}

# LLM Config
ENV_FILE = WORKSPACE / "swarm-controller" / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().strip().split(chr(10)):
        if "=" in line:
            key, val = line.split("=", 1)
            os.environ[key] = val

MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", os.environ.get("MINIMAX-API", ""))
MINIMAX_GROUP_ID = os.environ.get("MINIMAX_GROUP_ID", "")
MINIMAX_MODEL = "MiniMax-M2.7"

IGNORE_DIRS = {ignore_dirs_repr}
IGNORE_EXTENSIONS = {ignore_exts_repr}
MAX_LINES = {config.get("max_lines", 5000)}


def run(cmd, cwd=None, timeout=60):
    """Run shell command"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                            cwd=cwd or os.path.join(WORKSPACE, PROJECT), timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"


def log(msg):
    """Log to stdout"""
    print(f"[Agent] {{msg}}", flush=True)


def read_file(relative_path):
    """Read a file from the project"""
    path = os.path.join(WORKSPACE, PROJECT, relative_path)
    try:
        with open(path, 'r') as f:
            content = f.read()
        return {{"ok": True, "content": content[:5000]}}
    except Exception as e:
        return {{"ok": False, "error": str(e)}}


def list_files(relative_path):
    """List files in a directory"""
    path = os.path.join(WORKSPACE, PROJECT, relative_path)
    try:
        if os.path.isfile(path):
            return {{"ok": True, "files": [os.path.basename(path)], "type": "file"}}
        files = []
        for item in os.listdir(path):
            full = os.path.join(path, item)
            if os.path.isdir(full):
                files.append(item + "/")
            else:
                files.append(item)
        return {{"ok": True, "files": files}}
    except Exception as e:
        return {{"ok": False, "error": str(e)}}


def search_code(query):
    """Search for a pattern in all code files"""
    path = os.path.join(WORKSPACE, PROJECT)
    exts = {repr(config.get("file_extensions", [".gd"]))}
    try:
        result = subprocess.run(
            ["grep", "-r", "--include=*"] + exts[0].replace("'", "").split(",") + ["-l", query, path],
            capture_output=True, text=True, timeout=30
        )
        return {{"ok": True, "files": result.stdout.strip().split(chr(10)) if result.stdout.strip() else []}}
    except Exception as e:
        return {{"ok": False, "error": str(e)}}


def get_file_stats(relative_path):
    """Get file metadata"""
    path = os.path.join(WORKSPACE, PROJECT, relative_path)
    try:
        result = subprocess.run(["wc", "-l", path], capture_output=True, text=True)
        lines = int(result.stdout.strip().split()[0])
        size = os.path.getsize(path)
        return {{"ok": True, "lines": lines, "size": size}}
    except Exception as e:
        return {{"ok": False, "error": str(e)}}


def write_file(path_arg, content):
    """Write content to a file in the project"""
    p = Path(path_arg)
    project_root = Path(WORKSPACE) / PROJECT
    try:
        rel = p.relative_to(project_root)
        path = project_root / rel
    except ValueError:
        path = project_root / path_arg
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(content)
        return {{"ok": True, "path": str(path)}}
    except Exception as e:
        return {{"ok": False, "error": str(e)}}


def run_command(cmd, timeout=60):
    """Run a shell command"""
    result = run(cmd, timeout=timeout)
    return {{"ok": result[0] == 0, "stdout": result[1], "stderr": result[2]}}


def git_commit(message):
    """Commit all changes"""
    code, out, err = run("git add -A")
    if code != 0:
        return {{"ok": False, "error": "git add failed: " + err}}
    code, out, err = run("git commit -m \\"" + message + "\\"")
    if code != 0:
        return {{"ok": False, "error": "git commit failed: " + err}}
    return {{"ok": True, "message": "Committed", "output": out}}


def git_push():
    """Push commits to remote"""
    code, out, err = run("git push 2>&1")
    if code != 0:
        return {{"ok": False, "error": "git push failed: " + err}}
    return {{"ok": True, "message": "Pushed", "output": out}}


def parse_tool_calls(text):
    """Parse tool calls from LLM response"""
    tool_calls = []
    pattern = r'\\[TOOL_CALL\\]\\s*(\\{{.*?\\}})\\s*\\[/TOOL_CALL\\]'
    matches = re.findall(pattern, text, re.DOTALL)
    for match in matches:
        try:
            tool_calls.append(json.loads(match))
        except json.JSONDecodeError:
            for extra in ["}}", "}}}}"]:
                try:
                    tool_calls.append(json.loads(match + extra))
                    break
                except json.JSONDecodeError:
                    pass
    return tool_calls


def execute_tool(tool_call):
    """Execute a tool call and return result"""
    tool = tool_call.get("tool", "")
    args = tool_call.get("args", {{}})

    log(f"Executing tool: {{tool}}")

    if tool == "read_file":
        return read_file(args.get("path", ""))
    elif tool == "list_files":
        return list_files(args.get("path", "."))
    elif tool == "search_code":
        return search_code(args.get("query", ""))
    elif tool == "get_file_stats":
        return get_file_stats(args.get("path", "."))
    elif tool == "write_file":
        return write_file(args.get("path", ""), args.get("content", ""))
    elif tool == "run_command":
        return run_command(args.get("command", ""), args.get("timeout", 60))
    elif tool == "git_commit":
        return git_commit(args.get("message", "Agent commit"))
    elif tool == "git_push":
        return git_push()
    else:
        return {{"ok": False, "error": f"Unknown tool: {{tool}}"}}


def call_llm(system_prompt, messages):
    """Call Minimax API with conversation history"""
    if not MINIMAX_API_KEY:
        return "MINIMAX_API_KEY not set"

    url = "https://api.minimax.io/anthropic/v1/messages"
    headers = {{
        "Authorization": f"Bearer {{MINIMAX_API_KEY}}",
        "Content-Type": "application/json"
    }}

    data = {{
        "model": MINIMAX_MODEL,
        "max_tokens": 196608,
        "system": system_prompt,
        "messages": messages
    }}

    if MINIMAX_GROUP_ID:
        data["group_id"] = MINIMAX_GROUP_ID

    max_attempts = 4
    backoff = [10, 30, 60]

    for attempt in range(max_attempts):
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=120)
            if resp.status_code == 200:
                result = resp.json()
                if "content" in result:
                    text_parts = []
                    for item in result.get("content", []):
                        if item.get("type") == "text":
                            text_parts.append(item.get("text", ""))
                    return "".join(text_parts)
                return result.get("choices", [{{}}])[0].get("message", {{}}).get("content", "No response")
            elif resp.status_code == 429:
                wait = backoff[min(attempt, len(backoff) - 1)]
                log(f"Rate limited (429) — waiting {{wait}}s before retry {{attempt + 1}}/{{max_attempts}}")
                time.sleep(wait)
            else:
                return f"API error: {{resp.status_code}}"
        except Exception as e:
            if attempt < max_attempts - 1:
                wait = backoff[min(attempt, len(backoff) - 1)]
                log(f"API error ({{e}}) — waiting {{wait}}s before retry")
                time.sleep(wait)
            else:
                return f"Error: {{e}}"

    return "Error: API failed"


def main():
    log(f"Starting task: {{PROJECT}} ({{TASK_TYPE}})")
    log(f"Description: {{TASK_DESC}}")

    project_path = os.path.join(WORKSPACE, PROJECT)

    # Pull latest
    log("Pulling latest...")
    code, out, err = run("git pull origin main --ff-only")
    if code != 0:
        run("git branch --set-upstream-to=origin/main main 2>/dev/null || true")
        code2, out2, err2 = run("git pull origin main --ff-only")
        log("Git pull successful" if code2 == 0 else f"Git pull: {{err2[:200]}}")
    else:
        log("Git pull successful")

    # Scan project for largest file
    ignore_dirs = {repr(IGNORE_DIRS)}
    main_file = None
    max_lines = 0
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        for f in files:
            if f.endswith('.gd'):
                path = os.path.join(root, f)
                try:
                    lines = len(open(path).readlines())
                    if lines > max_lines:
                        max_lines = lines
                        main_file = os.path.relpath(path, project_path)
                except:
                    pass

    if main_file:
        log(f"Largest file: {{main_file}} ({{max_lines}} lines)")

    # Tool loop
    conversation = [{{"role": "user", "content": {user_prompt_json}}}]
    tool_loop_count = 0

    completion_keyword = "TASK_COMPLETE"

    while tool_loop_count < MAX_TOOL_LOOPS:
        log(f"Calling LLM... (loop {{tool_loop_count + 1}}/{{MAX_TOOL_LOOPS}})")

        response = call_llm({system_prompt_json}, conversation)
        log(f"LLM response: {{response[:500]}}...")

        conversation.append({{"role": "assistant", "content": response}})

        if completion_keyword in response:
            log("Task marked complete by LLM")
            break

        tool_calls = parse_tool_calls(response)

        if not tool_calls:
            log("No tool calls found - task complete")
            break

        tool_results = []
        for tool_call in tool_calls:
            log(f"Tool call: {{tool_call}}")
            result = execute_tool(tool_call)
            log(f"Result: {{str(result)[:200]}}")
            tool_results.append(f"Tool {{tool_call.get('tool', '?')}}: {{json.dumps(result)}}")

        conversation.append({{"role": "user", "content": chr(10).join(tool_results)}})

        tool_loop_count += 1

    # Auto commit and push
    code, out, err = run("git status --porcelain")
    if code == 0 and out.strip():
        changed = [l[3:].strip() for l in out.strip().splitlines() if l.strip()]
        new_files = [f for f in changed if not f.endswith(".md")]
        if new_files:
            names = ", ".join(os.path.basename(f) for f in new_files[:4])
            if len(new_files) > 4:
                names += f" (+{{len(new_files) - 4}} more)"
            auto_msg = f"{{TASK_TYPE}}: update {{names}}"
        else:
            auto_msg = f"{{TASK_TYPE}}: update plan for {{PROJECT}}"
        result = git_commit(auto_msg)
        if result.get("ok"):
            git_push()

    log("Task complete!")
    print(json.dumps({{"status": "success", "project": PROJECT, "task_id": TASK_ID}}))
    return 0


if __name__ == "__main__":
    exit_code = main()
    try:
        Path(__file__).with_suffix(".exit").write_text(str(exit_code))
    except Exception:
        pass
    sys.exit(exit_code)
'''
        return script


class AgentSpawner:
    """Handles spawning and managing agent processes"""
    
    def __init__(
        self,
        workspace: Path,
        data_dir: Path,
        tracker: Optional[AgentTracker] = None,
        factory: Optional[AgentFactory] = None
    ):
        self.workspace = workspace
        self.data_dir = data_dir
        self.tracker = tracker or AgentTracker(data_dir / "agents.json")
        self.factory = factory or AgentFactory()
    
    def spawn(
        self,
        task,
        config: Dict[str, Any]
    ) -> Optional[str]:
        """
        Spawn an agent to work on a task.
        
        Args:
            task: Task object
            config: Configuration dict
        
        Returns:
            Agent ID if successful, None otherwise
        """
        import sys
        print(f"[AgentSpawner.spawn] Called with task: {task}", file=sys.stderr, flush=True)
        
        project = task.project if hasattr(task, 'project') else task['project']
        print(f"[AgentSpawner.spawn] Project: {project}", file=sys.stderr, flush=True)
        
        # Generate agent script
        script_content = self.factory.generate_script(
            task=task,
            workspace=self.workspace,
            project_name=project,
            config=config
        )
        
        agent_id = str(uuid.uuid4())
        script_path = self.data_dir / f"agent_{agent_id}.py"
        script_path.write_text(script_content, encoding="utf-8")
        os.chmod(script_path, 0o755)
        
        # Spawn subprocess
        try:
            env = os.environ.copy()
            log_path = self.data_dir / f"agent_{agent_id}.log"
            log_file = open(log_path, "w")
            
            proc = subprocess.Popen(
                [sys.executable, str(script_path)],
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                **popen_session_kwargs(),
            )
            log_file.close()
            
            # Create and track agent
            agent = Agent(
                id=agent_id,
                project=project,
                task_type=task.type if hasattr(task, 'type') else task.get('type', 'refactor'),
                status="active",
                pid=proc.pid,
                log_path=str(log_path),
                script_path=str(script_path),
                task_id=task.id if hasattr(task, 'id') else task.get('id')
            )
            
            self.tracker.add(agent)
            self.tracker.register_handle(agent_id, proc)
            
            import sys
            print(f"[AgentSpawner] Spawned agent {agent_id} for {project}, pid={proc.pid}", file=sys.stderr, flush=True)
            return agent_id
            
        except Exception as e:
            import sys
            print(f"[AgentSpawner] Failed to spawn agent: {e}", file=sys.stderr, flush=True)
            return None
    
    def check_status(self) -> List[str]:
        """Check status of active agents, return list of finished agent IDs"""
        return self.tracker.check_active_handles()
    
    def kill(self, agent_id: str) -> bool:
        """Kill an agent process"""
        handle = self.tracker.unregister_handle(agent_id)
        if handle:
            try:
                handle.terminate()
                handle.wait(timeout=5)
            except:
                try:
                    handle.kill()
                except:
                    pass
            return True
        return False


# Global instances
_tracker: Optional[AgentTracker] = None
_spawner: Optional[AgentSpawner] = None


def get_agent_tracker(agents_file: Optional[Path] = None) -> AgentTracker:
    global _tracker
    if _tracker is None:
        _tracker = AgentTracker(agents_file)
    return _tracker


def get_agent_spawner(
    workspace: Optional[Path] = None,
    data_dir: Optional[Path] = None
) -> AgentSpawner:
    global _spawner
    if _spawner is None:
        if workspace is None:
            workspace = Path(os.path.expanduser("~/workspace"))
        if data_dir is None:
            data_dir = workspace / "swarm-controller" / "data"
        _spawner = AgentSpawner(workspace, data_dir)
    return _spawner
