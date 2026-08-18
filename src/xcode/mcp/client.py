"""长连接 MCP Client：并行连、隔离失败、虚工具、调用时抢一轮重连。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from xcode.mcp.config import CONNECT_TIMEOUT, McpServerSpec, load_mcp_server_specs
from xcode.tools.base import Tool, ToolContext, ToolResult

Connector = Callable[[McpServerSpec], Awaitable["McpHandle"]]


class McpHandle(Protocol):
    """一条活着的 MCP session：list/call/resources + 关闭。"""

    async def list_tools(self) -> list[Any]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...
    async def list_resources(self) -> list[str]: ...
    async def read_resource(self, uri: str) -> ToolResult: ...
    async def aclose(self) -> None: ...


@dataclass
class _ServerState:
    """单个 server 的运行态：connecting / connected / failed / disconnected。"""

    spec: McpServerSpec
    status: str
    error: str | None = None
    tool_count: int = 0
    handle: McpHandle | None = None
    remotes: list[Any] = field(default_factory=list)


class McpProxyTool(Tool):
    """把一次 MCP RPC 打扮成普通 Tool。"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        *,
        requires_approval: bool,
        handler: Callable[[dict[str, Any]], Awaitable[ToolResult]],
    ) -> None:
        """登记名字、schema、是否要审批，以及真正打 RPC 的 handler。"""
        self.name = name
        self.description = description
        self.parameters = parameters
        self.requires_approval = requires_approval
        self._handler = handler

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """走 registry 同一条路；忽略 ctx，RPC 由 manager 持有的 session 发出。"""
        _ = ctx
        return await self._handler(args)


class McpClientManager:
    """读配置、并行连 server、暴露 tools() 给 registry。"""

    def __init__(
        self,
        *,
        workspace: Path,
        data_home: Path,
        connector: Connector | None = None,
    ) -> None:
        """workspace / data_home 用来读配置；connector 可注入（单测），默认走 SDK。"""
        self.workspace = workspace.resolve()
        self.data_home = data_home
        self._connector = connector or self._default_connect
        self._states: dict[str, _ServerState] = {}
        self._tools: list[Tool] = []

    def tools(self) -> list[Tool]:
        """当前已连上的真工具 + 虚工具快照，给 build_registry 用。"""
        return list(self._tools)

    def status_text(self) -> str:
        """`/mcp` 只读列表。"""
        if not self._states:
            return "(no mcp servers)"
        lines: list[str] = []
        for name, state in self._states.items():
            extra = f"  {state.tool_count} tools" if state.status == "connected" else ""
            err = f"  {state.error}" if state.error else ""
            lines.append(f"{name}  {state.status}{extra}{err}")
        return "\n".join(lines)

    async def start(self) -> None:
        """读配置，并行 initialize；失败隔离，成功的才进 tools()。"""
        specs = load_mcp_server_specs(workspace=self.workspace, data_home=self.data_home)
        for spec in specs.values():
            self._states[spec.name] = _ServerState(spec=spec, status="connecting")
        await asyncio.gather(*(self._connect_one(spec) for spec in specs.values()))

    async def aclose(self) -> None:
        """关掉所有子进程 / HTTP session；出错吞掉，避免挡 TUI 退出。"""
        handles = [s.handle for s in self._states.values() if s.handle is not None]
        for handle in handles:
            try:
                await handle.aclose()
            except Exception:
                pass
            for state in self._states.values():
                if state.handle is handle:
                    state.handle = None

    async def _connect_one(self, spec: McpServerSpec) -> None:
        """连一台 server：成功则挂工具；异常记 failed，不进表。"""
        try:
            handle = await asyncio.wait_for(self._connector(spec), timeout=CONNECT_TIMEOUT)
            remotes = await handle.list_tools()
        except Exception as exc:
            self._states[spec.name] = _ServerState(
                spec=spec, status="failed", error=str(exc)
            )
            return
        wrapped = self._wrap_tools(spec, remotes)
        self._states[spec.name] = _ServerState(
            spec=spec,
            status="connected",
            handle=handle,
            remotes=list(remotes),
            tool_count=len(wrapped),
        )
        self._tools.extend(wrapped)

    def _wrap_tools(self, spec: McpServerSpec, remotes: list[Any]) -> list[Tool]:
        """server 的真工具 + list_resources / read_resource 虚工具，统一 mcp__ 前缀。"""
        tools: list[Tool] = []
        for remote in remotes:
            remote_name = str(getattr(remote, "name", ""))
            tools.append(
                McpProxyTool(
                    mcp_tool_name(spec.name, remote_name),
                    getattr(remote, "description", None) or f"MCP tool {remote_name}",
                    _remote_input_schema(remote),
                    requires_approval=not _read_only_hint(remote),
                    handler=self._tool_handler(spec.name, remote_name),
                )
            )
        tools.append(
            McpProxyTool(
                mcp_tool_name(spec.name, "list_resources"),
                f"List MCP resources from {spec.name}.",
                {"type": "object", "properties": {}},
                requires_approval=False,
                handler=self._list_resources_handler(spec.name),
            )
        )
        tools.append(
            McpProxyTool(
                mcp_tool_name(spec.name, "read_resource"),
                f"Read an MCP resource from {spec.name}.",
                {
                    "type": "object",
                    "properties": {
                        "uri": {"type": "string", "description": "Resource URI"},
                    },
                    "required": ["uri"],
                },
                requires_approval=False,
                handler=self._read_resource_handler(spec.name),
            )
        )
        return tools

    def _tool_handler(
        self, server: str, remote_name: str
    ) -> Callable[[dict[str, Any]], Awaitable[ToolResult]]:
        """闭包：把模型参数转成该 server 上的 tools/call。"""

        async def handler(args: dict[str, Any]) -> ToolResult:
            """execute 入口：转发到 tools/call。"""
            return await self._invoke(server, lambda h: h.call_tool(remote_name, args))

        return handler

    def _list_resources_handler(
        self, server: str
    ) -> Callable[[dict[str, Any]], Awaitable[ToolResult]]:
        """虚工具：内部打 resources/list，拼成纯文本。"""

        async def handler(args: dict[str, Any]) -> ToolResult:
            """execute 入口：不读 args，列出该 server 的 resources。"""
            _ = args

            async def op(handle: McpHandle) -> ToolResult:
                """一次 resources/list；server 未实现则当空，不拆连接。"""
                try:
                    items = await handle.list_resources()
                except Exception as exc:
                    if _is_method_missing(exc):
                        items = []
                    else:
                        raise
                return ToolResult("\n".join(items) or "(no resources)")

            return await self._invoke(server, op)

        return handler

    def _read_resource_handler(
        self, server: str
    ) -> Callable[[dict[str, Any]], Awaitable[ToolResult]]:
        """虚工具：内部打 resources/read；缺 uri 直接报错。"""

        async def handler(args: dict[str, Any]) -> ToolResult:
            """execute 入口：校验 uri 后打 resources/read。"""
            uri = args.get("uri")
            if not isinstance(uri, str) or not uri.strip():
                return ToolResult("read_resource error: uri is required", is_error=True)
            return await self._invoke(server, lambda h: h.read_resource(uri.strip()))

        return handler

    async def _invoke(
        self,
        server: str,
        op: Callable[[McpHandle], Awaitable[ToolResult]],
    ) -> ToolResult:
        """在已有 session 上执行一次 RPC；死了抢一轮重连，401/404 不抢。"""
        state = self._states.get(server)
        if state is None or state.handle is None:
            return ToolResult(f"mcp {server}: not connected", is_error=True)
        # --- 1) 现有 session ---
        try:
            return await op(state.handle)
        except Exception as exc:
            if _is_permanent(exc):
                state.status = "disconnected"
                state.error = str(exc)
                return ToolResult(f"mcp {server}: {exc}", is_error=True)
            # --- 2) 瞬时错误：关旧的、再 initialize 一次、重做这次调用 ---
            try:
                await self._reconnect(state)
            except Exception as rec_exc:
                state.status = "disconnected"
                state.error = str(rec_exc)
                return ToolResult(f"mcp {server}: {rec_exc}", is_error=True)
            if state.handle is None:
                return ToolResult(f"mcp {server}: not connected", is_error=True)
            try:
                return await op(state.handle)
            except Exception as exc2:
                state.status = "disconnected"
                state.error = str(exc2)
                return ToolResult(f"mcp {server}: {exc2}", is_error=True)

    async def _reconnect(self, state: _ServerState) -> None:
        """关掉旧 handle，按同一条 spec 再连；成功则 status 回到 connected。"""
        old = state.handle
        state.handle = None
        if old is not None:
            try:
                await old.aclose()
            except Exception:
                pass
        handle = await asyncio.wait_for(self._connector(state.spec), timeout=CONNECT_TIMEOUT)
        state.handle = handle
        state.status = "connected"
        state.error = None

    async def _default_connect(self, spec: McpServerSpec) -> McpHandle:
        """生产路径：用官方 SDK 拉起 stdio 或 streamable HTTP。"""
        return await connect_sdk(spec, self.workspace)


def mcp_tool_name(server: str, remote: str) -> str:
    """OpenAI 函数名：mcp__{server}__{tool}，非法字符换成 _。"""
    return f"mcp__{_safe_token(server)}__{_safe_token(remote)}"


def _safe_token(value: str) -> str:
    """只留 a-zA-Z0-9_-，其余变下划线。"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)


def _remote_input_schema(remote: Any) -> dict[str, Any]:
    """抽出 tools/list 里的参数 schema。mcp 2.0 是 input_schema，旧对象/JSON 是 inputSchema。"""
    raw = _first_attr(remote, "input_schema", "inputSchema")
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(mode="json")
    if isinstance(raw, dict) and raw:
        return raw
    return {"type": "object", "properties": {}}


def _read_only_hint(remote: Any) -> bool:
    """读 annotations.read_only_hint；没有标注视为危险（要审批）。"""
    annotations = getattr(remote, "annotations", None)
    if annotations is None and isinstance(remote, dict):
        annotations = remote.get("annotations")
    if annotations is None:
        return False
    return bool(_first_attr(annotations, "read_only_hint", "readOnlyHint"))


def _first_attr(obj: Any, *names: str) -> Any:
    """按顺序取属性；对象没有就当 dict 用同名 key。"""
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
        if isinstance(obj, dict) and name in obj:
            value = obj[name]
            if value is not None:
                return value
    return None


def _is_method_missing(exc: BaseException) -> bool:
    """JSON-RPC Method not found：server 没实现这个方法，不是掉线。"""
    text = str(exc).lower()
    return "method not found" in text or "methodnotfound" in text


def _is_permanent(exc: BaseException) -> bool:
    """401/403/404/缺 command 这类重试也没用，返回 True。"""
    text = str(exc).lower()
    if _is_method_missing(exc):
        return False
    if any(code in text for code in ("401", "403", "404")):
        return True
    if "unauthorized" in text or "not found" in text:
        return True
    if "missing command" in text:
        return True
    return False


async def connect_sdk(spec: McpServerSpec, workspace: Path) -> McpHandle:
    """官方 SDK 长连接。stdio 当家长；HTTP 打 URL。"""
    import httpx2
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    stack = AsyncExitStack()
    try:
        # --- 1) 开传输：stdio 拉子进程，HTTP 打 URL ---
        if spec.type in {"stdio", "local"}:
            if not spec.command:
                raise ValueError(f"MCP server {spec.name} is missing command")
            params = StdioServerParameters(
                command=spec.command,
                args=spec.args,
                env={**os.environ, **spec.env},
                cwd=spec.cwd or str(workspace),
            )
            errlog = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
            read, write = await stack.enter_async_context(
                stdio_client(params, errlog=errlog)
            )
        elif spec.type in {"http", "streamable_http", "streamable-http"}:
            if not spec.url:
                raise ValueError(f"MCP server {spec.name} is missing url")
            http_client = create_mcp_http_client(
                headers=spec.headers or None,
                timeout=httpx2.Timeout(spec.timeout, read=spec.timeout),
            )
            await stack.enter_async_context(http_client)
            read, write = await stack.enter_async_context(
                streamable_http_client(spec.url, http_client=http_client)
            )
        else:
            raise ValueError(f"Unsupported MCP transport: {spec.type}")
        # --- 2) initialize，失败则关掉刚开的传输 ---
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return SdkHandle(stack, session, spec)
    except Exception:
        await stack.aclose()
        raise


class SdkHandle:
    """包一层官方 ClientSession，统一成 McpHandle。"""

    def __init__(self, stack: AsyncExitStack, session: Any, spec: McpServerSpec) -> None:
        """stack 管传输生命周期；session 已 initialize。"""
        self._stack = stack
        self._session = session
        self._spec = spec

    async def list_tools(self) -> list[Any]:
        """RPC tools/list，返回 SDK 的 tool 对象列表。"""
        result = await self._session.list_tools()
        return list(result.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """RPC tools/call；超时用 spec.timeout。"""
        result = await self._session.call_tool(
            name, arguments, read_timeout_seconds=self._spec.timeout
        )
        return ToolResult(
            _content_to_text(getattr(result, "content", result)),
            is_error=_tool_is_error(result),
        )

    async def list_resources(self) -> list[str]:
        """RPC resources/list，压成「uri name description」行。"""
        try:
            result = await self._session.list_resources()
        except Exception as exc:
            if _is_method_missing(exc):
                return []
            raise
        lines = [
            f"{resource.uri} {resource.name or ''} {resource.description or ''}".strip()
            for resource in result.resources
        ]
        return lines

    async def read_resource(self, uri: str) -> ToolResult:
        """RPC resources/read，正文收成纯文本。"""
        result = await asyncio.wait_for(
            self._session.read_resource(uri),
            timeout=self._spec.timeout,
        )
        return ToolResult(_content_to_text(result.contents))

    async def aclose(self) -> None:
        """拆掉 session + 传输（stdio 子进程在这里被杀掉）。"""
        await self._stack.aclose()


def _tool_is_error(result: Any) -> bool:
    """mcp 2.0 字段是 is_error；旧对象可能还有 isError。"""
    if hasattr(result, "is_error"):
        return bool(result.is_error)
    if hasattr(result, "isError"):
        return bool(result.isError)
    return False


def _content_to_text(content: Any) -> str:
    """把 MCP ContentBlock / list / 带 text 的对象收成一段字符串。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(filter(None, (_content_to_text(item) for item in content)))
    if hasattr(content, "text"):
        return str(content.text)
    if hasattr(content, "data") and hasattr(content, "mimeType"):
        data = str(content.data)
        return f"[image {content.mimeType} base64 chars={len(data)}]"
    if hasattr(content, "resource"):
        return _content_to_text(content.resource)
    if hasattr(content, "model_dump"):
        return json.dumps(content.model_dump(mode="json"), ensure_ascii=False)
    return str(content)
