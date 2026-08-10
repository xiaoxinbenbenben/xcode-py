"""本地 token 估算（tiktoken）。

用途（两处共用同一套数字，避免「展示一套、触发另一套」）：
1. compact 是否该跑：estimated_input + reserve >= window * threshold
2. TUI 底栏 /status：显示 ~used/256k

说明：
- 对 OpenAI 模型名较准；对 DeepSeek 等兼容 API 只是近似，UI 用 ~ 前缀表示。
- 不依赖服务端 usage 账单，这样才能在「发送前」就知道会不会爆窗口。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import tiktoken


@lru_cache(maxsize=16)
def _encoding_for_model(model: str) -> tiktoken.Encoding:
    """按模型名取 encoding；未知则 fallback。"""
    name = (model or "").strip()
    if name:
        try:
            return tiktoken.encoding_for_model(name)
        except KeyError:
            pass
        # 常见兼容名
        lowered = name.lower()
        if "gpt-4o" in lowered or "o1" in lowered or "o3" in lowered or "o4" in lowered:
            try:
                return tiktoken.get_encoding("o200k_base")
            except Exception:  # noqa: BLE001
                pass
    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception:  # noqa: BLE001
        return tiktoken.get_encoding("cl100k_base")


def count_text_tokens(text: str, *, model: str = "") -> int:
    """估算一段文本的 token 数。"""
    if not text:
        return 0
    enc = _encoding_for_model(model)
    return len(enc.encode(text))


def count_messages_tokens(messages: list[dict[str, Any]], *, model: str = "") -> int:
    """粗估 chat messages 列表（含 tool_calls 结构）。"""
    total = 0
    for msg in messages:
        role = str(msg.get("role") or "")
        total += 4  # 每条消息开销近似
        total += count_text_tokens(role, model=model)
        content = msg.get("content")
        if isinstance(content, str):
            total += count_text_tokens(content, model=model)
        elif content is not None:
            total += count_text_tokens(json.dumps(content, ensure_ascii=False), model=model)
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            total += count_text_tokens(json.dumps(tool_calls, ensure_ascii=False), model=model)
        if msg.get("tool_call_id"):
            total += count_text_tokens(str(msg["tool_call_id"]), model=model)
    return total


def format_token_usage(used: int, window: int) -> str:
    """TUI 用短串，如 ~12.4k/256k。"""
    def _fmt(n: int) -> str:
        if n >= 1000:
            return f"{n / 1000:.1f}k".replace(".0k", "k")
        return str(n)

    return f"~{_fmt(used)}/{_fmt(window)}"
