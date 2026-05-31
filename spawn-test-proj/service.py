#!/usr/bin/env python3
"""HTTP service: GET /ping, GET /health, POST /spawn."""
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
import json


class Handler(BaseHTTPRequestHandler):
    counter = 0

    def _send_json(self, data: dict, code: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/ping":
            self._send_json({"ok": True})
        elif self.path == "/health":
            self._send_json({"status": "healthy"})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path == "/spawn":
            Handler.counter += 1
            self._send_json({"status": "ok", "spawned": True, "spawn_id": Handler.counter})
        else:
            self._send_json({"error": "not found"}, 404)

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    server = HTTPServer((host, port), Handler)
    print(f"Service running on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
