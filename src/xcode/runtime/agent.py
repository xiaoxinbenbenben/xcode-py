"""ReAct 主循环：调 LLM、向外 yield 扁平产品事件。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from xcode.config import Config
from xcode.context.builder import build_context_bundle
from xcode.runtime.events import (
    DONE,
    ERROR,
    TEXT_DELTA,
    THINKING_DELTA,
    TOOL_CALL,
    TOOL_RESULT,
    TURN_COMPLETE,
    USAGE,
    make_event,
    map_finish_reason,
)
from xcode.runtime.session import SessionRuntime, SessionStore
from xcode.tools.base import ToolContext
from xcode.tools.builtins import builtin_tools
from xcode.tools.registry import ToolRegistry


def build_registry(workspace=None, *, package_skills=None) -> ToolRegistry:
    """组装工具表（todo #7 回填中）。"""
    _ = workspace, package_skills
    return ToolRegistry(builtin_tools())


def _usage_from_chunk(chunk: Any) -> dict[str, int] | None:
    """从流式 chunk 抽出 token 用量；没有则返回 None。"""
    usage = getattr(chunk, "usage", None)
    if usage is None:
        return None
    return {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }


def _merge_tool_delta(acc: dict[int, dict[str, Any]], tc: Any) -> None:
    """把一片 tool_calls delta 累加进 acc（按 index）。副作用：改 acc。"""
    idx = int(getattr(tc, "index", 0) or 0)
    slot = acc.setdefault(
        idx,
        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
    )
    if getattr(tc, "id", None):
        slot["id"] = tc.id
    fn = getattr(tc, "function", None)
    if fn is None:
        return
    if getattr(fn, "name", None):
        slot["function"]["name"] = fn.name
    if getattr(fn, "arguments", None):
        slot["function"]["arguments"] += fn.arguments


def _parse_tool_input(raw: str) -> dict[str, Any]:
    """把工具 arguments 字符串收成 dict，坏 JSON 时兜底。"""
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


async def _iter_tool_executions(
    *,
    ordered_calls: list[dict[str, Any]],
    registry: ToolRegistry,
    tool_ctx: ToolContext,
    session: SessionRuntime,
) -> AsyncIterator[dict[str, Any]]:
    """执行本轮 tool_calls。

    输出：依次 yield `tool_call` / `tool_result`；副作用：追加纯文本 tool 消息。
    """
    for tc in ordered_calls:
        name = tc["function"]["name"]
        args = _parse_tool_input(tc["function"]["arguments"])
        yield make_event(TOOL_CALL, name=name, input=args)

        tool = registry.get(name)
        if tool is None:
            content = f"unknown tool: {name}"
            is_error = True
        else:
            result = await tool.execute(args, tool_ctx)
            content = result.text
            is_error = result.is_error

        yield make_event(TOOL_RESULT, name=name, result=content, is_error=is_error)
        session.messages.append(
            {"role": "tool", "tool_call_id": tc["id"], "content": content}
        )


async def run_agent(
    user_input: str,
    *,
    config: Config,
    session: SessionRuntime,
    store: SessionStore,
    client: Any | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """跑完一次用户请求的 ReAct，并流式产出产品事件。

    输入：用户文本 + 会话/配置；可选注入 `client`（测试用）。
    输出：异步迭代扁平事件；副作用：改写并保存 session。
    """
    _ = store
    # --- 1) 组装上下文与工具，写入本轮 user 消息 ---
    session.update_name_from_user_input(user_input)
    registry = build_registry(session.workspace_root)
    bundle = build_context_bundle(
        user_input=user_input,
        session=session,
        tool_names=registry.list_names(),
        model=config.model,
    )
    session.messages.append({"role": "user", "content": bundle.user_text})

    llm = client or AsyncOpenAI(api_key=config.api_key or "missing", base_url=config.base_url)
    tool_ctx = ToolContext(workspace=session.workspace_root)

    # --- 2) ReAct 多轮：LLM ↔ 工具 ---
    max_rounds = 24
    turn = 0
    total_tokens = 0
    try:
        for _ in range(max_rounds):
            turn += 1
            create_kwargs: dict[str, Any] = {
                "model": config.model,
                "messages": [{"role": "system", "content": bundle.system}, *session.messages],
                "stream": True,
            }
            openai_tools = registry.openai_tools()
            if openai_tools:
                create_kwargs["tools"] = openai_tools

            stream = await llm.chat.completions.create(**create_kwargs)


            assistant_text = ""
            tool_calls_acc: dict[int, dict[str, Any]] = {}
            finish_reason: str | None = None
            turn_tokens = 0

            # --- 2a) 收流：usage / thinking / text；tool 分片只累计 ---
            async for chunk in stream:
                usage = _usage_from_chunk(chunk)
                if usage is not None:
                    turn_tokens += usage["input_tokens"] + usage["output_tokens"]
                    yield make_event(USAGE, usage=usage)

                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason

                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                reasoning = getattr(delta, "reasoning_content", None)
                if isinstance(reasoning, str) and reasoning:
                    yield make_event(THINKING_DELTA, thinking=reasoning)

                content = getattr(delta, "content", None)
                if isinstance(content, str) and content:
                    assistant_text += content
                    yield make_event(TEXT_DELTA, text=content)

                for tc in getattr(delta, "tool_calls", None) or []:
                    _merge_tool_delta(tool_calls_acc, tc)

            total_tokens += turn_tokens
            stop_reason = map_finish_reason(finish_reason)
            if tool_calls_acc and stop_reason == "end_turn":
                stop_reason = "tool_use"
            yield make_event(TURN_COMPLETE, turn=turn, stop_reason=stop_reason)

            # --- 2b) 有工具：执行后 continue 再问模型 ---
            if tool_calls_acc:
                ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
                session.messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_text or None,
                        "tool_calls": ordered,
                    }
                )
                async for event in _iter_tool_executions(
                    ordered_calls=ordered,
                    registry=registry,
                    tool_ctx=tool_ctx,
                    session=session,
                ):
                    yield event
                continue

            # --- 2c) 无工具：收工 ---
            if assistant_text:
                session.messages.append({"role": "assistant", "content": assistant_text})
            yield make_event(DONE, total_turns=turn, total_tokens=total_tokens)
            break
        else:
            yield make_event(DONE, total_turns=turn, total_tokens=total_tokens)
    except Exception as exc:  # noqa: BLE001 — 边界转事件，避免打崩 TUI
        yield make_event(ERROR, error=str(exc))
        yield make_event(DONE, total_turns=turn, total_tokens=total_tokens)
    finally:
        # --- 3) 落盘会话 ---
        session.touch()
        session.save()
