"""权限引擎 MVP：敏感工具可走回调审批。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

SENSITIVE_TOOLS = {"Bash", "Edit", "Write", "BackgroundRun"}


@dataclass
class PermissionEngine:
    """决定工具调用是否放行。

    ask：可选交互回调；auto_allow=True 时跳过询问（测试 / -p 默认）。
    """

    ask: Callable[[str, dict[str, Any]], bool] | None = None
    auto_allow: bool = False
    always_allow: set[str] = field(default_factory=set)

    def check(self, tool_name: str, params: dict[str, Any]) -> bool:
        if self.auto_allow or tool_name in self.always_allow:
            return True
        if tool_name not in SENSITIVE_TOOLS:
            return True
        if self.ask is None:
            return True
        return bool(self.ask(tool_name, params))
