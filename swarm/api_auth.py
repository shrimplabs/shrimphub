"""Auth route handlers for the Swarm API."""

from flask import jsonify, request


def require_auth(config):
    """Check authentication using the current request headers.

    Returns the username on success, or a (response, status_code) tuple
    on failure (Flask-compatible return value, same as a decorator).
    """
    if not config.get("login_required", False):
        return None

    session_token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not session_token:
        return jsonify({"error": "Authentication required"}), 401

    from swarm.login import verify_session
    username = verify_session(session_token)
    if not username:
        return jsonify({"error": "Invalid or expired session"}), 401

    return username


def register_routes(app, config):
    """Register auth routes on the Flask app."""

    @app.route("/api/login", methods=["POST"])
    def login():
        """Authenticate user and create session."""
        if not config.get("login_required", False):
            return jsonify({"message": "Login not required"}), 200

        data = request.json or {}
        username = data.get("username", "")
        password = data.get("password", "")

        if not username or not password:
            return jsonify({"error": "Username and password required"}), 400

        from swarm.login import verify_credentials, create_session

        if verify_credentials(username, password, config):
            session_token = create_session(username)
            return jsonify({
                "success": True,
                "session_token": session_token,
                "username": username
            })

        return jsonify({"error": "Invalid credentials"}), 401

    @app.route("/api/logout", methods=["POST"])
    def logout():
        """Destroy session."""
        session_token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if session_token:
            from swarm.login import destroy_session
            destroy_session(session_token)
        return jsonify({"success": True})

    @app.route("/api/session", methods=["GET"])
    def check_session():
        """Verify current session."""
        if not config.get("login_required", False):
            return jsonify({"authenticated": True, "username": "guest"})

        session_token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not session_token:
            return jsonify({"authenticated": False}), 401

        from swarm.login import verify_session
        username = verify_session(session_token)

        if username:
            return jsonify({"authenticated": True, "username": username})

        return jsonify({"authenticated": False}), 401
