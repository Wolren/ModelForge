"""
DirectMCPClient - in-process MCP client, no HTTP needed.
"""
from __future__ import annotations
from typing import Any, Dict
from .server import ModelForgeMCPServer


class DirectMCPClient:
    def __init__(self, server: ModelForgeMCPServer):
        self._server = server

    def call(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        return self._server.invoke(tool_name, args)

    def list_tools(self):
        return self._server.list_tools()
