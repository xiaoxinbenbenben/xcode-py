"""#8 安全策略：requires_approval 审批流与审计 JSONL 落盘。"""

from __future__ import annotations

import asyncio
import json

from xcode.audit import audit_path
from xcode.runtime.agent import _iter_tool_executions
from xcode.runtime.session import SessionStore
from xcode.tools.base import Tool, ToolContext, ToolRegistry, ToolResult


class _DangerTool(Tool):
    """requires_approval=True 的假工具，记录调用次数。"""

    name = "danger"
    description = "fake dangerous tool"
    parameters = {"type": "object", "properties": {}}
    requires_approval = True

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult("did it")


class _SafeTool(Tool):
    """普通工具：不应触发审批询问。"""

    name = "safe"
    description = "fake safe tool"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult("safe done")


async def _run_tool(
    tool: Tool,
    tool_ctx: ToolContext,
    session,
    *,
    call_name: str | None = None,
) -> list[dict]:
    registry = ToolRegistry([tool])
    call = {
        "id": "call_1",
        "function": {"name": call_name or tool.name, "arguments": "{}"},
    }
    return [
        event
        async for event in _iter_tool_executions(
            ordered_calls=[call],
            registry=registry,
            tool_ctx=tool_ctx,
            session=session,
        )
    ]


def _collect(tool, tool_ctx, session, **kwargs) -> list[dict]:
    return asyncio.run(_run_tool(tool, tool_ctx, session, **kwargs))


def _session_and_ctx(tmp_path):
    workspace = tmp_path / "ws"
    data_home = tmp_path / "home"
    store = SessionStore(data_home)
    session = store.create(workspace)
    ctx = ToolContext(workspace=workspace, data_home=data_home)
    return session, ctx


def _audit_lines(data_home):
    path = audit_path(data_home)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_denied_without_callback(tmp_path):
    """无审批回调（-p 模式）→ 高危工具直接拒绝、不执行、审计 approved=False。"""
    session, ctx = _session_and_ctx(tmp_path)
    danger = _DangerTool()
    events = _collect(danger, ctx, session)

    result = events[-1]
    assert result["type"] == "tool_result"
    assert result["is_error"] is True
    assert result["result"] == "permission denied: danger"
    assert danger.calls == 0
    assert session.messages[-1]["content"] == "permission denied: danger"

    entry = _audit_lines(ctx.data_home)[-1]
    assert entry["tool"] == "danger"
    assert entry["approved"] is False
    assert entry["is_error"] is True
    assert entry["session_id"] == session.session_id


def test_denied_when_user_says_no(tmp_path):
    """回调返回 False → 拒绝。"""
    session, ctx = _session_and_ctx(tmp_path)
    ctx.ask_permission = _deny
    danger = _DangerTool()
    events = _collect(danger, ctx, session)

    assert events[-1]["is_error"] is True
    assert danger.calls == 0
    assert _audit_lines(ctx.data_home)[-1]["approved"] is False


def test_allowed_when_user_says_yes(tmp_path):
    """回调返回 True → 放行执行。"""
    session, ctx = _session_and_ctx(tmp_path)
    ctx.ask_permission = _allow
    danger = _DangerTool()
    events = _collect(danger, ctx, session)

    assert events[-1]["is_error"] is False
    assert events[-1]["result"] == "did it"
    assert danger.calls == 1
    entry = _audit_lines(ctx.data_home)[-1]
    assert entry["approved"] is True
    assert entry["is_error"] is False


def test_safe_tool_never_asks(tmp_path):
    """普通工具不触发审批，且照常审计。"""
    session, ctx = _session_and_ctx(tmp_path)

    async def _must_not_ask(text: str) -> bool:
        raise AssertionError("safe tool must not ask for permission")

    ctx.ask_permission = _must_not_ask
    safe = _SafeTool()
    events = _collect(safe, ctx, session)

    assert events[-1]["is_error"] is False
    assert events[-1]["result"] == "safe done"
    entry = _audit_lines(ctx.data_home)[-1]
    assert entry["tool"] == "safe"
    assert entry["approved"] is True


def test_audit_skipped_without_data_home(tmp_path):
    """ToolContext 未注入 data_home → 不写审计（不报错）。"""
    workspace = tmp_path / "ws"
    store = SessionStore(tmp_path / "home")
    session = store.create(workspace)
    ctx = ToolContext(workspace=workspace)  # 无 data_home

    danger = _DangerTool()
    events = _collect(danger, ctx, session)

    assert events[-1]["is_error"] is True  # 仍走拒绝
    assert _audit_lines(tmp_path / "home") == []


async def _allow(_: str) -> bool:
    return True


async def _deny(_: str) -> bool:
    return False
