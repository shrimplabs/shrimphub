"""
SQLite Storage Layer

Thread-safe SQLite storage for tasks, projects, and agents.
Uses WAL mode for safe concurrent reads from multiple threads.

Usage:
    from swarm import db
    db.init(Path("data/swarm.db"), migrate_from={
        "tasks":    Path("data/task-queue.json"),
        "projects": Path("data/projects.json"),
        "agents":   Path("data/agents.json"),
    })
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

_db_path: Optional[Path] = None
_local = threading.local()


def _json_dumps(value, default):
    if value is None:
        value = default
    return json.dumps(value)


def _json_loads(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _encode_deps(deps) -> str:
    """Serialize dependencies to JSON string, handling already-serialized strings."""
    if isinstance(deps, str):
        # Guard against double-encoding: if it's already a JSON string, decode then re-encode
        try:
            parsed = json.loads(deps)
            return json.dumps(parsed if isinstance(parsed, list) else [])
        except (json.JSONDecodeError, ValueError):
            return "[]"
    return json.dumps(deps if isinstance(deps, list) else [])


def _normalize_deps_list(raw) -> List[str]:
    """Normalize dependencies to a deduped list of non-empty strings."""
    if raw is None:
        return []
    if isinstance(raw, str):
        deps_str = raw.strip()
        if not deps_str:
            return []
        if deps_str.startswith("["):
            try:
                raw = json.loads(deps_str)
            except Exception:
                raw = [d.strip().strip('"\'') for d in deps_str.strip("[]").split(",") if d.strip().strip('"\'')]
        else:
            raw = [d.strip().strip('"\'') for d in deps_str.split(",") if d.strip()]
    if not isinstance(raw, list):
        raise ValueError("dependencies must be a list (or list-like string)")

    out: List[str] = []
    seen = set()
    for d in raw:
        if not isinstance(d, str):
            continue
        dep = d.strip()
        if not dep or dep in seen:
            continue
        seen.add(dep)
        out.append(dep)
    return out


def _would_introduce_cycle(task_id: str, new_deps: List[str]) -> bool:
    """Check whether setting task_id dependencies to new_deps would introduce a cycle."""
    rows = _connect().execute("SELECT id, dependencies FROM tasks").fetchall()
    graph: Dict[str, List[str]] = {}
    for r in rows:
        tid = r["id"]
        try:
            deps = _normalize_deps_list(r["dependencies"])
        except Exception:
            deps = []
        graph[tid] = deps

    graph[task_id] = list(new_deps)

    visiting = set()
    visited = set()

    def _dfs(node_id: str) -> bool:
        visiting.add(node_id)
        for dep in graph.get(node_id, []):
            if dep not in graph:
                continue  # missing dep doesn't form a cycle in known graph
            if dep in visiting:
                return True
            if dep not in visited and _dfs(dep):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    for nid in list(graph.keys()):
        if nid not in visited and _dfs(nid):
            return True
    return False


def _validate_task_dependencies(task_id: str, deps: List[str]) -> List[str]:
    deps = _normalize_deps_list(deps)
    if task_id in deps:
        raise ValueError("Task cannot depend on itself")
    if _would_introduce_cycle(task_id, deps):
        raise ValueError("Dependency cycle detected")
    return deps

_init_lock = threading.Lock()
_task_write_lock = threading.Lock()
_initialized = False


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def init(db_path: Path, migrate_from: Optional[Dict[str, Path]] = None):
    """
    Initialise the database.  Safe to call multiple times.

    Args:
        db_path:      Path to the SQLite file (created if absent).
        migrate_from: Optional dict mapping 'tasks'/'projects'/'agents'
                      to their legacy JSON files; data is imported once.
    """
    global _db_path, _initialized
    with _init_lock:
        _db_path = Path(db_path)
        _db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect()
        _create_schema(conn)
        if migrate_from and not _initialized:
            _migrate(conn, migrate_from)
        _initialized = True


def _connect() -> sqlite3.Connection:
    """Return the thread-local SQLite connection, creating it if needed."""
    if getattr(_local, "conn", None) is None:
        if _db_path is None:
            raise RuntimeError("swarm.db.init() must be called before use")
        conn = sqlite3.connect(str(_db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL allows concurrent readers; NORMAL sync is safe + fast
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def _create_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id           TEXT PRIMARY KEY,
            project      TEXT NOT NULL,
            type         TEXT NOT NULL DEFAULT 'feature',
            description  TEXT DEFAULT '',
            priority     INTEGER DEFAULT 50,
            status       TEXT DEFAULT 'pending',
            created      TEXT,
            started      TEXT,
            completed    TEXT,
            agent_id     TEXT,
            dependencies TEXT DEFAULT '[]',
            metadata         TEXT DEFAULT '{}',
            acceptance_test  TEXT DEFAULT '{}',
            attempts         INTEGER DEFAULT 0,
            max_attempts     INTEGER DEFAULT 3,
            run_after        TEXT,
            plan_id          TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);

        CREATE TABLE IF NOT EXISTS projects (
            name            TEXT PRIMARY KEY,
            status          TEXT DEFAULT 'active',
            managed         INTEGER DEFAULT 1,
            locked          INTEGER DEFAULT 0,
            locked_at       TEXT,
            unlocked_at     TEXT,
            last_update     TEXT,
            files           TEXT DEFAULT '{}',
            sprint          INTEGER DEFAULT 0,
            recent_commits  TEXT DEFAULT '[]',
            last_commit     TEXT,
            last_commit_msg TEXT,
            file_locks      TEXT DEFAULT '{}',
            profile         TEXT,
            notes           TEXT DEFAULT '',
            head_task_id    TEXT,
            closure_mode    TEXT DEFAULT 'build',
            closure_status  TEXT DEFAULT 'yellow',
            closure_spec    TEXT DEFAULT '{}',
            last_verification_at TEXT,
            last_verification_status TEXT,
            open_regression_count INTEGER DEFAULT 0,
            stall_count     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS verification_runs (
            id              TEXT PRIMARY KEY,
            project         TEXT NOT NULL,
            trigger_task_id TEXT,
            run_type        TEXT NOT NULL,
            status          TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            started_at      TEXT,
            completed_at    TEXT,
            results_json    TEXT DEFAULT '{}',
            artifacts_json  TEXT DEFAULT '{}',
            fingerprints_json TEXT DEFAULT '[]',
            metadata_json   TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_verification_runs_project ON verification_runs(project);
        CREATE INDEX IF NOT EXISTS idx_verification_runs_created_at ON verification_runs(created_at);
        CREATE INDEX IF NOT EXISTS idx_verification_runs_status ON verification_runs(status);

        CREATE TABLE IF NOT EXISTS regressions (
            id              TEXT PRIMARY KEY,
            project         TEXT NOT NULL,
            fingerprint     TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'open',
            severity        TEXT DEFAULT 'medium',
            first_seen_at   TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            occurrences     INTEGER DEFAULT 1,
            source_run_id   TEXT,
            linked_task_id  TEXT,
            details_json    TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_regressions_project ON regressions(project);
        CREATE INDEX IF NOT EXISTS idx_regressions_project_status ON regressions(project, status);
        CREATE INDEX IF NOT EXISTS idx_regressions_fingerprint ON regressions(fingerprint);

        CREATE TABLE IF NOT EXISTS agents (
            id           TEXT PRIMARY KEY,
            project      TEXT,
            task_type    TEXT,
            status       TEXT DEFAULT 'spawning',
            spawned_at   TEXT,
            completed_at TEXT,
            pid          INTEGER,
            exit_code    INTEGER,
            task_id      TEXT,
            log_path     TEXT,
            script_path  TEXT,
            output       TEXT DEFAULT '',
            metadata     TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);

        CREATE TABLE IF NOT EXISTS plans (
            id          TEXT PRIMARY KEY,
            project     TEXT NOT NULL,
            planner_task_id TEXT,
            created_at  TEXT,
            task_ids    TEXT DEFAULT '[]',
            task_graph  TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_plans_project ON plans(project);

        CREATE TABLE IF NOT EXISTS completed_task_ids (
            id           TEXT PRIMARY KEY,
            project      TEXT,
            completed_at TEXT
        );
    """)
    conn.commit()
    _evolve_schema(conn)


def _evolve_schema(conn: sqlite3.Connection):
    """Add new columns to existing tables without breaking older DBs."""
    existing_tasks = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    for col, ddl in [
        ("attempts",     "ALTER TABLE tasks ADD COLUMN attempts     INTEGER DEFAULT 0"),
        ("max_attempts", "ALTER TABLE tasks ADD COLUMN max_attempts INTEGER DEFAULT 3"),
        ("run_after",    "ALTER TABLE tasks ADD COLUMN run_after    TEXT"),
        ("plan_id",      "ALTER TABLE tasks ADD COLUMN plan_id      TEXT"),
        ("acceptance_test", "ALTER TABLE tasks ADD COLUMN acceptance_test TEXT DEFAULT '{}'"),
    ]:
        if col not in existing_tasks:
            conn.execute(ddl)
    # Index for plan_id -- created after column is guaranteed to exist
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_plan_id ON tasks(plan_id)")
    existing_projects = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "notes" not in existing_projects:
        conn.execute("ALTER TABLE projects ADD COLUMN notes TEXT DEFAULT ''")
    if "head_task_id" not in existing_projects:
        conn.execute("ALTER TABLE projects ADD COLUMN head_task_id TEXT")
    if "managed" not in existing_projects:
        conn.execute("ALTER TABLE projects ADD COLUMN managed INTEGER DEFAULT 1")
    for col, ddl in [
        ("closure_mode", "ALTER TABLE projects ADD COLUMN closure_mode TEXT DEFAULT 'build'"),
        ("closure_status", "ALTER TABLE projects ADD COLUMN closure_status TEXT DEFAULT 'yellow'"),
        ("closure_spec", "ALTER TABLE projects ADD COLUMN closure_spec TEXT DEFAULT '{}'"),
        ("last_verification_at", "ALTER TABLE projects ADD COLUMN last_verification_at TEXT"),
        ("last_verification_status", "ALTER TABLE projects ADD COLUMN last_verification_status TEXT"),
        ("open_regression_count", "ALTER TABLE projects ADD COLUMN open_regression_count INTEGER DEFAULT 0"),
        ("stall_count", "ALTER TABLE projects ADD COLUMN stall_count INTEGER DEFAULT 0"),
    ]:
        if col not in existing_projects:
            conn.execute(ddl)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS verification_runs (
            id              TEXT PRIMARY KEY,
            project         TEXT NOT NULL,
            trigger_task_id TEXT,
            run_type        TEXT NOT NULL,
            status          TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            started_at      TEXT,
            completed_at    TEXT,
            results_json    TEXT DEFAULT '{}',
            artifacts_json  TEXT DEFAULT '{}',
            fingerprints_json TEXT DEFAULT '[]',
            metadata_json   TEXT DEFAULT '{}'
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_verification_runs_project ON verification_runs(project)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_verification_runs_created_at ON verification_runs(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_verification_runs_status ON verification_runs(status)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS regressions (
            id              TEXT PRIMARY KEY,
            project         TEXT NOT NULL,
            fingerprint     TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'open',
            severity        TEXT DEFAULT 'medium',
            first_seen_at   TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            occurrences     INTEGER DEFAULT 1,
            source_run_id   TEXT,
            linked_task_id  TEXT,
            details_json    TEXT DEFAULT '{}'
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_regressions_project ON regressions(project)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_regressions_project_status ON regressions(project, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_regressions_fingerprint ON regressions(fingerprint)")
    existing_regressions = {row[1] for row in conn.execute("PRAGMA table_info(regressions)").fetchall()}
    for col, ddl in [
        ("resolved_at",        "ALTER TABLE regressions ADD COLUMN resolved_at TEXT"),
        ("resolved_by_run_id", "ALTER TABLE regressions ADD COLUMN resolved_by_run_id TEXT"),
    ]:
        if col not in existing_regressions:
            conn.execute(ddl)
    existing_agents = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
    for col, ddl in [
        ("input_tokens",  "ALTER TABLE agents ADD COLUMN input_tokens  INTEGER DEFAULT 0"),
        ("output_tokens", "ALTER TABLE agents ADD COLUMN output_tokens INTEGER DEFAULT 0"),
        ("loop_count",    "ALTER TABLE agents ADD COLUMN loop_count    INTEGER DEFAULT 0"),
    ]:
        if col not in existing_agents:
            conn.execute(ddl)
    existing_plans = {row[1] for row in conn.execute("PRAGMA table_info(plans)").fetchall()}
    if "planner_task_id" not in existing_plans:
        conn.execute("ALTER TABLE plans ADD COLUMN planner_task_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_planner_task_id ON plans(planner_task_id)")
    conn.commit()


def _migrate(conn: sqlite3.Connection, json_files: Dict[str, Path]):
    """Import JSON data into the database (runs only when tables are empty)."""
    cursor = conn.execute("SELECT COUNT(*) FROM tasks")
    if cursor.fetchone()[0] > 0:
        return  # already populated, skip

    # --- tasks ---
    tasks_file = json_files.get("tasks")
    if tasks_file and Path(tasks_file).exists():
        try:
            data = json.loads(Path(tasks_file).read_text())
            for t in data.get("tasks", []):
                conn.execute(
                    """INSERT OR IGNORE INTO tasks
                       (id,project,type,description,priority,status,
                        created,started,completed,agent_id,dependencies,metadata)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        t.get("id", ""),
                        t.get("project", ""),
                        t.get("type", "feature"),
                        t.get("description", ""),
                        t.get("priority", 50),
                        t.get("status", "pending"),
                        t.get("created"),
                        t.get("started"),
                        t.get("completed"),
                        t.get("agent_id"),
                        _encode_deps(t.get("dependencies", [])),
                        json.dumps(t.get("metadata", {})),
                    ),
                )
            print(f"[DB] Migrated {len(data.get('tasks', []))} tasks from {tasks_file}")
        except Exception as e:
            print(f"[DB] Migration warning (tasks): {e}")

    # --- projects ---
    projects_file = json_files.get("projects")
    if projects_file and Path(projects_file).exists():
        try:
            data = json.loads(Path(projects_file).read_text())
            for name, p in data.get("projects", {}).items():
                conn.execute(
                    """INSERT OR IGNORE INTO projects
                       (name,status,locked,locked_at,unlocked_at,last_update,
                        files,sprint,recent_commits,last_commit,last_commit_msg,
                        file_locks,profile)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        name,
                        p.get("status", "active"),
                        1 if p.get("locked", False) else 0,
                        p.get("locked_at"),
                        p.get("unlocked_at"),
                        p.get("last_update"),
                        json.dumps(p.get("files", {})),
                        p.get("sprint", 0),
                        json.dumps(p.get("recent_commits", [])),
                        p.get("last_commit"),
                        p.get("last_commit_msg"),
                        json.dumps(p.get("file_locks", {})),
                        p.get("profile"),
                    ),
                )
            print(f"[DB] Migrated {len(data.get('projects', {}))} projects from {projects_file}")
        except Exception as e:
            print(f"[DB] Migration warning (projects): {e}")

    # --- agents ---
    agents_file = json_files.get("agents")
    if agents_file and Path(agents_file).exists():
        try:
            data = json.loads(Path(agents_file).read_text())
            for a in data.get("agents", []):
                conn.execute(
                    """INSERT OR IGNORE INTO agents
                       (id,project,task_type,status,spawned_at,completed_at,
                        pid,exit_code,task_id,log_path,script_path,output,metadata)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        a.get("id", ""),
                        a.get("project"),
                        a.get("task_type"),
                        a.get("status", "active"),
                        a.get("spawned_at"),
                        a.get("completed_at"),
                        a.get("pid"),
                        a.get("exit_code"),
                        a.get("task_id"),
                        a.get("log_path"),
                        a.get("script_path"),
                        (a.get("output") or "")[-2000:],
                        json.dumps(a.get("metadata", {})),
                    ),
                )
            print(f"[DB] Migrated {len(data.get('agents', []))} agents from {agents_file}")
        except Exception as e:
            print(f"[DB] Migration warning (agents): {e}")

    conn.commit()


# ---------------------------------------------------------------------------
# Task operations
# ---------------------------------------------------------------------------

def task_get_all(exclude_statuses: tuple = ()) -> List[Dict]:
    if exclude_statuses:
        placeholders = ",".join("?" * len(exclude_statuses))
        rows = _connect().execute(
            f"SELECT * FROM tasks WHERE status NOT IN ({placeholders}) ORDER BY priority DESC, created ASC",
            exclude_statuses,
        ).fetchall()
    else:
        rows = _connect().execute(
            "SELECT * FROM tasks ORDER BY priority DESC, created ASC"
        ).fetchall()
    return [_task_row(r) for r in rows]


def task_get(task_id: str) -> Optional[Dict]:
    row = _connect().execute(
        "SELECT * FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    return _task_row(row) if row else None


def task_get_by_status(status: str) -> List[Dict]:
    rows = _connect().execute(
        "SELECT * FROM tasks WHERE status=? ORDER BY priority DESC, created ASC",
        (status,),
    ).fetchall()
    return [_task_row(r) for r in rows]


def task_get_by_project(project: str) -> List[Dict]:
    rows = _connect().execute(
        "SELECT * FROM tasks WHERE project=? ORDER BY priority DESC",
        (project,),
    ).fetchall()
    return [_task_row(r) for r in rows]


def task_upsert(task: Dict) -> str:
    with _task_write_lock:
        conn = _connect()
        task_id = task["id"]
        deps = _validate_task_dependencies(task_id, task.get("dependencies", []))
        project = task["project"]
        status = task.get("status", "pending")
        conn.execute(
            """INSERT INTO tasks
               (id,project,type,description,priority,status,
                created,started,completed,agent_id,dependencies,metadata,
                acceptance_test,attempts,max_attempts,run_after,plan_id)
               VALUES (:id,:project,:type,:description,:priority,:status,
                       :created,:started,:completed,:agent_id,:dependencies,:metadata,
                       :acceptance_test,:attempts,:max_attempts,:run_after,:plan_id)
               ON CONFLICT(id) DO UPDATE SET
                   project=excluded.project, type=excluded.type,
                   description=excluded.description, priority=excluded.priority,
                   status=excluded.status, started=excluded.started,
                   completed=excluded.completed, agent_id=excluded.agent_id,
                   dependencies=excluded.dependencies, metadata=excluded.metadata,
                   acceptance_test=excluded.acceptance_test,
                   attempts=excluded.attempts, max_attempts=excluded.max_attempts,
                   run_after=excluded.run_after, plan_id=excluded.plan_id""",
            {
                "id":           task_id,
                "project":      project,
                "type":         task.get("type", "feature"),
                "description":  task.get("description", ""),
                "priority":     task.get("priority", 50),
                "status":       status,
                "created":      task.get("created", datetime.now().isoformat()),
                "started":      task.get("started"),
                "completed":    task.get("completed"),
                "agent_id":     task.get("agent_id"),
                "dependencies": _encode_deps(deps),
                "metadata":     json.dumps(task.get("metadata", {})),
                "acceptance_test": json.dumps(task.get("acceptance_test", {})),
                "attempts":     task.get("attempts", 0),
                "max_attempts": task.get("max_attempts", 3),
                "run_after":    task.get("run_after"),
                "plan_id":      task.get("plan_id"),
            },
        )
        if status == "completed":
            conn.execute(
                "INSERT OR IGNORE INTO completed_task_ids (id, project, completed_at) VALUES (?, ?, ?)",
                (task_id, project, task.get("completed") or datetime.now().isoformat()),
            )
        conn.commit()
        return task_id


def task_delete(task_id: str) -> bool:
    conn = _connect()
    cur = conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    return cur.rowcount > 0


def _apply_status_invariants(conn: sqlite3.Connection, task_id: str, fields: Dict) -> tuple[Optional[str], Optional[str]]:
    """Normalize task lifecycle columns whenever a status transition is written.

    The scheduler treats ``pending`` as unclaimed work. A pending task carrying a
    stale completion timestamp or agent owner can look complete in history views
    while still being selectable, so normalize those columns at the DB boundary.
    """
    status = fields.get("status")
    completed_project = None
    completed_at = None
    if not status:
        return completed_project, completed_at

    current = conn.execute(
        "SELECT project, started, completed FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()

    if status == "pending":
        fields["started"] = None
        fields["completed"] = None
        fields["agent_id"] = None
    elif status == "in_progress":
        fields["completed"] = None
        if not fields.get("started"):
            fields["started"] = (current["started"] if current else None) or datetime.now().isoformat()
    elif status in {"completed", "failed", "cancelled"}:
        completed_at = fields.get("completed") or (current["completed"] if current else None) or datetime.now().isoformat()
        fields["completed"] = completed_at
        if status == "completed":
            completed_project = fields.get("project") or (current["project"] if current else "")

    return completed_project, completed_at


def task_update(task_id: str, fields: dict):
    """Partial update of any task columns by dict."""
    import json as _json
    with _task_write_lock:
        conn = _connect()
        row = {}
        for k, v in fields.items():
            if k == "metadata" and isinstance(v, dict):
                row[k] = _json.dumps(v)
            elif k == "acceptance_test" and isinstance(v, dict):
                row[k] = _json.dumps(v)
            elif k == "dependencies":
                deps = _validate_task_dependencies(task_id, v)
                row[k] = _encode_deps(deps)
            else:
                row[k] = v
        completed_project, completed_at = _apply_status_invariants(conn, task_id, row)
        set_sql = ", ".join(f"{k}=:{k}" for k in row)
        row["_id"] = task_id
        conn.execute(f"UPDATE tasks SET {set_sql} WHERE id=:_id", row)
        if completed_project is not None:
            conn.execute(
                "INSERT OR IGNORE INTO completed_task_ids (id, project, completed_at) VALUES (?, ?, ?)",
                (task_id, completed_project, completed_at),
            )
        elif row.get("status") and row.get("status") != "completed":
            conn.execute("DELETE FROM completed_task_ids WHERE id=?", (task_id,))
        conn.commit()


def task_update_status(task_id: str, status: str, **kwargs):
    """Update task status plus any extra columns passed as kwargs.

    When kwargs includes 'dependencies', it is normalised and validated
    (cycle / self-reference checks) before the write - the same path as
    task_update() and task_upsert().
    """
    import json as _json
    with _task_write_lock:
        conn = _connect()
        fields = {"status": status}
        for k, v in kwargs.items():
            if k == "metadata" and isinstance(v, dict):
                fields[k] = _json.dumps(v)
            elif k == "dependencies":
                deps = _validate_task_dependencies(task_id, v)
                fields[k] = _encode_deps(deps)
            else:
                fields[k] = v
        completed_project, completed_at = _apply_status_invariants(conn, task_id, fields)
        set_sql = ", ".join(f"{k}=:{k}" for k in fields)
        fields["_id"] = task_id
        conn.execute(f"UPDATE tasks SET {set_sql} WHERE id=:_id", fields)
        if completed_project is not None:
            conn.execute(
                "INSERT OR IGNORE INTO completed_task_ids (id, project, completed_at) VALUES (?, ?, ?)",
                (task_id, completed_project, completed_at),
            )
        elif fields.get("status") and fields.get("status") != "completed":
            conn.execute("DELETE FROM completed_task_ids WHERE id=?", (task_id,))
        conn.commit()


def task_record_completed(task_id: str, project: str = ""):
    """Permanently record a completed task ID so dependency checks survive pruning."""
    conn = _connect()
    conn.execute(
        "INSERT OR IGNORE INTO completed_task_ids (id, project, completed_at) VALUES (?, ?, ?)",
        (task_id, project, datetime.now().isoformat()),
    )
    conn.commit()


def backfill_completed_task_ids() -> list[str]:
    """Archive any live completed tasks missing from completed_task_ids."""
    conn = _connect()
    rows = conn.execute(
        """
        SELECT t.id, t.project, COALESCE(t.completed, t.created, ?) AS completed_at
        FROM tasks t
        LEFT JOIN completed_task_ids c ON c.id = t.id
        WHERE t.status='completed' AND c.id IS NULL
        """,
        (datetime.now().isoformat(),),
    ).fetchall()
    if not rows:
        return []
    inserted: list[str] = []
    for row in rows:
        conn.execute(
            "INSERT OR IGNORE INTO completed_task_ids (id, project, completed_at) VALUES (?, ?, ?)",
            (row["id"], row["project"] or "", row["completed_at"]),
        )
        inserted.append(row["id"])
    conn.commit()
    return inserted


def task_get_completed_ids() -> set:
    """Return all ever-completed task IDs.

    Now that completed tasks stay in the tasks table, this is the union of:
    - completed tasks in the live tasks table (primary source)
    - legacy completed_task_ids shadow table (pre-migration fallback)
    """
    conn = _connect()
    rows = conn.execute("SELECT id FROM tasks WHERE status='completed'").fetchall()
    ids = {r["id"] for r in rows}
    # DEPRECATED: legacy fallback for pre-migration completed tasks.
    # The completed_task_ids shadow table is redundant now that completed tasks
    # stay in the tasks table. Remove this fallback after 2025-07-01.
    rows2 = conn.execute("SELECT id FROM completed_task_ids").fetchall()
    ids |= {r["id"] for r in rows2}
    return ids


def task_get_completed_record(task_id: str) -> Optional[Dict]:
    """Return the archival completed-task record when a live task row no longer exists."""
    row = _connect().execute(
        "SELECT id, project, completed_at FROM completed_task_ids WHERE id=?",
        (task_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "project": row["project"],
        "status": "completed",
        "completed": row["completed_at"],
    }


def repair_malformed_task_rows() -> list[str]:
    """Repair obviously-invalid task rows that can poison scheduling.

    Current repair rules:
    - pending + completed timestamp => clear completed
    - pending + any agent_id => clear agent_id
    - pending + started timestamp => clear started
    """
    conn = _connect()
    rows = conn.execute(
        "SELECT id, project, status, started, completed, agent_id FROM tasks"
    ).fetchall()
    repaired: list[str] = []
    for row in rows:
        task_id = row["id"]
        status = row["status"] or ""
        started = row["started"]
        completed = row["completed"]
        agent_id = row["agent_id"]
        project = row["project"] or ""

        updates: dict[str, object] = {}

        if status == "pending" and completed:
            updates["completed"] = None
        if status == "pending" and agent_id:
            updates["agent_id"] = None
        if status == "pending" and started:
            updates["started"] = None

        if updates:
            sets = ", ".join(f"{k}=?" for k in updates.keys())
            conn.execute(
                f"UPDATE tasks SET {sets} WHERE id=?",
                tuple(updates.values()) + (task_id,),
            )
            if status == "pending" and "completed" in updates:
                conn.execute("DELETE FROM completed_task_ids WHERE id=?", (task_id,))
            repaired.append(task_id)

    if repaired:
        conn.commit()
    return repaired


def _task_row(row) -> Dict:
    d = dict(row)
    d["dependencies"]      = json.loads(d.get("dependencies") or "[]")
    d["metadata"]         = json.loads(d.get("metadata") or "{}")
    d["acceptance_test"]  = json.loads(d.get("acceptance_test") or "{}")
    return d


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

def _plan_row(row) -> Dict:
    d = dict(row)
    d["task_ids"]   = json.loads(d.get("task_ids") or "[]")
    d["task_graph"] = json.loads(d.get("task_graph") or "{}")
    return d


def plan_upsert(plan: Dict) -> str:
    conn = _connect()
    conn.execute(
        """INSERT INTO plans (id,project,planner_task_id,created_at,task_ids,task_graph)
           VALUES (:id,:project,:planner_task_id,:created_at,:task_ids,:task_graph)
           ON CONFLICT(id) DO UPDATE SET
               project=excluded.project, planner_task_id=excluded.planner_task_id,
               created_at=excluded.created_at,
               task_ids=excluded.task_ids, task_graph=excluded.task_graph""",
        {
            "id":              plan["id"],
            "project":         plan["project"],
            "planner_task_id": plan.get("planner_task_id"),
            "created_at":      plan.get("created_at", datetime.now().isoformat()),
            "task_ids":        json.dumps(plan.get("task_ids", [])),
            "task_graph":      json.dumps(plan.get("task_graph", {})),
        },
    )
    conn.commit()
    return plan["id"]


def plan_get(plan_id: str) -> Optional[Dict]:
    row = _connect().execute(
        "SELECT * FROM plans WHERE id=?", (plan_id,)
    ).fetchone()
    return _plan_row(row) if row else None


def plan_get_by_project(project: str) -> List[Dict]:
    rows = _connect().execute(
        "SELECT * FROM plans WHERE project=? ORDER BY created_at DESC", (project,)
    ).fetchall()
    return [_plan_row(r) for r in rows]


def plan_get_all() -> List[Dict]:
    rows = _connect().execute(
        "SELECT * FROM plans ORDER BY created_at DESC"
    ).fetchall()
    return [_plan_row(r) for r in rows]


def plan_delete(plan_id: str) -> bool:
    conn = _connect()
    cur = conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Project operations
# ---------------------------------------------------------------------------

def project_get_all() -> Dict[str, Dict]:
    rows = _connect().execute("SELECT * FROM projects").fetchall()
    return {r["name"]: _project_row(r) for r in rows}


def project_get(name: str) -> Optional[Dict]:
    row = _connect().execute(
        "SELECT * FROM projects WHERE name=?", (name,)
    ).fetchone()
    return _project_row(row) if row else None


def project_upsert(project: Dict):
    existing = project_get(project["name"]) or {}
    conn = _connect()
    conn.execute(
        """INSERT INTO projects
           (name,status,managed,locked,locked_at,unlocked_at,last_update,
            files,sprint,recent_commits,last_commit,last_commit_msg,
            file_locks,profile,head_task_id,closure_mode,closure_status,closure_spec,
            last_verification_at,last_verification_status,open_regression_count,stall_count)
           VALUES (:name,:status,:managed,:locked,:locked_at,:unlocked_at,:last_update,
                   :files,:sprint,:recent_commits,:last_commit,:last_commit_msg,
                   :file_locks,:profile,:head_task_id,:closure_mode,:closure_status,:closure_spec,
                   :last_verification_at,:last_verification_status,:open_regression_count,:stall_count)
           ON CONFLICT(name) DO UPDATE SET
               status=excluded.status, managed=excluded.managed, locked=excluded.locked,
               locked_at=excluded.locked_at, unlocked_at=excluded.unlocked_at,
               last_update=excluded.last_update, files=excluded.files,
               sprint=excluded.sprint, recent_commits=excluded.recent_commits,
               last_commit=excluded.last_commit, last_commit_msg=excluded.last_commit_msg,
               file_locks=excluded.file_locks, profile=excluded.profile,
               head_task_id=excluded.head_task_id, closure_mode=excluded.closure_mode,
               closure_status=excluded.closure_status, closure_spec=excluded.closure_spec,
               last_verification_at=excluded.last_verification_at,
               last_verification_status=excluded.last_verification_status,
               open_regression_count=excluded.open_regression_count,
               stall_count=excluded.stall_count""",
        {
            "name":            project["name"],
            "status":          project.get("status", "active"),
            "managed":         1 if project.get("managed", True) else 0,
            "locked":          1 if project.get("locked", False) else 0,
            "locked_at":       project.get("locked_at"),
            "unlocked_at":     project.get("unlocked_at"),
            "last_update":     project.get("last_update"),
            "files":           json.dumps(project.get("files", {})),
            "sprint":          project.get("sprint", 0),
            "recent_commits":  json.dumps(project.get("recent_commits", [])),
            "last_commit":     project.get("last_commit"),
            "last_commit_msg": project.get("last_commit_msg"),
            "file_locks":      json.dumps(project.get("file_locks", {})),
            "profile":         project.get("profile", existing.get("profile")),
            "head_task_id":    project.get("head_task_id", existing.get("head_task_id")),
            "closure_mode":    project.get("closure_mode", existing.get("closure_mode", "build")),
            "closure_status":  project.get("closure_status", existing.get("closure_status", "yellow")),
            "closure_spec":    json.dumps(project.get("closure_spec", existing.get("closure_spec", {}))),
            "last_verification_at": project.get("last_verification_at", existing.get("last_verification_at")),
            "last_verification_status": project.get("last_verification_status", existing.get("last_verification_status")),
            "open_regression_count": int(project.get("open_regression_count", existing.get("open_regression_count", 0)) or 0),
            "stall_count":     int(project.get("stall_count", existing.get("stall_count", 0)) or 0),
        },
    )
    conn.commit()


def project_update(name: str, fields: Dict):
    conn = _connect()
    row = {}
    for key, value in fields.items():
        if key in {"files", "file_locks"} and isinstance(value, dict):
            row[key] = json.dumps(value)
        elif key == "recent_commits" and isinstance(value, list):
            row[key] = json.dumps(value)
        elif key == "closure_spec" and isinstance(value, (dict, list)):
            row[key] = json.dumps(value)
        elif key in {"managed", "locked"}:
            row[key] = 1 if value else 0
        else:
            row[key] = value
    set_sql = ", ".join(f"{k}=:{k}" for k in row)
    row["_name"] = name
    conn.execute(f"UPDATE projects SET {set_sql} WHERE name=:_name", row)
    conn.commit()


def repair_project_lock_rows() -> list[str]:
    """Repair obviously-corrupted project lock rows caused by older column-order bugs."""
    conn = _connect()
    rows = conn.execute("SELECT name, locked, locked_at, unlocked_at FROM projects").fetchall()
    repaired: list[str] = []
    for row in rows:
        name = row["name"]
        locked = int(row["locked"] or 0)
        locked_at = row["locked_at"]
        unlocked_at = row["unlocked_at"]

        new_locked = locked
        new_locked_at = locked_at
        new_unlocked_at = unlocked_at
        changed = False

        # Corrupted values were showing up as literal "0"/"1" in timestamp columns.
        if isinstance(new_locked_at, str) and new_locked_at in {"0", "1"}:
            new_locked_at = None
            changed = True
        if isinstance(new_unlocked_at, str) and new_unlocked_at in {"0", "1"}:
            new_unlocked_at = None
            changed = True

        # If the project is unlocked, stale lock timestamps should not keep it looking active.
        if not new_locked and new_locked_at:
            new_locked_at = None
            changed = True

        # If the project is marked locked but has no real lock timestamp, unlock it.
        if new_locked and not new_locked_at:
            new_locked = 0
            changed = True

        if changed:
            conn.execute(
                "UPDATE projects SET locked=?, locked_at=?, unlocked_at=? WHERE name=?",
                (new_locked, new_locked_at, new_unlocked_at, name),
            )
            repaired.append(name)

    if repaired:
        conn.commit()
    return repaired


def project_set_locked(name: str, locked: bool):
    conn = _connect()
    ts = datetime.now().isoformat()
    if locked:
        conn.execute(
            "UPDATE projects SET locked=1, locked_at=? WHERE name=?", (ts, name)
        )
    else:
        conn.execute(
            "UPDATE projects SET locked=0, unlocked_at=? WHERE name=?", (ts, name)
        )
    conn.commit()


def project_get_notes(name: str) -> str:
    row = _connect().execute("SELECT notes FROM projects WHERE name=?", (name,)).fetchone()
    return (row["notes"] or "") if row else ""


def project_set_notes(name: str, notes: str):
    conn = _connect()
    conn.execute("UPDATE projects SET notes=? WHERE name=?", (notes, name))
    conn.commit()


def _project_row(row) -> Dict:
    d = dict(row)
    d["managed"]        = bool(d.get("managed", 1))
    d["locked"]         = bool(d.get("locked", 0))
    d["files"]          = _json_loads(d.get("files"), {})
    d["recent_commits"] = _json_loads(d.get("recent_commits"), [])
    d["file_locks"]     = _json_loads(d.get("file_locks"), {})
    d["closure_mode"] = d.get("closure_mode") or "build"
    d["closure_status"] = d.get("closure_status") or "yellow"
    d["closure_spec"] = _json_loads(d.get("closure_spec"), {})
    d["open_regression_count"] = int(d.get("open_regression_count") or 0)
    d["stall_count"] = int(d.get("stall_count") or 0)
    return d


def _verification_run_row(row) -> Dict:
    d = dict(row)
    d["results_json"] = _json_loads(d.get("results_json"), {})
    d["artifacts_json"] = _json_loads(d.get("artifacts_json"), {})
    d["fingerprints_json"] = _json_loads(d.get("fingerprints_json"), [])
    d["metadata_json"] = _json_loads(d.get("metadata_json"), {})
    return d


def verification_run_upsert(run: Dict) -> str:
    conn = _connect()
    conn.execute(
        """INSERT INTO verification_runs
           (id,project,trigger_task_id,run_type,status,created_at,started_at,completed_at,
            results_json,artifacts_json,fingerprints_json,metadata_json)
           VALUES (:id,:project,:trigger_task_id,:run_type,:status,:created_at,:started_at,:completed_at,
                   :results_json,:artifacts_json,:fingerprints_json,:metadata_json)
           ON CONFLICT(id) DO UPDATE SET
               project=excluded.project,
               trigger_task_id=excluded.trigger_task_id,
               run_type=excluded.run_type,
               status=excluded.status,
               created_at=excluded.created_at,
               started_at=excluded.started_at,
               completed_at=excluded.completed_at,
               results_json=excluded.results_json,
               artifacts_json=excluded.artifacts_json,
               fingerprints_json=excluded.fingerprints_json,
               metadata_json=excluded.metadata_json""",
        {
            "id": run["id"],
            "project": run["project"],
            "trigger_task_id": run.get("trigger_task_id"),
            "run_type": run.get("run_type", "post_task"),
            "status": run.get("status", "pending"),
            "created_at": run.get("created_at", datetime.now().isoformat()),
            "started_at": run.get("started_at"),
            "completed_at": run.get("completed_at"),
            "results_json": _json_dumps(run.get("results_json"), {}),
            "artifacts_json": _json_dumps(run.get("artifacts_json"), {}),
            "fingerprints_json": _json_dumps(run.get("fingerprints_json"), []),
            "metadata_json": _json_dumps(run.get("metadata_json"), {}),
        },
    )
    conn.commit()
    return run["id"]


def verification_run_get(run_id: str) -> Optional[Dict]:
    row = _connect().execute(
        "SELECT * FROM verification_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    return _verification_run_row(row) if row else None


def verification_run_list_by_project(project: str, limit: Optional[int] = None) -> List[Dict]:
    sql = "SELECT * FROM verification_runs WHERE project=? ORDER BY created_at DESC"
    params: tuple = (project,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (project, limit)
    rows = _connect().execute(sql, params).fetchall()
    return [_verification_run_row(r) for r in rows]


def verification_run_update(run_id: str, fields: Dict):
    conn = _connect()
    row = {}
    for key, value in fields.items():
        if key in {"results_json", "artifacts_json", "metadata_json"} and isinstance(value, dict):
            row[key] = json.dumps(value)
        elif key == "fingerprints_json" and isinstance(value, list):
            row[key] = json.dumps(value)
        else:
            row[key] = value
    set_sql = ", ".join(f"{k}=:{k}" for k in row)
    row["_id"] = run_id
    conn.execute(f"UPDATE verification_runs SET {set_sql} WHERE id=:_id", row)
    conn.commit()


def _regression_row(row) -> Dict:
    d = dict(row)
    d["details_json"] = _json_loads(d.get("details_json"), {})
    d["occurrences"] = int(d.get("occurrences") or 0)
    return d


def regression_upsert(regression: Dict) -> str:
    conn = _connect()
    conn.execute(
        """INSERT INTO regressions
           (id,project,fingerprint,status,severity,first_seen_at,last_seen_at,occurrences,
            source_run_id,linked_task_id,details_json)
           VALUES (:id,:project,:fingerprint,:status,:severity,:first_seen_at,:last_seen_at,:occurrences,
                   :source_run_id,:linked_task_id,:details_json)
           ON CONFLICT(id) DO UPDATE SET
               project=excluded.project,
               fingerprint=excluded.fingerprint,
               status=excluded.status,
               severity=excluded.severity,
               first_seen_at=excluded.first_seen_at,
               last_seen_at=excluded.last_seen_at,
               occurrences=excluded.occurrences,
               source_run_id=excluded.source_run_id,
               linked_task_id=excluded.linked_task_id,
               details_json=excluded.details_json""",
        {
            "id": regression["id"],
            "project": regression["project"],
            "fingerprint": regression["fingerprint"],
            "status": regression.get("status", "open"),
            "severity": regression.get("severity", "medium"),
            "first_seen_at": regression.get("first_seen_at", datetime.now().isoformat()),
            "last_seen_at": regression.get("last_seen_at", datetime.now().isoformat()),
            "occurrences": regression.get("occurrences", 1),
            "source_run_id": regression.get("source_run_id"),
            "linked_task_id": regression.get("linked_task_id"),
            "details_json": _json_dumps(regression.get("details_json"), {}),
        },
    )
    conn.commit()
    return regression["id"]


def regression_get(regression_id: str) -> Optional[Dict]:
    row = _connect().execute(
        "SELECT * FROM regressions WHERE id=?",
        (regression_id,),
    ).fetchone()
    return _regression_row(row) if row else None


def regression_find_open(project: str, fingerprint: str) -> Optional[Dict]:
    row = _connect().execute(
        "SELECT * FROM regressions WHERE project=? AND fingerprint=? AND status='open' ORDER BY last_seen_at DESC LIMIT 1",
        (project, fingerprint),
    ).fetchone()
    return _regression_row(row) if row else None


def regression_list_by_project(project: str, status: Optional[str] = None) -> List[Dict]:
    sql = "SELECT * FROM regressions WHERE project=?"
    params: tuple = (project,)
    if status is not None:
        sql += " AND status=?"
        params = (project, status)
    sql += " ORDER BY last_seen_at DESC, first_seen_at DESC"
    rows = _connect().execute(sql, params).fetchall()
    return [_regression_row(r) for r in rows]


def regression_update(regression_id: str, fields: Dict):
    conn = _connect()
    row = {}
    for key, value in fields.items():
        if key == "details_json" and isinstance(value, dict):
            row[key] = json.dumps(value)
        else:
            row[key] = value
    set_sql = ", ".join(f"{k}=:{k}" for k in row)
    row["_id"] = regression_id
    conn.execute(f"UPDATE regressions SET {set_sql} WHERE id=:_id", row)
    conn.commit()


# ---------------------------------------------------------------------------
# Agent operations
# ---------------------------------------------------------------------------

def agent_get_all() -> List[Dict]:
    rows = _connect().execute(
        "SELECT * FROM agents ORDER BY spawned_at DESC"
    ).fetchall()
    return [_agent_row(r) for r in rows]


def agent_get(agent_id: str) -> Optional[Dict]:
    row = _connect().execute(
        "SELECT * FROM agents WHERE id=?", (agent_id,)
    ).fetchone()
    return _agent_row(row) if row else None


def agent_get_active() -> List[Dict]:
    rows = _connect().execute(
        "SELECT * FROM agents WHERE status='active' ORDER BY spawned_at DESC"
    ).fetchall()
    return [_agent_row(r) for r in rows]


def agent_upsert(agent: Dict):
    conn = _connect()
    conn.execute(
        """INSERT INTO agents
           (id,project,task_type,status,spawned_at,completed_at,
            pid,exit_code,task_id,log_path,script_path,output,metadata,
            input_tokens,output_tokens)
           VALUES (:id,:project,:task_type,:status,:spawned_at,:completed_at,
                   :pid,:exit_code,:task_id,:log_path,:script_path,:output,:metadata,
                   :input_tokens,:output_tokens)
           ON CONFLICT(id) DO UPDATE SET
               project=excluded.project, task_type=excluded.task_type,
               status=excluded.status, spawned_at=excluded.spawned_at,
               completed_at=excluded.completed_at, pid=excluded.pid,
               exit_code=excluded.exit_code, task_id=excluded.task_id,
               log_path=excluded.log_path, script_path=excluded.script_path,
               output=excluded.output, metadata=excluded.metadata,
               input_tokens=excluded.input_tokens,
               output_tokens=excluded.output_tokens""",
        {
            "id":            agent["id"],
            "project":       agent.get("project"),
            "task_type":     agent.get("task_type"),
            "status":        agent.get("status", "active"),
            "spawned_at":    agent.get("spawned_at", datetime.now().isoformat()),
            "completed_at":  agent.get("completed_at"),
            "pid":           agent.get("pid"),
            "exit_code":     agent.get("exit_code"),
            "task_id":       agent.get("task_id"),
            "log_path":      agent.get("log_path"),
            "script_path":   agent.get("script_path"),
            "output":        (agent.get("output") or "")[-2000:],
            "metadata":      json.dumps(agent.get("metadata", {})),
            "input_tokens":  agent.get("input_tokens", 0),
            "output_tokens": agent.get("output_tokens", 0),
        },
    )
    conn.commit()


def agent_update_status(agent_id: str, status: str, **kwargs):
    """Update agent status plus any extra columns passed as kwargs."""
    conn = _connect()
    fields = {"status": status, **kwargs}
    set_sql = ", ".join(f"{k}=:{k}" for k in fields)
    fields["_id"] = agent_id
    conn.execute(f"UPDATE agents SET {set_sql} WHERE id=:_id", fields)
    conn.commit()


def _agent_row(row) -> Dict:
    d = dict(row)
    d["metadata"] = json.loads(d.get("metadata") or "{}")
    return d
