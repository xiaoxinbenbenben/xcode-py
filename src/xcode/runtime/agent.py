"""ReAct 主循环：调 LLM、执行工具、向外 yield 扁平产品事件。

## 一轮用户请求在这里怎么走
1. 用 build_context_bundle 拼 system（含 XCODE、memory_summary 等）
2. ``session.append_message(user)`` — 立刻写入 transcript
3. 循环最多 max_rounds 次：
   a. **ensure_context_budget**：估算 system+tools+messages+reserve，超阈则 light_model 摘要 + apply_compact
   b. 流式调用 chat.completions（messages = system + session.messages）
   c. 若模型要 tool：append assistant(tool_calls) → 执行工具 → 每个结果 append tool 消息 → continue
   d. 若纯文本：append assistant → DONE
4. finally：write_context 落盘送模缓存

## 与会话模块的约定
- 所有对话消息只通过 session.append_message，禁止直接改 messages 列表（tool 循环内也一样）
- compact 可在「用户轮第一次送模前」和「每个 tool 后再送模前」触发，避免单轮多 tool 撑爆窗口
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from openai import AsyncOpenAI

from xcode.audit import append_audit
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
from xcode.runtime.tokens import count_messages_tokens, count_text_tokens
from xcode.tools.base import ToolContext
from xcode.tools.builtins import builtin_tools
from xcode.tools.registry import ToolRegistry

_COMPACT_SYSTEM = """你是会话交接摘要器。根据对话历史写一份简洁 handoff summary，供后续 agent 继续工作。
要求：
- 用中文（用户主要用中文时）或与用户一致的语言
- 保留：目标、已做决策、改过的文件/路径、未完成项、约束与坑
- 不要复述大段代码或工具原文
- 控制在 400～800 字或等价信息量
只输出摘要正文，不要前言。"""


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


async def _approved(tool_ctx: ToolContext, name: str, args: dict[str, Any]) -> bool:
    """高危工具执行前的审批；**只被 requires_approval=True 的工具走到**。"""
    if tool_ctx.ask_permission is None:
        return False
    preview = json.dumps(args, ensure_ascii=False)[:200]
    return await tool_ctx.ask_permission(f"允许工具 {name} 执行？参数：{preview}")


def _audit(
    tool_ctx: ToolContext,
    session: SessionRuntime,
    *,
    name: str,
    args: dict[str, Any],
    approved: bool,
    is_error: bool,
) -> None:
    """工具执行落审计；data_home 未注入时跳过。"""
    if tool_ctx.data_home is None:
        return
    append_audit(
        tool_ctx.data_home,
        session_id=session.session_id,
        tool=name,
        args=args,
        approved=approved,
        is_error=is_error,
    )


def estimate_fixed_overhead(
    *,
    system: str,
    tools: list[dict[str, Any]] | None,
    model: str,
) -> int:
    """估算「不在 session.messages 里、但每次请求都会带上」的 token。

    包括 system 全文（XCODE、memory_summary 等）和 tools JSON schema。
    若只估 messages，会严重低估，导致该 compact 时不 compact。
    """
    total = count_text_tokens(system or "", model=model) + 8
    if tools:
        total += count_text_tokens(json.dumps(tools, ensure_ascii=False), model=model)
    return total


async def generate_compact_summary(
    client: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
) -> str:
    """调用 compact 模型，把当前窗口压成交接摘要字符串。

    会把 tool 消息展平成可读文本，并再次截断超长字段，避免「为了 compact 反而把输入撑爆」。
    输出只应是摘要正文，由 apply_compact 包进 <compact_summary>。
    """
    # 送摘要模型时再截一层，避免爆输入
    payload: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role not in {"user", "assistant", "tool", "system"}:
            continue
        item: dict[str, Any] = {"role": role if role != "tool" else "user"}
        content = msg.get("content")
        if role == "tool":
            item["content"] = f"[tool {msg.get('tool_call_id', '')}] {content}"
        elif role == "assistant" and msg.get("tool_calls"):
            item["content"] = (content or "") + "\n" + json.dumps(
                msg.get("tool_calls"), ensure_ascii=False
            )
        else:
            item["content"] = content if content is not None else ""
        if isinstance(item["content"], str) and len(item["content"]) > 4000:
            item["content"] = item["content"][:4000] + "…"
        payload.append(item)

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _COMPACT_SYSTEM},
            *payload,
            {"role": "user", "content": "请输出交接摘要。"},
        ],
        max_tokens=1200,
        stream=False,
    )
    choice = (resp.choices or [None])[0]
    if choice is None:
        return "(empty compact summary)"
    text = getattr(getattr(choice, "message", None), "content", None) or ""
    text = str(text).strip()
    return text or "(empty compact summary)"


async def ensure_context_budget(
    session: SessionRuntime,
    *,
    config: Config,
    client: Any,
    system: str,
    openai_tools: list[dict[str, Any]] | None,
) -> bool:
    """送模前预算闸门：超阈则生成摘要并 apply_compact。

    返回 True 表示刚 compact 过（调用方可打日志/事件）。
    失败会向上抛，由 run_agent 外层变成 ERROR 事件。
    """
    overhead = estimate_fixed_overhead(
        system=system, tools=openai_tools, model=config.model
    )
    if not session.needs_compact(
        overhead_tokens=overhead,
        context_window=config.context_window,
        compact_threshold=config.compact_threshold,
        reserved_output_tokens=config.reserved_output_tokens,
        model=config.model,
    ):
        return False
    summary = await generate_compact_summary(
        client,
        model=config.light_model or config.model,
        messages=session.messages,
    )
    session.apply_compact(summary, model=config.model)
    return True


async def run_compact(
    session: SessionRuntime,
    *,
    config: Config,
    client: Any | None = None,
) -> str:
    """TUI ``/compact``：无视阈值，强制摘要一次并立即 write_context。

    与 auto compact 共用 generate_compact_summary + apply_compact。
    """
    llm = client or AsyncOpenAI(api_key=config.api_key or "missing", base_url=config.base_url)
    summary = await generate_compact_summary(
        llm,
        model=config.light_model or config.model,
        messages=session.messages,
    )
    session.apply_compact(summary, model=config.model)
    session.write_context(model=config.model, fsync_transcript=True)
    return summary


async def _iter_tool_executions(
    *,
    ordered_calls: list[dict[str, Any]],
    registry: ToolRegistry,
    tool_ctx: ToolContext,
    session: SessionRuntime,
) -> AsyncIterator[dict[str, Any]]:
    """执行本轮 tool_calls；结果经 append_message 落盘。"""
    for tc in ordered_calls:
        name = tc["function"]["name"]
        args = _parse_tool_input(tc["function"]["arguments"])
        yield make_event(TOOL_CALL, name=name, input=args)

        tool = registry.get(name)
        if tool is None:
            content = f"unknown tool: {name}"
            is_error = True
        else:
            approved = not tool.requires_approval or await _approved(tool_ctx, name, args)
            if approved:
                result = await tool.execute(args, tool_ctx)
                content = result.text
                is_error = result.is_error
            else:
                content = f"permission denied: {name}"
                is_error = True
            _audit(
                tool_ctx,
                session,
                name=name,
                args=args,
                approved=approved,
                is_error=is_error,
            )

        yield make_event(TOOL_RESULT, name=name, result=content, is_error=is_error)
        session.append_message(
            {"role": "tool", "tool_call_id": tc["id"], "content": content}
        )


async def run_agent(
    user_input: str,
    *,
    config: Config,
    session: SessionRuntime,
    store: SessionStore,
    client: Any | None = None,
    ask_permission: Callable[[str], Awaitable[bool]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """跑完一次用户请求的 ReAct，并流式产出产品事件。"""
    _ = store
    session.tool_prune_chars = config.tool_prune_chars
    session.transcript_hard_cap = config.transcript_hard_cap
    session.update_name_from_user_input(user_input)
    registry = build_registry(session.workspace_root)
    llm = client or AsyncOpenAI(api_key=config.api_key or "missing", base_url=config.base_url)
    bundle = build_context_bundle(
        user_input=user_input,
        session=session,
        tool_names=registry.list_names(),
        model=config.model,
        data_home=config.data_home,
    )
    session.append_message({"role": "user", "content": bundle.user_text})

    tool_ctx = ToolContext(
        workspace=session.workspace_root,
        data_home=config.data_home,
        ask_permission=ask_permission,
    )
    openai_tools = registry.openai_tools() or None

    max_rounds = 24
    turn = 0
    total_tokens = 0
    try:
        for _ in range(max_rounds):
            turn += 1
            # 即将送模前：预算检查（用户轮初 / 每个 tool 后）
            compacted = await ensure_context_budget(
                session,
                config=config,
                client=llm,
                system=bundle.system,
                openai_tools=openai_tools,
            )
            if compacted:
                yield make_event(
                    TURN_COMPLETE,
                    turn=turn,
                    stop_reason="compacted",
                )

            create_kwargs: dict[str, Any] = {
                "model": config.model,
                "messages": [{"role": "system", "content": bundle.system}, *session.messages],
                "stream": True,
            }
            if openai_tools:
                create_kwargs["tools"] = openai_tools

            stream = await llm.chat.completions.create(**create_kwargs)

            assistant_text = ""
            tool_calls_acc: dict[int, dict[str, Any]] = {}
            finish_reason: str | None = None
            turn_tokens = 0

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

            if tool_calls_acc:
                ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
                session.append_message(
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

            if assistant_text:
                session.append_message({"role": "assistant", "content": assistant_text})
            yield make_event(DONE, total_turns=turn, total_tokens=total_tokens)
            break
        else:
            yield make_event(DONE, total_turns=turn, total_tokens=total_tokens)
    except Exception as exc:  # noqa: BLE001
        yield make_event(ERROR, error=str(exc))
        yield make_event(DONE, total_turns=turn, total_tokens=total_tokens)
    finally:
        session.touch()
        session.write_context(model=config.model, fsync_transcript=True)
        # 刷新展示用 token 数
        session.estimated_tokens = count_messages_tokens(
            session.messages, model=config.model
        )
