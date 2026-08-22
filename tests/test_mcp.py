"""MCP Client：配置合并 / 变量展开 / 长连接工具表 / 隔离 / 虚工具 / 抢重连。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from xcode.mcp.client import McpClientManager
from xcode.mcp.config import load_mcp_server_specs
from xcode.tools.base import ToolContext, ToolResult


def _write_mcp(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def test_project_mcp_json_overrides_user_by_name(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    _write_mcp(
        home / "mcp.json",
        {
            "github": {"command": "npx", "args": ["-y", "user-github"]},
            "fs": {"command": "npx", "args": ["-y", "user-fs"]},
        },
    )
    _write_mcp(
        ws / ".xcode" / "mcp.json",
        {"github": {"command": "uvx", "args": ["project-github"]}},
    )

    specs = load_mcp_server_specs(workspace=ws, data_home=home)
    assert set(specs) == {"github", "fs"}
    assert specs["github"].command == "uvx"
    assert specs["github"].args == ["project-github"]
    assert specs["fs"].args == ["-y", "user-fs"]


def test_disabled_and_missing_files_are_skipped(tmp_path):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_mcp(
        home / "mcp.json",
        {
            "on": {"command": "echo", "args": ["ok"]},
            "off": {"command": "echo", "args": ["no"], "enabled": False},
        },
    )
    (tmp_path / "ws" / ".claude").mkdir()
    (tmp_path / "ws" / ".mcp.json").write_text("{}", encoding="utf-8")

    specs = load_mcp_server_specs(workspace=ws, data_home=home)
    assert list(specs) == ["on"]
    assert specs["on"].enabled is True


def test_expands_home_project_dir_and_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("TOKEN", "secret-token")
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    _write_mcp(
        home / "mcp.json",
        {
            "fs": {
                "command": "${HOME}/bin/mcp",
                "args": ["${PROJECT_DIR}", "${MISSING_TOKEN}"],
                "env": {"TOKEN": "${TOKEN}"},
                "cwd": "${PROJECT_DIR}/sub",
            },
            "remote": {
                "type": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${TOKEN}"},
            },
        },
    )

    specs = load_mcp_server_specs(workspace=ws, data_home=home)
    assert specs["fs"].command == str(Path.home() / "bin" / "mcp")
    assert specs["fs"].args == [str(ws.resolve()), ""]
    assert specs["fs"].env == {"TOKEN": "secret-token"}
    assert specs["fs"].cwd == str(ws.resolve() / "sub")
    assert specs["remote"].type == "http"
    assert specs["remote"].url == "https://example.com/mcp"
    assert specs["remote"].headers == {"Authorization": "Bearer secret-token"}


class _Ann:
    def __init__(self, read_only: bool) -> None:
        self.readOnlyHint = read_only


class _RemoteTool:
    def __init__(
        self,
        name: str,
        *,
        description: str = "",
        schema: dict | None = None,
        read_only: bool = False,
    ) -> None:
        self.name = name
        self.description = description or name
        self.inputSchema = schema or {"type": "object", "properties": {}}
        self.annotations = _Ann(read_only)


class FakeHandle:
    def __init__(
        self,
        tools: list[_RemoteTool],
        *,
        fail_calls: int = 0,
        call_error: Exception | None = None,
        resources: list[str] | None = None,
    ) -> None:
        self.tools = tools
        self.fail_calls = fail_calls
        self.call_error = call_error
        self.resources = resources or []
        self.calls: list[tuple[str, dict]] = []
        self.reads: list[str] = []
        self.closed = False

    async def list_tools(self) -> list[_RemoteTool]:
        return self.tools

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        if self.call_error is not None:
            raise self.call_error
        if self.fail_calls > 0:
            self.fail_calls -= 1
            raise ConnectionError("session closed")
        self.calls.append((name, arguments))
        return ToolResult(f"called {name} {arguments}")

    async def list_resources(self) -> list[str]:
        return list(self.resources)

    async def read_resource(self, uri: str) -> ToolResult:
        self.reads.append(uri)
        return ToolResult(f"resource {uri}")

    async def aclose(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(self, mapping: dict[str, FakeHandle | Exception]) -> None:
        self.mapping = mapping
        self.connects: list[str] = []

    async def __call__(self, spec) -> FakeHandle:
        self.connects.append(spec.name)
        item = self.mapping[spec.name]
        if isinstance(item, Exception):
            raise item
        return item


def _mgr(
    tmp_path: Path,
    servers: dict,
    connector: FakeConnector,
) -> McpClientManager:
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    _write_mcp(home / "mcp.json", servers)
    return McpClientManager(workspace=ws, data_home=home, connector=connector)


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(workspace=tmp_path)


def test_start_registers_prefixed_tools_and_resource_wrappers(tmp_path):
    ctx = _ctx(tmp_path)
    handle = FakeHandle(
        [_RemoteTool("list_issues", read_only=True)],
        resources=["repo://acme/issues"],
    )
    connector = FakeConnector({"github": handle})
    mgr = _mgr(tmp_path, {"github": {"command": "npx"}}, connector)

    async def _run() -> None:
        await mgr.start()
        by_name = {t.name: t for t in mgr.tools()}
        assert list(by_name) == [
            "mcp__github__list_issues",
            "mcp__github__list_resources",
            "mcp__github__read_resource",
        ]
        listed = await by_name["mcp__github__list_resources"].execute({}, ctx)
        assert "repo://acme/issues" in listed.text
        read = await by_name["mcp__github__read_resource"].execute(
            {"uri": "repo://acme/issues"}, ctx
        )
        assert read.text == "resource repo://acme/issues"
        assert handle.reads == ["repo://acme/issues"]
        assert by_name["mcp__github__list_issues"].requires_approval is False
        await mgr.aclose()
        assert handle.closed is True

    asyncio.run(_run())


def test_failed_server_is_isolated_from_neighbors(tmp_path):
    good = FakeHandle([_RemoteTool("ping")])
    connector = FakeConnector({"good": good, "bad": RuntimeError("spawn failed")})
    mgr = _mgr(
        tmp_path,
        {"good": {"command": "echo"}, "bad": {"command": "nope"}},
        connector,
    )

    async def _run() -> None:
        await mgr.start()
        names = [t.name for t in mgr.tools()]
        assert "mcp__good__ping" in names
        assert "mcp__good__list_resources" in names
        assert all(not n.startswith("mcp__bad__") for n in names)
        status = mgr.status_text()
        assert "good" in status and "connected" in status
        assert "bad" in status and "failed" in status
        assert "spawn failed" in status
        await mgr.aclose()

    asyncio.run(_run())


def test_missing_read_only_hint_requires_approval(tmp_path):
    handle = FakeHandle([_RemoteTool("write_thing", read_only=False)])
    mgr = _mgr(
        tmp_path,
        {"db": {"command": "echo"}},
        FakeConnector({"db": handle}),
    )

    async def _run() -> None:
        await mgr.start()
        tool = {t.name: t for t in mgr.tools()}["mcp__db__write_thing"]
        assert tool.requires_approval is True
        await mgr.aclose()

    asyncio.run(_run())


def test_dead_session_reconnects_once_on_next_call(tmp_path):
    ctx = _ctx(tmp_path)
    first = FakeHandle([_RemoteTool("query")], fail_calls=1)
    second = FakeHandle([_RemoteTool("query")])
    queue: list[FakeHandle] = [first, second]

    async def connector(spec):
        _ = spec
        return queue.pop(0)

    mgr = _mgr(tmp_path, {"pg": {"command": "echo"}}, connector)

    async def _run() -> None:
        await mgr.start()
        tool = {t.name: t for t in mgr.tools()}["mcp__pg__query"]
        result = await tool.execute({"sql": "select 1"}, ctx)
        assert result.ok
        assert "called query" in result.text
        assert second.calls == [("query", {"sql": "select 1"})]
        assert queue == []
        await mgr.aclose()

    asyncio.run(_run())


def test_auth_error_does_not_reconnect(tmp_path):
    ctx = _ctx(tmp_path)
    handle = FakeHandle(
        [_RemoteTool("list_issues")],
        call_error=RuntimeError("401 Unauthorized"),
    )
    connects = {"n": 0}

    async def connector(spec):
        _ = spec
        connects["n"] += 1
        return handle

    mgr = _mgr(tmp_path, {"gh": {"command": "echo"}}, connector)

    async def _run() -> None:
        await mgr.start()
        assert connects["n"] == 1
        tool = {t.name: t for t in mgr.tools()}["mcp__gh__list_issues"]
        result = await tool.execute({}, ctx)
        assert result.is_error
        assert "401" in result.text
        assert connects["n"] == 1
        assert "disconnected" in mgr.status_text()
        await mgr.aclose()

    asyncio.run(_run())


def test_status_shows_connecting_until_start_finishes(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def connector(spec):
        _ = spec
        started.set()
        await release.wait()
        return FakeHandle([_RemoteTool("ping")])

    mgr = _mgr(tmp_path, {"slow": {"command": "echo"}}, connector)

    async def _run() -> None:
        task = asyncio.create_task(mgr.start())
        await started.wait()
        assert "slow" in mgr.status_text()
        assert "connecting" in mgr.status_text()
        release.set()
        await task
        assert "connected" in mgr.status_text()
        await mgr.aclose()

    asyncio.run(_run())


def test_build_registry_includes_mcp_tools(tmp_path):
    from xcode.runtime.agent import build_registry

    handle = FakeHandle([_RemoteTool("list_issues", read_only=True)])
    mgr = _mgr(tmp_path, {"github": {"command": "npx"}}, FakeConnector({"github": handle}))

    async def _run() -> None:
        await mgr.start()
        names = build_registry(extra_tools=mgr.tools()).list_names()
        assert "read_file" in names
        assert "mcp__github__list_issues" in names
        await mgr.aclose()

    asyncio.run(_run())


def test_mcp_slash_prints_status(tmp_path):
    from io import StringIO

    from rich.console import Console

    from xcode.config import Config
    from xcode.entrypoints.tui import _handle_slash
    from xcode.runtime.session import SessionStore

    handle = FakeHandle([_RemoteTool("ping")])
    mgr = _mgr(tmp_path, {"good": {"command": "echo"}}, FakeConnector({"good": handle}))
    store = SessionStore(tmp_path / "home")
    session = store.create(tmp_path / "ws")
    config = Config(
        api_key="x",
        base_url="http://127.0.0.1:9",
        model="dummy",
        light_model="dummy",
        data_home=tmp_path / "home",
    )
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=120, highlight=False)

    async def _run() -> None:
        await mgr.start()
        await _handle_slash(
            "/mcp",
            console=console,
            config=config,
            session=session,
            store=store,
            mcp_manager=mgr,
        )
        await mgr.aclose()

    asyncio.run(_run())
    out = buf.getvalue()
    assert "good" in out
    assert "connected" in out


def test_run_agent_sends_mcp_schemas(tmp_path):
    from types import SimpleNamespace

    from xcode.config import Config
    from xcode.runtime.agent import run_agent
    from xcode.runtime.session import SessionStore

    handle = FakeHandle([_RemoteTool("list_issues", read_only=True)])
    mgr = _mgr(tmp_path, {"github": {"command": "npx"}}, FakeConnector({"github": handle}))
    captured: list[dict] = []

    class _Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _Completions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return _Stream()

    config = Config(
        api_key="x",
        base_url="http://127.0.0.1:9",
        model="dummy",
        light_model="dummy",
        data_home=tmp_path / "home",
    )
    store = SessionStore(config.data_home)
    session = store.create(tmp_path / "ws")
    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

    async def _run() -> None:
        await mgr.start()
        async for _ in run_agent(
            "hi",
            config=config,
            session=session,
            client=client,
            mcp_manager=mgr,
        ):
            pass
        await mgr.aclose()

    asyncio.run(_run())
    assert captured
    names = [t["function"]["name"] for t in captured[0].get("tools") or []]
    assert "mcp__github__list_issues" in names
    assert "mcp__github__list_resources" in names


def test_sdk_handle_reads_is_error_not_isError():
    """mcp 2.0 CallToolResult 只有 is_error；读 isError 会炸，模型看到假失败。"""
    from types import SimpleNamespace

    from xcode.mcp.client import SdkHandle
    from xcode.mcp.config import McpServerSpec

    class _Sess:
        async def call_tool(self, name, arguments, read_timeout_seconds=None):
            _ = name, arguments, read_timeout_seconds
            return SimpleNamespace(content="hits", is_error=False)

    handle = SdkHandle(
        stack=SimpleNamespace(),
        session=_Sess(),
        spec=McpServerSpec(name="zhipu"),
    )
    result = asyncio.run(handle.call_tool("webSearchPro", {"q": "x"}))
    assert result.ok
    assert "hits" in result.text


def test_wraps_sdk_v2_input_schema_and_read_only_hint(tmp_path):
    """mcp 2.0 Tool 字段是 input_schema / read_only_hint；读 camelCase 会把 schema 丢成空对象。"""
    from mcp.types import Tool, ToolAnnotations

    remote = Tool(
        name="webSearchStd",
        description="web search",
        inputSchema={
            "type": "object",
            "properties": {
                "search_query": {"type": "string", "description": "query"},
            },
            "required": ["search_query"],
        },
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    handle = FakeHandle([remote])
    mgr = _mgr(
        tmp_path,
        {"zhipu": {"command": "echo"}},
        FakeConnector({"zhipu": handle}),
    )

    async def _run() -> None:
        await mgr.start()
        tool = {t.name: t for t in mgr.tools()}["mcp__zhipu__webSearchStd"]
        assert "search_query" in tool.parameters.get("properties", {})
        assert tool.parameters.get("required") == ["search_query"]
        assert tool.requires_approval is False
        await mgr.aclose()

    asyncio.run(_run())


def test_list_resources_method_not_found_is_empty_not_disconnect(tmp_path):
    """很多 server 不实现 resources；应回空列表，不能把整台 server 标成 disconnected。"""
    ctx = _ctx(tmp_path)
    handle = FakeHandle([_RemoteTool("webSearchPro", read_only=True)])

    async def list_resources() -> list[str]:
        raise RuntimeError("Method not found: resources/list")

    handle.list_resources = list_resources  # type: ignore[method-assign]
    mgr = _mgr(
        tmp_path,
        {"zhipu": {"command": "echo"}},
        FakeConnector({"zhipu": handle}),
    )

    async def _run() -> None:
        await mgr.start()
        tool = {t.name: t for t in mgr.tools()}["mcp__zhipu__list_resources"]
        result = await tool.execute({}, ctx)
        assert result.ok
        assert "(no resources)" in result.text
        assert "disconnected" not in mgr.status_text()
        await mgr.aclose()

    asyncio.run(_run())
