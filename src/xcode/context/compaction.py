"""历史压缩：把较早轮次折叠为 summary 文本。"""

from __future__ import annotations

from typing import Any


def should_auto_compact(messages: list[dict[str, Any]], *, threshold_turns: int) -> bool:
    """按用户轮次数粗略判断是否触发自动压缩。"""
    user_turns = sum(1 for m in messages if m.get("role") == "user")
    return user_turns >= threshold_turns


def compact_messages(
    messages: list[dict[str, Any]],
    *,
    keep_last: int = 8,
    existing_summary: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """压缩消息列表。

    输入：完整 messages、保留最近条数、已有 summary。
    输出：(压缩后 messages, 新 summary)。本地启发式摘要，不强制再调模型。
    """
    if len(messages) <= keep_last:
        return list(messages), existing_summary or ""

    older, recent = messages[:-keep_last], messages[-keep_last:]
    snippets: list[str] = []
    for msg in older:
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            snippets.append(f"{role}: {content.strip()[:240]}")
        elif msg.get("tool_calls"):
            names = [tc.get("function", {}).get("name", "?") for tc in msg["tool_calls"]]
            snippets.append(f"assistant:tools={','.join(names)}")
    block = "\n".join(snippets[-40:])
    summary = (existing_summary + "\n" if existing_summary else "") + block
    summary = summary[-4000:]
    return recent, summary
