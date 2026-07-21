"""Agent loop：openai chat completions + tool calling，流式产出 runtime events。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from openai import AsyncOpenAI

from xcode.config import Config
from xcode.context.builder import build_context_bundle
from xcode.context.compaction import compact_messages, should_auto_compact
from xcode.hooks.registry import HookEvent, HookRegistry
from xcode.permissions.engine import PermissionEngine
from xcode.runtime.events import EventBuilder, new_run_id
from xcode.runtime.session import SessionRuntime, SessionStore
from xcode.runtime.tracing import TraceLogger
from xcode.skills.loader import SkillTool, load_skills, skill_roots
from xcode.tasks.store import task_tools
from xcode.tools.base import ToolContext
from xcode.tools.builtins import builtin_tools
from xcode.tools.registry import ToolRegistry


def build_registry(workspace, *, package_skills=None) -> ToolRegistry:
    skills = load_skills(skill_roots(workspace, package_skills))
    tools = [*builtin_tools(), *task_tools(), SkillTool(skills)]
    return ToolRegistry(tools)


async def run_agent(
    user_input: str,
    *,
    config: Config,
    session: SessionRuntime,
    store: SessionStore,
    permission: PermissionEngine | None = None,
    hooks: HookRegistry | None = None,
    ask_permission: Callable[[str, dict[str, Any]], bool] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """执行一轮用户请求，异步产出结构化事件。"""
    permission = permission or PermissionEngine(auto_allow=True)
    hooks = hooks or HookRegistry()
    if ask_permission is not None:
        permission.ask = ask_permission
        permission.auto_allow = False

    run_id = new_run_id()
    events = EventBuilder(run_id=run_id, session_id=session.session_id)
    trace = TraceLogger(
        session.data_dir / "traces" / f"{run_id}.jsonl",
        enabled=config.trace_enabled,
    )

    def emit(event: dict[str, Any]):
        trace.log(event)
        return event

    yield emit(events.build("run_started", {"user_input": user_input, "model": config.model}))

    hooks.run(HookEvent.USER_PROMPT_SUBMIT, {"user_input": user_input, "session_id": session.session_id})
    session.update_name_from_user_input(user_input)

    registry = build_registry(session.workspace_root)
    memory_dir = store.memory_dir(session.workspace_root)
    bundle = build_context_bundle(
        user_input=user_input,
        session=session,
        tool_names=registry.list_names(),
        memory_dir=memory_dir,
    )
    yield emit(
        events.build(
            "context_built",
            {
                "mentioned_files": bundle.mention.mentioned_files,
                "history_items": len(bundle.history),
            },
        )
    )

    if should_auto_compact(session.messages, threshold_turns=config.auto_compact_turns):
        session.messages, session.summary = compact_messages(
            session.messages, existing_summary=session.summary
        )
        yield emit(events.build("compacted", {"reason": "auto", "kept": len(session.messages)}))

    # 本轮用户消息写入会话
    session.messages.append({"role": "user", "content": bundle.mention.cleaned_input})

    client = AsyncOpenAI(api_key=config.api_key or "missing", base_url=config.base_url)
    tool_ctx = ToolContext(
        workspace=session.workspace_root,
        session_data_dir=session.data_dir,
        todos=session.todos,
        max_output_chars=config.max_tool_output_chars,
        memory_dir=memory_dir,
        ask_permission=lambda name, params: permission.check(name, params),
    )

    max_rounds = 24
    try:
        for _ in range(max_rounds):
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": bundle.system},
                *session.messages,
            ]
            stream = await client.chat.completions.create(
                model=config.model,
                messages=messages,
                tools=registry.openai_tools(),
                stream=True,
            )

            assistant_text = ""
            tool_calls_acc: dict[int, dict[str, Any]] = {}
            finish_reason = None

            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                if choice is None:
                    continue
                delta = choice.delta
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                if delta and delta.content:
                    assistant_text += delta.content
                    yield emit(events.build("text_delta", {"text": delta.content}))
                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        slot = tool_calls_acc.setdefault(
                            idx,
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                slot["function"]["name"] = tc.function.name
                            if tc.function.arguments:
                                slot["function"]["arguments"] += tc.function.arguments

            if tool_calls_acc:
                ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
                session.messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_text or None,
                        "tool_calls": ordered,
                    }
                )
                need_compact = False
                for tc in ordered:
                    name = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"] or "{}"
                    try:
                        args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        args = {}
                    yield emit(events.build("tool_call", {"name": name, "arguments": args}))
                    hooks.run(HookEvent.BEFORE_TOOL, {"name": name, "arguments": args})
                    tool = registry.get(name)
                    if tool is None:
                        result_obj = {
                            "ok": False,
                            "summary": f"unknown tool: {name}",
                            "content": "",
                            "truncated": False,
                            "data": {},
                        }
                        content = json.dumps(result_obj, ensure_ascii=False)
                    else:
                        result = tool.execute(args, tool_ctx)
                        content = result.to_message_content(max_chars=config.max_tool_output_chars)
                        if result.data.get("compact"):
                            need_compact = True
                        yield emit(
                            events.build(
                                "tool_result",
                                {
                                    "name": name,
                                    "ok": result.ok,
                                    "summary": result.summary,
                                    "truncated": result.truncated,
                                },
                            )
                        )
                        hooks.run(
                            HookEvent.AFTER_TOOL,
                            {"name": name, "ok": result.ok, "summary": result.summary},
                        )
                    session.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": content,
                        }
                    )
                if need_compact:
                    session.messages, session.summary = compact_messages(
                        session.messages, existing_summary=session.summary
                    )
                    yield emit(events.build("compacted", {"reason": "tool"}))
                continue

            # 无 tool_calls：普通回复结束
            if assistant_text:
                session.messages.append({"role": "assistant", "content": assistant_text})
            yield emit(
                events.build(
                    "run_finished",
                    {"finish_reason": finish_reason or "stop", "text": assistant_text},
                )
            )
            break
        else:
            yield emit(events.build("run_finished", {"finish_reason": "max_rounds", "text": ""}))
    except Exception as exc:  # noqa: BLE001 — 边界处转为事件，避免打崩 TUI
        yield emit(events.build("error", {"message": str(exc)}))
        yield emit(events.build("run_finished", {"finish_reason": "error", "text": ""}))
    finally:
        session.touch()
        session.save()
        hooks.run(HookEvent.RUN_FINISHED, {"session_id": session.session_id, "run_id": run_id})
