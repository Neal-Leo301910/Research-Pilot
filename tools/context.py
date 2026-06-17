"""
tools/context.py

ToolUseContext — 工具执行时能访问的共享运行环境。

工具不再只拿到"输入参数"，还能拿到整个平台上下文：
  - handlers       本地 dispatch map
  - permission_ctx 权限信息（来自 PermissionManager）
  - mcp_clients    已连接的 MCP client map
  - messages       当前对话历史（只读引用，工具不得直接写）
  - app_state      跨工具共享的轻量 KV 状态
  - notifications  工具执行中产生的异步通知队列
  - cwd            工作目录
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ToolUseContext:
    """
    工具执行的共享上下文总线。

    所有工具通过同一个 ctx 实例访问平台能力，而不是靠参数透传。
    """
    # Layer 1 – dispatch
    handlers: dict[str, Any] = field(default_factory=dict)

    # Layer 2 – routing
    mcp_clients: dict[str, Any] = field(default_factory=dict)   # server_name -> MCPClient

    # Layer 3 – permission
    permission_ctx: dict[str, Any] = field(default_factory=dict)

    # Layer 4 – shared state
    messages: list = field(default_factory=list)       # read-only ref to main history
    app_state: dict[str, Any] = field(default_factory=dict)

    # Layer 5 – output channel
    notifications: list[dict] = field(default_factory=list)

    # Layer 6 – environment
    cwd: Path = field(default_factory=Path.cwd)

    def push_notification(self, kind: str, payload: Any) -> None:
        """工具执行过程中产生的异步通知入队。"""
        self.notifications.append({"kind": kind, "payload": payload})

    def drain_notifications(self) -> list[dict]:
        result = list(self.notifications)
        self.notifications.clear()
        return result


# ── ToolResultEnvelope ────────────────────────────────────────────────────────

@dataclass
class ToolResultEnvelope:
    """
    工具执行结果的统一包装。

    不再只是裸字符串，而是带类型信息的结构：
      ok          执行是否成功
      content     主要文本内容
      is_error    是否是错误结果
      attachments 附件列表（结构化数据、文件路径等）
    """
    ok: bool
    content: str
    is_error: bool = False
    attachments: list[Any] = field(default_factory=list)

    @classmethod
    def success(cls, content: str, attachments: list | None = None) -> "ToolResultEnvelope":
        return cls(ok=True, content=content, is_error=False, attachments=attachments or [])

    @classmethod
    def error(cls, content: str) -> "ToolResultEnvelope":
        return cls(ok=False, content=content, is_error=True)

    def to_api_content(self) -> str:
        """序列化为发给模型的字符串（Tool Result 的 content 字段）。"""
        import json
        payload = {
            "ok": self.ok,
            "content": self.content,
            "is_error": self.is_error,
        }
        if self.attachments:
            payload["attachments"] = self.attachments
        return json.dumps(payload, ensure_ascii=False, indent=2)
