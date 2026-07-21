"""大工具输出：截断预览 + 完整内容落盘以便 Read 回查。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4


@dataclass(slots=True)
class SpillResult:
    """截断结果：预览文本 + 完整文件相对路径。"""

    preview: str
    full_path: str
    original_chars: int
    kept_chars: int


def spill_large_output(
    *,
    tool_name: str,
    full_output: str,
    session_data_dir: Path,
    max_chars: int,
) -> SpillResult | None:
    """若输出超限则落盘并返回预览；未超限返回 None。"""
    if len(full_output) <= max_chars:
        return None
    out_dir = session_data_dir / "tool-output"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", tool_name).strip("-") or "tool"
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe}_{uuid4().hex[:8]}.txt"
    path = out_dir / filename
    path.write_text(full_output, encoding="utf-8")
    # 相对会话目录，模型可用 Read 打开（路径相对 workspace 时需绝对或会话内）
    # 这里返回相对 session_data_dir 的路径，并在提示里写清
    rel = f"tool-output/{filename}"
    preview = full_output[:max_chars].rstrip() + (
        f"\n\n…[truncated {len(full_output)} chars → full output at session:{rel} "
        f"(use Read on absolute path under session data if needed)]"
    )
    return SpillResult(
        preview=preview,
        full_path=str(path),
        original_chars=len(full_output),
        kept_chars=max_chars,
    )
