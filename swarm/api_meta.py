"""Meta Mode route handler for the Swarm API."""

from __future__ import annotations

import json
import threading
from typing import Any, Dict

from flask import jsonify, request


# Module-level globals -- wired from api.py via register_routes
config_ref: Dict = {}
_config_file_ref: Any = None
_config_write_lock_ref: threading.Lock | None = None


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register_routes(app, config: Dict,
               config_file: Any = None,
               _config_write_lock: threading.Lock | None = None) -> None:
    """Register meta-mode routes on the Flask app."""
    global config_ref, _config_file_ref, _config_write_lock_ref
    config_ref = config
    _config_file_ref = config_file
    _config_write_lock_ref = _config_write_lock

    @app.route("/api/meta-mode", methods=["GET"])
    def get_meta_mode():
        """Return current state of the master meta-mode toggle and all agent statuses."""
        return jsonify({
            "meta_mode_enabled": config_ref.get("meta_mode_enabled", False),
            "agents": {
                "gardener": {
                    "enabled": config_ref.get("gardener_enabled", False),
                    "last_run_ts": float(config_ref.get("_gardener_last_run_ts", 0.0)),
                },
                "cartographer": {
                    "enabled": config_ref.get("cartographer_enabled", False),
                    "last_run_ts": float(config_ref.get("_cartographer_last_run_ts", 0.0)),
                    "interval_hours": config_ref.get("cartographer_interval_hours", 2),
                },
                "librarian": {
                    "enabled": False,
                },
                "archaeologist": {
                    "enabled": False,
                },
                "auditor": {
                    "enabled": config_ref.get("meta_auditor_enabled", False),
                    "last_run_ts": float(config_ref.get("_meta_auditor_last_run_ts", 0.0)),
                    "interval_days": config_ref.get("meta_auditor_interval_days", 7),
                },
                "scheduler": {
                    "enabled": False,
                },
            },
        })

    @app.route("/api/meta-mode", methods=["POST"])
    def set_meta_mode():
        """Enable or disable meta mode and persist to config.json."""
        data = request.json or {}
        if "meta_mode_enabled" not in data:
            return jsonify({"error": "meta_mode_enabled field is required"}), 400

        enabled = bool(data["meta_mode_enabled"])
        config_ref["meta_mode_enabled"] = enabled

        # Sync to orchestrator module global
        _sync_meta_mode_globals(enabled)

        # Cascade: restart gardener scheduler based on meta mode state
        _cascade_to_schedulers(enabled)

        # Persist to config.json
        _persist_meta_mode(config_ref, _config_file_ref, _config_write_lock_ref)

        return jsonify({
            "meta_mode_enabled": enabled,
            "agents": {
                "gardener": {
                    "enabled": config_ref.get("gardener_enabled", False),
                    "last_run_ts": float(config_ref.get("_gardener_last_run_ts", 0.0)),
                },
                "cartographer": {
                    "enabled": config_ref.get("cartographer_enabled", False),
                    "last_run_ts": float(config_ref.get("_cartographer_last_run_ts", 0.0)),
                    "interval_hours": config_ref.get("cartographer_interval_hours", 2),
                },
                "librarian": {
                    "enabled": False,
                },
                "archaeologist": {
                    "enabled": False,
                },
                "auditor": {
                    "enabled": config_ref.get("meta_auditor_enabled", False),
                    "last_run_ts": float(config_ref.get("_meta_auditor_last_run_ts", 0.0)),
                },
                "scheduler": {
                    "enabled": False,
                },
            },
        })


def _sync_meta_mode_globals(enabled: bool) -> None:
    """Sync meta_mode_enabled to the orchestrator module."""
    try:
        from swarm import orchestrator as _orch
        _orch.META_MODE_ENABLED = enabled
    except Exception:
        pass


def _cascade_to_schedulers(meta_mode_enabled: bool) -> None:
    """Restart or stop individual agent schedulers based on meta mode state."""
    # Gardener scheduler is controlled by api_gardener._schedule_gardener()
    # which checks config["gardener_enabled"] at each fire. The scheduler timer
    # itself always runs -- it checks gardener_enabled and meta_mode_enabled
    # (via orchestrator globals) inside the fired closure. Re-trigger the
    # scheduler by calling the register_routes helper so the timer fires with
    # the updated global state on next interval.
    # Since we can't re-import the scheduler without circular import risk,
    # the simplest cascade is: the next monitor cycle will call
    # orchestrator.fill_slots -> _fire_idle_gardener, which now checks both
    # GARDENER_ENABLED and META_MODE_ENABLED. For the scheduled path, the
    # gardener timer continues -- it fires regardless of meta mode; the
    # actual task creation is guarded by orchestrator.META_MODE_ENABLED.
    pass


def _persist_meta_mode(config: Dict,
                       config_file: Any,
                       _config_write_lock: threading.Lock | None) -> None:
    """Persist meta_mode_enabled to config.json via the write lock."""
    if config_file is None or _config_write_lock is None:
        return
    with _config_write_lock:
        try:
            cfg = {}
            if config_file.exists():
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
            cfg["meta_mode_enabled"] = config.get("meta_mode_enabled", False)
            config_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
