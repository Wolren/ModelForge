"""
ModelForge MCP Server - plain Python, no external SDK.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Dict, List


@dataclass
class MCPTool:
    name: str
    description: str
    schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Dict[str, Any]]


class ModelForgeMCPServer:
    def __init__(self, tools: List[MCPTool]):
        self._tools: Dict[str, MCPTool] = {t.name: t for t in tools}

    def list_tools(self) -> List[Dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description, "inputSchema": t.schema}
            for t in self._tools.values()
        ]

    def invoke(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"Unknown MCP tool: {name!r}")
        return tool.handler(args)
