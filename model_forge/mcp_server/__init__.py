"""Model Forge MCP Server — expose the compiler pipeline as MCP tools."""

from __future__ import annotations

_HAS_MCP = False
try:
    from mcp.server.fastmcp import FastMCP  # noqa: F401

    _HAS_MCP = True
except ImportError:
    pass

_HAS_QGIS = False
try:
    from qgis.core import QgsApplication, QgsProject  # noqa: F401

    _HAS_QGIS = True
except ImportError:
    pass

__version__ = "1.0.1"

# Re-exported here so callers can ``from model_forge.mcp_server import SCHEMA_VERSION``
# without an import cycle. The canonical value lives in ``llm_config``.
from .llm_config import SCHEMA_VERSION  # noqa: E402,F401

__all__ = [
    "__version__",
    "SCHEMA_VERSION",
    "_HAS_MCP",
    "_HAS_QGIS",
]
