"""Config, provider, strategy, and thinking routes for the Swarm API."""

from flask import jsonify, request

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from flask import jsonify, request
from swarm.config_store import ConfigReadError, ConfigWriteRefused, read_config_file, update_config_file


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
        try:
            update_config_file(config_file, {"max_active_agents": value}, lock=_config_write_lock)
        except (ConfigReadError, ConfigWriteRefused) as e:
            print(f"[Config] Warning: could not persist max_active_agents: {e}")
        print(f"[Config] max_active_agents set to {value}")
        return jsonify({"max_active_agents": value})
# ---------- Auto Mode ----------
    @app.route("/api/auto-mode", methods=["GET"])
    def get_auto_mode():
        with auto_mode_state["lock"]:
            return jsonify({
                "enabled": auto_mode_state["enabled"],
                "suspended_for_quota": auto_mode_state["suspended_for_quota"],
                "suspended_for_containment": auto_mode_state.get("suspended_for_containment", False),
                "containment_trip": auto_mode_state.get("containment_trip"),
            })
    @app.route("/api/auto-mode", methods=["POST"])
    def set_auto_mode():
        data = request.json or {}
        enabled = bool(data.get("enabled", False))
        suspend = bool(data.get("suspend", False))
        ack_at = None
        with auto_mode_state["lock"]:
            # Re-enabling is the operator's acknowledgement of a containment
            # trip: rows extracted before now no longer count toward a spike.
            if enabled and auto_mode_state.get("suspended_for_containment"):
                ack_at = datetime.now(timezone.utc).isoformat()
                auto_mode_state["suspended_for_containment"] = False
                auto_mode_state["containment_trip"] = None
            auto_mode_state["enabled"] = enabled
            if suspend:
                # Manual quota suspension — watcher will lift it when quota drops below threshold
                auto_mode_state["suspended_for_quota"] = True
            else:
                auto_mode_state["suspended_for_quota"] = False  # user explicitly set it
            auto_mode_state["user_disabled"] = not enabled  # prevent quota-resume from overriding manual off
        config["auto_mode_enabled"] = enabled
        updates = {"auto_mode_enabled": enabled}
        if ack_at:
            from swarm import containment_breaker
            ack_counts = containment_breaker.ack_snapshot(db)
            config["containment_ack_at"] = ack_at
            config[containment_breaker.ACK_COUNTS_KEY] = ack_counts
            updates["containment_ack_at"] = ack_at
            updates[containment_breaker.ACK_COUNTS_KEY] = ack_counts
            print(f"[Containment] Acknowledged by operator at {ack_at} -- auto mode re-enabled")
        try:
            update_config_file(config_file, updates, lock=_config_write_lock)
        except (ConfigReadError, ConfigWriteRefused) as e:
            print(f"[Config] Warning: could not persist auto_mode_enabled: {e}")
        caller = request.remote_addr or "unknown"
        ua = request.headers.get("User-Agent", "")[:60]
        if suspend:
            print(f"[Auto] Manually suspended via HTTP | caller={caller} ua={ua}")
            return jsonify({"enabled": enabled, "suspended_for_quota": True})
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
        try:
            update_config_file(config_file, {"auto_scale": enabled}, lock=_config_write_lock)
        except (ConfigReadError, ConfigWriteRefused) as e:
            print(f"[Config] Warning: could not persist auto_scale: {e}")
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
        try:
            update_config_file(config_file, {"quota_limit_percent": value}, lock=_config_write_lock)
        except (ConfigReadError, ConfigWriteRefused) as e:
            print(f"[Config] Warning: could not persist quota_limit_percent: {e}")
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
        try:
            update_config_file(config_file, {"qa_max_cycles": value}, lock=_config_write_lock)
        except (ConfigReadError, ConfigWriteRefused) as e:
            print(f"[Config] Warning: could not persist qa_max_cycles: {e}")
        print(f"[Config] qa_max_cycles set to {value}")
        return jsonify({"qa_max_cycles": value})

# ---------- Human Review Flag ----------
    @app.route("/api/human-review-flag", methods=["GET"])
    def get_human_review_flag():
        return jsonify({"enabled": config.get("human_review_flag_enabled", False)})

    @app.route("/api/human-review-flag", methods=["POST"])
    def set_human_review_flag():
        import swarm.agent_lifecycle as _al
        data = request.json or {}
        enabled = bool(data.get("enabled", False))
        config["human_review_flag_enabled"] = enabled
        _al.HUMAN_REVIEW_FLAG_ENABLED = enabled
        try:
            update_config_file(config_file, {"human_review_flag_enabled": enabled}, lock=_config_write_lock)
        except (ConfigReadError, ConfigWriteRefused) as e:
            print(f"[Config] Warning: could not persist human_review_flag_enabled: {e}")
        print(f"[Config] human_review_flag_enabled set to {enabled}")
        return jsonify({"enabled": enabled})

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
        try:
            update_config_file(config_file, {"use_worktrees": enabled}, lock=_config_write_lock)
        except (ConfigReadError, ConfigWriteRefused) as e:
            print(f"[Config] Warning: could not persist use_worktrees: {e}")
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
            if not data.get("base_url") or (not data.get("api_key_env") and not data.get("api_key")):
                return jsonify({"error": f"Unknown provider '{provider}'. Supply base_url and api_key_env (or api_key) to register it."}), 400
            _runner_mod.LLM_PROVIDERS[provider] = {
                "base_url":    data["base_url"],
                "model":       data.get("model", ""),
                "api_key_env": data.get("api_key_env", ""),
                "format":      data.get("format", "openai"),
                "max_tokens":  data.get("max_tokens", 8096),
            }
        # Inject api_key directly into env and in-memory config if provided
        if data.get("api_key"):
            env_var = data.get("api_key_env") or f"{provider.upper().replace('-', '_')}_API_KEY"
            os.environ[env_var] = data["api_key"]
            _runner_mod.LLM_PROVIDERS.setdefault(provider, {})["api_key_env"] = env_var
            _runner_mod.LLM_PROVIDERS[provider]["api_key"] = data["api_key"]
        # Override model/base_url if provided for existing providers
        if hasattr(_runner_mod, "LLM_PROVIDERS") and provider in _runner_mod.LLM_PROVIDERS:
            if data.get("model"):
                _runner_mod.LLM_PROVIDERS[provider]["model"] = data["model"]
            if data.get("base_url"):
                _runner_mod.LLM_PROVIDERS[provider]["base_url"] = data["base_url"]
        elif data.get("model") and hasattr(_runner_mod, "LLM_PROVIDERS"):
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
        try:
            _updates = {"llm_provider": provider}
            # Always persist the full provider config so custom providers survive restarts
            if _full_pcfg:
                _existing_providers = dict(read_config_file(config_file).get("llm_providers") or {})
                _existing_providers[provider] = dict(_full_pcfg)
                _updates["llm_providers"] = _existing_providers
            update_config_file(config_file, _updates, lock=_config_write_lock)
        except (ConfigReadError, ConfigWriteRefused) as e:
            print(f"[Config] Warning: could not persist llm_provider: {e}")
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
        try:
            update_config_file(config_file, {
                "thinking_enabled": enabled,
                "thinking_budget": budget,
                "thinking_task_types": config.get("thinking_task_types", []),
            }, lock=_config_write_lock)
        except (ConfigReadError, ConfigWriteRefused) as e:
            print(f"[Config] Warning: could not persist thinking settings: {e}")
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
        try:
            update_config_file(config_file, {"mcp_servers": servers}, lock=_config_write_lock)
        except (ConfigReadError, ConfigWriteRefused) as e:
            print(f"[Config] Warning: could not persist mcp_servers: {e}")
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
        try:
            update_config_file(config_file, updated, lock=_config_write_lock)
        except (ConfigReadError, ConfigWriteRefused) as e:
            print(f"[Config] Warning: could not persist vision providers: {e}")
        print(f"[Config] vision_providers updated: {list(updated.keys())}")
        return jsonify({"ok": True, **updated})

    # ---------- Event Bus ----------
    # ---------- Adaptive Flat ----------
    @app.route("/api/adaptive-flat", methods=["GET"])
    def get_adaptive_flat():
        return jsonify({"enabled": bool(config.get("adaptive_flat", True))})

    @app.route("/api/adaptive-flat", methods=["POST"])
    def set_adaptive_flat():
        import swarm.agent_runtime as _rt
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled", True))
        config["adaptive_flat"] = enabled
        _rt.ADAPTIVE_FLAT = enabled
        import swarm_runner as _runner_mod
        _runner_mod._config["adaptive_flat"] = enabled
        if config_file.exists():
            try:
                update_config_file(config_file, {"adaptive_flat": enabled}, lock=_config_write_lock)
            except (ConfigReadError, ConfigWriteRefused) as e:
                print(f"[Config] Warning: could not persist adaptive_flat: {e}")
        print(f"[Config] adaptive_flat={enabled}")
        return jsonify({"ok": True, "enabled": enabled})

    # ---------- Model Policy (roadmap #8 strategic routing layer) ----------
    @app.route("/api/model-policy", methods=["GET"])
    def get_model_policy():
        return jsonify({
            "enabled": bool(config.get("model_policy_enabled", True)),
            "model_policy": config.get("model_policy", {}),
            "project_quota_caps": config.get("project_quota_caps", {}),
        })

    @app.route("/api/model-policy", methods=["POST"])
    def set_model_policy():
        """Rollback lever for the strategic policy layer. When disabled,
        resolve_policy() calls are unaffected server-side (the policy
        module has no separate on/off switch of its own) -- this flag is
        consumed by swarm_runner.py at wrapper-generation time to emit an
        empty model_policy config, which collapses every resolve_policy()
        call to the legacy-fallback / DEFAULT_POLICY_TABLE path (identical
        to pre-graduation behavior)."""
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled", True))
        config["model_policy_enabled"] = enabled
        import swarm_runner as _runner_mod
        _runner_mod._config["model_policy_enabled"] = enabled
        if config_file.exists():
            try:
                update_config_file(config_file, {"model_policy_enabled": enabled}, lock=_config_write_lock)
            except (ConfigReadError, ConfigWriteRefused) as e:
                print(f"[Config] Warning: could not persist model_policy_enabled: {e}")
        print(f"[Config] model_policy_enabled={enabled}")
        return jsonify({"ok": True, "enabled": enabled})

    @app.route("/api/event-bus", methods=["GET"])
    def get_event_bus():
        from swarm.events import bus as _eb
        from swarm import agent_lifecycle as _lc
        return jsonify({
            "enabled": _eb.enabled,
            "stats": {
                "published": _eb.stats["published"],
                "dropped_disabled": _eb.stats["dropped_disabled"],
                "handled": _eb.stats["handled"],
                "handler_errors": _eb.stats["handler_errors"],
                "by_type": dict(_eb.stats["by_type"]),
            },
        })

    @app.route("/api/event-bus", methods=["POST"])
    def set_event_bus():
        from swarm.events import bus as _eb
        from swarm import agent_lifecycle as _lc
        body = request.get_json(silent=True) or {}
        enabled = bool(body.get("enabled", False))
        _eb.set_enabled(enabled)
        _lc.EVENT_BUS_ENABLED = enabled
        config["event_bus_enabled"] = enabled
        if config_file.exists():
            try:
                update_config_file(config_file, {"event_bus_enabled": enabled}, lock=_config_write_lock)
            except (ConfigReadError, ConfigWriteRefused) as e:
                print(f"[Config] Warning: could not persist event_bus_enabled: {e}")
        print(f"[Config] event_bus_enabled={enabled}")
        return jsonify({"ok": True, "enabled": enabled})

    # ---------- Log Rotation ----------
    @app.route("/api/log-rotation", methods=["GET"])
    def get_log_rotation():
        import time as _time
        retention = int(config.get("log_retention_days", 0))
        action    = config.get("log_rotation_action", "delete")
        extract   = bool(config.get("log_extract_signals", False))
        # Count eligible files (older than retention_days)
        eligible = 0
        if retention > 0:
            cutoff = _time.time() - retention * 86400
            for f in data_dir.glob("agent_*.log"):
                try:
                    if f.stat().st_mtime < cutoff:
                        eligible += 1
                except Exception:
                    pass
        last_run_file = data_dir / "log_rotation_last_run.txt"
        last_run = None
        if last_run_file.exists():
            try:
                import datetime as _dt
                last_run = _dt.datetime.fromtimestamp(
                    float(last_run_file.read_text()), tz=_dt.timezone.utc
                ).isoformat()
            except Exception:
                pass
        signals_count = len(db.agent_signals_query(limit=1))
        return jsonify({
            "log_retention_days": retention,
            "log_rotation_action": action,
            "log_extract_signals": extract,
            "eligible_files": eligible,
            "last_run": last_run,
            "signals_extracted": signals_count > 0,
        })

    @app.route("/api/log-rotation/run", methods=["POST"])
    def run_log_rotation():
        import threading as _threading
        from swarm.log_rotation import rotate_logs as _rotate_logs
        _threading.Thread(
            target=_rotate_logs, args=(str(data_dir), db),
            daemon=True, name="manual-log-rotation",
        ).start()
        return jsonify({"ok": True, "message": "Log rotation started in background"})

    @app.route("/api/log-rotation/signals", methods=["GET"])
    def get_signals_summary():
        from swarm.log_rotation import get_signals_summary as _summary
        project = request.args.get("project")
        return jsonify(_summary(db, project=project))
