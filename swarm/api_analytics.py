"""Analytics route handlers for the Swarm API (roadmap #7).

Thin wrappers over swarm/analytics.py read-only aggregate queries. All GET,
all safe to poll from the dashboard. Optional ?project= filter where relevant.
"""

from pathlib import Path

from flask import jsonify, request

from swarm import analytics


def register_routes(app, db, data_dir, workspace):
    """Register analytics routes on the Flask app."""
    data_dir = Path(data_dir)
    workspace = Path(workspace)

    @app.route("/api/analytics/overview", methods=["GET"])
    def analytics_overview():
        project = request.args.get("project") or None
        return jsonify(analytics.overview(db, data_dir, project=project))

    @app.route("/api/analytics/value-repair", methods=["GET"])
    def analytics_value_repair():
        project = request.args.get("project") or None
        managed_only = request.args.get("managed", "").lower() in ("1", "true", "yes")
        if project:
            return jsonify(analytics.value_repair(db, project=project))
        managed_projects = None
        if managed_only:
            from swarm import orchestrator as _orch
            managed_projects = getattr(_orch, "MANAGED_PROJECTS", None) or None
        return jsonify({"by_project": analytics.value_repair_by_project(db, projects=managed_projects),
                        "overall": analytics.value_repair(db)})

    @app.route("/api/analytics/deaths", methods=["GET"])
    def analytics_deaths():
        project = request.args.get("project") or None
        return jsonify(analytics.deaths(db, project=project))

    @app.route("/api/analytics/mechanisms", methods=["GET"])
    def analytics_mechanisms():
        project = request.args.get("project") or None
        return jsonify(analytics.mechanisms(db, project=project))

    @app.route("/api/analytics/research-roi", methods=["GET"])
    def analytics_research_roi():
        project = request.args.get("project") or None
        return jsonify(analytics.research_feeder_roi(db, project=project))

    @app.route("/api/analytics/ship-candidates", methods=["GET"])
    def analytics_ship_candidates():
        managed_only = request.args.get("managed", "").lower() in ("1", "true", "yes")
        managed_projects = None
        if managed_only:
            from swarm import orchestrator as _orch
            managed_projects = getattr(_orch, "MANAGED_PROJECTS", None) or None
        return jsonify({"candidates": analytics.ship_candidates(db, data_dir, workspace, projects=managed_projects)})
