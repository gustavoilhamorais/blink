"""Model Context Protocol (MCP) server and client.

The MCP server (blink.mcp.server) exposes Blink terminal capabilities
to AI agents via the JSON-RPC 2.0-based MCP specification.

See: https://modelcontextprotocol.io
"""

from blink.mcp.server import AuditLogger, MCPServer, run_server

__all__ = ["MCPServer", "AuditLogger", "run_server"]
