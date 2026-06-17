"""
core/messages.py

消息规范化 + 输入组装管道。

关键概念：
  NormalizedMessage   标准化后的单条消息结构
  ReminderMessage     临时提醒，不进 system prompt，走消息流
  PromptInputPipeline 三面并列的 API payload 组装管道：
                        system prompt | messages | tools
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ── NormalizedMessage ─────────────────────────────────────────────────────────

@dataclass
class NormalizedMessage:
    """
    规范化后的单条消息。

    content 始终是"块列表"，而不是裸字符串，
    支持 text / tool_use / tool_result / attachment 等类型。
    """
    role: str                          # "user" | "assistant"
    content: list[dict]                # list of content blocks

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


# ── ReminderMessage ───────────────────────────────────────────────────────────

@dataclass
class ReminderMessage:
    """
    当前轮临时提醒——不放进 system prompt，而是注入消息流。

    例如：当前 permission mode、todo 提醒、hook 注入的补充说明。
    """
    content: str
    source: str = "system"             # "system" | "hook" | "todo"

    def to_user_message(self) -> dict:
        return {"role": "user", "content": [{"type": "text", "text": self.content}]}


# ── 低层：content block 规范化 ────────────────────────────────────────────────

def content_block_to_dict(block) -> dict | None:
    if isinstance(block, dict):
        return {k: v for k, v in block.items() if not str(k).startswith("_")}
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    if hasattr(block, "dict"):
        return block.dict(exclude_none=True)
    block_type = getattr(block, "type", None)
    if block_type == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id":    getattr(block, "id", ""),
            "name":  getattr(block, "name", ""),
            "input": getattr(block, "input", {}),
        }
    return None


def normalize_messages(messages: list) -> list:
    """Strip unknown metadata, fill orphaned tool_use results, and merge same-role runs."""
    cleaned = []
    for msg in messages:
        clean = {"role": msg["role"]}
        content = msg.get("content", "")
        if isinstance(content, str):
            clean["content"] = content
        elif isinstance(content, list):
            blocks = [content_block_to_dict(b) for b in content]
            clean["content"] = [b for b in blocks if b is not None]
        else:
            clean["content"] = str(content)
        cleaned.append(clean)

    # fill orphaned tool_use (no matching tool_result)
    existing_results = set()
    for msg in cleaned:
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    existing_results.add(block.get("tool_use_id"))

    for msg in list(cleaned):
        if msg["role"] != "assistant" or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and block.get("id") not in existing_results):
                cleaned.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": block["id"], "content": "(cancelled)"}],
                })

    if not cleaned:
        return cleaned

    # merge consecutive same-role messages
    merged = [cleaned[0]]
    for msg in cleaned[1:]:
        if msg["role"] == merged[-1]["role"]:
            prev = merged[-1]
            prev_c = prev["content"] if isinstance(prev["content"], list) else [{"type": "text", "text": str(prev["content"])}]
            curr_c = msg["content"]  if isinstance(msg["content"],  list) else [{"type": "text", "text": str(msg["content"])}]
            prev["content"] = prev_c + curr_c
        else:
            merged.append(msg)
    return merged


def extract_text(content) -> str:
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text:
            texts.append(text)
    return "\n".join(texts).strip()


def estimate_context_size(messages: list) -> int:
    return len(json.dumps(normalize_messages(messages), ensure_ascii=False, default=str))


def estimate_tokens(messages: list) -> int:
    return estimate_context_size(messages) // 4


# ── 消息输入管道 ──────────────────────────────────────────────────────────────

def attach_memory(messages: list, memory_attachments: list[dict]) -> list:
    """把 memory attachment 注入消息流（不放进 system prompt）。"""
    if not memory_attachments:
        return messages
    injection = {
        "role": "user",
        "content": [{"type": "text", "text": att["content"]} for att in memory_attachments],
    }
    return [injection] + messages


def append_reminders(messages: list, reminders: list[ReminderMessage]) -> list:
    """把当前轮临时提醒追加到消息流末尾。"""
    for reminder in reminders:
        messages.append(reminder.to_user_message())
    return messages


def build_messages_pipeline(
    raw_messages: list,
    attachments: list[dict] | None = None,
    reminders: list[ReminderMessage] | None = None,
) -> list:
    """
    消息输入组装管道：

      raw_messages
        -> normalize_messages()       规范化 + 修复孤立 tool_use
        -> attach_memory()            注入 memory attachment
        -> append_reminders()         追加临时提醒
        -> 最终 messages 列表

    与 system prompt、tools 并列，共同组成 API payload 的三个输入面。
    """
    messages = normalize_messages(raw_messages)
    messages = attach_memory(messages, attachments or [])
    messages = append_reminders(messages, reminders or [])
    return messages

def collect_tool_result_blocks(messages: list) -> list[tuple[int, int, dict]]:
    blocks = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            block_dict = block if isinstance(block, dict) else content_block_to_dict(block)
            if isinstance(block_dict, dict) and block_dict.get("type") == "tool_result":
                blocks.append((message_index, block_index, block_dict))
    return blocks