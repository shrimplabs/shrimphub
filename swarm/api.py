"""
Swarm API Module

Flask API for the Swarm Controller.
Provides endpoints for managing projects, tasks, and agents.
"""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
import json
import os
import sqlite3
import sys
import threading
import time

from flask import Flask, jsonify, request, send_file, send_from_directory

from swarm.constants import AGENT_TIMEOUT
from swarm.task_chains import chain_to_project_head


_config_write_lock = threading.Lock()

# Re-export for backward compatibility with tests that import from swarm.api
from swarm.api_webhook import fire_webhook as _fire_webhook


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

    # Import modules after app creation to avoid circular imports
    from swarm import db
    from swarm.tasks import SQLiteTaskSource
    from swarm.projects import SQLiteProjectRegistry
    from swarm.agents import SQLiteAgentTracker

    # Initialise SQLite (migrates from JSON files on first run)
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

    task_source      = SQLiteTaskSource()
    project_registry = SQLiteProjectRegistry(workspace)
    agent_tracker    = SQLiteAgentTracker()

    # Initialise orchestrator with runtime config
    from swarm import orchestrator
    orchestrator.WORKSPACE           = workspace
    orchestrator.DATA_DIR            = data_dir
    orchestrator.HISTORY_FILE        = data_dir / "agent-history.jsonl"
    orchestrator.MAX_ACTIVE_AGENTS   = config.get("max_active_agents", 3)
    orchestrator.MAX_LINES           = config.get("max_lines", 5000)
    orchestrator.LOCK_PROJECT        = config.get("lock_project", False)
    orchestrator.AGENT_TIMEOUT       = config.get("agent_timeout", AGENT_TIMEOUT)
    orchestrator.QUOTA_LIMIT_PERCENT = config.get("quota_limit_percent", 90)
    orchestrator.USE_WORKTREES       = config.get("use_worktrees", True)
    orchestrator.MCP_SERVERS         = config.get("mcp_servers", {})
    orchestrator.IGNORE_DIRS         = set(config.get("ignore_dirs", []))
    orchestrator.IGNORE_EXTENSIONS   = set(config.get("ignore_extensions", []))
    orchestrator.MANAGED_PROJECTS         = config.get("managed_projects", [])
    orchestrator.TASK_SELECTION_STRATEGY  = config.get("task_selection_strategy", "priority")
    orchestrator.PAUSED_PROJECTS          = config.get("paused_projects", [])
    orchestrator.AUTO_REPLAN_PROJECTS     = config.get("auto_replan_projects", [])
    # On startup, assume QA is already done for all auto_replan projects so the first
    # fill_slots fires the sprint planner (not QA) when the queue is empty.
    orchestrator._projects_sprint_qa_done.update(config.get("auto_replan_projects", []))
    orchestrator.MINIMAX_API_KEY     = os.environ.get("MINIMAX_API_KEY", "")
    orchestrator.LLM_PROVIDER        = config.get("llm_provider", "minimax")
    orchestrator.FALLBACK_PROVIDERS  = config.get("fallback_providers", [])
    orchestrator.WEBHOOK_URL         = config.get("completion_webhook_url", "")

    # Re-configure agent_lifecycle with the resolved runtime values. The module-level
    # configure() call in orchestrator.py runs at import time with WORKSPACE=Path("."),
    # so it must be re-applied here after api.py has resolved the real paths.
    from swarm import agent_lifecycle
    agent_lifecycle.configure(
        workspace=workspace,
        data_dir=data_dir,
        use_worktrees=config.get("use_worktrees", True),
        webhook_url=config.get("completion_webhook_url", ""),
        auto_replan_projects=config.get("auto_replan_projects", []),
        paused_projects=config.get("paused_projects", []),
        max_active_agents=config.get("max_active_agents", 3),
        agent_timeout=config.get("agent_timeout", AGENT_TIMEOUT),
        project_registry=project_registry,
    )

    # Import generate_task_script from the runner (single source of truth for prompts).
    # Sync the runner's module-level config vars so generate_task_script uses resolved values.
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
    except Exception:
        generate_task_script = None
    print(f"[API] SQLite db initialised at {db_path}")

    # Orphan detection: on startup, any agent still "active" has no live process
    # handle (they were lost on the previous server shutdown). If the agent has
    # been running longer than agent_timeout, mark it failed and reset its task.
    _agent_timeout = config.get("agent_timeout", AGENT_TIMEOUT)
    _now = datetime.now()
    for _a in db.agent_get_active():
        if _a.get("status") != "active":
            continue
        spawned_at = _a.get("spawned_at", "")
        try:
            age = (_now - datetime.fromisoformat(spawned_at)).total_seconds()
        except Exception:
            age = _agent_timeout + 1  # unknown age → treat as timed out
        _aid = _a["id"]
        if age > _agent_timeout:
            print(f"[API] Orphan agent {_aid[:8]} (age {int(age)}s) — marking failed")
            db.agent_update_status(_aid, "failed",
                                   completed_at=_now.isoformat(), exit_code=-1,
                                   output="[Swarm] Orphan: server restarted while agent was running")
            if _a.get("task_id"):
                db.task_update_status(_a["task_id"], "pending")
        else:
            # Agent was spawned recently enough — assume it's still running.
            # It will be reaped normally on the next monitor tick once its
            # process exits, or timed out by the existing hung-agent check.
            print(f"[API] Orphan agent {_aid[:8]} (age {int(age)}s) — within timeout, will reap normally")

    _start_time = time.time()

    # Clean up any worktrees left over from a previous server run
    orchestrator.cleanup_orphaned_worktrees(workspace)

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
    from swarm.api_auth import register_routes as _register_auth_routes, require_auth
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

    def _is_transient_monitor_db_error(exc: Exception) -> bool:
        """Tests and startup can swap DBs while an old daemon monitor is winding down."""
        return isinstance(exc, sqlite3.OperationalError) and "no such table:" in str(exc)

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
                orchestrator.check_ghost_merge_tasks()
                orchestrator.check_dep_violations()
                orchestrator.check_agent_status()

                # Check for rate-limit pressure from agent subprocesses.
                # Rate limiting is separate from quota: use its own cooldown rather
                # than the quota-suspension flag (which would immediately auto-resume).
                rate_limited = orchestrator.check_rate_limit_flags()
                if rate_limited:
                    new_cooldown = time.time() + _rate_limit_cooldown_secs
                    _rate_limit_cooldown_until[0] = new_cooldown
                    print(f"[Auto] {rate_limited} rate limited — spawn cooldown {_rate_limit_cooldown_secs}s")

                # Also check rolling 429 event pressure (agents write rl_events.jsonl)
                try:
                    _rl_file = data_dir / "rl_events.jsonl"
                    if _rl_file.exists():
                        _now = time.time()
                        _window = 120  # 2-minute window
                        _rl_lines = _rl_file.read_text().splitlines()
                        _recent_count = 0
                        for _l in _rl_lines:
                            try:
                                _ev = json.loads(_l)
                                if _now - _ev.get("t", 0) < _window:
                                    _recent_count += 1
                            except Exception:
                                pass
                        # Trim old events (keep last 200 lines)
                        if len(_rl_lines) > 200:
                            _rl_file.write_text("\n".join(_rl_lines[-200:]) + "\n")
                        if _recent_count >= 5 and time.time() >= _rate_limit_cooldown_until[0]:
                            _rate_limit_cooldown_until[0] = time.time() + _rate_limit_cooldown_secs
                            print(f"[Auto] {_recent_count} rate limit events in {_window}s — spawn cooldown {_rate_limit_cooldown_secs}s")
                except Exception:
                    pass

                over_limit, pct_used, *_ = orchestrator.check_quota_limit()
                if over_limit:
                    with auto_mode_state["lock"]:
                        if auto_mode_state["enabled"]:
                            auto_mode_state["enabled"] = False
                            auto_mode_state["suspended_for_quota"] = True
                            print(f"[Auto] Quota limit exceeded ({pct_used:.1f}%) — auto mode suspended")
                    continue

                # Quota cleared — resume if we suspended due to quota
                with auto_mode_state["lock"]:
                    if auto_mode_state["suspended_for_quota"] and not auto_mode_state["enabled"]:
                        auto_mode_state["enabled"] = True
                        auto_mode_state["suspended_for_quota"] = False
                        print("[Auto] Quota OK — auto mode resumed")

                # Fill agent slots if auto mode is on and not in rate-limit cooldown
                with auto_mode_state["lock"]:
                    _auto_on = auto_mode_state["enabled"]
                _in_cooldown = time.time() < _rate_limit_cooldown_until[0]
                if _in_cooldown:
                    _remaining = int(_rate_limit_cooldown_until[0] - time.time())
                    print(f"[Auto] Rate limit cooldown — {_remaining}s remaining, skipping spawn")
                elif _auto_on and generate_task_script is not None:
                    _spawned, _skipped = orchestrator.fill_slots(generate_task_script)
                    if _spawned:
                        print(f"[Monitor] fill_slots spawned {len(_spawned)} agent(s): {[s[:8] for s in _spawned]}")
                    elif orchestrator.get_active_count() < config.get("max_active_agents", 3):
                        print(f"[Monitor] fill_slots idle (active={orchestrator.get_active_count()}, pending tasks exist but none runnable or no tasks)")

                # Run auto-replan for projects with auto-replan enabled
                for _proj in config.get("auto_replan_projects", []):
                    _queue_empty = (len(db.task_get_by_status("pending")) == 0 or
                                    not any(t.get("project") == _proj for t in db.task_get_by_status("pending")))
                    if _queue_empty and orchestrator.get_active_count() == 0:
                        with auto_mode_state["lock"]:
                            mode_on = auto_mode_state["enabled"]
                        if mode_on:
                            try:
                                _already = any(
                                    t.get("project") == _proj and t.get("type") == "project_plan"
                                    for t in db.task_get_by_status("pending") + db.task_get_by_status("in_progress")
                                )
                                if not _already:
                                    import time as _time
                                    _plan_id = f"project-plan-{_proj}-{int(_time.time())}"
                                    db.task_upsert({
                                        "id": _plan_id,
                                        "project": _proj,
                                        "type": "project_plan",
                                        "description": (
                                            f"Generate a dependency-ordered task plan for {_proj}. "
                                            f"Read GAME_DESIGN.md and the existing codebase, then create all "
                                            f"necessary tasks via the API with proper dependencies so systems "
                                            f"are built and wired together in the correct order."
                                        ),
                                        "priority": 100,
                                        "status": "pending",
                                        "attempts": 0,
                                        "max_attempts": 2,
                                        "dependencies": chain_to_project_head(db, _proj, task_id=_plan_id, ensure_head=True),
                                        "metadata": {},
                                    })
                                    print(f"[Auto] Spawned project_plan task {_plan_id} for {_proj} (chained after project head)")
                            except Exception as _e:
                                print(f"[Auto] Error in auto-replan for {_proj}: {_e}")
            except Exception as exc:
                if _is_transient_monitor_db_error(exc):
                    time.sleep(0.25)
                    continue
                import traceback
                traceback.print_exc()

    if config.get("disable_monitor", False):
        monitor_thread = SimpleNamespace(is_alive=lambda: False)
    else:
        monitor_thread = threading.Thread(target=_monitor, daemon=True)
        monitor_thread.start()

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
    )

    _reg_tasks(
        app,
        task_source=task_source,
        db=db,
        workspace=workspace,
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
            for name, proj in project_registry.get_all().items():
                project_registry.set_managed(name, name in requested)
            for name in requested:
                if not project_registry.get(name):
                    project_registry.add_project(name, managed=True)
                else:
                    project_registry.set_managed(name, True)
            config["managed_projects"] = sorted(requested)
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

    from swarm.api_deps import register_routes as _register_deps_routes
    _register_deps_routes(app, task_source=task_source, db=db, data_dir=data_dir, project_registry=project_registry)

    from swarm.api_broadcast import register_routes as _register_broadcast_routes
    _register_broadcast_routes(app, data_dir=data_dir)

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

    orchestrator.MANAGED_PROJECTS = _managed_projects_from_registry()
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
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    run_app()
