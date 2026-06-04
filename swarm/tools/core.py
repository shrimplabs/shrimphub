"""
swarm.tools.core -- file I/O, git, shell, web, and task tools.

Config vars in this module are set by agent_runtime._sync_core_globals()
at the start of each agent run, mirroring the values set by the wrapper
script on swarm.agent_runtime before calling main().
"""

import json
import os
import re
from datetime import datetime
import urllib.request as _ur
from pathlib import Path

from swarm.tools.shell import run, run_command, run_python, git_commit, git_push, _safe_cwd  # noqa: F401
from swarm.tools._shared import _project_root, log, _sanitize_text  # noqa: F401
from swarm.tools.path_guard import _resolve_project_path
from swarm.tools.files import (  # noqa: F401
    read_file, list_files, search_code, get_file_stats, get_file_outline,  # noqa: F401
    read_file_range, patch_file, write_file, append_file,
)


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

def _read_text_with_fallback(path: str | Path) -> tuple[str, str]:
    """Read text files that may have been authored with Windows ANSI tools.

    Always returns LF-only text regardless of what line endings are on disk,
    so patch_file matches reliably on Windows without agents needing to
    account for CRLF vs LF differences.
    """
    raw = Path(path).read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            return text.replace('\r\n', '\n').replace('\r', '\n'), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").replace('\r\n', '\n').replace('\r', '\n'), "utf-8-replace"


def _read_lines_with_fallback(path: str | Path) -> tuple[list[str], str]:
    text, encoding = _read_text_with_fallback(path)
    return text.splitlines(keepends=True), encoding



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
        text, tokens, _thinking = call_llm(helper_system, [{"role": "user", "content": helper_user}])
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
# Re-exports from swarm/tools/tasks.py (backward compatibility)
# The task management tools were extracted to swarm/tools/tasks.py in the
# refactor/tools-split branch.
# ---------------------------------------------------------------------------

from swarm.tools.tasks import (  # noqa: F401, E402
    create_task,
    create_tasks_file_aware,
    create_tasks,
    delegate_task_batch,
    list_tasks,
    list_subtasks,
    annotate_downstream_tasks,
    split_task,
    prune_task,
    insert_dependency,
    set_task_complexity,
)
