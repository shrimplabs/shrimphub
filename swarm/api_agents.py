"""Agents route handlers for the Swarm API."""

from flask import jsonify, request

import json
import os
import time
import urllib.request
from pathlib import Path
from flask import Response, jsonify, request, send_file

from swarm.platform import kill_pid_tree, kill_godot_children


def register_routes(app, agent_tracker, orchestrator, db, data_dir, _last_monitor_tick, monitor_thread, _start_time, config, running_commit=""):
    """Register routes on the Flask app."""
    # Stale-code detection state: compare the commit this process is running
    # against the repo's current HEAD, re-checked at most once per 60s.
    _repo_commit_cache = {"value": "", "checked_at": 0.0}
    _shrimp_cache: dict = {"ok": None, "backends": {}, "checked_at": 0.0}

    def _check_shrimp_router() -> dict:
        now = time.time()
        if now - _shrimp_cache["checked_at"] > 30:
            try:
                req = urllib.request.Request(
                    "http://localhost:8090/health",
                    headers={"Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = json.loads(resp.read())
                _shrimp_cache["ok"] = bool(data.get("ok"))
                _shrimp_cache["backends"] = data.get("backends", {})
            except Exception:
                _shrimp_cache["ok"] = False
                _shrimp_cache["backends"] = {}
            _shrimp_cache["checked_at"] = now
        return _shrimp_cache

    def _current_repo_commit() -> str:
        now = time.time()
        if now - _repo_commit_cache["checked_at"] > 60:
            try:
                import subprocess
                _repo_commit_cache["value"] = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                    cwd=str(Path(__file__).parent.parent),
                ).stdout.strip()
            except Exception:
                _repo_commit_cache["value"] = ""
            _repo_commit_cache["checked_at"] = now
        return _repo_commit_cache["value"]
# ---------- Agents ----------
    @app.route("/api/agents", methods=["GET"])
    def list_agents():
        agents = agent_tracker.get_all()
        for agent in agents:
            if agent.status == "active" and agent.log_path:
                try:
                    log_path = Path(agent.log_path)
                    if log_path.exists():
                        agent.output = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                except Exception:
                    pass
                # Read live token counts written each LLM loop by agent_runtime
                if agent.task_id:
                    try:
                        tok_file = Path(data_dir) / f"agent_{agent.task_id}_tokens.json"
                        if tok_file.exists():
                            tok = json.loads(tok_file.read_text())
                            agent.input_tokens = tok.get("input", 0)
                            agent.output_tokens = tok.get("output", 0)
                            agent.metadata["conv_estimate"] = tok.get("conv_estimate", 0)
                            agent.metadata["compact_threshold"] = tok.get("compact_threshold", 120000)
                            agent.metadata["cache_read_tokens"] = tok.get("cache_read", 0)
                            agent.metadata["cache_write_tokens"] = tok.get("cache_write", 0)
                    except Exception:
                        pass
        import re as _re
        result = []
        for a in agents:
            d = a.to_dict()
            if a.status == "active" and a.output:
                m = list(_re.finditer(r'\(loop (\d+)/(\d+)\)', a.output))
                if m:
                    last = m[-1]
                    d["current_loop"] = int(last.group(1))
                    d["max_loops"] = int(last.group(2))
            result.append(d)
        return jsonify({"agents": result})
    @app.route("/api/agents/<agent_id>", methods=["GET"])
    def get_agent(agent_id):
        agent = agent_tracker.get(agent_id)
        if agent is None:
            return jsonify({"error": "Agent not found"}), 404
        return jsonify({"agent": agent.to_dict()})
    @app.route("/api/agents/<agent_id>/output", methods=["GET"])
    @app.route("/api/agent/<agent_id>/output", methods=["GET"])
    def get_agent_output(agent_id):
        agent = agent_tracker.get(agent_id)
        log_path = None
        if agent is not None:
            log_path = agent.log_path
        else:
            # Agent pruned from DB — search history JSONL
            history_file = Path(data_dir) / "agent-history.jsonl"
            if history_file.exists():
                for line in history_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        rec = json.loads(line)
                        if rec.get("id") == agent_id:
                            log_path = rec.get("log_path")
                            break
                    except Exception:
                        continue
        if log_path is None:
            return jsonify({"error": "Agent not found"}), 404
        output = ""
        try:
            output = Path(log_path).read_text(encoding="utf-8", errors="replace")[-8000:]
        except Exception:
            output = "Log file not available"
        return jsonify({"output": output})
    @app.route("/api/agents/<agent_id>/stream", methods=["GET"])
    def stream_agent_output(agent_id):
        """Server-Sent Events stream of the agent's log tail (#9)."""
        from flask import Response, stream_with_context
        # Number of tail lines to send on first connect (avoids replaying huge logs)
        INITIAL_TAIL_LINES = int(request.args.get("tail", 200))

        def _generate():
            idle_ticks = 0
            log_fh = None
            try:
                while True:
                    agent = agent_tracker.get(agent_id)
                    if agent is None:
                        yield "event: done\ndata: Agent not found\n\n"
                        return
                    log_path = agent.log_path and Path(agent.log_path)
                    if log_path and log_path.exists():
                        try:
                            if log_fh is None:
                                # First open: send only the last INITIAL_TAIL_LINES lines,
                                # then seek to end so subsequent reads only return new bytes.
                                with open(log_path, encoding="utf-8", errors="replace") as tmp:
                                    all_lines = tmp.readlines()
                                tail_lines = all_lines[-INITIAL_TAIL_LINES:] if INITIAL_TAIL_LINES > 0 else []
                                if tail_lines:
                                    for line in tail_lines:
                                        yield f"data: {line.rstrip()}\n"
                                    yield "\n"
                                log_fh = open(log_path, encoding="utf-8", errors="replace")
                                log_fh.seek(0, 2)  # seek to end
                            chunk = log_fh.read()
                            if chunk:
                                for line in chunk.splitlines():
                                    yield f"data: {line}\n"
                                yield "\n"
                                idle_ticks = 0
                            else:
                                idle_ticks += 1
                        except Exception:
                            pass
                    if agent.status != "active":
                        yield "event: done\ndata: finished\n\n"
                        return
                    # Keep-alive ping every ~10s of silence
                    if idle_ticks > 0 and idle_ticks % 5 == 0:
                        yield ": ping\n\n"
                    time.sleep(2)
            finally:
                if log_fh is not None:
                    log_fh.close()
        return Response(
            stream_with_context(_generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    @app.route("/api/agents/<agent_id>/kill", methods=["POST"])
    @app.route("/api/kill/<agent_id>", methods=["POST"])
    def kill_agent(agent_id):
        import signal
        with orchestrator._handle_lock:
            handle = orchestrator._active_handles.get(agent_id)
        if handle:
            try:
                print(f"[SwarmKill] reason=manual_api_kill agent={agent_id[:8]} pid={handle['process'].pid}")
                kill_godot_children(handle["process"].pid)
                handle["process"].kill()
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"success": False, "error": str(e)})
        # Handle lost after restart — kill by PID from DB
        agent = db.agent_get(agent_id)
        if agent and agent.get("pid"):
            try:
                print(f"[SwarmKill] reason=manual_api_kill_lost_handle agent={agent_id[:8]} pid={agent['pid']}")
                kill_pid_tree(int(agent["pid"]))
                db.agent_update_status(agent_id, "failed")
                db.task_update_status(agent["task_id"], "pending", agent_id=None)
                return jsonify({"success": True})
            except ProcessLookupError:
                return jsonify({"success": False, "error": "Process already exited"})
            except Exception as e:
                return jsonify({"success": False, "error": str(e)})
        return jsonify({"success": False, "error": "Agent not found"})

    @app.route("/api/agents/<agent_id>/hint", methods=["POST"])
    def hint_agent(agent_id):
        data = request.json or {}
        message = (data.get("message") or "").strip()
        if not message:
            return jsonify({"success": False, "error": "message required"})
        agent = db.agent_get(agent_id)
        if not agent:
            return jsonify({"success": False, "error": "Agent not found"})
        task_id = agent.get("task_id", "")
        hint_path = data_dir / f"agent_{task_id}.hint"
        hint_path.write_text(message)
        return jsonify({"success": True, "task_id": task_id})

    @app.route("/api/agents/active", methods=["GET"])
    def get_active_agents():
        return jsonify({
            "active_count": orchestrator.get_active_count(),
            "max_active": config.get("max_active_agents", 3)
        })

    @app.route("/api/agents/reconcile", methods=["POST"])
    def reconcile_agents():
        result = orchestrator.reconcile_agent_runtime_state()
        # Join finish threads so the response reflects final task state.
        # This is a manual admin endpoint; a short block is acceptable.
        for t in result.pop("_finish_threads", []):
            t.join(timeout=30)
        return jsonify(result)
    @app.route("/api/health", methods=["GET"])
    def health():
        monitor_lag = time.time() - _last_monitor_tick[0]
        try:
            import swarm_runner as _runner_mod
            prompt_warnings = list(_runner_mod._prompt_warnings)
        except Exception:
            prompt_warnings = []
        repo_commit = _current_repo_commit()
        return jsonify({
            "status": "ok",
            "monitor_alive": monitor_thread.is_alive(),
            "monitor_lag_seconds": round(monitor_lag, 1),
            "active_agents": orchestrator.get_active_count(),
            "max_agents": orchestrator.MAX_ACTIVE_AGENTS,
            "uptime_seconds": int(time.time() - _start_time),
            "prompt_warnings": prompt_warnings,
            "running_commit": running_commit,
            "repo_commit": repo_commit,
            "code_stale": bool(running_commit and repo_commit and running_commit != repo_commit),
            "shrimp_router": _check_shrimp_router(),
            "circuit_breaker": {
                "open": orchestrator._circuit_breaker["open"],
                "reason": orchestrator._circuit_breaker["reason"],
            },
        })
