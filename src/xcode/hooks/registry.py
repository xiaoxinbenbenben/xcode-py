"""Hooks 注册表与默认生命周期钩子。"""

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


def build_default_hooks(*, trace_log: Callable[[dict[str, Any]], None] | None = None) -> HookRegistry:
    """构建默认 hooks：在关键生命周期点写入可选 trace 回调。"""
    registry = HookRegistry()
    if trace_log is None:
        return registry

    def _wrap(event: HookEvent) -> HookFn:
        def _fn(payload: dict[str, Any]) -> None:
            trace_log({"hook": event.value, **payload})

        return _fn

    for ev in HookEvent:
        registry.on(ev, _wrap(ev))
    return registry
