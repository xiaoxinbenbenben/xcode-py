"""工具基类、执行上下文与极简结果。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ToolContext:
    """workspace 必填。ask_permission 仅 requires_approval 工具会调；None 则拒绝。"""

    workspace: Path
    data_home: Path | None = None
    ask_permission: Callable[[str], Awaitable[bool]] | None = None
    snapshot: Any = None
    code_index: Any = None


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
    requires_approval: bool = False

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


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        if tools:
            for tool in tools:
                self.register(tool)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return sorted(self._tools)

    def openai_tools(self) -> list[dict]:
        return [t.openai_schema() for t in self._tools.values()]
