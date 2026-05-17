"""Webhook route handlers for the Swarm API.

Routes: GET/POST /api/webhook, POST /api/webhook/test
"""

import json

from flask import jsonify, request


def fire_webhook(config: dict, event: str, **kwargs):
    """POST an event notification to completion_webhook_url if configured.

    Events:
      task_completed  — project, task_id, task_type, description (first line), diff_stat
      task_failed     — project, task_id, task_type, description (first line), attempts, max_attempts
      queue_empty     — agents_completed, agents_failed

    Auto-detects service from URL:
      discord.com/api/webhooks → Discord embeds
      hooks.slack.com          → Slack text
      ntfy.sh                  → plain text + headers
      anything else            → generic JSON
    """
    url = config.get("completion_webhook_url", "")
    if not url:
        return
    try:
        import urllib.request

        if event == "task_completed":
            project  = kwargs.get("project", "")
            task_id  = kwargs.get("task_id", "")
            ttype    = kwargs.get("task_type", "")
            desc     = kwargs.get("description", "")
            diff     = kwargs.get("diff_stat", "")
            title    = f"✅ Task completed — {project}"
            summary  = f"{ttype}: {desc}"
            detail   = f"\n`{diff.splitlines()[-1]}`" if diff else ""
            color    = 0x3fb950
            ntfy_tag = "white_check_mark"
        elif event == "task_failed":
            project  = kwargs.get("project", "")
            task_id  = kwargs.get("task_id", "")
            ttype    = kwargs.get("task_type", "")
            desc     = kwargs.get("description", "")
            attempts = kwargs.get("attempts", 0)
            max_att  = kwargs.get("max_attempts", 3)
            title    = f"❌ Task failed — {project}"
            summary  = f"{ttype}: {desc} (attempt {attempts}/{max_att})"
            detail   = ""
            color    = 0xf85149
            ntfy_tag = "x"
        else:  # queue_empty
            from swarm import db as _db
            all_agents = _db.agent_get_all()
            completed = sum(1 for a in all_agents if a.get("exit_code") == 0)
            failed    = sum(1 for a in all_agents if a.get("status") == "failed")
            title    = "🤖 Swarm queue empty"
            summary  = f"{completed} completed · {failed} failed"
            detail   = ""
            color    = 0x3fb950 if failed == 0 else 0xf0883e
            ntfy_tag = "white_check_mark"
            kwargs.update({"agents_completed": completed, "agents_failed": failed})

        if "discord.com/api/webhooks" in url:
            body = json.dumps({
                "embeds": [{
                    "title": title,
                    "description": summary + detail,
                    "color": color,
                }],
            }).encode()
            headers = {"Content-Type": "application/json"}
        elif "hooks.slack.com" in url:
            body = json.dumps({"text": f"*{title}*\n{summary}{detail}"}).encode()
            headers = {"Content-Type": "application/json"}
        elif "ntfy.sh" in url:
            body = (summary + detail).encode()
            headers = {"Content-Type": "text/plain", "Title": title, "Tags": ntfy_tag}
        else:
            body = json.dumps({"event": event, "title": title, "summary": summary, **kwargs}).encode()
            headers = {"Content-Type": "application/json"}

        headers["User-Agent"] = "Mozilla/5.0 (compatible; SwarmController/1.0)"
        req = urllib.request.Request(url, data=body, headers=headers)
        urllib.request.urlopen(req, timeout=10)
        print(f"[Webhook] {event} notification sent")
    except Exception as e:
        print(f"[Webhook] Failed to send {event} to {url}: {e}")


def register_routes(app, config, config_file, _config_write_lock, orchestrator):
    """Register webhook routes on the Flask app."""

    @app.route("/api/webhook", methods=["GET"])
    def get_webhook():
        return jsonify({"url": config.get("completion_webhook_url", "")})

    @app.route("/api/webhook", methods=["POST"])
    def set_webhook():
        data = request.json or {}
        url = data.get("url", "")
        config["completion_webhook_url"] = url
        orchestrator.WEBHOOK_URL = url
        try:
            with _config_write_lock:
                cfg = json.loads(config_file.read_text()) if config_file.exists() else {}
                cfg["completion_webhook_url"] = url
                config_file.write_text(json.dumps(cfg, indent=2) + "\n")
        except Exception as e:
            print(f"[Webhook] Warning: could not persist webhook URL: {e}")
        return jsonify({"success": True, "url": url})

    @app.route("/api/webhook/test", methods=["POST"])
    def test_webhook():
        """Send a test payload to the configured webhook URL."""
        data = request.get_json(silent=True) or {}
        url = data.get("url") or config.get("completion_webhook_url", "")
        if not url:
            return jsonify({"ok": False, "error": "No webhook URL configured"}), 400
        try:
            import urllib.request, urllib.error
            if "discord.com/api/webhooks" in url:
                body = json.dumps({"content": "🤖 **Swarm webhook test** — connection OK"}).encode()
                headers = {"Content-Type": "application/json"}
            elif "hooks.slack.com" in url:
                body = json.dumps({"text": ":robot_face: *Swarm webhook test* — connection OK"}).encode()
                headers = {"Content-Type": "application/json"}
            elif "ntfy.sh" in url:
                body = "Swarm webhook test - connection OK".encode()
                headers = {"Content-Type": "text/plain", "Title": "Swarm: test", "Tags": "white_check_mark"}
            else:
                body = json.dumps({"event": "test", "message": "Swarm webhook test — connection OK"}).encode()
                headers = {"Content-Type": "application/json"}
            headers["User-Agent"] = "Mozilla/5.0 (compatible; SwarmController/1.0)"
            req = urllib.request.Request(url, data=body, headers=headers)
            try:
                urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")
                return jsonify({"ok": False, "error": f"HTTP {e.code}: {detail}"}), 200
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 200
