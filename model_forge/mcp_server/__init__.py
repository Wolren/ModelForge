"""Model Forge MCP Server — expose the compiler pipeline as MCP tools."""

from __future__ import annotations

from .llm_config import SCHEMA_VERSION

_HAS_MCP = False
try:
    import mcp.server.mcpserver  # noqa: F401

    _HAS_MCP = True
except ImportError:
    pass

_HAS_QGIS = False
try:
    import qgis.core  # noqa: F401

    _HAS_QGIS = True
except ImportError:
    pass

__version__ = "1.0.1"

__all__ = [
    "SCHEMA_VERSION",
    "_HAS_MCP",
    "_HAS_QGIS",
    "__version__",
]
