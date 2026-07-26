"""ReAct 主循环：调 LLM、跑工具、向外 yield 扁平产品事件。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

from openai import AsyncOpenAI

from xcode.config import Config
from xcode.context.builder import build_context_bundle
from xcode.context.compaction import compact_messages, should_auto_compact
from xcode.hooks.registry import HookEvent, HookRegistry, build_default_hooks
from xcode.permissions.engine import PermissionEngine
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
    new_run_id,
)
from xcode.runtime.session import SessionRuntime, SessionStore
from xcode.runtime.tracing import TraceLogger
from xcode.skills.loader import SkillTool, load_skills, skill_roots
from xcode.tasks.store import task_tools
from xcode.tools.base import FileSnapshot, ToolContext
from xcode.tools.builtins import builtin_tools
from xcode.tools.registry import ToolRegistry


def build_registry(workspace, *, package_skills=None) -> ToolRegistry:
    """组装当前工作区可用的工具表。"""
    skills = load_skills(skill_roots(workspace, package_skills))
    return ToolRegistry([*builtin_tools(), *task_tools(), SkillTool(skills)])


def _load_snapshots(raw: dict[str, dict[str, Any]]) -> dict[str, FileSnapshot]:
    return {k: FileSnapshot.from_dict(v) for k, v in raw.items()}


def _dump_snapshots(snaps: dict[str, FileSnapshot]) -> dict[str, dict[str, Any]]:
    return {k: v.as_dict() for k, v in snaps.items()}


def _emit(trace: TraceLogger, event: dict[str, Any]) -> dict[str, Any]:
    """事件先落 trace，再原样返回，供 `yield _emit(...)`。"""
    trace.log(event)
    return event


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


def _iter_tool_executions(
    *,
    ordered_calls: list[dict[str, Any]],
    registry: ToolRegistry,
    tool_ctx: ToolContext,
    hooks: HookRegistry,
    config: Config,
    session: SessionRuntime,
    trace: TraceLogger,
) -> Iterator[dict[str, Any]]:
    """执行本轮已拼好的 tool_calls。

    输入：完整 tool_calls 列表 + 运行上下文。
    输出：依次 yield `tool_call` / `tool_result` 产品事件。
    副作用：把 tool 结果追加进 `session.messages`；必要时压缩历史（只打 trace）。
    """
    need_compact = False
    for tc in ordered_calls:
        name = tc["function"]["name"]
        args = _parse_tool_input(tc["function"]["arguments"])
        yield _emit(trace, make_event(TOOL_CALL, name=name, input=args))
        hooks.run(HookEvent.BEFORE_TOOL, {"name": name, "arguments": args})

        tool = registry.get(name)
        if tool is None:
            result_text = f"unknown tool: {name}"
            content = json.dumps(
                {
                    "status": "error",
                    "text": result_text,
                    "error": {"code": "UNKNOWN_TOOL", "message": result_text},
                },
                ensure_ascii=False,
            )
            yield _emit(
                trace,
                make_event(TOOL_RESULT, name=name, result=result_text, is_error=True),
            )
        else:
            result = tool.execute(args, tool_ctx)
            content = result.to_message_content(max_chars=config.max_tool_output_chars)
            need_compact = need_compact or bool(result.data.get("compact"))
            yield _emit(
                trace,
                make_event(
                    TOOL_RESULT,
                    name=name,
                    result=result.text,
                    is_error=not result.ok,
                ),
            )
            hooks.run(
                HookEvent.AFTER_TOOL,
                {
                    "name": name,
                    "ok": result.ok,
                    "summary": result.summary,
                    "status": result.status,
                },
            )

        session.messages.append(
            {"role": "tool", "tool_call_id": tc["id"], "content": content}
        )

    if need_compact:
        session.messages, session.summary = compact_messages(
            session.messages, existing_summary=session.summary
        )
        trace.log({"type": "trace_compacted", "reason": "tool"})


async def run_agent(
    user_input: str,
    *,
    config: Config,
    session: SessionRuntime,
    store: SessionStore,
    permission: PermissionEngine | None = None,
    hooks: HookRegistry | None = None,
    ask_permission: Callable[[str, dict[str, Any]], bool] | None = None,
    client: Any | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """跑完一次用户请求的 ReAct，并流式产出产品事件。

    输入：用户文本 + 会话/配置；可选注入 `client`（测试用）。
    输出：异步迭代扁平事件；副作用：改写并保存 session（消息、快照）。
    """
    # --- 1) 准备：trace / 权限 / hooks ---
    run_id = new_run_id()
    trace = TraceLogger(
        session.data_dir / "traces" / f"{run_id}.jsonl",
        enabled=config.trace_enabled,
    )

    if permission is None:
        permission = PermissionEngine.from_settings(
            data_home=config.data_home,
            workspace=session.workspace_root,
            ask=ask_permission,
            auto_allow=ask_permission is None,
        )
    elif ask_permission is not None:
        permission.ask = ask_permission
        permission.auto_allow = False

    if hooks is None:
        hooks = build_default_hooks(
            trace_log=(lambda payload: trace.log({"type": "hook", "payload": payload}))
            if config.trace_enabled
            else None
        )

    hooks.run(HookEvent.USER_PROMPT_SUBMIT, {"user_input": user_input, "session_id": session.session_id})
    session.update_name_from_user_input(user_input)

    # --- 2) 组装上下文与工具，写入本轮 user 消息 ---
    registry = build_registry(session.workspace_root)
    memory_dir = store.memory_dir(session.workspace_root)
    bundle = build_context_bundle(
        user_input=user_input,
        session=session,
        tool_names=registry.list_names(),
        memory_dir=memory_dir,
    )

    if should_auto_compact(session.messages, threshold_turns=config.auto_compact_turns):
        session.messages, session.summary = compact_messages(
            session.messages, existing_summary=session.summary
        )
        trace.log({"type": "trace_compacted", "reason": "auto", "kept": len(session.messages)})

    session.messages.append({"role": "user", "content": bundle.mention.cleaned_input})

    llm = client or AsyncOpenAI(api_key=config.api_key or "missing", base_url=config.base_url)
    tool_ctx = ToolContext(
        workspace=session.workspace_root,
        session_data_dir=session.data_dir,
        todos=session.todos,
        max_output_chars=config.max_tool_output_chars,
        memory_dir=memory_dir,
        snapshots=_load_snapshots(session.snapshots),
        ask_permission=lambda name, params: permission.check(name, params),
    )

    # --- 3) ReAct 多轮：LLM ↔ 工具 ---
    max_rounds = 24
    turn = 0
    total_tokens = 0
    try:
        for _ in range(max_rounds):
            turn += 1
            stream = await llm.chat.completions.create(
                model=config.model,
                messages=[{"role": "system", "content": bundle.system}, *session.messages],
                tools=registry.openai_tools(),
                stream=True,
            )

            assistant_text = ""
            tool_calls_acc: dict[int, dict[str, Any]] = {}
            finish_reason: str | None = None
            turn_tokens = 0

            # --- 3a) 收流：usage / thinking / text；tool 分片只累计 ---
            async for chunk in stream:
                usage = _usage_from_chunk(chunk)
                if usage is not None:
                    turn_tokens += usage["input_tokens"] + usage["output_tokens"]
                    yield _emit(trace, make_event(USAGE, usage=usage))

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
                    yield _emit(trace, make_event(THINKING_DELTA, thinking=reasoning))

                content = getattr(delta, "content", None)
                if isinstance(content, str) and content:
                    assistant_text += content
                    yield _emit(trace, make_event(TEXT_DELTA, text=content))

                for tc in getattr(delta, "tool_calls", None) or []:
                    _merge_tool_delta(tool_calls_acc, tc)

            total_tokens += turn_tokens
            stop_reason = map_finish_reason(finish_reason)
            if tool_calls_acc and stop_reason == "end_turn":
                stop_reason = "tool_use"
            yield _emit(trace, make_event(TURN_COMPLETE, turn=turn, stop_reason=stop_reason))

            # --- 3b) 有工具：执行后 continue 再问模型 ---
            if tool_calls_acc:
                ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
                session.messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_text or None,
                        "tool_calls": ordered,
                    }
                )
                for event in _iter_tool_executions(
                    ordered_calls=ordered,
                    registry=registry,
                    tool_ctx=tool_ctx,
                    hooks=hooks,
                    config=config,
                    session=session,
                    trace=trace,
                ):
                    yield event
                continue

            # --- 3c) 无工具：收工 ---
            if assistant_text:
                session.messages.append({"role": "assistant", "content": assistant_text})
            yield _emit(trace, make_event(DONE, total_turns=turn, total_tokens=total_tokens))
            break
        else:
            # for 耗尽：达到 max_rounds
            yield _emit(trace, make_event(DONE, total_turns=turn, total_tokens=total_tokens))
    except Exception as exc:  # noqa: BLE001 — 边界转事件，避免打崩 TUI
        yield _emit(trace, make_event(ERROR, error=str(exc)))
        yield _emit(trace, make_event(DONE, total_turns=turn, total_tokens=total_tokens))
    finally:
        # --- 4) 落盘会话与快照 ---
        session.snapshots = _dump_snapshots(tool_ctx.snapshots)
        session.touch()
        session.save()
        hooks.run(HookEvent.RUN_FINISHED, {"session_id": session.session_id, "run_id": run_id})
