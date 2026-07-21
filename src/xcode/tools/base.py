"""工具基类与统一响应协议。"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass(slots=True)
class ToolResult:
    """统一工具返回：ok 摘要 + 可截断正文。"""

    ok: bool
    summary: str
    content: str = ""
    truncated: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def to_message_content(self, *, max_chars: int) -> str:
        """序列化为发给模型的 tool message 文本。"""
        body = self.content
        truncated = self.truncated
        if len(body) > max_chars:
            body = body[:max_chars] + "\n…[truncated]"
            truncated = True
        payload = {
            "ok": self.ok,
            "summary": self.summary,
            "truncated": truncated,
            "content": body,
            "data": self.data,
        }
        return json.dumps(payload, ensure_ascii=False)


@dataclass(slots=True)
class ToolContext:
    """工具执行时可见的运行时上下文。"""

    workspace: Path
    session_data_dir: Path
    todos: list[dict[str, str]]
    max_output_chars: int
    memory_dir: Path
    ask_permission: Callable[[str, dict[str, Any]], bool] | None = None


class Tool(ABC):
    name: str
    description: str
    parameters: dict[str, Any]

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行工具。输入：模型给出的 args 与 ToolContext；输出：ToolResult。"""


def resolve_workspace_path(workspace: Path, raw: str) -> Path:
    """把相对/绝对路径规范到绝对路径，并校验仍在 workspace 内。"""
    workspace = workspace.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = workspace / path
    path = path.resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise PermissionError(f"path outside workspace: {path}") from exc
    return path
