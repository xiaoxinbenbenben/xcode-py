"""@file 提及预处理：从用户输入提取路径并校验存在性。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_MENTION_RE = re.compile(r"@([^\s]+)")


@dataclass(slots=True)
class MentionResult:
    cleaned_input: str
    mentioned_files: list[str]
    reminders: list[str]


def preprocess_mentions(user_input: str, workspace: Path) -> MentionResult:
    """解析 @path，返回清洗后输入、有效文件列表与 reminder 文案。"""
    mentioned: list[str] = []
    reminders: list[str] = []
    workspace = workspace.resolve()

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(1).rstrip(".,;:!?")
        path = Path(raw)
        if not path.is_absolute():
            path = workspace / path
        try:
            path = path.resolve()
            path.relative_to(workspace)
        except (OSError, ValueError):
            return match.group(0)
        if path.is_file():
            rel = str(path.relative_to(workspace))
            mentioned.append(rel)
            reminders.append(f"User mentioned file @{rel} — prefer Read before editing.")
            return f"`{rel}`"
        return match.group(0)

    cleaned = _MENTION_RE.sub(_replace, user_input)
    # 去重保持顺序
    uniq: list[str] = []
    for item in mentioned:
        if item not in uniq:
            uniq.append(item)
    return MentionResult(cleaned_input=cleaned, mentioned_files=uniq, reminders=reminders)
