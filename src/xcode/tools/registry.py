"""工具注册表：按名查找与导出 OpenAI schemas。"""

from __future__ import annotations

from typing import Iterable

from xcode.tools.base import Tool


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
