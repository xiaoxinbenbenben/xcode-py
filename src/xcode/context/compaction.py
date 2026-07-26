"""历史压缩：按体积/轮次折叠较早消息，保持 tool 成对。"""

from __future__ import annotations

import json
from typing import Any


def estimate_chars(messages: list[dict[str, Any]]) -> int:
    """粗估消息占用字符数（本地启发式，不调 tokenizer）。"""
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
        elif content is not None:
            total += len(json.dumps(content, ensure_ascii=False))
        for tc in msg.get("tool_calls") or []:
            total += len(json.dumps(tc, ensure_ascii=False))
    return total


def should_auto_compact(
    messages: list[dict[str, Any]],
    *,
    threshold_turns: int = 40,
    threshold_chars: int = 48_000,
) -> bool:
    """用户轮次或估算体积超阈值则触发压缩。"""
    user_turns = sum(1 for m in messages if m.get("role") == "user")
    if user_turns >= threshold_turns:
        return True
    return estimate_chars(messages) >= threshold_chars


def _find_safe_cut(messages: list[dict[str, Any]], keep_last: int) -> int:
    """计算 recent 起点，避免孤立的 role=tool 消息。

    输入：完整 messages、希望保留的近似条数。
    输出：recent 起始下标（0..len）。
    """
    if len(messages) <= keep_last:
        return 0
    cut = len(messages) - keep_last
    # 若 cut 落在 tool 消息上，向前扩到带 tool_calls 的 assistant
    while cut < len(messages) and messages[cut].get("role") == "tool":
        cut -= 1
        if cut < 0:
            return 0
    if cut < len(messages) and messages[cut].get("role") == "assistant" and messages[cut].get("tool_calls"):
        # 保留整组 assistant+tools：起点就是该 assistant
        return cut
    # 若 cut 之后立刻是 tool（说明 assistant 被切掉），再往前找
    while cut > 0 and cut < len(messages) and messages[cut].get("role") == "tool":
        cut -= 1
    return max(0, cut)


def _summarize_message(msg: dict[str, Any]) -> str | None:
    role = msg.get("role", "?")
    content = msg.get("content")
    if role == "tool":
        preview = content.strip()[:160] if isinstance(content, str) else ""
        return f"tool[{msg.get('tool_call_id', '')[:8]}]: {preview}" if preview else f"tool:{msg.get('tool_call_id', '?')}"
    if msg.get("tool_calls"):
        names = [tc.get("function", {}).get("name", "?") for tc in msg["tool_calls"]]
        extra = f" | {content.strip()[:120]}" if isinstance(content, str) and content.strip() else ""
        return f"assistant:tools={','.join(names)}{extra}"
    if isinstance(content, str) and content.strip():
        return f"{role}: {content.strip()[:240]}"
    return None


def compact_messages(
    messages: list[dict[str, Any]],
    *,
    keep_last: int = 12,
    existing_summary: str | None = None,
    max_summary_chars: int = 6000,
) -> tuple[list[dict[str, Any]], str]:
    """压缩消息列表。

    输入：完整 messages、保留条数、已有 summary。
    输出：(压缩后 messages, 新 summary)。本地启发式，不调模型。
    """
    # --- 1) 找安全切点（不拆散 assistant+tool 组） ---
    cut = _find_safe_cut(messages, keep_last)
    if cut <= 0:
        return list(messages), existing_summary or ""

    older, recent = messages[:cut], messages[cut:]

    # --- 2) 把 older 压成可读 summary 行 ---
    lines: list[str] = []
    tool_names: list[str] = []
    user_n = assistant_n = tool_n = 0
    for msg in older:
        role = msg.get("role")
        if role == "user":
            user_n += 1
        elif role == "assistant":
            assistant_n += 1
            for tc in msg.get("tool_calls") or []:
                name = tc.get("function", {}).get("name")
                if name:
                    tool_names.append(str(name))
        elif role == "tool":
            tool_n += 1
        line = _summarize_message(msg)
        if line:
            lines.append(line)

    # --- 3) 拼 summary，recent 原样保留 ---
    header = (
        f"[compacted {len(older)} msgs: user={user_n} assistant={assistant_n} "
        f"tool={tool_n}; tools={','.join(sorted(set(tool_names))[:12]) or '-'}]"
    )
    body = "\n".join(lines[-50:])
    parts = [p for p in [existing_summary, header, body] if p]
    summary = "\n".join(parts)
    if len(summary) > max_summary_chars:
        summary = summary[-max_summary_chars:]
    return recent, summary
