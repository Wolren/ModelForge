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
        self._validate_args(tool, args)
        return tool.handler(args)

    @staticmethod
    def _validate_args(tool: MCPTool, args: Dict[str, Any]) -> None:
        if not isinstance(args, dict):
            raise ValueError(f"Invalid args for {tool.name}: expected an object.")

        schema = tool.schema or {}
        required = schema.get("required", [])
        for key in required:
            if key not in args:
                raise ValueError(f"Missing required arg '{key}' for tool '{tool.name}'.")

        type_map = {
            "object": dict,
            "array": list,
            "string": str,
            "number": (int, float),
            "boolean": bool,
            "integer": int,
        }
        props = schema.get("properties", {})
        for key, value in args.items():
            prop = props.get(key)
            if not prop:
                continue
            expected = prop.get("type")
            py_type = type_map.get(expected)
            if py_type and not isinstance(value, py_type):
                raise ValueError(
                    f"Invalid type for '{key}' in tool '{tool.name}': expected {expected}."
                )
