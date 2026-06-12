"""CLI entry point for Model Forge MCP server.

Usage:
    python -m model_forge.mcp_server                    # stdio mode (Claude Desktop)
    python -m model_forge.mcp_server --transport sse    # SSE mode (browser/custom clients)
    python -m model_forge.mcp_server --transport sse --port 9090
"""

import sys

from .server import main

sys.exit(main())
