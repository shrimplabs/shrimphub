#!/usr/bin/env python3
"""
MCP Client for Swarm Agents
Supports connecting to MCP servers (like godot-mcp) for specialized tools

Usage:
    Configure servers in config.json:
    {
        "mcp_servers": {
            "godot": {
                "command": "python3",
                "args": ["-m", "godot_mcp"],
                "env": {"GODOT_PATH": "godot"}
            }
        }
    }
"""

import json
import os
import subprocess
import threading
import queue
from typing import Any, Optional


class MCPClient:
    """Client for connecting to MCP servers via stdio"""
    
    def __init__(self, servers: dict = None):
        """
        Initialize MCP client with server configs.
        
        servers format:
        {
            "godot": {
                "command": "/path/to/server",
                "args": ["arg1", "arg2"],
                "env": {"KEY": "value"}
            }
        }
        """
        self.servers = servers or {}
        self.processes: dict[str, subprocess.Popen] = {}
        self.request_id = 0
        self._lock = threading.Lock()
    
    def start_server(self, name: str, cwd: str = None) -> bool:
        """Start an MCP server"""
        if name not in self.servers:
            print(f"[MCP] Unknown server: {name}")
            return False
        
        if name in self.processes:
            return True
        
        config = self.servers[name]
        cmd = [config.get("command", "")] + config.get("args", [])
        env = {**os.environ, **config.get("env", {})}
        
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=cwd
            )
            self.processes[name] = proc
            print(f"[MCP] Started server: {name}")
            
            # Send initialize request
            self._send_request(name, "initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "swarm-agent", "version": "1.0"}
            })
            
            return True
        except Exception as e:
            print(f"[MCP] Failed to start {name}: {e}")
            return False
    
    def _send_request(self, server: str, method: str, params: dict = None) -> dict:
        """Send a JSON-RPC request to the MCP server"""
        if server not in self.processes:
            return {"error": f"Server {server} not running"}
        
        proc = self.processes[server]
        
        with self._lock:
            self.request_id += 1
            req_id = self.request_id
            
            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {}
            }
        
        try:
            request_str = json.dumps(request) + "\n"
            proc.stdin.write(request_str)
            proc.stdin.flush()
            
            # Read response
            response_str = proc.stdout.readline()
            if not response_str:
                return {"error": "No response from server"}
            
            response = json.loads(response_str)
            
            if "error" in response:
                return {"error": response["error"]}
            
            return response.get("result", {})
        except Exception as e:
            return {"error": str(e)}
    
    def list_tools(self, server: str) -> list:
        """List available tools on an MCP server"""
        if server not in self.processes:
            if not self.start_server(server):
                return []
        
        result = self._send_request(server, "tools/list")
        if "error" in result:
            print(f"[MCP] List tools error: {result['error']}")
            return []
        
        return result.get("tools", [])
    
    def call_tool(self, server: str, tool: str, arguments: dict = None) -> dict:
        """Call a tool on an MCP server"""
        if server not in self.processes:
            # Try to start the server first
            if not self.start_server(server):
                return {"error": f"Failed to start server: {server}"}
        
        result = self._send_request(server, "tools/call", {
            "name": tool,
            "arguments": arguments or {}
        })
        
        if "error" in result:
            return {"error": result["error"]}
        
        # MCP tools/call returns {content: [...]}
        content = result.get("content", [])
        if content and content[0].get("type") == "text":
            try:
                return json.loads(content[0].get("text", "{}"))
            except:
                return {"ok": True, "result": content[0].get("text", "")}
        
        return {"ok": True, "result": result}
    
    def stop_server(self, name: str):
        """Stop an MCP server"""
        if name in self.processes:
            proc = self.processes[name]
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            del self.processes[name]
            print(f"[MCP] Stopped server: {name}")
    
    def stop_all(self):
        """Stop all MCP servers"""
        for name in list(self.processes.keys()):
            self.stop_server(name)


def create_mcp_client(config: dict = None) -> Optional[MCPClient]:
    """Create an MCP client from config"""
    mcp_servers = config.get("mcp_servers", {}) if config else {}
    if not mcp_servers:
        return None
    return MCPClient(mcp_servers)


if __name__ == "__main__":
    # Test
    client = MCPClient({
        "godot": {
            "command": "python3",
            "args": ["-m", "godot_mcp"]
        }
    })
    
    print("MCP Client ready")
    print("To use: client.start_server('godot'), client.list_tools('godot'), etc.")
