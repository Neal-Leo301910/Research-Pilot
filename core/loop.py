"""
core/loop.py

心智模型：一条 query = 一组"继续原因"串起来的状态转移。

每次循环推进之前，必须明确记录 transition 原因：
  - tool_result_continuation   正常主线，工具执行完毕继续
  - max_tokens_recovery        输出被截断，追加续写提示继续
  - compact_retry              上下文过长压缩后继续
  - transport_retry            基础设施抖动，退避后重试
  - stop_hook_continuation     外部 hook 阻止本轮结束，继续
  - budget_continuation        系统主动利用剩余预算继续推进
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ── Transition reason type ────────────────────────────────────────────────────

TransitionReason = Literal[
    "tool_result_continuation",
    "max_tokens_recovery",
    "compact_retry",
    "transport_retry",
    "stop_hook_continuation",
    "budget_continuation",
]

TRANSITIONS: tuple[str, ...] = (
    "tool_result_continuation",
    "max_tokens_recovery",
    "compact_retry",
    "transport_retry",
    "stop_hook_continuation",
    "budget_continuation",
)


# ── Per-query state ───────────────────────────────────────────────────────────

@dataclass
class QueryState:
    """Complete mutable state for one user query's agent loop."""

    messages: list = field(default_factory=list)

    # Progress counters
    turn_count: int = 1

    # Per-reason continuation budgets
    max_tokens_recovery_count: int = 0   # how many times we've appended CONTINUATION_MESSAGE
    compact_retry_count: int = 0         # how many times we've compacted and retried
    transport_retry_count: int = 0       # how many times we've slept and retried on infra errors

    # One-shot flags
    has_attempted_reactive_compact: bool = False

    # Transition tracing — set before every `continue`
    transition: TransitionReason | None = None

    # Max budgets (override at construction if needed)
    max_tokens_recovery_budget: int = 3
    compact_retry_budget: int = 2
    transport_retry_budget: int = 3

    def record_transition(self, reason: TransitionReason) -> None:
        """Set the current transition and bump the relevant counter."""
        self.transition = reason
        if reason == "max_tokens_recovery":
            self.max_tokens_recovery_count += 1
        elif reason == "compact_retry":
            self.compact_retry_count += 1
            self.has_attempted_reactive_compact = True
        elif reason == "transport_retry":
            self.transport_retry_count += 1
        elif reason == "tool_result_continuation":
            self.turn_count += 1

    def budget_ok(self, reason: TransitionReason) -> bool:
        """Return False when a particular recovery path is exhausted."""
        if reason == "max_tokens_recovery":
            return self.max_tokens_recovery_count < self.max_tokens_recovery_budget
        if reason == "compact_retry":
            return self.compact_retry_count < self.compact_retry_budget
        if reason == "transport_retry":
            return self.transport_retry_count < self.transport_retry_budget
        return True  # tool_result_continuation / stop_hook / budget have no hard cap here

    def summary(self) -> str:
        parts = [
            f"turn={self.turn_count}",
            f"transition={self.transition}",
            f"max_tokens_recovery={self.max_tokens_recovery_count}/{self.max_tokens_recovery_budget}",
            f"compact_retry={self.compact_retry_count}/{self.compact_retry_budget}",
            f"transport_retry={self.transport_retry_count}/{self.transport_retry_budget}",
        ]
        return "QueryState(" + ", ".join(parts) + ")"
