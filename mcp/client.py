"""
mcp/client.py

MCP 客户端 6 层模型：

  Layer 1  Config Layer        ScopedMcpServerConfig   server 配置
  Layer 2  Transport Layer     stdio / sse              连接通道
  Layer 3  Connection State    MCPConnectionState       connected/pending/failed/needs-auth
  Layer 4  Capability Layer    tools/resources/prompts/elicitation
  Layer 5  Auth Layer          needs_auth / auth_token
  Layer 6  Router Integration  get_agent_tools() 接回 tool router

tools 只是其中一层，不是全部。
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Literal


# ── Layer 1: Config ───────────────────────────────────────────────────────────

McpTransportType = Literal["stdio", "sse"]
McpScope = Literal["project", "user", "global"]


@dataclass
class ScopedMcpServerConfig:
    """
    MCP server 配置。

    scope 区分配置来源：
      project  来自项目 .claude-plugin / plugin.json
      user     来自用户级配置
      global   来自全局配置
    """
    name: str
    transport: McpTransportType = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    scope: McpScope = "project"

    @classmethod
    def from_dict(cls, name: str, d: dict, scope: McpScope = "project") -> "ScopedMcpServerConfig":
        return cls(
            name=name,
            transport=d.get("type", "stdio"),
            command=d.get("command", ""),
            args=d.get("args", []),
            env=d.get("env", {}),
            scope=scope,
        )


# ── Layer 3: Connection State ─────────────────────────────────────────────────

McpConnectionStatus = Literal["pending", "connected", "failed", "needs-auth", "disabled"]


@dataclass
class MCPConnectionState:
    """
    运行时连接状态 —— 不是 config，是当前这一刻的连接情况。

    status 转移：
      pending → connected   成功握手
      pending → failed      连接异常
      pending → needs-auth  需要认证
      connected → failed    运行中断开
    """
    server_name: str
    status: MCPConnectionStatus = "pending"
    error_message: str = ""
    server_info: dict = field(default_factory=dict)  # 来自 initialize 响应

    def mark_connected(self, server_info: dict) -> None:
        self.status = "connected"
        self.server_info = server_info
        self.error_message = ""

    def mark_failed(self, reason: str) -> None:
        self.status = "failed"
        self.error_message = reason

    def mark_needs_auth(self, reason: str = "") -> None:
        self.status = "needs-auth"
        self.error_message = reason


# ── Layer 4: Capabilities ─────────────────────────────────────────────────────

@dataclass
class MCPCapabilities:
    """
    服务器声明的能力集。

    tools       模型可调用的工具列表（主线能力）
    resources   服务器提供的资源（文件、数据库记录等）
    prompts     服务器预定义的 prompt 模板
    elicitation 服务器发起的用户输入请求
    """
    tools: list[dict] = field(default_factory=list)
    resources: list[dict] = field(default_factory=list)
    prompts: list[dict] = field(default_factory=list)
    supports_elicitation: bool = False


# ── Layer 5: Auth ─────────────────────────────────────────────────────────────

@dataclass
class MCPAuthState:
    """Auth 层状态。"""
    needs_auth: bool = False
    auth_token: str = ""
    auth_scheme: str = ""    # "bearer" / "basic" / ""

    def is_authenticated(self) -> bool:
        return not self.needs_auth or bool(self.auth_token)


# ── Layer 6: ElicitationRequest ───────────────────────────────────────────────

@dataclass
class ElicitationRequest:
    """
    服务器主动向用户请求额外输入（MCP elicitation 能力）。

    注：这不是"模型调工具"，而是"服务器请求用户输入"。
    """
    server_name: str
    message: str
    requested_schema: dict = field(default_factory=dict)
    request_id: str = ""


# ── MCPClient ─────────────────────────────────────────────────────────────────

class MCPClient:
    """
    MCP 客户端，封装 6 层模型。

    外部只需关心：
      connect()         建立连接（Layer 2-3）
      list_tools()      获取工具列表（Layer 4 - tools）
      call_tool()       调用工具（Layer 6 - router integration）
      get_agent_tools() 获取 ToolSpec 列表供 tool router 注册
      disconnect()      断开连接

    进阶能力（教学版可选实现）：
      list_resources()  列出资源（Layer 4 - resources）
      list_prompts()    列出 prompt 模板（Layer 4 - prompts）
      pending_elicitations  待处理的用户输入请求（Layer 4 - elicitation）
    """

    def __init__(self, config: ScopedMcpServerConfig | None = None,
                 server_name: str = "", command: str = "",
                 args: list | None = None, env: dict | None = None):
        # 支持旧式参数（向后兼容）和新式 ScopedMcpServerConfig
        if config is not None:
            self.config = config
        else:
            self.config = ScopedMcpServerConfig(
                name=server_name,
                command=command,
                args=args or [],
                env=env or {},
            )
        self.server_name = self.config.name

        # 运行时状态（Layer 3-5）
        self.connection = MCPConnectionState(server_name=self.server_name)
        self.capabilities = MCPCapabilities()
        self.auth = MCPAuthState()
        self.pending_elicitations: list[ElicitationRequest] = []

        # 传输层
        self.process = None
        self._request_id = 0
        self._env = {**os.environ, **self.config.env}

    # ── Layer 2-3: Transport + Connection ────────────────────────────────────

    def connect(self) -> bool:
        """建立连接，完成握手，填充 connection state 和 capabilities。"""
        try:
            self.process = subprocess.Popen(
                [self.config.command] + self.config.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
                text=True,
            )
            self._send({
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"elicitation": {}},
                    "clientInfo": {"name": "teaching-agent", "version": "1.0"},
                },
            })
            response = self._recv()
            if response and "result" in response:
                result = response["result"]
                self.connection.mark_connected(result.get("serverInfo", {}))
                # 解析服务器声明的能力
                server_caps = result.get("capabilities", {})
                self.capabilities.supports_elicitation = "elicitation" in server_caps
                self._send({"method": "notifications/initialized"})
                return True

            if response and "error" in response:
                err = response["error"].get("message", "unknown")
                if "auth" in err.lower() or "unauthorized" in err.lower():
                    self.connection.mark_needs_auth(err)
                    self.auth.needs_auth = True
                else:
                    self.connection.mark_failed(err)
                return False

        except FileNotFoundError:
            self.connection.mark_failed(f"Command not found: {self.config.command}")
            print(f"[MCP] Server command not found: {self.config.command}")
        except Exception as e:
            self.connection.mark_failed(str(e))
            print(f"[MCP] Connection failed: {e}")
        return False

    @property
    def is_connected(self) -> bool:
        return self.connection.status == "connected"

    # ── Layer 4: Capabilities ─────────────────────────────────────────────────

    def list_tools(self) -> list:
        """获取工具列表（Layer 4 - tools，主线能力）。"""
        if not self.is_connected:
            return []
        self._send({"method": "tools/list", "params": {}})
        response = self._recv()
        if response and "result" in response:
            self.capabilities.tools = response["result"].get("tools", [])
        return self.capabilities.tools

    def list_resources(self) -> list:
        """获取资源列表（Layer 4 - resources）。"""
        if not self.is_connected:
            return []
        self._send({"method": "resources/list", "params": {}})
        response = self._recv()
        if response and "result" in response:
            self.capabilities.resources = response["result"].get("resources", [])
        return self.capabilities.resources

    def list_prompts(self) -> list:
        """获取 prompt 模板列表（Layer 4 - prompts）。"""
        if not self.is_connected:
            return []
        self._send({"method": "prompts/list", "params": {}})
        response = self._recv()
        if response and "result" in response:
            self.capabilities.prompts = response["result"].get("prompts", [])
        return self.capabilities.prompts

    def handle_elicitation(self, request_id: str, response_data: dict) -> bool:
        """响应服务器发起的用户输入请求（Layer 4 - elicitation）。"""
        self._send({
            "method": "elicitation/response",
            "params": {"id": request_id, "response": response_data},
        })
        result = self._recv()
        return result is not None and "result" in result

    # ── Layer 6: Router Integration ───────────────────────────────────────────

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        if not self.is_connected:
            return f"MCP Error: not connected to {self.server_name}"
        self._send({"method": "tools/call", "params": {"name": tool_name, "arguments": arguments}})
        response = self._recv()
        if response and "result" in response:
            content = response["result"].get("content", [])
            return "\n".join(c.get("text", str(c)) for c in content)
        if response and "error" in response:
            return f"MCP Error: {response['error'].get('message', 'unknown')}"
        return "MCP Error: no response"

    def get_agent_tools(self) -> list[dict]:
        """返回 ToolSpec 列表，供 tool router 注册（Layer 6）。"""
        tools = []
        for tool in self.capabilities.tools:
            tools.append({
                "name": f"mcp__{self.server_name}__{tool['name']}",
                "description": tool.get("description", ""),
                "input_schema": tool.get("inputSchema", {"type": "object", "properties": {}}),
            })
        return tools

    def status_summary(self) -> dict:
        """返回当前连接和能力状态摘要，供 /mcp 调试命令使用。"""
        return {
            "server":         self.server_name,
            "scope":          self.config.scope,
            "transport":      self.config.transport,
            "status":         self.connection.status,
            "error":          self.connection.error_message,
            "tools":          len(self.capabilities.tools),
            "resources":      len(self.capabilities.resources),
            "prompts":        len(self.capabilities.prompts),
            "elicitation":    self.capabilities.supports_elicitation,
            "needs_auth":     self.auth.needs_auth,
            "server_info":    self.connection.server_info,
        }

    # ── disconnect ────────────────────────────────────────────────────────────

    def disconnect(self):
        if self.process:
            try:
                self._send({"method": "shutdown"})
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
            self.process = None
            self.connection.mark_failed("disconnected")

    # ── transport ─────────────────────────────────────────────────────────────

    def _send(self, message: dict):
        if not self.process or self.process.poll() is not None:
            return
        self._request_id += 1
        envelope = {"jsonrpc": "2.0", "id": self._request_id, **message}
        try:
            self.process.stdin.write(json.dumps(envelope) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            self.connection.mark_failed("broken pipe")

    def _recv(self) -> dict | None:
        if not self.process or self.process.poll() is not None:
            return None
        try:
            line = self.process.stdout.readline()
            if line:
                data = json.loads(line)
                # 检测 elicitation 请求（服务器主动发起）
                if data.get("method") == "elicitation/create":
                    params = data.get("params", {})
                    self.pending_elicitations.append(ElicitationRequest(
                        server_name=self.server_name,
                        message=params.get("message", ""),
                        requested_schema=params.get("requestedSchema", {}),
                        request_id=str(data.get("id", "")),
                    ))
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return None
