"""Config, provider, strategy, and thinking routes for the Swarm API."""

from flask import jsonify, request

import json
import os
import threading
from datetime import datetime, timedelta
from flask import jsonify, request


def register_routes(app, config, config_file, orchestrator, _runner_mod, data_dir, _config_write_lock, auto_mode_state, generate_task_script, db):
    """Register routes on the Flask app."""
    # Re-apply persisted thinking budget to in-memory provider on startup
    if config.get("thinking_enabled") and config.get("thinking_budget", 0) > 0:
        import swarm.agent_runtime as _rt
        provider = _runner_mod.LLM_PROVIDER
        budget = config["thinking_budget"]
        if hasattr(_runner_mod, "LLM_PROVIDERS") and provider in _runner_mod.LLM_PROVIDERS:
            _runner_mod.LLM_PROVIDERS[provider]["thinking_budget"] = budget
        if provider in _rt.LLM_PROVIDERS:
            _rt.LLM_PROVIDERS[provider]["thinking_budget"] = budget
# ---------- Config ----------
    @app.route("/api/config", methods=["GET"])
    def get_config():
        return jsonify(config)
    @app.route("/api/config", methods=["POST"])
    def update_config():
        data = request.json or {}
        for key, value in data.items():
            config[key] = value
        return jsonify(config)
    @app.route("/api/max-agents", methods=["GET"])
    def get_max_agents():
        return jsonify({
            "max_active_agents": config.get("max_active_agents", 3),
            "max_lines": config.get("max_lines", 5000),
            "ignore_dirs": sorted(config.get("ignore_dirs", [])),
            "ignore_extensions": sorted(config.get("ignore_extensions", []))
        })
    @app.route("/api/max-agents", methods=["POST"])
    def set_max_agents():
        data = request.json or {}
        value = data.get("max_active_agents")
        if not isinstance(value, int) or value < 1:
            return jsonify({"error": "max_active_agents must be a positive integer"}), 400
        config["max_active_agents"] = value
        if orchestrator.AUTO_SCALE:
            # In auto-scale mode, max_active_agents is the ceiling
            orchestrator.AUTO_SCALE_CEILING = value
            # Clamp live count immediately if ceiling was lowered
            if orchestrator._auto_scale_current > value:
                orchestrator._auto_scale_current = value
                orchestrator.MAX_ACTIVE_AGENTS = value
        else:
            orchestrator.MAX_ACTIVE_AGENTS = value
        with _config_write_lock:
            cfg = {}
            if config_file.exists():
                try:
                    cfg = json.loads(config_file.read_text())
                except Exception:
                    pass
            cfg["max_active_agents"] = value
            config_file.write_text(json.dumps(cfg, indent=2) + "\n")
        print(f"[Config] max_active_agents set to {value}")
        return jsonify({"max_active_agents": value})
# ---------- Auto Mode ----------
    @app.route("/api/auto-mode", methods=["GET"])
    def get_auto_mode():
        with auto_mode_state["lock"]:
            return jsonify({
                "enabled": auto_mode_state["enabled"],
                "suspended_for_quota": auto_mode_state["suspended_for_quota"],
            })
    @app.route("/api/auto-mode", methods=["POST"])
    def set_auto_mode():
        data = request.json or {}
        enabled = bool(data.get("enabled", False))
        with auto_mode_state["lock"]:
            auto_mode_state["enabled"] = enabled
            auto_mode_state["suspended_for_quota"] = False  # user explicitly set it
            auto_mode_state["user_disabled"] = not enabled  # prevent quota-resume from overriding manual off
        config["auto_mode_enabled"] = enabled
        with _config_write_lock:
            cfg = {}
            if config_file.exists():
                try:
                    cfg = json.loads(config_file.read_text())
                except Exception:
                    pass
            cfg["auto_mode_enabled"] = enabled
            config_file.write_text(json.dumps(cfg, indent=2) + "\n")
        caller = request.remote_addr or "unknown"
        ua = request.headers.get("User-Agent", "")[:60]
        if enabled and generate_task_script is not None:
            spawned_ids, _ = orchestrator.fill_slots(generate_task_script)
            print(f"[Auto] Mode enabled via HTTP — spawned {len(spawned_ids)} agent(s) | caller={caller} ua={ua}")
            return jsonify({"enabled": True, "spawned": len(spawned_ids)})
        print(f"[Auto] Mode disabled via HTTP | caller={caller} ua={ua}")
        return jsonify({"enabled": auto_mode_state["enabled"]})
# ---------- Auto-scale ----------
    @app.route("/api/auto-scale", methods=["GET"])
    def get_auto_scale():
        return jsonify({
            "enabled": orchestrator.AUTO_SCALE,
            "current": orchestrator.MAX_ACTIVE_AGENTS,
            "ceiling": orchestrator.AUTO_SCALE_CEILING,
        })

    @app.route("/api/auto-scale", methods=["POST"])
    def set_auto_scale():
        data = request.json or {}
        enabled = bool(data.get("enabled", False))
        orchestrator.AUTO_SCALE = enabled
        config["auto_scale"] = enabled
        if enabled:
            # Ceiling = whatever max_active_agents is currently set to
            orchestrator.AUTO_SCALE_CEILING = config.get("max_active_agents", 60)
            # Start from current active count so we don't burst above where we already are
            orchestrator._auto_scale_current = max(3, orchestrator.get_active_count() or 3)
            orchestrator.MAX_ACTIVE_AGENTS = orchestrator._auto_scale_current
            orchestrator._auto_scale_last_change = 0.0
            orchestrator._auto_scale_clean_cycles = 0
        else:
            # Restore MAX_ACTIVE_AGENTS to the config ceiling when disabling
            orchestrator.MAX_ACTIVE_AGENTS = config.get("max_active_agents", 3)
        with _config_write_lock:
            cfg = {}
            if config_file.exists():
                try:
                    cfg = json.loads(config_file.read_text())
                except Exception:
                    pass
            cfg["auto_scale"] = enabled
            config_file.write_text(json.dumps(cfg, indent=2) + "\n")
        print(f"[AutoScale] {'enabled' if enabled else 'disabled'} (ceiling={orchestrator.AUTO_SCALE_CEILING})")
        return jsonify({
            "enabled": enabled,
            "current": orchestrator.MAX_ACTIVE_AGENTS,
            "ceiling": orchestrator.AUTO_SCALE_CEILING,
        })
# ---------- Rescan ----------
    @app.route("/api/rescan", methods=["POST"])
    def rescan():
        orchestrator.update_project_registry(
            tuple(config.get("file_extensions", [".gd"]))
        )
        return jsonify({"status": "ok"})
# ---------- Auto-scale history ----------
    @app.route("/api/auto-scale/history", methods=["GET"])
    def get_auto_scale_history():
        """Return 429 event counts bucketed by hour-of-day and by date for graphing."""
        import json as _json
        from datetime import datetime as _dt
        from collections import defaultdict as _dd
        archive = data_dir / "rl_events_archive.jsonl"
        by_hour = _dd(int)   # hour 0-23 → count (all-time)
        by_date_hour = _dd(lambda: _dd(int))  # date str → hour → count
        total = 0
        if archive.exists():
            for line in archive.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    ev = _json.loads(line)
                    t = ev.get("t", 0)
                    dt = _dt.fromtimestamp(t)
                    by_hour[dt.hour] += 1
                    by_date_hour[dt.strftime("%Y-%m-%d")][dt.hour] += 1
                    total += 1
                except Exception:
                    pass
        # Build hourly series (0-23, fill missing with 0)
        hourly = [{"hour": h, "count": by_hour.get(h, 0)} for h in range(24)]
        # Build daily breakdown (last 7 days)
        from datetime import timedelta as _td
        today = _dt.now().date()
        daily = []
        for i in range(6, -1, -1):
            d = (today - _td(days=i)).strftime("%Y-%m-%d")
            day_hours = by_date_hour.get(d, {})
            daily.append({
                "date": d,
                "total": sum(day_hours.values()),
                "by_hour": [day_hours.get(h, 0) for h in range(24)],
            })
        return jsonify({"total": total, "by_hour": hourly, "daily": daily})

# ---------- Quota ----------
    @app.route("/api/quota", methods=["GET"])
    def get_quota():
        try:
            import requests
            api_key = os.environ.get("MINIMAX_API_KEY")
            if api_key:
                resp = requests.get(
                    "https://www.minimax.io/v1/api/openplatform/coding_plan/remains",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=10
                )
                if resp.status_code == 200:
                    return jsonify(resp.json())
            return jsonify({"error": "No API key"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    @app.route("/api/quota-limit", methods=["GET"])
    def get_quota_limit():
        over_limit, pct_used, pct_remaining, used_count, total = orchestrator.check_quota_limit()
        return jsonify({
            "limit_percent": orchestrator.QUOTA_LIMIT_PERCENT,
            "used_percent": round(pct_used, 1),
            "remaining_percent": round(pct_remaining, 1),
            "used_count": used_count,
            "total_count": total,
            "over_limit": over_limit
        })
    @app.route("/api/quota-limit", methods=["POST"])
    def set_quota_limit():
        data = request.json or {}
        value = data.get("limit_percent")
        if not isinstance(value, (int, float)) or value < 0 or value > 100:
            return jsonify({"error": "limit_percent must be 0-100"}), 400
        orchestrator.QUOTA_LIMIT_PERCENT = value
        config["quota_limit_percent"] = value
        with _config_write_lock:
            cfg = {}
            if config_file.exists():
                try:
                    cfg = json.loads(config_file.read_text())
                except Exception:
                    pass
            cfg["quota_limit_percent"] = value
            config_file.write_text(json.dumps(cfg, indent=2) + "\n")
        print(f"[Config] quota_limit_percent set to {value}")
        return jsonify({"limit_percent": value})
# ---------- QA Max Cycles ----------
    @app.route("/api/qa-max-cycles", methods=["GET"])
    def get_qa_max_cycles():
        return jsonify({"qa_max_cycles": config.get("qa_max_cycles", 3)})
    @app.route("/api/qa-max-cycles", methods=["POST"])
    def set_qa_max_cycles():
        data = request.json or {}
        value = data.get("qa_max_cycles")
        if not isinstance(value, int) or value < 1:
            return jsonify({"error": "qa_max_cycles must be a positive integer"}), 400
        config["qa_max_cycles"] = value
        with _config_write_lock:
            cfg = {}
            if config_file.exists():
                try:
                    cfg = json.loads(config_file.read_text())
                except Exception:
                    pass
            cfg["qa_max_cycles"] = value
            config_file.write_text(json.dumps(cfg, indent=2) + "\n")
        print(f"[Config] qa_max_cycles set to {value}")
        return jsonify({"qa_max_cycles": value})
# ---------- Worktrees ----------
    @app.route("/api/worktrees", methods=["GET"])
    def get_worktrees():
        return jsonify({"use_worktrees": orchestrator.USE_WORKTREES})
    @app.route("/api/worktrees", methods=["POST"])
    def set_worktrees():
        data = request.json or {}
        enabled = bool(data.get("enabled", False))
        orchestrator.USE_WORKTREES = enabled
        config["use_worktrees"] = enabled
        with _config_write_lock:
            cfg = {}
            if config_file.exists():
                try:
                    cfg = json.loads(config_file.read_text())
                except Exception:
                    pass
            cfg["use_worktrees"] = enabled
            config_file.write_text(json.dumps(cfg, indent=2) + "\n")
        print(f"[Config] use_worktrees set to {enabled}")
        return jsonify({"use_worktrees": enabled})
# ---------- LLM Providers ----------
    @app.route("/api/providers", methods=["GET"])
    def list_providers():
        """List all configured providers and their settings (keys redacted)."""
        try:
            providers = _runner_mod.LLM_PROVIDERS
        except Exception:
            providers = {}
        safe = {}
        for name, cfg in providers.items():
            safe[name] = {k: v for k, v in cfg.items() if k != "api_key_env"}
            safe[name]["api_key_env"] = cfg.get("api_key_env", "")
            # Indicate whether the env var is set
            safe[name]["api_key_set"] = bool(os.environ.get(cfg.get("api_key_env", ""), ""))
        return jsonify({
            "providers": safe,
            "current": config.get("llm_provider", "minimax"),
        })
    @app.route("/api/provider", methods=["GET"])
    def get_provider():
        return jsonify({
            "provider": config.get("llm_provider", "minimax"),
            "model": (
                _runner_mod.LLM_PROVIDERS
                .get(config.get("llm_provider", "minimax"), {})
                .get("model", "")
            ) if hasattr(_runner_mod, "LLM_PROVIDERS") else "",
        })
    @app.route("/api/provider", methods=["POST"])
    def set_provider():
        """Set active provider and optionally override model."""
        data = request.json or {}
        provider = data.get("provider")
        if not provider:
            return jsonify({"error": "provider required"}), 400
        # Allow registering a new provider on the fly
        if provider not in getattr(_runner_mod, "LLM_PROVIDERS", {}):
            if not data.get("base_url") or not data.get("api_key_env"):
                return jsonify({"error": f"Unknown provider '{provider}'. Supply base_url and api_key_env to register it."}), 400
            _runner_mod.LLM_PROVIDERS[provider] = {
                "base_url":    data["base_url"],
                "model":       data.get("model", ""),
                "api_key_env": data["api_key_env"],
                "format":      data.get("format", "openai"),
                "max_tokens":  data.get("max_tokens", 8096),
            }
        # Override model if provided
        if data.get("model") and hasattr(_runner_mod, "LLM_PROVIDERS"):
            _runner_mod.LLM_PROVIDERS[provider]["model"] = data["model"]
        _runner_mod.LLM_PROVIDER = provider
        orchestrator.LLM_PROVIDER = provider
        config["llm_provider"] = provider
        # Update fallback list: move the new primary to front, keep rest
        existing_fallbacks = config.get("fallback_providers", [])
        if existing_fallbacks:
            rotated = [provider] + [p for p in existing_fallbacks if p != provider]
            orchestrator.FALLBACK_PROVIDERS = rotated
        if data.get("model"):
            config.setdefault("llm_providers", {}).setdefault(provider, {})["model"] = data["model"]
        # Persist full provider config to config.json (not just model) so restarts work
        _full_pcfg = _runner_mod.LLM_PROVIDERS.get(provider, {})
        with _config_write_lock:
            cfg_data = {}
            if config_file.exists():
                try:
                    cfg_data = json.loads(config_file.read_text())
                except Exception:
                    pass
            cfg_data["llm_provider"] = provider
            # Always persist the full provider config so custom providers survive restarts
            if _full_pcfg:
                cfg_data.setdefault("llm_providers", {})[provider] = dict(_full_pcfg)
            config_file.write_text(json.dumps(cfg_data, indent=2) + "\n")
        print(f"[Config] llm_provider set to {provider}")
        return jsonify({"provider": provider, "model": _runner_mod.LLM_PROVIDERS.get(provider, {}).get("model", "")})
# ---------- Thinking Toggle ----------
    @app.route("/api/thinking", methods=["GET"])
    def get_thinking():
        budget = _runner_mod.LLM_PROVIDERS.get(_runner_mod.LLM_PROVIDER, {}).get("thinking_budget", 0)
        return jsonify({
            "enabled": budget > 0,
            "budget_tokens": budget,
            "provider": _runner_mod.LLM_PROVIDER,
            "task_types": config.get("thinking_task_types", []),
        })
    @app.route("/api/thinking", methods=["POST"])
    def set_thinking():
        data = request.json or {}
        enabled = data.get("enabled", False)
        budget = int(data.get("budget_tokens", data.get("budget", 10000))) if enabled else 0
        provider = _runner_mod.LLM_PROVIDER
        if hasattr(_runner_mod, "LLM_PROVIDERS") and provider in _runner_mod.LLM_PROVIDERS:
            _runner_mod.LLM_PROVIDERS[provider]["thinking_budget"] = budget
        import swarm.agent_runtime as _rt
        if provider in _rt.LLM_PROVIDERS:
            _rt.LLM_PROVIDERS[provider]["thinking_budget"] = budget
        # Persist to config.json so it survives restarts
        config["thinking_enabled"] = enabled
        config["thinking_budget"] = budget
        if enabled and not config.get("thinking_task_types"):
            # Default: enable for all meaningful task types
            config["thinking_task_types"] = [
                "feature", "bug", "refactor", "polish",
                "qa", "harness_qa", "hybrid_qa", "plan", "python_plan", "project_plan", "research"
            ]
        elif not enabled:
            config["thinking_task_types"] = []
        with _config_write_lock:
            cfg = {}
            if config_file.exists():
                try:
                    cfg = json.loads(config_file.read_text())
                except Exception:
                    pass
            cfg["thinking_enabled"] = enabled
            cfg["thinking_budget"] = budget
            cfg["thinking_task_types"] = config.get("thinking_task_types", [])
            config_file.write_text(json.dumps(cfg, indent=2))
        print(f"[Config] thinking {'enabled' if enabled else 'disabled'} for {provider} (budget={budget})")
        return jsonify({"enabled": enabled, "budget_tokens": budget, "provider": provider,
                        "task_types": config.get("thinking_task_types", [])})
# ---------- MCP Servers ----------
    @app.route("/api/mcp-servers", methods=["GET"])
    def get_mcp_servers():
        return jsonify({"mcp_servers": orchestrator.MCP_SERVERS})
    @app.route("/api/mcp-servers", methods=["POST"])
    def set_mcp_servers():
        data = request.json or {}
        servers = data.get("mcp_servers", {})
        orchestrator.MCP_SERVERS = servers
        config["mcp_servers"] = servers
        with _config_write_lock:
            cfg = {}
            if config_file.exists():
                try:
                    cfg = json.loads(config_file.read_text())
                except Exception:
                    pass
            cfg["mcp_servers"] = servers
            config_file.write_text(json.dumps(cfg, indent=2) + "\n")
        print(f"[Config] mcp_servers updated")
        return jsonify({"mcp_servers": servers})
# ---------- Vision Providers ----------
    @app.route("/api/vision-providers", methods=["GET"])
    def get_vision_providers():
        return jsonify({
            "vision_provider": config.get("vision_provider", "claude"),
            "vision_provider_fast": config.get("vision_provider_fast", ""),
            "vision_providers": config.get("vision_providers", {}),
        })
    @app.route("/api/vision-providers", methods=["POST"])
    def set_vision_providers():
        data = request.json or {}
        updated = {}
        for key in ("vision_provider", "vision_provider_fast", "vision_providers"):
            if key in data:
                config[key] = data[key]
                updated[key] = data[key]
        with _config_write_lock:
            cfg = {}
            if config_file.exists():
                try:
                    cfg = json.loads(config_file.read_text())
                except Exception:
                    pass
            cfg.update(updated)
            config_file.write_text(json.dumps(cfg, indent=2) + "\n")
        print(f"[Config] vision_providers updated: {list(updated.keys())}")
        return jsonify({"ok": True, **updated})
