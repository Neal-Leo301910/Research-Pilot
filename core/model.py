"""
core/model.py

纯粹的模型调用层 —— 只负责一次 API 调用，不做任何 retry 循环。

返回值:
  response                  调用成功
  raise ModelTransportError 可重试的网络/基础设施错误  → 调用方处理 transport_retry
  raise ModelOverloadError  prompt 过长需压缩         → 调用方处理 compact_retry
"""
from __future__ import annotations

import random
import time

from anthropic import APIError

from config import MODEL, client, BACKOFF_BASE_DELAY, BACKOFF_MAX_DELAY
from core.messages import normalize_messages


# ── 自定义异常 ────────────────────────────────────────────────────────────────

class ModelTransportError(Exception):
    """网络抖动、速率限制、服务暂时不可用 → transport_retry"""
    def __init__(self, message: str, original: Exception):
        super().__init__(message)
        self.original = original


class ModelOverloadError(Exception):
    """Prompt 过长 → compact_retry"""
    def __init__(self, message: str, original: Exception):
        super().__init__(message)
        self.original = original


# ── 谓词函数 ──────────────────────────────────────────────────────────────────

def should_retry_transport(error: Exception) -> bool:
    if isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return True
    if isinstance(error, APIError):
        body = str(error).lower()
        if "overlong_prompt" in body or ("prompt" in body and "long" in body):
            return False
        return True
    return False


def should_recompact(error: Exception) -> bool:
    if isinstance(error, APIError):
        body = str(error).lower()
        return "overlong_prompt" in body or ("prompt" in body and "long" in body)
    return False


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def backoff_delay(attempt: int) -> float:
    """指数退避 + 随机抖动，上限 BACKOFF_MAX_DELAY。"""
    return min(BACKOFF_BASE_DELAY * (2 ** attempt), BACKOFF_MAX_DELAY) + random.uniform(0, 1)


# ── 单次调用 ──────────────────────────────────────────────────────────────────

def call_model_once(messages: list, system: str, tools: list):
    """
    执行一次模型调用。接受已组装好的 payload 三要素。

    成功              → 返回 response
    Prompt 过长       → raise ModelOverloadError
    网络/基础设施错误  → raise ModelTransportError
    其他异常           → 直接传播
    """
    try:
        return client.messages.create(
            model=MODEL,
            system=system,
            messages=normalize_messages(messages),
            tools=tools,
            max_tokens=8000,
        )
    except APIError as e:
        if should_recompact(e):
            raise ModelOverloadError(f"Prompt too long: {e}", e)
        raise ModelTransportError(f"API error: {e}", e)
    except (ConnectionError, TimeoutError, OSError) as e:
        raise ModelTransportError(f"Connection error: {e}", e)
