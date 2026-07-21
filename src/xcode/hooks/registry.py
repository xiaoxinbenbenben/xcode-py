"""Hooks 注册表 MVP：在关键生命周期点触发回调。"""

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum
from typing import Any, Callable


class HookEvent(StrEnum):
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    RUN_FINISHED = "run_finished"


HookFn = Callable[[dict[str, Any]], None]


class HookRegistry:
    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[HookFn]] = defaultdict(list)

    def on(self, event: HookEvent, fn: HookFn) -> None:
        self._hooks[event].append(fn)

    def run(self, event: HookEvent, payload: dict[str, Any]) -> None:
        for fn in self._hooks.get(event, []):
            fn(payload)
