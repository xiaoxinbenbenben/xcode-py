"""工具基类、执行上下文与极简结果。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ToolContext:
    """工具执行上下文。

    workspace 必填；data_home / ask_permission 由运行时注入。
    ask_permission：高危工具执行前的审批回调，**仅当工具声明
    requires_approval=True 时才会被运行时调用**；普通工具不询问。
    回调为 None（如 -p 非交互模式未注入）时不询问、一律拒绝。
    注意：当前内置工具均未声明 requires_approval（首个使用者是
    任务 3 的 revert_turn；bash 等 -p 模式仍需可用的工具暂不设）。
    """

    workspace: Path
    data_home: Path | None = None
    ask_permission: Callable[[str], Awaitable[bool]] | None = None


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
