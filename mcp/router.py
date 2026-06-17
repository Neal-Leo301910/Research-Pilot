"""
mcp/router.py

MCPToolRouter —— 感知连接状态的 MCP 工具路由层。

升级点：
  - 路由前检查 ConnectionState（不路由到 failed/needs-auth server）
  - 暴露 get_connection_states() 供 /mcp 调试
  - 暴露 drain_elicitations() 收集 pending 用户输入请求
"""
from __future__ import annotations

from mcp.client import MCPClient, MCPConnectionState, ElicitationRequest


class MCPToolRouter:
    """
    MCP 工具路由器。

    路由逻辑：
      native tools → ctx.handlers（不经过这里）
      mcp__ tools  → 按 server_name 找对应 MCPClient，检查状态后调用
    """

    def __init__(self):
        self.clients: dict[str, MCPClient] = {}

    def register_client(self, mcp_client: MCPClient) -> None:
        self.clients[mcp_client.server_name] = mcp_client

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name.startswith("mcp__")

    def call(self, tool_name: str, arguments: dict) -> str:
        """路由并调用 MCP 工具，路由前检查连接状态。"""
        parts = tool_name.split("__", 2)
        if len(parts) != 3:
            return f"Error: Invalid MCP tool name: {tool_name}"
        _, server_name, actual_tool = parts

        client = self.clients.get(server_name)
        if not client:
            return f"Error: MCP server not found: {server_name}"

        # Layer 3: 连接状态检查
        if not client.is_connected:
            status = client.connection.status
            if status == "needs-auth":
                return f"Error: MCP server '{server_name}' needs authentication: {client.connection.error_message}"
            return f"Error: MCP server '{server_name}' is {status}: {client.connection.error_message}"

        return client.call_tool(actual_tool, arguments)

    def get_all_tools(self) -> list[dict]:
        """返回所有 connected server 的 ToolSpec 列表。"""
        tools = []
        for client in self.clients.values():
            if client.is_connected:
                tools.extend(client.get_agent_tools())
        return tools

    # ── 连接状态查询 ──────────────────────────────────────────────────────────

    def get_connection_states(self) -> list[dict]:
        """返回所有 server 的连接状态摘要，供 /mcp 调试命令使用。"""
        return [c.status_summary() for c in self.clients.values()]

    def get_connected_servers(self) -> list[str]:
        return [name for name, c in self.clients.items() if c.is_connected]

    def get_failed_servers(self) -> list[str]:
        return [name for name, c in self.clients.items()
                if c.connection.status in {"failed", "needs-auth"}]

    # ── Elicitation 收集 ──────────────────────────────────────────────────────

    def drain_elicitations(self) -> list[ElicitationRequest]:
        """收集所有 server 的 pending elicitation 请求。"""
        result = []
        for client in self.clients.values():
            result.extend(client.pending_elicitations)
            client.pending_elicitations.clear()
        return result

    # ── Resources / Prompts 代理 ──────────────────────────────────────────────

    def list_server_resources(self, server_name: str) -> list:
        client = self.clients.get(server_name)
        if not client or not client.is_connected:
            return []
        return client.list_resources()

    def list_server_prompts(self, server_name: str) -> list:
        client = self.clients.get(server_name)
        if not client or not client.is_connected:
            return []
        return client.list_prompts()
