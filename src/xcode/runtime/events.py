"""产品事件：扁平 dict（type + 字段），给 CLI/TUI 消费。"""

from __future__ import annotations

from typing import Any, Final

TEXT_DELTA: Final = "text_delta"
THINKING_DELTA: Final = "thinking_delta"
USAGE: Final = "usage"
TURN_COMPLETE: Final = "turn_complete"
TOOL_CALL: Final = "tool_call"
TOOL_RESULT: Final = "tool_result"
ERROR: Final = "error"
DONE: Final = "done"


def make_event(event_type: str, **fields: Any) -> dict[str, Any]:
    """拼一条产品事件。

    输入：事件类型 + 顶层字段；输出：`{"type": event_type, ...fields}`。
    """
    return {"type": event_type, **fields}


def map_finish_reason(reason: str | None) -> str:
    """把 API 的 finish_reason 收成产品层 stop_reason。"""
    if reason in {"tool_calls", "tool_use"}:
        return "tool_use"
    if reason == "length":
        return "max_tokens"
    if reason == "content_filter":
        return "stop_sequence"
    if reason in {None, "", "stop"}:
        return "end_turn"
    return str(reason)
