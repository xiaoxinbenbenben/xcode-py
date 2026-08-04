"""工具基类、执行上下文与极简结果。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ToolContext:
    """工具执行上下文；目前仅 workspace。"""

    workspace: Path


@dataclass(slots=True)
class ToolResult:
    """工具执行结果：纯文本 + 是否错误。"""

    text: str
    is_error: bool = False

    @property
    def ok(self) -> bool:
        return not self.is_error


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
    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行工具；返回纯文本结果（I/O 工具为 async，便于子进程/HTTP）。"""


def resolve_workspace_path(workspace: Path, raw: str) -> Path:
    """把相对/绝对路径解析到 workspace 内；越界抛 PermissionError。"""
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
