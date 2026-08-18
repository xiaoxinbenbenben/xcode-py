"""MCP Client：读 mcp.json、长连接外部 server、把 tools/resources 挂进 registry。"""

from xcode.mcp.client import McpClientManager
from xcode.mcp.config import McpServerSpec, load_mcp_server_specs

__all__ = ["McpClientManager", "McpServerSpec", "load_mcp_server_specs"]
