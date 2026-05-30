#!/usr/bin/env python3
"""Python HTTP Spawn Service for spawn-test-proj.

Implements the service-smoke smoke check and primary request path.
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8765
spawn_count = 0

class SpawnHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/spawn':
            global spawn_count
            spawn_count += 1
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length > 0 else b'{}'
            
            response = json.dumps({
                "status": "ok", 
                "spawned": True,
                "spawn_id": spawn_count
            })
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response.encode())
        elif self.path == '/health':
            response = json.dumps({"status": "healthy", "port": PORT}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == '/health':
            response = json.dumps({"status": "healthy", "port": PORT}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        else:
            self.send_response(404)
            self.end_headers()

def run_server(blocking=True):
    global PORT
    server = HTTPServer(('127.0.0.1', PORT), SpawnHandler)
    print(f"SpawnService: HTTP server running on port {PORT}")
    if blocking:
        server.serve_forever()
    else:
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        return server_thread

def check_service_ready():
    """Check if service is responding."""
    import urllib.request
    try:
        response = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2)
        return response.status == 200
    except Exception:
        return False

if __name__ == '__main__':
    run_server(blocking=True)
