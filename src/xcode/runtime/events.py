"""结构化 runtime 事件，供 CLI/TUI 统一消费。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class EventBuilder:
    """按 run 生成带公共字段的事件字典。"""

    run_id: str
    session_id: str

    def build(self, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "type": event_type,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "ts": _utc_now(),
            "payload": payload or {},
        }


def new_run_id() -> str:
    return f"run-{uuid4().hex[:12]}"
