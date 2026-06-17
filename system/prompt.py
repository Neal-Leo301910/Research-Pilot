"""
system/prompt.py

系统提示组装 + API payload 最终组装。

关键概念：
  PromptParts      各段 system prompt 的具名容器
  SystemPromptBuilder  把 PromptParts 各段组装成最终字符串
  build_api_payload    三面并列地组装最终 API payload：
                         system prompt | messages | tools
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from config import WORKDIR, REPO_ROOT, MODEL, DYNAMIC_BOUNDARY


# ── PromptParts ───────────────────────────────────────────────────────────────

@dataclass
class PromptParts:
    """
    system prompt 各段的具名容器。

    每段职责：
      core        身份、规则、基本行为准则
      tools       工具能力描述（来自 ToolSpec 列表）
      skills      当前可用技能
      memory      跨会话持久记忆
      claude_md   项目/用户级 CLAUDE.md 指令
      dynamic     当前时间、工作目录等运行时信息
    """
    core:      str = ""
    tools:     str = ""
    skills:    str = ""
    memory:    str = ""
    claude_md: str = ""
    dynamic:   str = ""


# ── SystemPromptBuilder ───────────────────────────────────────────────────────

class SystemPromptBuilder:
    """把 PromptParts 各段组装成最终 system prompt 字符串。"""

    def __init__(self, workdir: Path, tools: list, skill_registry, memory_mgr, permission_gate):
        self.workdir = workdir
        self.tools = tools
        self.skill_registry = skill_registry
        self.memory_mgr = memory_mgr
        self.permission_gate = permission_gate

    def build_parts(self) -> PromptParts:
        return PromptParts(
            core=self._core(),
            tools=self._tools(),
            skills=self._skills(),
            memory=self._memory(),
            claude_md=self._claude_md(),
            dynamic=self._dynamic(),
        )

    def build(self) -> str:
        parts = self.build_parts()
        sections = [s for s in [
            parts.core,
            parts.tools,
            parts.skills,
            parts.memory,
            parts.claude_md,
        ] if s]
        sections.append(DYNAMIC_BOUNDARY)
        sections.append(parts.dynamic)
        return "\n\n".join(sections)

    # ── 各段构造 ──────────────────────────────────────────────────────────────

    def _core(self) -> str:
        return (
            f"You are a coding agent operating in {self.workdir}.\n"
            "Coordinate tools, teammates, tasks, schedules, worktrees, hooks, memory, and MCP integrations.\n"
            "Always verify before assuming. Prefer reading files over guessing."
        )

    def _tools(self) -> str:
        if not self.tools:
            return ""
        lines = ["# Available tools"]
        for tool in self.tools:
            props = tool.get("input_schema", {}).get("properties", {})
            params = ", ".join(props.keys())
            lines.append(f"- {tool['name']}({params}): {tool.get('description', '')}")
        return "\n".join(lines)

    def _skills(self) -> str:
        return "# Available skills\n" + self.skill_registry.describe_available()

    def _memory(self) -> str:
        return self.memory_mgr.load_memory_prompt()

    def _claude_md(self) -> str:
        sources = []
        user_claude = Path.home() / ".claude" / "CLAUDE.md"
        if user_claude.exists():
            sources.append(("user global (~/.claude/CLAUDE.md)", user_claude.read_text(encoding="utf-8")))
        project_claude = REPO_ROOT / "CLAUDE.md"
        if project_claude.exists():
            sources.append(("project root (CLAUDE.md)", project_claude.read_text(encoding="utf-8")))
        cwd = Path.cwd()
        if cwd != REPO_ROOT:
            sub = cwd / "CLAUDE.md"
            if sub.exists():
                sources.append((f"subdir ({cwd.name}/CLAUDE.md)", sub.read_text(encoding="utf-8")))
        if not sources:
            return ""
        parts = ["# CLAUDE.md instructions"]
        for label, content in sources:
            parts.append(f"## From {label}")
            parts.append(content.strip())
        return "\n\n".join(parts)

    def _dynamic(self) -> str:
        from permissions.manager import is_workspace_trusted
        lines = [
            f"Current date: {datetime.now().date().isoformat()}",
            f"Working directory: {self.workdir}",
            f"Repository root: {REPO_ROOT}",
            f"Model: {MODEL}",
            f"Permission mode: {self.permission_gate.mode}",
            f"Workspace trusted for hooks: {is_workspace_trusted()}",
        ]
        return "# Dynamic context\n" + "\n".join(lines)


# ── API payload 最终组装 ──────────────────────────────────────────────────────

_MEMORY_GUIDANCE = """
# Memory guidance
Use save_memory only for cross-session information worth recalling later:
- user preferences
- repeated user feedback
- non-obvious project facts or decision reasons
- pointers to external resources
Do not save secrets, temporary task state, or facts easily re-read from the repository.
"""

_AGENT_GUIDANCE = (
    "\n\nUse todo for multi-step work and keep exactly one item in_progress."
    "\nUse task for isolated one-shot subagents when exploration would clutter the main context."
    "\nUse load_skill when a task needs specialized instructions before acting."
)


def build_system_prompt(workdir, tools, skill_registry, memory_mgr, permission_gate) -> str:
    """
    组装 system prompt 字符串（API payload 的第一面）。

    system prompt 只放：身份、规则、工具能力描述、长期说明。
    不放：tool_result、hook 注入的补充说明、当前轮临时提醒。
    这些走消息流（build_messages_pipeline）。
    """
    builder = SystemPromptBuilder(workdir, tools, skill_registry, memory_mgr, permission_gate)
    return builder.build() + _AGENT_GUIDANCE + _MEMORY_GUIDANCE


def build_api_payload(
    system: str,
    messages: list,
    tools: list,
) -> dict[str, Any]:
    """
    三面并列地组装最终 API payload。

    system prompt、messages、tools 是并列输入面，而不是互相替代：
      system   <- build_system_prompt()          长期规则 + 工具描述
      messages <- build_messages_pipeline()      对话历史 + 提醒 + attachment
      tools    <- build_tool_pool()              ToolSpec 列表（模型看见的 schema）

    调用方直接把这个 dict ** 展开给 client.messages.create()。
    """
    return {
        "system":   system,
        "messages": messages,
        "tools":    tools,
    }
