"""
Swarm API Module

Flask API for the Swarm Controller.
Provides endpoints for managing projects, tasks, and agents.
"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
import json
import os
import sqlite3
import subprocess
import threading
import time

from flask import Flask, jsonify, request, send_file, send_from_directory

from swarm.constants import AGENT_TIMEOUT
from swarm.task_chains import chain_to_project_head
from swarm.api_webhook import fire_webhook as _fire_webhook  # noqa: F401 re-export for tests

_config_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Startup helpers (extracted from create_app for focused testability)
# ---------------------------------------------------------------------------

def _init_db(data_dir: Path):
    """Initialise SQLite, run schema migrations, and repair any corrupted rows.

    Idempotent: safe to call multiple times (db.init() is a no-op if already
    initialised with the same path).  Extracted from create_app so DB startup
    can be tested independently of Flask app construction.
    """
    from swarm import db
    db_path = data_dir / "swarm.db"
    db.init(db_path, migrate_from={
        "tasks":    data_dir / "task-queue.json",
        "projects": data_dir / "projects.json",
        "agents":   data_dir / "agents.json",
    })
    try:
        repaired_project_locks = db.repair_project_lock_rows()
        if repaired_project_locks:
            print(f"[API] Repaired corrupted project lock rows: {', '.join(sorted(repaired_project_locks))}")
        repaired_task_rows = db.repair_malformed_task_rows()
        if repaired_task_rows:
            print(f"[API] Repaired malformed task rows: {', '.join(sorted(repaired_task_rows))}")
        backfilled_completed = db.backfill_completed_task_ids()
        if backfilled_completed:
            print(f"[API] Backfilled completed task archive rows: {', '.join(sorted(backfilled_completed))}")
    except Exception as exc:
        print(f"[API] Startup repair skipped: {exc}")
    return db_path


def _wire_runtime(config: Dict[str, Any], workspace: Path, data_dir: Path, project_registry) -> Optional[object]:
    """Wire orchestrator, agent_lifecycle, and swarm_runner module globals from config.

    Returns the generate_task_script callable (or None if swarm_runner unavailable).
    Extracted from create_app so runtime wiring can be tested or re-applied without
    reconstructing the full Flask app.
    """
    from swarm import orchestrator, agent_lifecycle
    from swarm.constants import AGENT_TIMEOUT

    orchestrator.WORKSPACE           = workspace
    orchestrator.DATA_DIR            = data_dir
    orchestrator.HISTORY_FILE        = data_dir / "agent-history.jsonl"
    orchestrator.MAX_ACTIVE_AGENTS   = config.get("max_active_agents", 3)
    orchestrator.MAX_LINES           = config.get("max_lines", 5000)
    orchestrator.LOCK_PROJECT        = config.get("lock_project", False)
    orchestrator.AGENT_TIMEOUT       = config.get("agent_timeout", AGENT_TIMEOUT)
    orchestrator.QUOTA_LIMIT_PERCENT = config.get("quota_limit_percent", 90)
    orchestrator.SPAWN_PER_CYCLE     = config.get("spawn_per_cycle", 3)
    orchestrator.AUTO_SCALE          = config.get("auto_scale", False)
    orchestrator.AUTO_SCALE_CEILING  = config.get("max_active_agents", 60)
    # _auto_scale_current starts at the floor (default 1) and ramps up. Never at the
    # ceiling -- otherwise every restart jumps to max concurrency and only backs off
    # after 429s arrive. Floor is read from config or falls back to the module default.
    _startup_ceiling = config.get("max_active_agents", 60)
    _startup_floor = max(1, int(config.get("auto_scale_floor", orchestrator._auto_scale_floor)))
    orchestrator._auto_scale_current = _startup_floor
    if config.get("auto_scale", False):
        orchestrator.MAX_ACTIVE_AGENTS = _startup_floor
    orchestrator.USE_WORKTREES       = config.get("use_worktrees", True)
    orchestrator.MCP_SERVERS         = config.get("mcp_servers", {})
    orchestrator.IGNORE_DIRS         = set(config.get("ignore_dirs", []))
    orchestrator.IGNORE_EXTENSIONS   = set(config.get("ignore_extensions", []))
    orchestrator.MANAGED_PROJECTS         = config.get("managed_projects", [])
    orchestrator.TASK_SELECTION_STRATEGY  = config.get("task_selection_strategy", "priority")
    orchestrator.PAUSED_PROJECTS          = config.get("paused_projects", [])
    orchestrator.ALLOW_SELF_MODIFICATION  = config.get("allow_self_modification", False)
    orchestrator.AUTO_REPLAN_PROJECTS     = config.get("auto_replan_projects", [])
    orchestrator._projects_sprint_qa_done.update(config.get("auto_replan_projects", []))
    orchestrator.MINIMAX_API_KEY     = os.environ.get("MINIMAX_API_KEY", "")
    orchestrator.LLM_PROVIDER        = config.get("llm_provider", "minimax")
    orchestrator.FALLBACK_PROVIDERS  = config.get("fallback_providers", [])
    orchestrator.WEBHOOK_URL         = config.get("completion_webhook_url", "")
    orchestrator.GARDENER_ENABLED       = config.get("gardener_enabled", False)
    orchestrator.GARDENER_MAX_TASKS      = config.get("gardener_max_tasks_per_run", 10)
    orchestrator.GARDENER_SKIP_PROJECTS = config.get("gardener_skip_projects", [])
    orchestrator.LAST_GARDENER_RUN_TS   = float(config.get("_gardener_last_run_ts", 0.0))
    orchestrator.META_MODE_ENABLED      = config.get("meta_mode_enabled", False)
    orchestrator.CARTOGRAPHER_ENABLED    = config.get("cartographer_enabled", False)
    orchestrator.CARTOGRAPHER_INTERVAL_HOURS = config.get("cartographer_interval_hours", 2)
    orchestrator.LIBRARIAN_ENABLED           = config.get("librarian_enabled", False)
    orchestrator.LIBRARIAN_TRIGGER_INTERVAL  = config.get("librarian_trigger_interval", 50)
    orchestrator.LIBRARIAN_MAX_PROMPT_TASKS  = config.get("librarian_max_prompt_tasks", 3)
    orchestrator.LIBRARIAN_COMPLETION_COUNTER = float(config.get("_librarian_completion_counter", 0))
    orchestrator.ARCHAEOLOGIST_ENABLED           = config.get("archaeologist_enabled", False)
    orchestrator.ARCHAEOLOGIST_STALL_THRESHOLD_HOURS = config.get("archaeologist_stall_threshold_hours", 72)
    orchestrator.ARCHAEOLOGIST_MAX_CONCURRENT    = config.get("archaeologist_max_concurrent", 2)
    orchestrator.SCHEDULER_ENABLED           = config.get("scheduler_enabled", False)
    orchestrator.SCHEDULER_INTERVAL_MINUTES  = config.get("scheduler_interval_minutes", 15)
    orchestrator.SCHEDULER_ALLOW_PAUSE       = config.get("scheduler_allow_pause", True)
    orchestrator.SCHEDULER_ALLOW_AGENT_CEILING_ADJUST = config.get("scheduler_allow_agent_ceiling_adjust", True)
    orchestrator.SCHEDULER_OFF_PEAK_HOURS    = config.get("scheduler_off_peak_hours", [0, 6])
    orchestrator.LOG_RETENTION_DAYS  = int(config.get("log_retention_days", 0))
    orchestrator.LOG_ROTATION_ACTION = config.get("log_rotation_action", "delete")
    orchestrator.LOG_EXTRACT_SIGNALS = bool(config.get("log_extract_signals", False))

    agent_lifecycle.configure(
        workspace=workspace,
        data_dir=data_dir,
        use_worktrees=config.get("use_worktrees", True),
        webhook_url=config.get("completion_webhook_url", ""),
        auto_replan_projects=config.get("auto_replan_projects", []),
        paused_projects=config.get("paused_projects", []),
        lock_project=config.get("lock_project", False),
        max_active_agents=config.get("max_active_agents", 3),
        agent_timeout=config.get("agent_timeout", AGENT_TIMEOUT),
        project_registry=project_registry,
        human_review_flag_enabled=config.get("human_review_flag_enabled", False),
        playthrough_auto_enabled=config.get("playthrough_auto_enabled", False),
    )

    from swarm.agent_recovery import configure_recovery as _configure_recovery
    _configure_recovery(config)

    try:
        import swarm_runner as _runner_mod
        _runner_mod.WORKSPACE          = workspace
        _runner_mod.DATA_DIR           = data_dir
        _runner_mod.MAX_LINES          = config.get("max_lines", 5000)
        _runner_mod.IGNORE_DIRS        = set(config.get("ignore_dirs", []))
        _runner_mod.IGNORE_EXTENSIONS  = set(config.get("ignore_extensions", []))
        _runner_mod.MCP_SERVERS        = config.get("mcp_servers", {})
        _runner_mod.LLM_PROVIDER       = config.get("llm_provider", "minimax")
        if config.get("llm_providers"):
            for _n, _c in config["llm_providers"].items():
                if _n in _runner_mod.LLM_PROVIDERS:
                    _runner_mod.LLM_PROVIDERS[_n].update(_c)
                else:
                    _runner_mod.LLM_PROVIDERS[_n] = _c
        from swarm_runner import generate_task_script
        return generate_task_script, _runner_mod
    except Exception:
        # Log the import failure so auto-mode-disabled states surface in server logs
        # instead of silently never spawning agents.
        import logging
        logging.getLogger(__name__).exception(
            "Failed to import swarm_runner -- auto-mode disabled"
        )
        return None, None


def _handle_startup_orphans(data_dir: Path, agent_timeout: float):
    """Mark long-running orphan agents as failed on startup.

    After a server restart, agents that were 'active' have no live process handle.
    Those older than agent_timeout are marked failed and their tasks reset to pending.
    Recent orphans are left alone -- they will be reaped by the monitor on the next tick.

    Extracted from create_app so orphan handling can be tested independently.
    """
    from swarm import db
    _now = datetime.now()
    for _a in db.agent_get_active():
        if _a.get("status") != "active":
            continue
        spawned_at = _a.get("spawned_at", "")
        try:
            age = (_now - datetime.fromisoformat(spawned_at)).total_seconds()
        except Exception:
            age = agent_timeout + 1
        _aid = _a["id"]
        if age > agent_timeout:
            print(f"[API] Orphan agent {_aid[:8]} (age {int(age)}s) -- marking failed")
            db.agent_update_status(_aid, "failed",
                                   completed_at=_now.isoformat(), exit_code=-1,
                                   output="[Swarm] Orphan: server restarted while agent was running")
            if _a.get("task_id"):
                db.task_update_status(_a["task_id"], "pending")
        else:
            print(f"[API] Orphan agent {_aid[:8]} (age {int(age)}s) -- within timeout, will reap normally")


def create_app(
    workspace: Optional[Path] = None,
    data_dir: Optional[Path] = None,
    config: Optional[Dict[str, Any]] = None,
    config_file: Optional[Path] = None,
) -> Flask:
    """Create and configure the Flask application"""

    # Load config from file first so workspace/data_dir can come from it
    if config is None:
        config = {}

    # Resolve config file: explicit arg (tests use tmp) or project root default
    project_root = Path(__file__).parent.parent
    if config_file is None:
        config_file = project_root / "config.json"
    if config_file.exists():
        try:
            file_config = json.loads(config_file.read_text(encoding="utf-8"))
            # File provides defaults; explicit config (e.g. from tests) takes priority
            merged = file_config
            merged.update(config)
            config = merged
        except Exception:
            pass

    # Resolve workspace: config > explicit arg > default
    if workspace is None:
        workspace = Path(os.path.expanduser(config.get("workspace", "~/workspace")))
    if data_dir is None:
        # data lives inside the project root, not inside the workspace
        data_dir = project_root / "data"

    # Set defaults
    config.setdefault("workspace", str(workspace))
    config.setdefault("max_lines", 5000)
    config.setdefault("max_active_agents", 3)
    config.setdefault("agent_timeout", AGENT_TIMEOUT)
    config.setdefault("ignore_dirs", ["addons", ".git", ".godot"])
    config.setdefault("ignore_extensions", [
        ".fbx", ".obj", ".glb", ".gltf", ".blend",
        ".png", ".jpg", ".jpeg", ".webp", ".svg",
        ".wav", ".mp3", ".ogg", ".opus",
        ".ttf", ".otf", ".woff",
        ".zip", ".tar", ".gz",
        ".import", ".uid"
    ])
    config.setdefault("file_extensions", [".gd"])
    config.setdefault("language", "GDScript")
    config.setdefault("managed_projects", [])
    config.setdefault("disable_monitor", "PYTEST_CURRENT_TEST" in os.environ)
    configured_godot_path = str(config.get("godot_path") or "").strip()
    if configured_godot_path and not os.environ.get("GODOT_PATH"):
        os.environ["GODOT_PATH"] = configured_godot_path

    app = Flask(__name__)
    app.config["WORKSPACE_ROOT"] = str(workspace)
    app.config["DATA_DIR"] = str(data_dir)
    app.config["CONFIG_FILE"] = str(config_file)

    try:
        from flask_compress import Compress
        app.config["COMPRESS_MIMETYPES"] = [
            "application/json", "text/html", "text/css",
            "application/javascript", "text/javascript",
        ]
        app.config["COMPRESS_LEVEL"] = 6
        app.config["COMPRESS_MIN_SIZE"] = 4096
        Compress(app)
    except ImportError:
        pass  # flask-compress not installed; responses uncompressed

    # Import modules after app creation to avoid circular imports
    from swarm import db
    from swarm.tasks import SQLiteTaskSource
    from swarm.projects import SQLiteProjectRegistry
    from swarm.agents import SQLiteAgentTracker

    # Initialise SQLite, run schema migrations, and repair any corrupted rows.
    db_path = _init_db(data_dir)
    print(f"[API] SQLite db initialised at {db_path}")

    task_source      = SQLiteTaskSource()
    project_registry = SQLiteProjectRegistry(workspace)
    agent_tracker    = SQLiteAgentTracker()

    # Wire orchestrator, agent_lifecycle, and swarm_runner globals from config.
    from swarm import orchestrator
    generate_task_script, _runner_mod = _wire_runtime(config, workspace, data_dir, project_registry)

    # Load agent profile plugins from plugins/ directory (startup-only, no hot-reload).
    from swarm.plugins import load_plugins as _load_plugins
    _load_plugins(project_root)

    # Mark long-running orphan agents as failed; recent orphans reap on next tick.
    _handle_startup_orphans(data_dir, config.get("agent_timeout", AGENT_TIMEOUT))

    # Reap any Godot processes left over from agents that were SIGKILLed before
    # their atexit handlers ran (zombies that would squat on StateServer ports).
    try:
        from swarm.qa_tools import sweep_godot_zombies as _sweep_godot_zombies
        _sweep_godot_zombies()
    except Exception as _sgz_err:
        print(f"[Startup] Godot zombie sweep error: {_sgz_err}")

    _start_time = time.time()

    # Capture the commit this process is running so /api/health can detect
    # stale code (server running for days while the repo moved on).
    try:
        _running_commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(Path(__file__).parent.parent),
        ).stdout.strip()
    except Exception:
        _running_commit = ""

    # Clean up any worktrees left over from a previous server run (background thread
    # so server starts immediately instead of blocking on potentially hundreds of worktrees)
    def _startup_worktree_cleanup():
        try:
            orchestrator.cleanup_orphaned_worktrees(workspace)
        except Exception as _wt_err:
            print(f"[Startup] Worktree cleanup error: {_wt_err}")
    threading.Thread(target=_startup_worktree_cleanup, daemon=True, name="startup-wt-cleanup").start()

    def _startup_script_cleanup():
        """Delete orphaned agent_*.py wrapper scripts from previous runs."""
        try:
            import glob as _glob
            active_ids = {a.get("id") for a in db.agent_get_all()}
            pattern = str(Path(data_dir) / "agent_*.py")
            deleted = 0
            for path in _glob.glob(pattern):
                agent_id = Path(path).stem[len("agent_"):]
                if agent_id not in active_ids:
                    try:
                        Path(path).unlink()
                        deleted += 1
                    except Exception:
                        pass
            if deleted:
                print(f"[Startup] Cleaned up {deleted} orphaned agent wrapper scripts")
        except Exception as _sc_err:
            print(f"[Startup] Script cleanup error: {_sc_err}")
    threading.Thread(target=_startup_script_cleanup, daemon=True, name="startup-script-cleanup").start()

    # Auto mode state persists across restart so scheduler behavior is stable.
    auto_mode_state = {
        "enabled": bool(config.get("auto_mode_enabled", False)),
        "suspended_for_quota": False,
        "lock": threading.Lock(),
    }

    # CORS helper
    def add_cors(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
    app.after_request(add_cors)

    # ============ Routes ============

    @app.route("/")
    def index():
        """Serve dashboard"""
        dashboard_path = project_root / "dashboard.html"
        if dashboard_path.exists():
            return send_file(dashboard_path)
        return jsonify({"message": "Swarm Controller API", "version": "0.2.0"})

    @app.route("/dashboard")
    def dashboard():
        dashboard_path = project_root / "dashboard.html"
        if dashboard_path.exists():
            return send_file(dashboard_path)
        return jsonify({"message": "No dashboard found"})

    @app.route("/dashboard-utils.js")
    def serve_dashboard_utils_js():
        return send_from_directory(project_root, "dashboard-utils.js", mimetype="application/javascript")

    @app.route("/dashboard-core.js")
    def serve_dashboard_core_js():
        return send_from_directory(project_root, "dashboard-core.js", mimetype="application/javascript")

    @app.route("/dashboard-projects.js")
    def serve_dashboard_projects_js():
        return send_from_directory(project_root, "dashboard-projects.js", mimetype="application/javascript")

    @app.route("/dashboard-agents.js")
    def serve_dashboard_agents_js():
        return send_from_directory(project_root, "dashboard-agents.js", mimetype="application/javascript")

    @app.route("/dashboard-tasks.js")
    def serve_dashboard_tasks_js():
        return send_from_directory(project_root, "dashboard-tasks.js", mimetype="application/javascript")

    @app.route("/dashboard-config.js")
    def serve_dashboard_config_js():
        return send_from_directory(project_root, "dashboard-config.js", mimetype="application/javascript")

    @app.route("/dashboard-wizard.js")
    def serve_dashboard_wizard_js():
        return send_from_directory(project_root, "dashboard-wizard.js", mimetype="application/javascript")

    @app.route("/dashboard-chat.js")
    def serve_dashboard_chat_js():
        return send_from_directory(project_root, "dashboard-chat.js", mimetype="application/javascript")

    @app.route("/dashboard.js")
    def serve_dashboard_js():
        return send_from_directory(project_root, "dashboard.js", mimetype="application/javascript")

    @app.route("/dashboard_closure.js")
    def serve_dashboard_closure_js():
        return send_from_directory(project_root, "dashboard_closure.js", mimetype="application/javascript")

    @app.route("/dashboard_deps_integrity.js")
    def serve_dashboard_deps_integrity_js():
        return send_from_directory(project_root, "dashboard_deps_integrity.js", mimetype="application/javascript")

    @app.route("/dashboard.css")
    def serve_dashboard_css():
        return send_from_directory(project_root, "dashboard.css", mimetype="text/css")

    @app.route("/api/ping", methods=["GET"])
    def ping():
        """Health-check endpoint. Returns {"ok": true, "ts": <unix_timestamp>}."""
        return jsonify({"ok": True, "ts": time.time()})

    # ---------- Login ----------
    from swarm.api_auth import register_routes as _register_auth_routes
    _register_auth_routes(app, config=config)

    # ---------- Spawning (extracted to api_spawn.py) ----------

    from swarm.api_spawn import register_routes as _register_spawn_routes
    _register_spawn_routes(
        app,
        task_source=task_source,
        orchestrator=orchestrator,
        generate_task_script=generate_task_script,
        config=config,
        db=db,
        auto_mode_state=auto_mode_state,
        data_dir=data_dir,
        workspace=workspace,
        project_registry=project_registry,
        config_file=config_file,
        config_write_lock=_config_write_lock,
    )


    # ---------- History ----------

    from swarm.api_history import register_routes as _register_history_routes
    _register_history_routes(
        app,
        task_source=task_source,
        db=db,
        data_dir=data_dir,
        orchestrator=orchestrator,
        config=config,
    )

    # ---------- Background monitor ----------

    _last_monitor_tick = [time.time()]  # mutable container for closure
    _rate_limit_cooldown_until = [0.0]   # timestamp until which spawning is paused
    _rate_limit_cooldown_secs = 300      # 5-minute cooldown on rate-limit exhaustion
    # In-memory set of already-archived rl_event lines -- avoids re-reading the archive
    # file every monitor cycle (was O(n²) on a 14k-line file). Seeded from existing
    # archive on startup so restarts don't re-append old events.
    _rl_archive_file_seed = data_dir / "rl_events_archive.jsonl"
    try:
        _rl_archived_set: set = set(
            l.strip() for l in _rl_archive_file_seed.read_text().splitlines() if l.strip()
        ) if _rl_archive_file_seed.exists() else set()
    except Exception:
        _rl_archived_set: set = set()
    _ghost_sweep_counter = [0]           # incremented each cycle; sweep runs every 20 cycles (~100s)
    _orphan_godot_sweep_counter = [0]    # incremented each cycle; orphan Godot sweep runs every 60 cycles (~5 min)
    _worktree_cleanup_counter = [0]      # incremented each cycle; worktree cleanup runs every 720 cycles (~1 hour)
    # Persist last run time across restarts via a small state file
    _audit_learnings_state_file = data_dir / "audit_learnings_last_run.txt"
    try:
        _audit_learnings_last_run = [float(_audit_learnings_state_file.read_text().strip())]
    except Exception:
        _audit_learnings_last_run = [0.0]
    _log_rotation_state_file = data_dir / "log_rotation_last_run.txt"
    try:
        _log_rotation_last_run = [float(_log_rotation_state_file.read_text().strip())]
    except Exception:
        _log_rotation_last_run = [0.0]
    _LOG_ROTATION_INTERVAL = 3600  # check once per hour

    def _is_transient_monitor_db_error(exc: Exception) -> bool:
        """Tests and startup can swap DBs while an old daemon monitor is winding down."""
        return isinstance(exc, sqlite3.OperationalError) and "no such table:" in str(exc)

    def _sweep_ghost_deps(db, data_dir) -> list:
        """Remove dep edges that point to task IDs absent from both the active DB
        and task-history.jsonl.  These are true ghosts -- deleted or never created --
        that permanently block their dependents.

        Returns a list of (task_id, removed_dep_id) tuples for logging.
        """
        all_tasks = db.task_get_all()
        active_ids = {t["id"] for t in all_tasks}
        completed_ids = db.task_get_completed_ids()

        # Skip the 102MB JSONL read of task-history.jsonl -- completed tasks
        # stay in the DB now, so active_ids | completed_ids is sufficient.
        known_ids = active_ids | completed_ids

        pruned = []

        for task in all_tasks:
            # Check all non-completed statuses -- in_progress tasks with ghost deps
            # get killed by the dep violation checker, so catch them here first.
            if task.get("status") in ("completed", "cancelled", "archived"):
                continue
            deps = list(task.get("dependencies") or [])
            ghost_deps = [d for d in deps if d not in known_ids]
            if ghost_deps:
                new_deps = [d for d in deps if d not in ghost_deps]
                db.task_update(task["id"], {"dependencies": new_deps})
                for gd in ghost_deps:
                    pruned.append((task["id"], gd))

        return pruned

    def _sweep_orphaned_godot_processes() -> None:
        """Kill Godot processes listening on the StateServer port range (11009-11049)
        that are not children of any active agent process.

        These accumulate when agents crash before calling kill_game(), leaving
        Godot instances holding ports and preventing future agents from launching.
        """
        import subprocess as _sp
        try:
            lsof = _sp.run(
                ["lsof", "-i", ":11009-11209", "-t"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            return

        godot_pids = set()
        for pid_str in lsof.stdout.strip().splitlines():
            pid_str = pid_str.strip()
            if not pid_str.isdigit():
                continue
            pid = int(pid_str)
            try:
                comm = _sp.run(
                    ["ps", "-p", str(pid), "-o", "comm="],
                    capture_output=True, text=True, timeout=3,
                ).stdout.strip().lower()
                if "godot" in comm:
                    godot_pids.add(pid)
            except Exception:
                pass

        if not godot_pids:
            return

        # Collect PIDs of active agent processes
        from swarm.agent_lifecycle import _active_handles, _handle_lock
        active_agent_pids = set()
        with _handle_lock:
            for data in _active_handles.values():
                try:
                    active_agent_pids.add(data["process"].pid)
                except Exception:
                    pass

        # Find children of active agents
        active_children: set = set()
        for agent_pid in active_agent_pids:
            try:
                children = _sp.run(
                    ["pgrep", "-P", str(agent_pid)],
                    capture_output=True, text=True, timeout=5,
                )
                for cpid_str in children.stdout.split():
                    if cpid_str.strip().isdigit():
                        active_children.add(int(cpid_str.strip()))
            except Exception:
                pass

        orphans = godot_pids - active_children
        for pid in orphans:
            try:
                import os as _os
                _os.kill(pid, 9)
                print(f"[Monitor] Swept orphaned Godot PID {pid} from StateServer port range")
            except Exception:
                pass

    def _monitor():
        while True:
            try:
                # Poll frequently when agents are running or auto-mode is on;
                # back off when the system is idle to reduce overhead. This must
                # stay inside the guard: tests can swap temporary DBs while a
                # daemon monitor from a previous app instance is still winding down.
                active = orchestrator.get_active_count()
                with auto_mode_state["lock"]:
                    mode_on = auto_mode_state["enabled"]
                    suspended = auto_mode_state["suspended_for_quota"]
                sleep_secs = 5 if (active > 0 or mode_on or suspended) else 30
                time.sleep(sleep_secs)

                _last_monitor_tick[0] = time.time()
                # Pre-fetch shared state once per cycle to avoid redundant full-table
                # scans across check_dep_violations, fill_slots, etc.
                # Scope to managed projects only: historical tasks from dead runs
                # (old void-patrol-run4, etc.) should not be scanned every 5s.
                _managed = config.get("managed_projects") or None  # None = no filter (all)
                _cycle_completed_ids = db.task_get_completed_ids(projects=_managed)
                _cycle_all_task_ids = db.task_get_all_ids(projects=_managed)
                orchestrator.check_ghost_merge_tasks()
                orchestrator.check_dep_violations(
                    completed_ids=_cycle_completed_ids,
                    all_task_ids=_cycle_all_task_ids,
                )
                orchestrator.check_agent_status()
                orchestrator.check_infra_freeze(db, config)

                # Periodic ghost dep sweep: remove dep edges pointing to IDs that
                # are absent from both the active DB and task history.  These are
                # truly deleted/never-created tasks that will never unblock their
                # dependents.  Runs every 20 cycles (~100s) to avoid DB churn.
                _ghost_sweep_counter[0] += 1
                if _ghost_sweep_counter[0] >= 20:
                    _ghost_sweep_counter[0] = 0
                    try:
                        _sweep_pruned = _sweep_ghost_deps(db, data_dir)
                        if _sweep_pruned:
                            print(f"[Monitor] Ghost dep sweep pruned {len(_sweep_pruned)} stale edge(s): {_sweep_pruned}")
                    except Exception as _sweep_err:
                        print(f"[Monitor] Ghost dep sweep error: {_sweep_err}")

                # Orphaned Godot sweep: kill any Godot process on the StateServer
                # port range (11009-11049) that is not a child of an active agent.
                # These pile up when agents crash before calling kill_game().
                _orphan_godot_sweep_counter[0] += 1
                if _orphan_godot_sweep_counter[0] >= 10:
                    _orphan_godot_sweep_counter[0] = 0
                    try:
                        _sweep_orphaned_godot_processes()
                    except Exception as _godot_sweep_err:
                        print(f"[Monitor] Orphan Godot sweep error: {_godot_sweep_err}")

                # Periodic worktree cleanup: remove stale .wt-* dirs in background
                # so it never blocks the monitor tick. Runs every 720 cycles (~1 hour).
                _worktree_cleanup_counter[0] += 1
                if _worktree_cleanup_counter[0] >= 720:
                    _worktree_cleanup_counter[0] = 0
                    threading.Thread(
                        target=orchestrator.cleanup_orphaned_worktrees,
                        kwargs={"workspace": workspace},
                        daemon=True,
                        name="periodic-wt-cleanup",
                    ).start()

                # Periodic log rotation (timestamp-based, runs once per hour).
                if time.time() - _log_rotation_last_run[0] >= _LOG_ROTATION_INTERVAL:
                    if getattr(orchestrator, "LOG_RETENTION_DAYS", 0) > 0:
                        from swarm.log_rotation import rotate_logs as _rotate_logs
                        threading.Thread(
                            target=_rotate_logs,
                            args=(str(data_dir), db),
                            daemon=True,
                            name="periodic-log-rotation",
                        ).start()
                    _log_rotation_last_run[0] = time.time()
                    try:
                        _log_rotation_state_file.write_text(str(_log_rotation_last_run[0]))
                    except Exception:
                        pass

                # Check for rate-limit pressure from agent subprocesses.
                # Rate limiting is separate from quota: use its own cooldown rather
                # than the quota-suspension flag (which would immediately auto-resume).
                # When auto-scale is on, skip the cooldown -- the scaler handles rate
                # reduction by stepping down MAX_ACTIVE_AGENTS directly.
                rate_limited = orchestrator.check_rate_limit_flags()
                if rate_limited and not orchestrator.AUTO_SCALE:
                    new_cooldown = time.time() + _rate_limit_cooldown_secs
                    _rate_limit_cooldown_until[0] = new_cooldown
                    print(f"[Auto] {rate_limited} rate limited -- spawn cooldown {_rate_limit_cooldown_secs}s")

                # Also check rolling 429 event pressure (agents write rl_events.jsonl)
                _recent_429_count = 0
                try:
                    _rl_file = data_dir / "rl_events.jsonl"
                    if _rl_file.exists():
                        _now = time.time()
                        _window = 120  # 2-minute window
                        _rl_lines = _rl_file.read_text().splitlines()
                        for _l in _rl_lines:
                            try:
                                _ev = json.loads(_l)
                                if _now - _ev.get("t", 0) < _window:
                                    _recent_429_count += 1
                            except Exception:
                                pass
                        # Archive new events: only append lines not yet seen (tracked in memory).
                        # Never read the archive file back -- that was O(n²) on a 14k-line file.
                        _archive_file = data_dir / "rl_events_archive.jsonl"
                        _new_lines = [_l for _l in _rl_lines if _l.strip() and _l.strip() not in _rl_archived_set]
                        if _new_lines:
                            with open(_archive_file, "a") as _af:
                                _af.write("\n".join(_new_lines) + "\n")
                            _rl_archived_set.update(_l.strip() for _l in _new_lines)
                        # Trim rolling file (keep last 200 lines)
                        if len(_rl_lines) > 200:
                            _rl_file.write_text("\n".join(_rl_lines[-200:]) + "\n")
                        if _recent_429_count >= 5 and time.time() >= _rate_limit_cooldown_until[0] and not orchestrator.AUTO_SCALE:
                            _rate_limit_cooldown_until[0] = time.time() + _rate_limit_cooldown_secs
                            print(f"[Auto] {_recent_429_count} rate limit events in {_window}s -- spawn cooldown {_rate_limit_cooldown_secs}s")
                except Exception:
                    pass

                # Auto-scale: adjust MAX_ACTIVE_AGENTS based on 429 pressure
                orchestrator.auto_scale_step(_recent_429_count)

                # Quota suspension is handled by the dedicated _quota_watcher thread
                # (runs every 10s independent of monitor lag). Read the flag here
                # only to skip fill_slots when suspended.
                with auto_mode_state["lock"]:
                    over_limit = auto_mode_state["suspended_for_quota"]

                if over_limit:
                    continue

                # Fill agent slots if auto mode is on and not in rate-limit cooldown
                with auto_mode_state["lock"]:
                    _auto_on = auto_mode_state["enabled"]
                _in_cooldown = time.time() < _rate_limit_cooldown_until[0]
                if _in_cooldown:
                    _remaining = int(_rate_limit_cooldown_until[0] - time.time())
                    print(f"[Auto] Rate limit cooldown -- {_remaining}s remaining, skipping spawn")
                elif _auto_on and generate_task_script is not None:
                    _spawned, _skipped = orchestrator.fill_slots(generate_task_script)
                    if _spawned:
                        print(f"[Monitor] fill_slots spawned {len(_spawned)} agent(s): {[s[:8] for s in _spawned]}")
                    elif _skipped:
                        print(f"[Monitor] fill_slots spawn failures for: {_skipped} (active={orchestrator.get_active_count()})")
                    elif orchestrator.get_active_count() < config.get("max_active_agents", 3):
                        print(f"[Monitor] fill_slots idle (active={orchestrator.get_active_count()}, no runnable tasks)")

                # Auto-replan is now handled solely by orchestrator.fill_slots sprint cycle
                # (removed duplicate monitor-thread block per bug-106264195-0537, bead swarm-controller-ojf6).

                # Daily audit_learnings: surface swarm health patterns across all projects.
                # Runs at most once every 24 hours; writes AUDIT_LEARNINGS_REPORT.md.
                _AUDIT_LEARNINGS_INTERVAL = 86400  # 24 hours
                if time.time() - _audit_learnings_last_run[0] >= _AUDIT_LEARNINGS_INTERVAL:
                    try:
                        _al_pending = any(
                            t.get("type") == "audit_learnings"
                            and t.get("status") in ("pending", "in_progress")
                            for t in db.task_get_by_status("pending") + db.task_get_by_status("in_progress")
                        )
                        if not _al_pending:
                            _al_id = f"audit-learnings-{int(time.time())}"
                            db.task_upsert({
                                "id": _al_id,
                                "project": "swarm-controller",
                                "type": "audit_learnings",
                                "description": (
                                    "Daily swarm health audit: scan all learnings under data/learnings/ "
                                    "across all projects and task types. Identify recurring patterns "
                                    "grouped by task type (not cross-type, to avoid poisoning). "
                                    "Write a diagnostic report to data/AUDIT_LEARNINGS_REPORT.md -- "
                                    "do NOT create tasks automatically."
                                ),
                                "priority": 50,
                                "status": "pending",
                                "dependencies": [],
                                "metadata": {"auto_spawned": True},
                                "attempts": 0,
                                "max_attempts": 2,
                            })
                            _audit_learnings_last_run[0] = time.time()
                            try:
                                _audit_learnings_state_file.write_text(str(_audit_learnings_last_run[0]))
                            except Exception:
                                pass
                            print(f"[Monitor] Spawned daily audit_learnings task {_al_id}")
                    except Exception as _ale:
                        print(f"[Monitor] Daily audit_learnings spawn error: {_ale}")

            except Exception as exc:
                if _is_transient_monitor_db_error(exc):
                    time.sleep(0.25)
                    continue
                import traceback
                traceback.print_exc()
                # Sleep briefly and continue -- never let an unhandled exception
                # (e.g. disk-full OSError during spawn) kill the monitor thread.
                time.sleep(5)
                continue

    if config.get("disable_monitor", False):
        monitor_thread = SimpleNamespace(is_alive=lambda: False)
    else:
        # Startup ghost dep sweep: clear any deps pointing to tasks that completed
        # and were archived before this restart. Runs once immediately so tasks are
        # never stuck waiting for the periodic 20-cycle sweep after a restart.
        try:
            _startup_pruned = _sweep_ghost_deps(db, data_dir)
            if _startup_pruned:
                print(f"[Startup] Ghost dep sweep cleared {len(_startup_pruned)} stale edge(s): {_startup_pruned}")
        except Exception as _startup_sweep_err:
            print(f"[Startup] Ghost dep sweep error: {_startup_sweep_err}")

        def _quota_signal_agents(sig: int):
            """Send signal to all active agent processes (SIGSTOP or SIGCONT).
            Covers both in-memory handles and DB-tracked orphaned agents (PID only).

            When SIGSTOPping, records freeze_started on each handle so the watchdog
            timeout clock is paused.  When SIGCONTing, adds the frozen duration to
            the handle's started time so elapsed time only counts un-frozen wall time.
            """
            import signal as _signal
            from swarm.agent_lifecycle import _active_handles, _handle_lock
            sig_name = "SIGSTOP" if sig == _signal.SIGSTOP else "SIGCONT"
            is_stop = (sig == _signal.SIGSTOP)
            now = time.time()

            # Collect PIDs from in-memory handles + pause/resume timeout clock
            pids: dict[int, str] = {}  # pid -> agent_id
            with _handle_lock:
                for agent_id, data in _active_handles.items():
                    proc = data.get("process")
                    pid = data.get("pid")
                    if proc and proc.poll() is None:
                        pids[proc.pid] = agent_id
                    elif pid:
                        pids[pid] = agent_id

                    # Pause/resume timeout clock
                    if is_stop:
                        data["freeze_started"] = now
                    else:
                        freeze_started = data.pop("freeze_started", None)
                        if freeze_started:
                            frozen_secs = now - freeze_started
                            data["started"] = data.get("started", now) + frozen_secs
                            print(f"[Quota] Agent {agent_id[:8]} unfrozen after {frozen_secs:.0f}s -- timeout clock adjusted")

            # Also cover DB-tracked agents not in _active_handles (orphans from prior session)
            for agent in db.agent_get_active():
                pid = agent.get("pid")
                if pid and pid not in pids:
                    try:
                        os.kill(pid, 0)  # check liveness
                        pids[pid] = agent["id"]
                    except OSError:
                        pass  # process gone

            count = 0
            for pid, agent_id in pids.items():
                try:
                    os.kill(pid, sig)
                    count += 1
                except Exception as _e:
                    print(f"[Quota] Failed to send {sig_name} to agent {agent_id[:8]} (pid {pid}): {_e}")
            if count:
                print(f"[Quota] Sent {sig_name} to {count} agent(s)")

        def _quota_watcher():
            """Lightweight thread that checks quota every 10s independently of the
            monitor thread.  The monitor can block for minutes on Godot validation;
            this ensures quota suspension fires promptly regardless.
            Also SIGSTOPs all running agents when over limit and SIGCONTs on resume."""
            _agents_frozen = False
            _last_pct_used = 0.0
            _log_tick = 0
            while True:
                try:
                    time.sleep(10)
                    # Guard: if swarm_runner import failed, _runner_mod is None.
                    # Skip the watcher body to avoid AttributeError on _runner_mod.LLM_PROVIDER.
                    if _runner_mod is None:
                        continue
                    over_limit, pct_used, pct_remaining, *_ = orchestrator.check_quota_limit()
                    _log_tick += 1

                    # Log every 60s (6 ticks) so there's a breadcrumb trail in logs
                    if _log_tick % 6 == 0:
                        print(f"[Quota] {pct_used:.1f}% used / {pct_remaining:.1f}% remaining")

                    # Detect quota window reset: used% dropped significantly
                    if _last_pct_used > 20.0 and pct_used < (_last_pct_used - 15.0):
                        print(f"[Quota] Window reset detected: {_last_pct_used:.1f}% → {pct_used:.1f}% used")
                    _last_pct_used = pct_used

                    if over_limit:
                        local_fallback = config.get("local_fallback_on_quota", False)
                        if local_fallback:
                            # Switch new agent spawns to local fallback provider instead of suspending
                            _local_providers = [
                                p for p in orchestrator.FALLBACK_PROVIDERS
                                if config.get("llm_providers", {}).get(p, {}).get("base_url", "").startswith("http://localhost")
                                or config.get("llm_providers", {}).get(p, {}).get("base_url", "").startswith("http://127.")
                            ]
                            if _local_providers and not auto_mode_state.get("_quota_fallback_active"):
                                _primary = config.get("llm_provider", "minimax")
                                _fallback = _local_providers[0]
                                auto_mode_state["_quota_fallback_active"] = True
                                auto_mode_state["_quota_fallback_primary"] = _primary
                                _runner_mod.LLM_PROVIDER = _fallback
                                orchestrator.LLM_PROVIDER = _fallback
                                print(f"[Quota] Limit exceeded ({pct_used:.1f}% used) -- switching new spawns to local fallback: {_fallback}")
                            elif not _local_providers:
                                # No local providers configured -- fall back to suspend
                                with auto_mode_state["lock"]:
                                    if auto_mode_state["enabled"]:
                                        auto_mode_state["enabled"] = False
                                        auto_mode_state["suspended_for_quota"] = True
                                        print(f"[Quota] Limit exceeded ({pct_used:.1f}% used) -- no local fallback configured, suspending")
                                if not _agents_frozen:
                                    import signal as _sig
                                    _quota_signal_agents(_sig.SIGSTOP)
                                    _agents_frozen = True
                        else:
                            with auto_mode_state["lock"]:
                                if auto_mode_state["enabled"]:
                                    auto_mode_state["enabled"] = False
                                    auto_mode_state["suspended_for_quota"] = True
                                    print(f"[Quota] Limit exceeded ({pct_used:.1f}% used) -- auto mode suspended")
                            if not _agents_frozen:
                                import signal as _sig
                                _quota_signal_agents(_sig.SIGSTOP)
                                _agents_frozen = True
                    else:
                        # Quota OK -- restore primary provider if we were in fallback mode
                        if auto_mode_state.get("_quota_fallback_active"):
                            _primary = auto_mode_state.pop("_quota_fallback_primary", config.get("llm_provider", "minimax"))
                            auto_mode_state.pop("_quota_fallback_active", None)
                            _runner_mod.LLM_PROVIDER = _primary
                            orchestrator.LLM_PROVIDER = _primary
                            print(f"[Quota] Quota OK ({pct_used:.1f}% used) -- restored primary provider: {_primary}")
                        with auto_mode_state["lock"]:
                            if auto_mode_state["suspended_for_quota"] and not auto_mode_state["enabled"] and not auto_mode_state.get("user_disabled"):
                                auto_mode_state["enabled"] = True
                                auto_mode_state["suspended_for_quota"] = False
                                print(f"[Quota] Quota OK ({pct_used:.1f}% used) -- auto mode resumed")
                        if _agents_frozen:
                            import signal as _sig
                            _quota_signal_agents(_sig.SIGCONT)
                            _agents_frozen = False
                except Exception as _qw_err:
                    print(f"[Quota] Watcher error: {_qw_err}")

        monitor_thread = threading.Thread(target=_monitor, daemon=True)
        monitor_thread.start()
        quota_watcher_thread = threading.Thread(target=_quota_watcher, daemon=True, name="quota-watcher")
        quota_watcher_thread.start()

    # ---------- Route modules (extracted) ----------

    from swarm.api_projects import register_routes as _reg_projects
    from swarm.api_tasks import register_routes as _reg_tasks
    from swarm.api_agents import register_routes as _reg_agents
    from swarm.api_config import register_routes as _reg_config
    from swarm.api_metrics import register_routes as _reg_metrics

    _reg_projects(
        app,
        project_registry=project_registry,
        workspace=workspace,
        task_source=task_source,
        orchestrator=orchestrator,
        generate_task_script=generate_task_script,
        db=db,
        config=config,
        data_dir=data_dir,
        config_file=config_file,
        config_write_lock=_config_write_lock,
    )

    _reg_tasks(
        app,
        task_source=task_source,
        db=db,
        workspace=workspace,
        config=config,
    )

    _reg_agents(
        app,
        agent_tracker=agent_tracker,
        orchestrator=orchestrator,
        db=db,
        data_dir=data_dir,
        _last_monitor_tick=_last_monitor_tick,
        monitor_thread=monitor_thread,
        _start_time=_start_time,
        config=config,
        running_commit=_running_commit,
    )

    _reg_config(
        app,
        config=config,
        config_file=config_file,
        orchestrator=orchestrator,
        _runner_mod=_runner_mod,
        data_dir=data_dir,
        _config_write_lock=_config_write_lock,
        auto_mode_state=auto_mode_state,
        generate_task_script=generate_task_script,
        db=db,
    )

    _reg_metrics(
        app,
        data_dir=data_dir,
        workspace=workspace,
        config=config,
        db=db,
        agent_tracker=agent_tracker,
    )

    # ---------- Gardener ----------

    from swarm.api_snapshots import register_routes as _reg_snapshots
    _reg_snapshots(
        app,
        project_registry=project_registry,
        workspace=workspace,
        db=db,
        config=config,
        data_dir=data_dir,
        orchestrator=orchestrator,
        config_file=config_file,
    )

    from swarm.api_gardener import register_routes as _reg_gardener
    _reg_gardener(
        app,
        config=config,
        data_dir=data_dir,
        config_file=config_file,
        _config_write_lock=_config_write_lock,
    )

    # ---------- Meta Mode ----------

    from swarm.api_meta import register_routes as _reg_meta
    _reg_meta(
        app,
        config=config,
        config_file=config_file,
        _config_write_lock=_config_write_lock,
    )

    # ---------- Meta Auditor ----------
    from swarm.api_meta_auditor import register_routes as _reg_auditor
    _reg_auditor(
        app,
        config=config,
        config_file=config_file,
        _config_write_lock=_config_write_lock,
    )

    # ---------- Cartographer ----------
    from swarm.api_cartographer import register_routes as _reg_cartographer
    _reg_cartographer(
        app,
        config=config,
        data_dir=data_dir,
        config_file=config_file,
        _config_write_lock=_config_write_lock,
    )

    # ---------- Librarian ----------
    from swarm.api_librarian import register_routes as _reg_librarian
    _reg_librarian(
        app,
        config=config,
        data_dir=data_dir,
        config_file=config_file,
        _config_write_lock=_config_write_lock,
    )

    # ---------- Archaeologist ----------
    from swarm.api_archaeologist import register_routes as _reg_archaeologist
    _reg_archaeologist(
        app,
        config=config,
        data_dir=data_dir,
        config_file=config_file,
        _config_write_lock=_config_write_lock,
    )

    # ---------- Scheduler ----------
    from swarm.api_scheduler import register_routes as _reg_scheduler
    _reg_scheduler(
        app,
        config=config,
        data_dir=data_dir,
        config_file=config_file,
        _config_write_lock=_config_write_lock,
    )

    # ---------- Strategies ----------

    @app.route("/api/strategies", methods=["GET"])
    def list_strategies():
        from swarm.strategies import list_strategies as get_list
        return jsonify({"strategies": get_list()})

    @app.route("/api/strategy", methods=["GET"])
    def get_current_strategy():
        strategy_name = config.get("task_selection_strategy", "priority")
        return jsonify({"strategy": strategy_name})

    @app.route("/api/strategy", methods=["POST"])
    def set_strategy():
        data = request.json or {}
        strategy = data.get("strategy", "priority")
        from swarm.strategies import list_strategies
        if strategy not in list_strategies():
            return jsonify({"error": f"Unknown strategy: {strategy}"}), 400
        config["task_selection_strategy"] = strategy
        orchestrator.TASK_SELECTION_STRATEGY = strategy
        return jsonify({"strategy": strategy})

    # ---------- Managed / Paused Projects ----------

    def _managed_projects_from_registry():
        return sorted(
            name for name, proj in project_registry.get_all().items()
            if getattr(proj, "managed", True)
        )

    def _persist_managed_projects():
        orchestrator.MANAGED_PROJECTS = _managed_projects_from_registry()
        config["managed_projects"] = orchestrator.MANAGED_PROJECTS
        with _config_write_lock:
            existing_cfg = json.loads(config_file.read_text()) if config_file.exists() else {}
            existing_cfg["managed_projects"] = config.get("managed_projects", [])
            existing_cfg["paused_projects"] = config.get("paused_projects", [])
            config_file.write_text(json.dumps(existing_cfg, indent=2))

    @app.route("/api/managed-projects", methods=["GET"])
    def get_managed_projects():
        return jsonify({
            "managed_projects": _managed_projects_from_registry(),
            "paused_projects": orchestrator.PAUSED_PROJECTS,
        })

    @app.route("/api/managed-projects", methods=["POST"])
    def set_managed_projects():
        data = request.json or {}
        if "managed_projects" in data:
            requested = {str(name) for name in data["managed_projects"]}
            merge = bool(data.get("merge", False))
            if merge:
                # Additive: add requested projects without unmanaging anything else
                for name in requested:
                    if not project_registry.get(name):
                        project_registry.add_project(name, managed=True)
                    else:
                        project_registry.set_managed(name, True)
            else:
                # Replace: unmanage everything not in the list, but guard against
                # accidental empty-list wipe.
                if requested:
                    for name, proj in project_registry.get_all().items():
                        project_registry.set_managed(name, name in requested)
                for name in requested:
                    if not project_registry.get(name):
                        project_registry.add_project(name, managed=True)
                    else:
                        project_registry.set_managed(name, True)
            config["managed_projects"] = sorted(
                n for n, p in project_registry.get_all().items()
                if getattr(p, "managed", True)
            )
            orchestrator.MANAGED_PROJECTS = config["managed_projects"]
        if "paused_projects" in data:
            orchestrator.PAUSED_PROJECTS = list(data["paused_projects"])
            config["paused_projects"] = orchestrator.PAUSED_PROJECTS
        try:
            _persist_managed_projects()
        except Exception as e:
            print(f"[Warning] Could not persist managed_projects: {e}")
        return jsonify({
            "managed_projects": _managed_projects_from_registry(),
            "paused_projects": orchestrator.PAUSED_PROJECTS,
        })

    # ---------- Auto-Replan Toggle ----------

    @app.route("/api/auto-replan", methods=["GET"])
    def get_auto_replan():
        return jsonify({"auto_replan_projects": orchestrator.AUTO_REPLAN_PROJECTS})

    @app.route("/api/auto-replan/<project_name>", methods=["POST"])
    def toggle_auto_replan(project_name):
        data = request.json or {}
        enabled = data.get("enabled", True)
        current = list(orchestrator.AUTO_REPLAN_PROJECTS)
        if enabled and project_name not in current:
            current.append(project_name)
        elif not enabled and project_name in current:
            current.remove(project_name)
        orchestrator.AUTO_REPLAN_PROJECTS = current
        config["auto_replan_projects"] = current
        try:
            with _config_write_lock:
                existing_cfg = json.loads(config_file.read_text()) if config_file.exists() else {}
                existing_cfg["auto_replan_projects"] = current
                config_file.write_text(json.dumps(existing_cfg, indent=2))
        except Exception as e:
            print(f"[Warning] Could not persist auto_replan_projects: {e}")
        return jsonify({"project": project_name, "enabled": enabled, "auto_replan_projects": current})

    @app.route("/api/xogot-projects", methods=["GET"])
    def get_xogot_projects():
        return jsonify({"xogot_projects": config.get("xogot_projects", [])})

    @app.route("/api/xogot-projects/<project_name>", methods=["POST"])
    def toggle_xogot_project(project_name):
        data = request.json or {}
        enabled = data.get("enabled", True)
        current = list(config.get("xogot_projects", []))
        if enabled and project_name not in current:
            current.append(project_name)
        elif not enabled and project_name in current:
            current.remove(project_name)
        config["xogot_projects"] = current
        try:
            with _config_write_lock:
                existing_cfg = json.loads(config_file.read_text()) if config_file.exists() else {}
                existing_cfg["xogot_projects"] = current
                config_file.write_text(json.dumps(existing_cfg, indent=2))
        except Exception as e:
            print(f"[Warning] Could not persist xogot_projects: {e}")
        return jsonify({"project": project_name, "enabled": enabled, "xogot_projects": current})

    from swarm.api_deps import register_routes as _register_deps_routes
    _register_deps_routes(app, task_source=task_source, db=db, data_dir=data_dir, project_registry=project_registry)

    from swarm.api_broadcast import register_routes as _register_broadcast_routes
    _register_broadcast_routes(app, data_dir=data_dir)

    # ---------- Analytics (roadmap #7) ----------
    from swarm.api_analytics import register_routes as _register_analytics_routes
    _register_analytics_routes(app, db=db, data_dir=data_dir, workspace=workspace)

    # ---------- Plans ----------

    from swarm.api_plans import register_routes as _register_plans_routes
    _register_plans_routes(app, db=db)

    # ---------- Webhook ----------

    from swarm.api_webhook import register_routes as _register_webhook_routes
    _register_webhook_routes(
        app,
        config=config,
        config_file=config_file,
        _config_write_lock=_config_write_lock,
        orchestrator=orchestrator,
    )

    # ---------- Project Wizard (extracted to api_wizard.py) ----------

    from swarm.api_wizard import register_routes as _register_wizard_routes
    _register_wizard_routes(
        app,
        config=config,
        config_file=config_file,
        _config_write_lock=_config_write_lock,
        orchestrator=orchestrator,
        db=db,
    )

    from swarm.task_chains import ensure_project_head as _ensure_project_head

    # Ensure all managed projects are registered in the project registry on startup
    _startup_exts = config.get("file_extensions", [".gd"])
    _startup_ignore = set(config.get("ignore_dirs", []))
    for _proj in config.get("managed_projects", []):
        _proj_path = orchestrator.WORKSPACE / _proj
        if _proj_path.exists() and not project_registry.get(_proj):
            try:
                project_registry.add_project(_proj)
                _files = project_registry.scan_project_files(_proj, _startup_exts, _startup_ignore)
                project_registry.update_file_counts(_proj, _files)
                print(f"[Startup] Registered project: {_proj}")
            except Exception as _e:
                print(f"[Startup] Could not scan {_proj}: {_e}")
        # Ensure every registered project has a genesis task
        _p = project_registry.get(_proj)
        if _p and not _p.head_task_id:
            _head = _ensure_project_head(db, _proj)
            if _head:
                project_registry.set_head_task_id(_proj, _head)
        if _p and not getattr(_p, "managed", True):
            project_registry.set_managed(_proj, True)

    # Auto-discover projects with a .swarmproject marker file in the workspace.
    # These appear in the dashboard but are NOT added to managed_projects (no agents)
    # unless the marker file explicitly sets managed: true.
    import yaml as _yaml
    for _candidate in (orchestrator.WORKSPACE.iterdir() if orchestrator.WORKSPACE.exists() else []):
        _marker = _candidate / ".swarmproject"
        if not _marker.exists() or not _candidate.is_dir():
            continue
        _proj_name = _candidate.name
        try:
            _marker_cfg = _yaml.safe_load(_marker.read_text()) or {}
        except Exception:
            _marker_cfg = {}
        _marker_name = _marker_cfg.get("name", _proj_name)
        _marker_managed = bool(_marker_cfg.get("managed", False))
        if not project_registry.get(_marker_name):
            try:
                project_registry.add_project(_marker_name, managed=_marker_managed)
                _files = project_registry.scan_project_files(_marker_name, _startup_exts, _startup_ignore)
                project_registry.update_file_counts(_marker_name, _files)
                print(f"[Startup] Auto-discovered project: {_marker_name} (managed={_marker_managed})")
            except Exception as _e:
                print(f"[Startup] Could not register discovered project {_marker_name}: {_e}")
        if _marker_managed and _marker_name not in config.get("managed_projects", []):
            config.setdefault("managed_projects", []).append(_marker_name)
            print(f"[Startup] Auto-discovered project {_marker_name} added to managed list")

    orchestrator.MANAGED_PROJECTS = _managed_projects_from_registry()
    # Merge: keep anything in config.json that failed to register (don't drop it)
    config_managed = set(config.get("managed_projects", []))
    registry_managed = set(orchestrator.MANAGED_PROJECTS)
    if config_managed - registry_managed:
        for _missing in config_managed - registry_managed:
            print(f"[Startup] WARNING: {_missing} in config but not in registry -- preserving in managed list")
        orchestrator.MANAGED_PROJECTS = sorted(config_managed | registry_managed)
    config["managed_projects"] = orchestrator.MANAGED_PROJECTS

    try:
        from swarm.maintenance.file_locks import reconcile_stale_file_locks
        repaired_file_locks = reconcile_stale_file_locks(db, project_registry)
        if repaired_file_locks:
            print(f"[API] Reconciled {len(repaired_file_locks)} stale project file lock(s)")
    except Exception as exc:
        print(f"[API] Stale file lock reconciliation skipped: {exc}")

    # ---------- Chat, project-chat, create-project-tasks (extracted to api_chat.py) ----------

    from swarm.api_chat import register_routes as _register_chat_routes
    _register_chat_routes(
        app,
        config=config,
        config_file=config_file,
        _config_write_lock=_config_write_lock,
        orchestrator=orchestrator,
        workspace=workspace,
        db=db,
        auto_mode_state=auto_mode_state,
        generate_task_script=generate_task_script,
    )

    return app


def run_app(
    workspace: Optional[Path] = None,
    data_dir: Optional[Path] = None,
    port: int = 5001
):
    """Run the Flask app"""
    app = create_app(workspace, data_dir)
    try:
        import json as _json
        _cfg = _json.loads((Path(__file__).parent.parent / "config.json").read_text())
    except Exception:
        _cfg = {}
    bind_host = "127.0.0.1" if _cfg.get("login_required") else "0.0.0.0"
    app.run(host=bind_host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    run_app()
