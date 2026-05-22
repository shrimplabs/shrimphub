"""
swarm.tools.core -- file I/O, git, shell, web, and task tools.

Config vars in this module are set by agent_runtime._sync_core_globals()
at the start of each agent run, mirroring the values set by the wrapper
script on swarm.agent_runtime before calling main().
"""

import json
import os
import re
import sys
from datetime import datetime
import urllib.request as _ur
import time
import uuid
from pathlib import Path
import random

from swarm import db as swarm_db
from swarm.task_chains import chain_to_project_head
from swarm.tools import _shared
from swarm.tools.shell import run, run_command, git_commit, git_push, _safe_cwd  # noqa: F401
from swarm.tools._shared import _project_root
from swarm.tools.path_guard import _is_vendor_path, _protected_path_reason, _protected_path_error, _command_targets_protected_path, _embedded_protected_path_matches, _command_attempts_embedded_protected_write, _check_catastrophic_command, _command_attempts_protected_write, _looks_like_dependency_path, _CATASTROPHIC_PATTERNS, _resolve_project_path
from swarm.tools.files import read_file, list_files, search_code, get_file_stats, get_file_outline, read_file_range, patch_file, write_file, append_file


# ---------------------------------------------------------------------------
# Config vars -- set via _sync_core_globals() in agent_runtime before use
# ---------------------------------------------------------------------------

WORKSPACE: Path = Path(".")
DATA_DIR: str = "data"
PROJECT: str = ""
PROJECT_PATH_OVERRIDE: str = ""
WORKTREE_BRANCH: str = ""
TASK_TYPE: str = "feature"
TASK_ID: str = "unknown"
TASK_PRIORITY: int = 50
MAX_LINES: int = 5000
IGNORE_DIRS: set = {"addons", ".git", ".godot"}
IGNORE_EXTENSIONS: set = frozenset()
MAX_TOOL_LOOPS: int = 120
API_PORT: int = 5001
QA_MAX_CYCLES: int = 3
MCP_SERVERS: dict = {}
MANAGED_PROJECTS: list = []
READONLY: bool = False
mcp_client = None


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def log(msg: str):
    print(f"[Agent] {msg}", flush=True)


# Map Windows-1252 "fancy" punctuation → plain ASCII equivalents.
# Applied to all text written via write_file / patch_file so LLM-generated
# em-dashes and smart quotes never produce non-UTF-8 bytes in GDScript files.
_FANCY_PUNCT_TABLE = str.maketrans({
    "\u2014": "--",  # em-dash → two hyphens
    "\u2013": "-",   # en-dash → hyphen
    "\u201c": '"',   # left double quote → ASCII quote
    "\u201d": '"',   # right double quote → ASCII quote
    "\u2018": "'",   # left single quote → ASCII apostrophe
    "\u2019": "'",   # right single quote → ASCII apostrophe
    "\u2026": "...", # ellipsis → three dots
    "\u00b7": "*",   # middle dot → asterisk
})


def _sanitize_text(content: str) -> str:
    """Replace fancy Unicode punctuation with ASCII equivalents and normalise line endings."""
    return content.translate(_FANCY_PUNCT_TABLE).replace('\r\n', '\n').replace('\r', '\n')


def _api_request(method: str, path: str, payload: dict | None = None, timeout: int = 15) -> dict:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = _ur.Request(
        f"http://localhost:{API_PORT}{path}",
        data=body,
        method=method,
        headers=headers,
    )
    with _ur.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _get_task_from_api(task_id: str) -> dict:
    return _api_request("GET", f"/api/tasks/{task_id}", timeout=10).get("task") or {}


def _patch_task_via_api(task_id: str, updates: dict) -> dict:
    return _api_request("PATCH", f"/api/tasks/{task_id}", payload=updates, timeout=10).get("task") or {}





# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

def mcp_call_tool(server: str, tool: str, args=None) -> dict:
    if mcp_client is None:
        return {"ok": False, "error": "MCP not configured"}
    return mcp_client.call_tool(server, tool, args)


def mcp_list_tools(server: str) -> list:
    if mcp_client is None:
        return []
    return mcp_client.list_tools(server)


# ---------------------------------------------------------------------------
# Search / fetch tools
# ---------------------------------------------------------------------------

def rag_query(question: str, top_k: int = 5) -> dict:
    """Query the RAG system for relevant code context.

    Requires 'rag' section in config.json:
        {"rag": {"enabled": true, "index_path": "/path/to/godot-doc-index"}}

    Returns a clear "RAG not configured" message when index_path is absent
    rather than a stack trace.
    """
    # Load index path from swarm config.json
    try:
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "config.json")
        with open(config_path) as f:
            config = json.load(f)
        rag_cfg = config.get("rag", {})
        if not rag_cfg.get("enabled", False):
            return {"ok": False, "error": "RAG is disabled in config.json (set rag.enabled to true)"}
        index_path = rag_cfg.get("index_path", "").strip()
        if not index_path:
            return {
                "ok": False,
                "error": (
                    "RAG index_path not configured in config.json. "
                    "Add {\"rag\": {\"index_path\": \"/path/to/godot-doc-index\"}} to your config.json."
                ),
            }
        index_path = str(Path(index_path).expanduser().resolve())
        if not os.path.isdir(index_path):
            return {"ok": False, "error": f"RAG index_path does not exist: {index_path}"}
    except Exception as err:
        return {"ok": False, "error": f"Failed to read config.json: {err}"}

    try:
        import sys as _sys
        _sys.path.insert(0, index_path)
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("HF_DATASETS_OFFLINE", "1")

        import yaml
        config_yaml = os.path.join(index_path, "config.yaml")
        with open(config_yaml) as f:
            cfg = yaml.safe_load(f)
        persist_dir = cfg["vector_db"]["persist_directory"]
        if not os.path.isabs(persist_dir):
            persist_dir = os.path.join(index_path, persist_dir)

        from src.embeddings import Embeddings
        from src.vector_store import VectorStore
        from src.retriever import Retriever
        embeddings = Embeddings(
            model_name=cfg["embeddings"]["model"],
            device=cfg["embeddings"]["device"]
        )
        vector_store = VectorStore(
            persist_directory=persist_dir,
            collection_name=cfg["vector_db"].get("collection_name", "godot_docs")
        )
        retriever = Retriever(
            vector_store=vector_store,
            embeddings=embeddings,
            top_k=top_k,
            similarity_threshold=cfg["retrieval"].get("similarity_threshold", 0.0),
            use_query_expansion=cfg["retrieval"].get("use_query_expansion", True),
            use_keyword_boost=cfg["retrieval"].get("use_keyword_boost", True)
        )
        context = retriever.get_context(question)
        raw_docs = retriever.retrieve(question)
        return {
            "ok": True,
            "context": context,
            "sources": [d.get("source", "") for d in raw_docs],
            "num_results": len(raw_docs),
        }
    except Exception as err:
        return {"ok": False, "error": str(err)[:200]}


def web_search(query: str, max_results: int = 3) -> dict:
    """Search the web. Falls back through Tavily -> Brave -> Serper -> DuckDuckGo."""
    import urllib.request

    max_results = min(max_results, 5)

    # Try Tavily first
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily_key:
        try:
            req = urllib.request.Request(
                "https://api.tavily.com/search",
                data=json.dumps({"query": query, "max_results": max_results}).encode(),
                headers={"Content-Type": "application/json", "Api-Key": tavily_key},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            results = []
            for r in data.get("results", [])[:max_results]:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("content", r.get("snippet", "")),
                })
            if results:
                return {"ok": True, "results": results}
        except Exception:
            pass

    # Try Brave Search
    brave_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if brave_key:
        try:
            q = urllib.parse.quote(query)
            req = urllib.request.Request(
                f"https://api.search.brave.com/res/v1/web/search?q={q}&count={max_results}",
                headers={"Accept": "application/json", "X-Subscription-Token": brave_key},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            results = []
            for r in data.get("web", {}).get("results", [])[:max_results]:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("description", r.get("snippet", "")),
                })
            if results:
                return {"ok": True, "results": results}
        except Exception:
            pass

    # Try Serper (Google)
    serper_key = os.environ.get("SERPER_API_KEY", "").strip()
    if serper_key:
        try:
            req = urllib.request.Request(
                "https://google.serper.dev/search",
                data=json.dumps({"q": query, "num": max_results}).encode(),
                headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            results = []
            for r in data.get("organic", [])[:max_results]:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("link", ""),
                    "snippet": r.get("snippet", ""),
                })
            if results:
                return {"ok": True, "results": results}
        except Exception:
            pass

    # Fall back to DuckDuckGo HTML scraper (no key required)
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}&kl=us-en"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        results = []
        import re as _re
        for result_div in _re.finditer(r'<div class="result__body">(.+?)</div>', html, _re.DOTALL):
            div_html = result_div.group(1)
            a_match = _re.search(r'<a class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', div_html)
            snippet_match = _re.search(r'<a class="result__snippet"[^>]*>([^<]+)</a>', div_html)
            if a_match:
                results.append({
                    "url": a_match.group(1),
                    "title": _re.sub(r'<[^>]+>', '', a_match.group(2)).strip(),
                    "snippet": (snippet_match.group(1).strip() if snippet_match else ""),
                })
            if len(results) >= max_results:
                break
        if not results:
            for a_tag in _re.finditer(r'<a class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', html):
                results.append({
                    "url": a_tag.group(1),
                    "title": _re.sub(r'<[^>]+>', '', a_tag.group(2)).strip(),
                    "snippet": "",
                })
                if len(results) >= max_results:
                    break
        if results:
            return {"ok": True, "results": results}
    except Exception:
        pass

    return {"ok": False, "error": "All search providers failed. Set BRAVE_API_KEY, TAVILY_API_KEY, or SERPER_API_KEY in .env for reliable results."}


def fetch_url(url: str, extract_text: bool = True) -> dict:
    """Fetch a URL and return its content as clean markdown."""
    import re
    try:
        import urllib.request
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        if extract_text:
            try:
                import html2text
                h = html2text.HTML2Text()
                h.ignore_links = False
                h.ignore_images = True
                h.ignore_tables = False
                h.body_width = 0
                h.single_line_break = True
                content = h.handle(raw)
                content = re.sub(r"\n{3,}", "\n\n", content).strip()
            except ImportError:
                content = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL | re.IGNORECASE)
                content = re.sub(r"<style[^>]*>.*?</style>", "", content, flags=re.DOTALL | re.IGNORECASE)
                content = re.sub(r"<[^>]+>", " ", content)
                content = re.sub(r"\s+", " ", content).strip()
        else:
            content = raw

        truncated = False
        if len(content) > 8000:
            content = content[:8000] + "\n\n[... truncated at 8000 characters ...]"
            truncated = True

        return {"ok": True, "content": content, "length": len(content), "truncated": truncated}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Broadcast log tools
# ---------------------------------------------------------------------------

def broadcast_read(tail: int = 50) -> dict:
    """Read recent entries from the project broadcast log."""
    try:
        import urllib.request as _ur
        url = f"http://localhost:{API_PORT}/api/broadcast/{PROJECT}?tail={tail}"
        with _ur.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def broadcast_write(message: str) -> dict:
    """Append an observation or intent to the project broadcast log."""
    try:
        import urllib.request as _ur
        body = json.dumps({"agent_id": TASK_ID, "task_id": TASK_ID, "message": message}).encode()
        req = _ur.Request(
            f"http://localhost:{API_PORT}/api/broadcast/{PROJECT}",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def delegate_helper(question: str, files: list | None = None, scope: str = "", max_chars: int = 12000) -> dict:
    """Run a transient read-only helper analysis and return findings to the parent task.

    This is intentionally non-durable: it does not create a task row and must not mutate
    project state. The helper only sees the provided question, optional scope text, and
    bounded excerpts from the requested files.
    """
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "question is required"}

    files = [f for f in (files or []) if isinstance(f, str) and f.strip()]
    max_chars = max(2000, min(int(max_chars or 12000), 24000))

    context_parts = [
        f"Parent task id: {TASK_ID}",
        f"Parent task type: {TASK_TYPE}",
        f"Project: {PROJECT}",
    ]
    if scope.strip():
        context_parts.append(f"Scope:\n{scope.strip()}")

    remaining = max_chars
    consulted = []
    for rel_path in files[:8]:
        resolved = _resolve_project_path(rel_path)
        if not resolved.exists() or not resolved.is_file():
            continue
        try:
            text = resolved.read_text(errors="replace")
        except Exception:
            continue
        budget = min(max(500, remaining // max(1, (len(files[:8]) - len(consulted)))), 4000)
        excerpt = text[:budget]
        consulted.append(rel_path)
        context_parts.append(f"File: {rel_path}\n```text\n{excerpt}\n```")
        remaining -= len(excerpt)
        if remaining <= 0:
            break

    helper_system = (
        "You are a transient read-only helper agent working for a parent swarm task. "
        "Answer the question using only the provided context. "
        "Do not propose file edits unless explicitly asked; prefer analysis, risks, and concrete next-step guidance. "
        "Keep the response concise and actionable."
    )
    helper_user = "\n\n".join(context_parts) + f"\n\nQuestion:\n{question}"

    try:
        from swarm.llm_utils import call_llm
        text, tokens = call_llm(helper_system, [{"role": "user", "content": helper_user}])
    except Exception as e:
        return {"ok": False, "error": str(e)}

    try:
        parent_task = _get_task_from_api(TASK_ID)
        parent_meta = dict(parent_task.get("metadata") or {})
        helper_history = list(parent_meta.get("helper_delegations") or [])
        helper_history.append({
            "at": datetime.now().isoformat(),
            "question": question[:280],
            "files": consulted,
            "scope": scope.strip()[:280],
            "answer_summary": text.strip()[:400],
            "input_tokens": tokens.get("input", 0) if isinstance(tokens, dict) else 0,
            "output_tokens": tokens.get("output", 0) if isinstance(tokens, dict) else 0,
        })
        parent_meta["helper_delegations"] = helper_history[-8:]
        _patch_task_via_api(TASK_ID, {"metadata": parent_meta})
    except Exception as e:
        log(f"[delegate_helper] WARNING: failed to persist helper metadata: {e}")

    return {
        "ok": True,
        "answer": text.strip(),
        "files_consulted": consulted,
        "input_tokens": tokens.get("input", 0) if isinstance(tokens, dict) else 0,
        "output_tokens": tokens.get("output", 0) if isinstance(tokens, dict) else 0,
    }


# ---------------------------------------------------------------------------
# Task management tools
# ---------------------------------------------------------------------------

def create_task(description: str, task_type: str = "feature", priority: int = 50,
                dependencies: list = None, project: str = None,
                parent_task_id: str = None, metadata: dict = None) -> dict:
    """Create a task in the swarm controller. Returns the task ID.

    task_type: feature | bug | refactor | polish | qa
    priority: capped at 90 (agents cannot create higher-priority tasks)
    dependencies: list of task ID strings that must exist
    project: project name (defaults to current project)
    parent_task_id: pass TASK_ID to create a sub-task; max depth is 2
    metadata: extra key/value metadata to store on the task
    """
    VALID_TYPES = {"feature", "bug", "polish", "refactor", "qa", "python_plan", "plan"}
    if task_type not in VALID_TYPES:
        return {"ok": False, "error": f"Invalid task type: '{task_type}'. Must be one of: {', '.join(VALID_TYPES)}"}

    priority = min(int(priority), 90)
    proj = project or PROJECT

    task_metadata = dict(metadata) if metadata else {}
    if parent_task_id:
        import urllib.request as _parent_ur
        try:
            with _parent_ur.urlopen(f"http://localhost:{API_PORT}/api/tasks", timeout=10) as resp:
                all_tasks = json.loads(resp.read()).get("tasks", [])
            parent = next((t for t in all_tasks if t.get("id") == parent_task_id), None)
            if not parent:
                return {"ok": False, "error": f"Parent task not found: {parent_task_id}"}
            parent_depth = parent.get("metadata", {}).get("task_depth", 0)
            if parent_depth >= 2:
                return {"ok": False, "error": "max sub-task depth (2) reached"}
            task_metadata["parent_task_id"] = parent_task_id
            task_metadata["task_depth"] = parent_depth + 1
        except Exception as e:
            return {"ok": False, "error": f"Failed to resolve parent_task_id: {str(e)}"}

    if isinstance(dependencies, str):
        deps_str = dependencies.strip()
        if deps_str.startswith("["):
            try:
                dependencies = json.loads(deps_str)
            except Exception:
                dependencies = [d.strip().strip('"\'') for d in deps_str.strip("[]").split(",") if d.strip().strip('"\'')]
        elif deps_str:
            dependencies = [d.strip().strip('"\'') for d in deps_str.split(",") if d.strip()]
        else:
            dependencies = []

    if dependencies:
        try:
            with _ur.urlopen(f"http://localhost:{API_PORT}/api/tasks", timeout=10) as resp:
                data = json.loads(resp.read())
            existing_ids = {t["id"] for t in data.get("tasks", []) if t.get("project") == proj}
            missing = [d for d in dependencies if d not in existing_ids]
            if missing:
                return {"ok": False, "error": f"Dependency task IDs not found: {missing}"}
        except Exception as e:
            return {"ok": False, "error": f"Failed to validate dependencies: {str(e)}"}

    generated_id = f"{task_type}-{int(time.time() * 1000) % 10**9}-agent"

    task = {
        "id": generated_id,
        "project": proj,
        "type": task_type,
        "priority": priority,
        "description": description,
        "dependencies": dependencies or [],
        "max_attempts": 3,
    }
    if task_metadata:
        task["metadata"] = task_metadata

    try:
        body = json.dumps(task).encode()
        req = _ur.Request(
            f"http://localhost:{API_PORT}/api/tasks",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        task_id = result.get("task", {}).get("id", generated_id)
        log(f"[create_task] Created {task_type} task {task_id}: {description[:60]}")
        return {"ok": True, "task_id": task_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def delegate_task_batch(children: list, mode: str = "integrate", project: str = None) -> dict:
    """Create a structured batch of child tasks linked back to the current task.

    Children are durable tasks in the project graph. Each child may declare:
      - description: required
      - type: feature|bug|refactor|polish
      - priority: optional
      - files: list[str] write scope (used for file-aware auto ordering)
      - dependencies: explicit external task ids
      - depends_on_children: list[int] indices into this batch
      - metadata: extra metadata
    """
    if not isinstance(children, list) or not children:
        return {"ok": False, "error": "children list is empty"}
    if len(children) > 6:
        return {"ok": False, "error": "delegation batch exceeds rollout limit of 6 children"}
    if mode not in {"wait", "integrate", "replace"}:
        return {"ok": False, "error": "mode must be one of: wait, integrate, replace"}

    proj = project or PROJECT
    valid_types = {"feature", "bug", "refactor", "polish"}
    batch_id = f"delegation-{uuid.uuid4().hex[:10]}"

    file_owners: dict[str, list[int]] = {}
    normalized_children: list[dict] = []

    for i, child in enumerate(children):
        if not isinstance(child, dict):
            return {"ok": False, "error": f"child {i} must be an object"}
        desc = (child.get("description") or "").strip()
        if not desc:
            return {"ok": False, "error": f"child {i} is missing description"}
        child_type = child.get("type", "feature")
        if child_type not in valid_types:
            return {"ok": False, "error": f"child {i} has invalid type '{child_type}'"}

        files = [f.strip() for f in (child.get("files") or []) if isinstance(f, str) and f.strip()]
        if not files:
            return {"ok": False, "error": f"child {i} must declare files for delegated write ownership"}
        explicit_ids = [d for d in (child.get("dependencies") or []) if isinstance(d, str) and d.strip()]
        bad_dep = next((dep for dep in explicit_ids if _looks_like_dependency_path(dep)), None)
        if bad_dep:
            return {
                "ok": False,
                "error": (
                    f"child {i} has invalid dependency '{bad_dep}'. "
                    "Use dependencies for task IDs and files for ownership/write scope."
                ),
            }
        depends_on_children = []
        for raw in (child.get("depends_on_children") or []):
            if isinstance(raw, int) and 0 <= raw < len(children) and raw != i:
                depends_on_children.append(raw)
            else:
                return {"ok": False, "error": f"child {i} has invalid depends_on_children entry: {raw}"}

        for f in files:
            file_owners.setdefault(f, []).append(i)

        normalized_children.append({
            "description": desc,
            "type": child_type,
            "priority": min(int(child.get("priority", 50)), 90),
            "files": files,
            "dependencies": explicit_ids,
            "depends_on_children": sorted(set(depends_on_children)),
            "metadata": dict(child.get("metadata") or {}),
        })

    for path, owners in sorted(file_owners.items()):
        if len(owners) < 2:
            continue
        owner_set = set(owners)
        for idx in owners:
            ordered_peers = set(normalized_children[idx]["depends_on_children"]) & owner_set
            ordered_by_peers = {
                other_idx for other_idx in owners
                if idx in normalized_children[other_idx]["depends_on_children"]
            }
            if not (ordered_peers or ordered_by_peers):
                return {
                    "ok": False,
                    "error": (
                        f"overlapping delegated write scope '{path}' requires explicit sequential ordering "
                        f"between children {owners}"
                    ),
                }

    root_child_indices: list[int] = []
    for i, child in enumerate(normalized_children):
        if not child["depends_on_children"] and TASK_ID not in child["dependencies"]:
            child["dependencies"] = [*child["dependencies"], TASK_ID]
            root_child_indices.append(i)

    batch_tasks = []
    for i, child in enumerate(normalized_children):
        metadata = dict(child["metadata"])
        metadata.update({
            "parent_task_id": TASK_ID,
            "delegation_batch_id": batch_id,
            "delegation_mode": mode,
            "delegated_files": child["files"],
            "delegated_child_index": i,
        })
        batch_tasks.append({
            "id": f"{child['type']}-{uuid.uuid4().hex[:12]}-agent",
            "project": proj,
            "type": child["type"],
            "description": child["description"],
            "priority": child["priority"],
            "dependencies": child["dependencies"],
            "depends_on": sorted(set(child["depends_on_children"])),
            "max_attempts": 3,
            "metadata": metadata,
        })

    body = json.dumps({
        "project": proj,
        "tasks": batch_tasks,
        "chain_to_head": False,
    }).encode()
    try:
        req = _ur.Request(
            f"http://localhost:{API_PORT}/api/tasks/batch",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if "error" in result:
        return {"ok": False, "error": result["error"]}

    ids = result.get("ids", [])
    successor_task_id = None
    successor_kind = None

    def _create_successor_task(kind: str, description: str) -> str:
        successor_id = f"{TASK_TYPE}-{uuid.uuid4().hex[:12]}-agent"
        successor_metadata = {
            "parent_task_id": TASK_ID,
            "delegation_batch_id": batch_id,
            "delegation_mode": mode,
            "delegation_successor_kind": kind,
            "delegation_child_task_ids": ids,
            "delegated_files": sorted({path for child in normalized_children for path in child["files"]}),
        }
        successor_payload = {
            "id": successor_id,
            "project": proj,
            "type": TASK_TYPE if TASK_TYPE in valid_types else "feature",
            "description": description,
            "priority": TASK_PRIORITY,
            "max_attempts": 3,
            "dependencies": ids,
            "metadata": successor_metadata,
        }
        created = _api_request("POST", "/api/tasks", payload=successor_payload, timeout=15)
        return (created.get("task") or {}).get("id", successor_id)

    if mode == "integrate":
        successor_kind = "integration"
        successor_task_id = _create_successor_task(
            "integration",
            (
                f"Integrate delegated child work for parent task {TASK_ID}.\n\n"
                "Read the completed child tasks in this delegation batch, reconcile their changes, "
                "resolve any seams between subsystems, run the narrowest validation that proves the "
                "combined work behaves correctly, then finish the original parent objective."
            ),
        )
    elif mode == "wait":
        successor_kind = "resume"
        successor_task_id = _create_successor_task(
            "resume",
            (
                f"Resume delegated parent task {TASK_ID} after its child tasks complete.\n\n"
                "Review the child-task outputs in this delegation batch, continue the remaining work "
                "that still belongs to the original parent task, and then validate and wrap it up."
            ),
        )

    try:
        parent_task = _get_task_from_api(TASK_ID)
        parent_meta = dict(parent_task.get("metadata") or {})
        parent_meta.update({
            "delegation_batch_id": batch_id,
            "delegation_mode": mode,
            "delegation_state": "delegated",
            "delegated_child_task_ids": ids,
            "delegated_child_count": len(ids),
            "delegated_root_child_indices": root_child_indices,
        })
        if successor_task_id:
            parent_meta["delegation_successor_task_id"] = successor_task_id
            parent_meta["delegation_successor_kind"] = successor_kind
        _patch_task_via_api(TASK_ID, {"metadata": parent_meta})
    except Exception as e:
        log(f"[delegate_task_batch] WARNING: failed to persist parent delegation metadata: {e}")

    log(
        f"[delegate_task_batch] Created {len(ids)} child tasks under {TASK_ID} "
        f"(mode={mode}, successor={successor_task_id or 'none'})"
    )
    response = {
        "ok": True,
        "task_ids": ids,
        "count": len(ids),
        "delegation_batch_id": batch_id,
        "mode": mode,
        "root_child_indices": root_child_indices,
        "parent_action": "complete_parent",
    }
    if successor_task_id:
        response["successor_task_id"] = successor_task_id
        response["successor_kind"] = successor_kind
    return response


def create_tasks_file_aware(tasks: list, project: str = None) -> dict:
    """Create multiple tasks with automatic file-aware dependency chaining.

    Analyses which files each task will touch and automatically adds dependencies
    between any two tasks that share a file -- preventing merge conflicts from
    parallel agents working on the same file.

    Each task dict must have:
      - "type": "feature" | "bug" | "refactor" | "polish"
      - "description": str
      - "files": list[str] -- files this task will CREATE or MODIFY
      - "priority": int (optional, default 50)
      - "dependencies": list[str] (optional) -- explicit extra task IDs

    Returns {"ok": True, "tasks": [...], "count": N} on success.
    """
    if not tasks:
        return {"ok": False, "error": "tasks list is empty"}

    proj = project or PROJECT

    # Build file → first-owner index map and auto-chain deps
    file_owner: dict = {}   # filename → index of first task that owns it
    auto_deps: list = [set() for _ in tasks]

    for i, task in enumerate(tasks):
        explicit_ids = [d for d in task.get("dependencies", []) if isinstance(d, str)]
        bad_dep = next((dep for dep in explicit_ids if _looks_like_dependency_path(dep)), None)
        if bad_dep:
            return {
                "ok": False,
                "error": (
                    f"Task {i} has invalid dependency '{bad_dep}'. "
                    "Planner dependencies must be task IDs only; keep file ownership in files=[...]."
                ),
            }
        for f in task.get("files", []):
            f = f.strip()
            if not f:
                continue
            if f in file_owner:
                auto_deps[i].add(file_owner[f])
            else:
                file_owner[f] = i

    # Topological sort: roots first
    order = []
    remaining = list(range(len(tasks)))
    created_indices: list = []

    while remaining:
        ready = [i for i in remaining if all(d in created_indices for d in auto_deps[i])]
        if not ready:
            ready = [remaining[0]]
        for i in ready:
            order.append(i)
            created_indices.append(i)
            remaining.remove(i)

    ordered_pos = {task_idx: pos for pos, task_idx in enumerate(order)}
    batch_tasks = []
    generated_ids: dict[int, str] = {}

    for i in order:
        task = tasks[i]
        explicit_ids = [d for d in task.get("dependencies", []) if isinstance(d, str)]
        if TASK_TYPE in ("project_plan", "plan", "python_plan") and TASK_ID and not explicit_ids and not auto_deps[i]:
            explicit_ids = [TASK_ID]
        task_type = task.get("type", "feature")
        priority = min(int(task.get("priority", 50)), 90)
        generated_id = f"{task_type}-{uuid.uuid4().hex[:12]}-agent"
        generated_ids[i] = generated_id
        metadata = dict(task.get("metadata") or {})
        if TASK_TYPE in ("project_plan", "plan", "python_plan"):
            metadata.setdefault("parent_task_id", TASK_ID)
            metadata.setdefault("planner_task_type", TASK_TYPE)
            metadata.setdefault("planned_files", list(task.get("files", [])))
            metadata.setdefault("file_aware_auto_dep_indices", sorted(auto_deps[i]))

        batch_tasks.append({
            "id": generated_id,
            "project": proj,
            "type": task_type,
            "description": task.get("description", ""),
            "priority": priority,
            "dependencies": explicit_ids,
            "depends_on": [ordered_pos[d] for d in sorted(auto_deps[i])],
            "max_attempts": 3,
            "metadata": metadata,
        })

    body = json.dumps({
        "project": proj,
        "tasks": batch_tasks,
        "chain_to_head": True,
    }).encode()
    try:
        req = _ur.Request(
            f"http://localhost:{API_PORT}/api/tasks/batch",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if "error" in result:
        return {"ok": False, "error": result["error"]}

    response_id_map = {
        int(index): task_id
        for index, task_id in (result.get("id_map") or {}).items()
    }
    results = []
    for batch_pos, i in enumerate(order):
        task_id = response_id_map.get(batch_pos, generated_ids[i])
        task_type = batch_tasks[batch_pos]["type"]
        auto_dep_ids = [
            response_id_map.get(ordered_pos[d], generated_ids[d])
            for d in sorted(auto_deps[i])
        ]
        results.append({
            "task_id": task_id,
            "type": task_type,
            "auto_deps": auto_dep_ids,
            "parallel": len(auto_deps[i]) == 0,
        })
        log(f"[create_tasks_file_aware] Created {task_type} {task_id}: {batch_tasks[batch_pos]['description'][:60]}")

    return {
        "ok": True,
        "tasks": results,
        "count": len(results),
        "errors": None,
    }


def create_tasks(tasks: list, project: str = None, chain_to_head: bool = False) -> dict:
    """Create multiple tasks in one call with reliable DAG dependency wiring.

    Use this instead of calling create_task() repeatedly when you need to create
    3+ tasks with dependencies between them. All IDs are generated upfront so
    depends_on index references always resolve -- no chicken-and-egg problem.

    Each task dict supports:
      - "type": "feature" | "bug" | "refactor" | "polish" | "qa" | "plan"
      - "description": str
      - "priority": int (optional, default 50)
      - "depends_on": list[int] -- indices into THIS tasks list (resolved to IDs before creation)
      - "dependencies": list[str] -- explicit task IDs outside this batch (merged with depends_on)

    chain_to_head: if True, the first rootless task in the batch automatically depends on
                   the project HEAD (last completed task), keeping history connected.

    Example -- bug fixes in parallel, feature waits for both:
      create_tasks([
        {"type": "bug",     "description": "Fix crash in login",   "priority": 80},
        {"type": "bug",     "description": "Fix session timeout",  "priority": 80},
        {"type": "feature", "description": "Add OAuth",            "priority": 50, "depends_on": [0, 1]},
      ])

    Returns {"ok": bool, "ids": [...], "id_map": {"0": "<id>", ...}, "count": N}
    """
    proj = project or PROJECT
    body = json.dumps({
        "project": proj,
        "tasks": tasks,
        "chain_to_head": chain_to_head,
    }).encode()
    try:
        req = _ur.Request(
            f"http://localhost:{API_PORT}/api/tasks/batch",
            data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        if "error" in result:
            return {"ok": False, "error": result["error"]}
        ids = result.get("ids", [])
        log(f"[create_tasks] Created {len(ids)} tasks for '{proj}'")
        return {"ok": True, "ids": ids, "id_map": result.get("id_map", {}), "count": len(ids)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_tasks(project: str = None) -> dict:
    """List all tasks for the current (or specified) project."""
    target_project = project if project else PROJECT
    try:
        with _ur.urlopen(f"http://localhost:{API_PORT}/api/tasks", timeout=10) as resp:
            data = json.loads(resp.read())
        tasks = [
            {
                "id": t["id"],
                "type": t.get("type", ""),
                "status": t.get("status", ""),
                "description": t.get("description", "")[:80],
            }
            for t in data.get("tasks", [])
            if t.get("project") == target_project
        ]
        return {"ok": True, "tasks": tasks, "project": target_project}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_subtasks(parent_task_id: str = None) -> dict:
    """List sub-tasks. Defaults to the current TASK_ID if parent not specified."""
    target = parent_task_id if parent_task_id else TASK_ID
    try:
        with _ur.urlopen(f"http://localhost:{API_PORT}/api/tasks", timeout=10) as resp:
            data = json.loads(resp.read())
        subtasks = [
            {
                "id": t["id"],
                "type": t.get("type", ""),
                "status": t.get("status", ""),
                "description": t.get("description", "")[:80],
            }
            for t in data.get("tasks", [])
            if t.get("metadata", {}).get("parent_task_id") == target
        ]
        return {"ok": True, "subtasks": subtasks, "parent_task_id": target}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Adaptive Graph tools ──────────────────────────────────────────────────────
# These tools allow an agent to mutate downstream tasks in the dependency graph
# as it discovers information during its own work. Agents may only touch tasks
# that are downstream of their own task (pending, not in-progress).

def _get_downstream_task_ids(task_id: str, all_tasks: list) -> set:
    """Return all task IDs that transitively depend on task_id."""
    dependents = set()
    frontier = {task_id}
    while frontier:
        next_frontier = set()
        for t in all_tasks:
            if t["id"] in dependents:
                continue
            if any(d in frontier for d in (t.get("dependencies") or [])):
                dependents.add(t["id"])
                next_frontier.add(t["id"])
        frontier = next_frontier
    return dependents


def _fetch_all_project_tasks(proj: str) -> tuple[list, str]:
    """Fetch all tasks for the project. Returns (tasks, error)."""
    try:
        with _ur.urlopen(f"http://localhost:{API_PORT}/api/tasks", timeout=10) as resp:
            data = json.loads(resp.read())
        tasks = [t for t in data.get("tasks", []) if t.get("project") == proj]
        return tasks, ""
    except Exception as e:
        return [], str(e)


def annotate_downstream_tasks(findings: str, task_ids: list = None) -> dict:
    """Prepend a 'CONTEXT FROM UPSTREAM AGENT' block to downstream pending tasks.

    Call this before TASK_COMPLETE to share what you learned with agents that
    will work on tasks that depend on yours. Useful for:
    - Sharing API shapes, file paths, or architectural decisions you made
    - Warning about constraints or gotchas you discovered
    - Clarifying assumptions the planner made that turned out to be wrong

    findings: free-text summary of what you learned (2-10 sentences)
    task_ids: optional list of specific downstream task IDs to annotate;
              if omitted, all pending downstream tasks are annotated
    """
    if not findings or not findings.strip():
        return {"ok": False, "error": "findings must not be empty"}

    proj = PROJECT
    all_tasks, err = _fetch_all_project_tasks(proj)
    if err:
        return {"ok": False, "error": err}

    downstream_ids = _get_downstream_task_ids(TASK_ID, all_tasks)
    targets = [
        t for t in all_tasks
        if t["id"] in downstream_ids
        and t["status"] == "pending"
        and (task_ids is None or t["id"] in task_ids)
    ]

    if not targets:
        # Give a specific reason so agents don't retry blindly
        if downstream_ids:
            non_pending = [t for t in all_tasks if t["id"] in downstream_ids and t["status"] != "pending"]
            statuses = {t["status"] for t in non_pending}
            return {"ok": True, "annotated": 0, "note": f"downstream tasks exist but none are pending (statuses: {sorted(statuses)}) -- they may already be in_progress or completed"}
        elif task_ids:
            return {"ok": True, "annotated": 0, "note": "the task_ids you specified are not transitively downstream of your task -- only tasks that depend on yours (directly or indirectly) can be annotated"}
        else:
            return {"ok": True, "annotated": 0, "note": "no tasks are transitively downstream of your task -- other pending tasks in the project are parallel branches, not dependents"}

    annotation_block = (
        f"\n\n---\n"
        f"CONTEXT FROM UPSTREAM AGENT (task {TASK_ID}):\n"
        f"{findings.strip()}\n"
        f"---\n"
    )

    annotated = []
    errors = []
    for t in targets:
        new_desc = t.get("description", "") + annotation_block
        try:
            result = _api_request("PATCH", f"/api/tasks/{t['id']}", {"description": new_desc})
            if result.get("error"):
                errors.append(f"{t['id']}: {result['error']}")
            else:
                annotated.append(t["id"])
                log(f"[AdaptiveGraph] Annotated downstream task {t['id']}")
        except Exception as e:
            errors.append(f"{t['id']}: {e}")

    return {"ok": True, "annotated": len(annotated), "task_ids": annotated, "errors": errors}


def split_task(task_id: str, replacement_tasks: list) -> dict:
    """Replace a pending downstream task with multiple smaller tasks.

    The original task is deleted. Tasks that depended on it are rewired to
    depend on the last replacement task. The replacements are chained in order
    (each depends on the previous).

    task_id: ID of the downstream pending task to replace
    replacement_tasks: list of dicts, each with:
        - description (required)
        - type: feature | bug | refactor | polish (default: same as original)
        - priority: int (default: same as original)

    Safety: only works on pending tasks downstream of the current task.
    Cannot split tasks that are in-progress or already completed.
    """
    if not replacement_tasks or len(replacement_tasks) < 2:
        return {"ok": False, "error": "replacement_tasks must contain at least 2 tasks"}

    proj = PROJECT
    all_tasks, err = _fetch_all_project_tasks(proj)
    if err:
        return {"ok": False, "error": err}

    # Safety: must be downstream and pending
    downstream_ids = _get_downstream_task_ids(TASK_ID, all_tasks)
    original = next((t for t in all_tasks if t["id"] == task_id), None)
    if not original:
        return {"ok": False, "error": f"Task {task_id} not found in project {proj}"}
    if task_id not in downstream_ids:
        return {"ok": False, "error": f"Task {task_id} is not downstream of the current task {TASK_ID} -- cannot split. Only pending tasks that transitively depend on your task are eligible. Try annotate_downstream_tasks() instead to pass context forward."}
    if original["status"] != "pending":
        return {"ok": False, "error": f"Task {task_id} is {original['status']} -- can only split pending tasks"}

    original_deps = original.get("dependencies") or []
    original_type = original.get("type", "feature")
    original_priority = original.get("priority", 50)

    # Build replacement task records, chained in sequence
    new_ids = []
    new_tasks = []
    for i, rt in enumerate(replacement_tasks):
        new_id = f"{proj}-split-{int(time.time() * 1000) % 10**9}-{i}"
        deps = original_deps if i == 0 else [new_ids[i - 1]]
        new_tasks.append({
            "id": new_id,
            "project": proj,
            "type": rt.get("type", original_type),
            "priority": int(rt.get("priority", original_priority)),
            "description": rt["description"].strip(),
            "status": "pending",
            "dependencies": deps,
            "metadata": {"split_from": task_id, "split_index": i, "created_by_agent": TASK_ID},
        })
        new_ids.append(new_id)

    # Cycle check: verify no new task introduces a cycle
    # (simple check: none of the new IDs appear in their own transitive deps)
    existing_ids = {t["id"] for t in all_tasks}
    for nt in new_tasks:
        for d in nt["dependencies"]:
            if d not in existing_ids and d not in new_ids:
                return {"ok": False, "error": f"Dependency {d} does not exist"}

    # Create replacement tasks
    created = []
    for nt in new_tasks:
        try:
            result = _api_request("POST", "/api/tasks", nt)
            if result.get("error"):
                return {"ok": False, "error": f"Failed to create replacement task: {result['error']}", "created_so_far": created}
            created.append(nt["id"])
        except Exception as e:
            return {"ok": False, "error": str(e), "created_so_far": created}

    # Rewire: tasks that depended on the original now depend on the last replacement
    last_id = new_ids[-1]
    rewired = []
    for t in all_tasks:
        deps = t.get("dependencies") or []
        if task_id in deps and t["id"] != task_id:
            new_deps = [last_id if d == task_id else d for d in deps]
            try:
                _api_request("PATCH", f"/api/tasks/{t['id']}", {"dependencies": new_deps})
                rewired.append(t["id"])
            except Exception as e:
                log(f"[AdaptiveGraph] Warning: failed to rewire {t['id']}: {e}")

    # Delete the original task
    try:
        _api_request("DELETE", f"/api/tasks/{task_id}")
        log(f"[AdaptiveGraph] Deleted original task {task_id}, replaced with {new_ids}")
    except Exception as e:
        return {"ok": False, "error": f"Replacements created but failed to delete original: {e}",
                "created": created, "rewired": rewired}

    return {
        "ok": True,
        "original_deleted": task_id,
        "replacements_created": created,
        "tasks_rewired": rewired,
    }


def prune_task(task_id: str, reason: str) -> dict:
    """Mark a pending downstream task as completed (redundant/already done).

    Use this when your work has made a downstream task unnecessary -- for example
    you implemented something that was going to be a separate task, or you
    discovered the task described work that doesn't need to happen.

    task_id: ID of the downstream pending task to prune
    reason: explanation of why the task is no longer needed (recorded in metadata)

    Safety: only works on pending tasks downstream of the current task.
    """
    if not reason or not reason.strip():
        return {"ok": False, "error": "reason must not be empty"}

    proj = PROJECT
    all_tasks, err = _fetch_all_project_tasks(proj)
    if err:
        return {"ok": False, "error": err}

    downstream_ids = _get_downstream_task_ids(TASK_ID, all_tasks)
    target = next((t for t in all_tasks if t["id"] == task_id), None)
    if not target:
        return {"ok": False, "error": f"Task {task_id} not found in project {proj}"}
    if task_id not in downstream_ids:
        return {"ok": False, "error": f"Task {task_id} is not downstream of {TASK_ID} -- cannot prune. Only pending tasks that transitively depend on your task are eligible. Try annotate_downstream_tasks() to pass context to tasks you cannot prune."}
    if target["status"] != "pending":
        return {"ok": False, "error": f"Task {task_id} is {target['status']} -- can only prune pending tasks"}

    existing_meta = target.get("metadata") or {}
    try:
        _api_request("PATCH", f"/api/tasks/{task_id}", {
            "status": "completed",
            "metadata": {
                **existing_meta,
                "pruned_by_agent": TASK_ID,
                "prune_reason": reason.strip(),
            },
        })
        log(f"[AdaptiveGraph] Pruned task {task_id}: {reason[:80]}")
        return {"ok": True, "pruned": task_id, "reason": reason.strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def insert_dependency(from_task_id: str, to_task_id: str) -> dict:
    """Add a dependency edge: from_task_id will now depend on to_task_id.

    Use this when you discover that two existing downstream tasks have an
    ordering constraint the planner didn't know about -- i.e. from_task_id
    cannot safely run until to_task_id completes.

    from_task_id: the task that needs to wait
    to_task_id: the task that must complete first

    Safety:
    - Both tasks must be pending and downstream of the current task
    - The edge must not create a cycle
    - Cannot modify tasks that are in-progress or completed
    """
    proj = PROJECT
    all_tasks, err = _fetch_all_project_tasks(proj)
    if err:
        return {"ok": False, "error": err}

    downstream_ids = _get_downstream_task_ids(TASK_ID, all_tasks)
    from_task = next((t for t in all_tasks if t["id"] == from_task_id), None)
    to_task = next((t for t in all_tasks if t["id"] == to_task_id), None)

    if not from_task:
        return {"ok": False, "error": f"Task {from_task_id} not found"}
    if not to_task:
        return {"ok": False, "error": f"Task {to_task_id} not found"}
    if from_task_id not in downstream_ids:
        return {"ok": False, "error": f"{from_task_id} is not downstream of {TASK_ID} -- insert_dependency only works between tasks that transitively depend on yours"}
    if to_task_id not in downstream_ids:
        return {"ok": False, "error": f"{to_task_id} is not downstream of {TASK_ID} -- insert_dependency only works between tasks that transitively depend on yours"}
    if from_task["status"] != "pending":
        return {"ok": False, "error": f"{from_task_id} is {from_task['status']} -- can only insert deps on pending tasks"}
    if to_task["status"] == "in_progress":
        return {"ok": False, "error": f"{to_task_id} is in_progress -- inserting this dep would block work already underway"}

    # Cycle check: would adding from→to create a cycle?
    # A cycle exists if to_task_id is already reachable from from_task_id
    # (i.e. to already transitively depends on from)
    reachable_from_from = _get_downstream_task_ids(from_task_id, all_tasks)
    if to_task_id in reachable_from_from:
        return {"ok": False, "error": f"Adding {from_task_id} → {to_task_id} would create a cycle"}
    if from_task_id == to_task_id:
        return {"ok": False, "error": "A task cannot depend on itself"}

    existing_deps = list(from_task.get("dependencies") or [])
    if to_task_id in existing_deps:
        return {"ok": True, "note": "dependency already exists", "from": from_task_id, "to": to_task_id}

    new_deps = existing_deps + [to_task_id]
    try:
        _api_request("PATCH", f"/api/tasks/{from_task_id}", {"dependencies": new_deps})
        log(f"[AdaptiveGraph] Inserted dep: {from_task_id} now depends on {to_task_id}")
        return {"ok": True, "inserted": {"from": from_task_id, "to": to_task_id}}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def set_task_complexity(task_id: str, complexity: str, reason: str = "") -> dict:
    """Tag a task with a complexity estimate: 'simple' or 'complex'.

    Use this when you discover during your work that a downstream pending task is
    significantly harder or easier than typical. The scheduler uses this to adjust
    priority and attempt allocation.

    complexity: 'simple' (straightforward, likely 1 attempt) or
                'complex' (high risk, many unknowns, benefits from extra attempts)
    reason: brief explanation (shown in dashboard, helps future agents)

    Safety:
    - Only pending tasks may be annotated
    - complexity must be 'simple' or 'complex'
    """
    if complexity not in {"simple", "complex"}:
        return {"ok": False, "error": "complexity must be 'simple' or 'complex'"}

    proj = PROJECT
    all_tasks, err = _fetch_all_project_tasks(proj)
    if err:
        return {"ok": False, "error": err}

    task = next((t for t in all_tasks if t["id"] == task_id), None)
    if not task:
        return {"ok": False, "error": f"Task {task_id} not found in project {proj}"}
    if task["status"] != "pending":
        return {"ok": False, "error": f"Task {task_id} is {task['status']} -- can only annotate pending tasks"}

    metadata = dict(task.get("metadata") or {})
    metadata["complexity"] = complexity
    if reason:
        metadata["complexity_reason"] = reason

    # Bump max_attempts for complex tasks so they get extra chances
    update: dict = {"metadata": metadata}
    if complexity == "complex":
        current_max = task.get("max_attempts", 3)
        if current_max < 5:
            update["max_attempts"] = 5

    try:
        _api_request("PATCH", f"/api/tasks/{task_id}", update)
        log(f"[AdaptiveGraph] Set complexity={complexity} on {task_id}: {reason}")
        return {"ok": True, "task_id": task_id, "complexity": complexity, "max_attempts": update.get("max_attempts")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
