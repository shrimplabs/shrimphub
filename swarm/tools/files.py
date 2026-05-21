"""File I/O tools extracted from swarm.tools.core.

Globals (TASK_TYPE, READONLY, MAX_LINES, etc.) are set on swarm.tools.core
by _sync_core_globals() at the start of each agent run. We read them at call
time via a lazy import of swarm.tools.core so the values are always current.
"""

from __future__ import annotations


def read_file(relative_path: str, offset: int = 0, limit: int = 0) -> dict:
    """Read a file. offset/limit are in lines (0 = no limit)."""
    import swarm.tools.core as _core
    path = _core.os.path.join(_core._project_root(), relative_path)
    try:
        lines, encoding = _core._read_lines_with_fallback(path)
        total = len(lines)
        if offset or limit:
            chunk = lines[offset: offset + limit] if limit else lines[offset:]
            content = "".join(chunk)
            return {"ok": True, "content": content, "total_lines": total,
                    "returned_lines": len(chunk), "offset": offset, "encoding": encoding}
        return {"ok": True, "content": "".join(lines), "total_lines": total, "encoding": encoding}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def list_files(relative_path: str = ".") -> dict:
    """List files and directories at the given relative path."""
    import swarm.tools.core as _core
    path = _core.os.path.join(_core._project_root(), relative_path)
    try:
        if _core.os.path.isfile(path):
            return {"ok": True, "files": [_core.os.path.basename(path)], "type": "file"}
        files = []
        for item in _core.os.listdir(path):
            full = _core.os.path.join(path, item)
            files.append(item + "/" if _core.os.path.isdir(full) else item)
        return {"ok": True, "files": files}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def search_code(query: str) -> dict:
    """Search all GDScript (.gd) files for a string."""
    import swarm.tools.core as _core
    path = _core._project_root()
    try:
        matches = []
        for root, dirs, files in _core.os.walk(path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                if not fname.endswith(".gd"):
                    continue
                fpath = _core.os.path.join(root, fname)
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        if query in f.read():
                            matches.append(fpath)
                except OSError:
                    pass
        return {"ok": True, "files": matches}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_file_stats(relative_path: str) -> dict:
    """Return line count and byte size for a file."""
    import swarm.tools.core as _core
    path = _core.os.path.join(_core._project_root(), relative_path)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = sum(1 for _ in f)
        size = _core.os.path.getsize(path)
        return {"ok": True, "lines": lines, "size": size}
    except Exception as e:
        return {"ok": False, "error": str(e)}



def get_file_outline(relative_path: str) -> dict:
    """Get outline of functions and classes in a file with line numbers.

    For Python: uses AST.
    For GDScript: uses regex patterns.
    """
    import ast
    import re
    import swarm.tools.core as _core
    path = _core.os.path.join(_core._project_root(), relative_path)

    if not _core.os.path.exists(path):
        return {"ok": False, "error": "file not found"}

    ext = _core.os.path.splitext(path)[1].lower()

    if ext == '.py':
        try:
            source, _encoding = _core._read_text_with_fallback(path)
            tree = ast.parse(source)
            outline = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    outline.append({
                        "type": "function",
                        "name": node.name,
                        "line": node.lineno,
                        "end_line": node.end_lineno,
                    })
                elif isinstance(node, ast.ClassDef):
                    outline.append({
                        "type": "class",
                        "name": node.name,
                        "line": node.lineno,
                        "end_line": node.end_lineno,
                    })
            outline.sort(key=lambda x: x["line"])
            return {"ok": True, "outline": outline}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    elif ext == '.gd':
        try:
            lines, _encoding = _core._read_lines_with_fallback(path)

            outline = []
            func_pattern = re.compile(r'^\s*func\s+(\w+)')
            class_pattern = re.compile(r'^\s*class\s+(\w+)')


            for i, line in enumerate(lines, start=1):
                func_match = func_pattern.match(line)
                class_match = class_pattern.match(line)
                if func_match:
                    outline.append({"type": "function", "name": func_match.group(1), "line": i, "end_line": None})
                elif class_match:
                    outline.append({"type": "class", "name": class_match.group(1), "line": i, "end_line": None})

            def get_indent(line):
                return len(line) - len(line.lstrip()) if line.strip() else 999

            for i, item in enumerate(outline):
                start_line = item["line"]
                start_indent = get_indent(lines[start_line - 1]) if start_line <= len(lines) else 0
                end_line = len(lines)
                for j in range(i + 1, len(outline)):
                    next_line = lines[outline[j]["line"] - 1] if outline[j]["line"] <= len(lines) else ""
                    if get_indent(next_line) <= start_indent:
                        end_line = outline[j]["line"] - 1
                        break
                item["end_line"] = end_line

            return {"ok": True, "outline": outline}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    else:
        return {"ok": False, "error": "unsupported file type"}


def read_file_range(relative_path: str, start_line: int, end_line: int) -> dict:
    """Read a specific line range from a file. Line numbers are 1-indexed and inclusive.
    Hard cap: max 300 lines per call.
    """
    import swarm.tools.core as _core
    path = _core.os.path.join(_core._project_root(), relative_path)
    if not _core.os.path.exists(path):
        return {"ok": False, "error": "file not found"}
    try:
        all_lines, encoding = _core._read_lines_with_fallback(path)

        total_lines = len(all_lines)
        start_line = max(1, start_line)
        end_line = min(total_lines, end_line)

        truncated = False
        if end_line - start_line > 300:
            end_line = start_line + 300
            truncated = True

        content = "".join(all_lines[start_line - 1:end_line])


        return {
            "ok": True,
            "content": content,
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": total_lines,
            "truncated": truncated,
            "encoding": encoding,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

def patch_file(relative_path: str, old: str, new: str) -> dict:
    """Replace an exact string in a file.

    Fails if file not found, old string not found, or old string appears
    more than once (ambiguous). Provides a fuzzy hint when old not found.
    """
    import swarm.tools.core as _core
    from swarm.tools.path_guard import _protected_path_reason, _protected_path_error, _resolve_project_path

    if _core.READONLY:
        return {"ok": False, "error": "Read-only task: patch_file is disabled"}
    if _core.TASK_TYPE in ("python_plan", "plan"):
        return {"ok": False, "error": "plan tasks are read-only planners -- use create_task() to delegate implementation"}
    if str(relative_path).startswith("res://"):
        return {"ok": False, "error": f"'res://' is a Godot virtual path, not a filesystem path. Use the real path instead: {str(relative_path).replace('res://', '<project_dir>/')}"}

    path = _resolve_project_path(relative_path)
    protected_reason = _protected_path_reason(path)
    if protected_reason:
        return _protected_path_error(relative_path, protected_reason)

    if not _core.os.path.exists(path):
        return {"ok": False, "error": "file not found"}

    try:
        content, _encoding = _core._read_text_with_fallback(path)

        count = content.count(old)

        if count == 0:
            import difflib
            old_first = old.splitlines()[0].strip() if old else ""
            lines = content.splitlines()
            best_ratio, best_line_no, best_lines = 0.0, -1, []
            for i, line in enumerate(lines):
                r = difflib.SequenceMatcher(None, old_first, line.strip()).ratio()
                if r > best_ratio:
                    best_ratio, best_line_no = r, i + 1
                    best_lines = lines[max(0, i - 1):i + 3]
            hint = ""
            if best_ratio > 0.5 and best_line_no >= 0:
                preview = "\n".join(f"  {l}" for l in best_lines)
                hint = f" Closest match at line {best_line_no} (similarity {best_ratio:.0%}):\n{preview}"
            return {"ok": False, "error": f"string not found in {relative_path}.{hint}", "searched_chars": len(content)}


        if count > 1:
            return {"ok": False, "error": f"ambiguous: found {count} occurrences"}

        new_content = _core._sanitize_text(content.replace(old, new, 1))

        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(new_content)

        return {"ok": True, "replaced": 1}
    except Exception as e:
        return {"ok": False, "error": str(e)}



def write_file(path_arg: str, content: str) -> dict:
    """Write content to a file, creating parent directories as needed."""
    import swarm.tools.core as _core
    from swarm.tools.path_guard import _protected_path_reason, _protected_path_error, _resolve_project_path

    if _core.READONLY:
        return {"ok": False, "error": "Read-only task: write_file is disabled"}
    if _core.TASK_TYPE in ("python_plan", "plan"):
        return {"ok": False, "error": "plan tasks are read-only planners -- use create_task() to delegate implementation"}
    if "res://" in str(path_arg):
        return {"ok": False, "error": f"'res://' is a Godot virtual path, not a filesystem path. Use the real path instead, e.g. the project directory + the relative path after 'res://': {str(path_arg).replace('res://', '<project_dir>/')}"}
    path = _resolve_project_path(path_arg)
    protected_reason = _protected_path_reason(path)
    if protected_reason:
        return _protected_path_error(path_arg, protected_reason)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(_core._sanitize_text(content), encoding='utf-8', newline="\n")
        return {"ok": True, "path": str(path)}
    except Exception as e:
        return {"ok": False, "error": str(e)}



def append_file(path_arg: str, content: str) -> dict:
    """Append content to a file without reading/rewriting the whole thing.

    Creates the file if it does not exist (including parent directories).
    Appends a newline before content if the file is non-empty and doesn't end in newline.
    """
    import swarm.tools.core as _core
    from swarm.tools.path_guard import _protected_path_reason, _protected_path_error, _resolve_project_path

    if _core.READONLY:
        return {"ok": False, "error": "Read-only task: append_file is disabled"}
    if "res://" in str(path_arg):
        return {"ok": False, "error": f"'res://' is a Godot virtual path, not a filesystem path. Use the real path instead: {str(path_arg).replace('res://', '<project_dir>/')}"}

    path = _resolve_project_path(path_arg)
    protected_reason = _protected_path_reason(path)
    if protected_reason:
        return _protected_path_error(path_arg, protected_reason)

    path.parent.mkdir(parents=True, exist_ok=True)

    add_newline = False
    if path.exists() and path.stat().st_size > 0:
        with open(path, 'rb') as f:
            f.seek(-1, 2)
            last_byte = f.read(1)
            if last_byte != b"\n":
                add_newline = True

    with open(path, 'a', encoding='utf-8', newline='\n') as f:
        if add_newline:
            f.write("\n")
        f.write(content)

    bytes_written = len(content) + (1 if add_newline else 0)
    return {"ok": True, "bytes_written": bytes_written}
