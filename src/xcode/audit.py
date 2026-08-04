"""审计日志：工具执行明细落盘 JSONL（todo #8；展示归 #23）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def audit_path(data_home: Path) -> Path:
    """返回审计日志文件路径。"""
    return data_home / "audit.jsonl"


def append_audit(
    data_home: Path,
    *,
    session_id: str,
    tool: str,
    args: dict[str, Any],
    approved: bool,
    is_error: bool,
) -> None:
    """追加一条审计记录（副作用：写 JSONL 一行）。"""
    entry = {
        "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "session_id": session_id,
        "tool": tool,
        "args": args,
        "approved": approved,
        "is_error": is_error,
    }
    path = audit_path(data_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
