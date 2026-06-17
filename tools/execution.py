"""
tools/execution.py

工具执行运行时：分批 → 并发/串行 → 收集结果 → 按序合并 context modifier。

关键数据结构：
  TrackedTool           显式跟踪每个工具的生命周期状态
  ToolExecutionBatch    按并发安全性分好的一批工具
  MessageUpdate         工具执行过程中产出的消息/context 更新

核心执行流：
  tool_use blocks
    -> partition_tool_calls()       按并发安全性分批
    -> for each batch:
         if safe  -> run_concurrently()   并发执行
         else     -> run_serially()       串行执行
    -> 收集 queued_context_modifiers 按原始顺序落地
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from typing import Any, Callable

from tools.context import ToolUseContext, ToolResultEnvelope

# ── 哪些工具是并发安全的（只读、无副作用）────────────────────────────────────

_CONCURRENCY_SAFE: frozenset[str] = frozenset({
    "read_file",
    "skill_list",
    "load_skill",
    "task_list",
    "task_get",
    "check_background",
    "cron_list",
    "worktree_list",
    "worktree_status",
    "worktree_events",
    "list_teammates",
    "memory_list",
})


def is_concurrency_safe(tool_name: str, tool_input: dict) -> bool:
    """只读、无副作用的工具可以并发执行。"""
    if tool_name.startswith("mcp__"):
        # MCP 工具保守起见默认串行，除非工具名包含明确的只读信号
        return False
    return tool_name in _CONCURRENCY_SAFE


# ── TrackedTool ───────────────────────────────────────────────────────────────

@dataclass
class TrackedTool:
    """
    显式跟踪单个工具调用的完整生命周期。

    status 转移:  queued -> executing -> completed | error
    """
    id: str                          # tool_use block id
    name: str
    input: dict
    status: str = "queued"           # queued / executing / completed / error
    is_concurrency_safe: bool = False
    result: ToolResultEnvelope | None = None
    context_modifiers: list[Callable] = field(default_factory=list)

    def mark_executing(self) -> None:
        self.status = "executing"

    def mark_done(self, result: ToolResultEnvelope) -> None:
        self.result = result
        self.status = "completed" if result.ok else "error"


# ── ToolExecutionBatch ────────────────────────────────────────────────────────

@dataclass
class ToolExecutionBatch:
    """一批工具，要么全部并发安全，要么全部需要串行。"""
    is_concurrency_safe: bool
    tools: list[TrackedTool] = field(default_factory=list)


# ── MessageUpdate ─────────────────────────────────────────────────────────────

@dataclass
class MessageUpdate:
    """工具执行过程中产出的更新——可能是消息，也可能是 context 修改。"""
    tool_id: str
    message: dict | None = None          # 立刻往上游发的消息块
    context_modifier: Callable | None = None  # 暂存，最后按序落地


# ── 分批 ──────────────────────────────────────────────────────────────────────

def partition_tool_calls(tool_use_blocks: list) -> list[ToolExecutionBatch]:
    """
    把 tool_use block 列表按并发安全性分成若干批次。

    规则：
      - 遇到不安全工具时，先把当前安全批提交，再开新的不安全批。
      - 连续的安全工具合并进同一批。
    """
    batches: list[ToolExecutionBatch] = []
    current_safe_batch: list[TrackedTool] = []

    for block in tool_use_blocks:
        if getattr(block, "type", None) != "tool_use":
            continue
        tool_input = dict(getattr(block, "input", None) or {})
        safe = is_concurrency_safe(block.name, tool_input)
        tracked = TrackedTool(
            id=block.id,
            name=block.name,
            input=tool_input,
            is_concurrency_safe=safe,
        )
        if safe:
            current_safe_batch.append(tracked)
        else:
            # 先提交已有的安全批
            if current_safe_batch:
                batches.append(ToolExecutionBatch(is_concurrency_safe=True, tools=list(current_safe_batch)))
                current_safe_batch = []
            # 不安全工具独占一个批次
            batches.append(ToolExecutionBatch(is_concurrency_safe=False, tools=[tracked]))

    if current_safe_batch:
        batches.append(ToolExecutionBatch(is_concurrency_safe=True, tools=current_safe_batch))

    return batches


# ── 执行单个工具 ───────────────────────────────────────────────────────────────

def _execute_one(tracked: TrackedTool, ctx: ToolUseContext) -> ToolResultEnvelope:
    tracked.mark_executing()
    try:
        if tracked.name.startswith("mcp__"):
            parts = tracked.name.split("__", 2)
            if len(parts) != 3:
                return ToolResultEnvelope.error(f"Invalid MCP tool name: {tracked.name}")
            _, server_name, actual_tool = parts
            mcp_client = ctx.mcp_clients.get(server_name)
            if not mcp_client:
                return ToolResultEnvelope.error(f"MCP server not found: {server_name}")
            result_str = mcp_client.call_tool(actual_tool, tracked.input)
            return ToolResultEnvelope.success(result_str)

        handler = ctx.handlers.get(tracked.name)
        if not handler:
            return ToolResultEnvelope.error(f"Unknown tool: {tracked.name}")
        output = handler(tracked.input, ctx)
        # handler 可以返回 ToolResultEnvelope 或裸字符串（向后兼容）
        if isinstance(output, ToolResultEnvelope):
            return output
        return ToolResultEnvelope.success(str(output))
    except Exception as e:
        return ToolResultEnvelope.error(f"Error: {e}")


# ── 并发批执行 ────────────────────────────────────────────────────────────────

def run_concurrently(batch: ToolExecutionBatch, ctx: ToolUseContext) -> list[MessageUpdate]:
    """
    并发执行一批安全工具。

    context_modifier 暂存进 MessageUpdate，不立即落地，
    由调用方按原始顺序统一 apply。
    """
    updates: dict[str, MessageUpdate] = {t.id: MessageUpdate(tool_id=t.id) for t in batch.tools}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch.tools)) as pool:
        future_to_tool = {pool.submit(_execute_one, t, ctx): t for t in batch.tools}
        for future in concurrent.futures.as_completed(future_to_tool):
            tracked = future_to_tool[future]
            try:
                result = future.result()
            except Exception as e:
                result = ToolResultEnvelope.error(f"Executor error: {e}")
            tracked.mark_done(result)
            updates[tracked.id].message = {
                "type": "tool_result",
                "tool_use_id": tracked.id,
                "content": result.to_api_content(),
            }

    # 按原始顺序返回
    return [updates[t.id] for t in batch.tools]


# ── 串行批执行 ────────────────────────────────────────────────────────────────

def run_serially(batch: ToolExecutionBatch, ctx: ToolUseContext) -> list[MessageUpdate]:
    """串行执行一批工具，context 修改直接落地（无顺序冲突风险）。"""
    updates: list[MessageUpdate] = []
    for tracked in batch.tools:
        result = _execute_one(tracked, ctx)
        tracked.mark_done(result)
        updates.append(MessageUpdate(
            tool_id=tracked.id,
            message={
                "type": "tool_result",
                "tool_use_id": tracked.id,
                "content": result.to_api_content(),
            },
        ))
    return updates


# ── 顶层：执行所有批次，按序收集结果 ─────────────────────────────────────────

def execute_tool_batches(
    tool_use_blocks: list,
    ctx: ToolUseContext,
) -> tuple[list[dict], list[TrackedTool]]:
    """
    完整执行流：分批 → 执行 → 按序合并 context modifier → 收集 tool_result。

    返回：
      tool_results   list[dict]          tool_result 块列表，直接追加进 messages
      all_tracked    list[TrackedTool]   所有工具的执行记录（含状态、结果）
    """
    batches = partition_tool_calls(tool_use_blocks)
    tool_results: list[dict] = []
    all_tracked: list[TrackedTool] = []

    # queued_context_modifiers: 并发批次里暂存，按原始顺序落地
    queued_modifiers: dict[str, list[Callable]] = {}

    for batch in batches:
        if batch.is_concurrency_safe:
            updates = run_concurrently(batch, ctx)
        else:
            updates = run_serially(batch, ctx)

        for update in updates:
            if update.message:
                tool_results.append(update.message)
            if update.context_modifier:
                queued_modifiers.setdefault(update.tool_id, []).append(update.context_modifier)

        all_tracked.extend(batch.tools)

    # 按原始工具顺序落地 context modifier（避免并发乱序）
    for tracked in all_tracked:
        for modifier in queued_modifiers.get(tracked.id, []):
            modifier(ctx)

    return tool_results, all_tracked
