"""TUI 补全：slash 带说明；`@` 后补工作区路径。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from prompt_toolkit.completion import CompleteEvent, Completer, Completion, PathCompleter
from prompt_toolkit.document import Document

SLASH_ITEMS: tuple[tuple[str, str], ...] = (
    ("/help", "命令说明"),
    ("/resume", "浏览并切入会话"),
    ("/sessions", "同 /resume"),
    ("/new", "新开会话"),
    ("/rename", "给当前会话起名"),
    ("/snapshot", "把改过的文件打成命名档"),
    ("/restore", "撤回文件（不改对话）"),
    ("/last", "展开上一条工具输出"),
    ("/compact", "强制压缩当前对话"),
    ("/tools", "列出内置工具"),
    ("/status", "模型 / 窗口 / 显示模式"),
    ("/memory", "读或清空项目记忆"),
    ("/skills", "技能名单 / 启停 / 加载"),
    ("/mcp", "MCP server 状态"),
    ("/exit", "退出"),
    ("/quit", "退出"),
)


class XcodeCompleter(Completer):
    """`/` 前缀走命令表；最后一个 `@token` 走路径。"""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self._paths = PathCompleter(
            expanduser=True,
            only_directories=False,
            get_paths=lambda: [str(self.workspace)],
        )

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        at = text.rfind("@")
        slash = text.rfind("/")
        space_after_at = at >= 0 and any(ch.isspace() for ch in text[at + 1 :])
        if at >= 0 and not space_after_at and at >= slash:
            yield from self._path_completions(text[at + 1 :], complete_event)
            return
        stripped = text.lstrip()
        if stripped.startswith("/"):
            yield from self._slash_completions(stripped)
            return

    def _slash_completions(self, stripped: str) -> Iterable[Completion]:
        token = stripped.split()[0]
        needle = token.lower()
        for name, desc in SLASH_ITEMS:
            if name.startswith(needle):
                yield Completion(
                    name,
                    start_position=-len(token),
                    display=name,
                    display_meta=desc,
                )

    def _path_completions(
        self, prefix: str, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        sub = Document(prefix, cursor_position=len(prefix))
        yield from self._paths.get_completions(sub, complete_event)
