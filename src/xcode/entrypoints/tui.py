"""交互层：TUI banner / prompt / slash / 流式渲染。

## 与会话、记忆的接线（读 slash 与主循环时对照）
- 启动：外部传入已 resolve 的 SessionRuntime（transcript/context 已 load）
- 每轮用户输入非 slash → run_agent（内部 append_message + 可能 compact）
- 一轮结束后 → _submit_round：slice_round → MemoryPipeline.submit（后台 stage1/2）
- 退出 finally → registry.drain_all：尽量把记忆队列刷完
- /compact：强制会话压缩；/memory *：读或 clear 长期记忆；无 /clear 会话
- /resume 无参：选择器；启动空会话不回放；切入旧会话后回放并按 Markdown 渲染
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
from dataclasses import dataclass
from typing import Any, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from openai import AsyncOpenAI

from xcode import __version__
from xcode.config import Config
from xcode.entrypoints.complete import SLASH_ITEMS, XcodeCompleter
from xcode.entrypoints.session_picker import (
    pick_session,
    replay_turns,
    rows_from_metas,
    format_session_line,
)
from xcode.memory import MemoryStore, PipelineRegistry, should_extract, slice_round
from xcode.runtime.agent import build_registry, run_agent, run_compact
from xcode.runtime.session import SessionRuntime, SessionStore
from xcode.runtime.snapshot import SnapshotStore
from xcode.runtime.tokens import count_messages_tokens, format_token_usage
from xcode.skill import SkillRegistry, handle_skills_arg

@dataclass
class _SlashResult:
    """slash 处理结果：是否退出，以及是否切换到另一个 session。"""

    exit: bool = False
    session: SessionRuntime | None = None
    invoke: str | None = None

# 工业蓝图：深底 + 信号绿强调，克制层次
_PT_STYLE = Style.from_dict(
    {
        "prompt": "bold #e8eef4 bg:#111820",
        "prompt.brand": "bold #5dffa8 bg:#111820",
        "prompt.dim": "#8b9aab bg:#111820",
        "bottom-toolbar": "noreverse #8b9aab bg:#0a0f14",
        "bottom-toolbar.strong": "noreverse bold #5dffa8 bg:#0a0f14",
        "placeholder": "#5a6a7a",
    }
)


def _build_key_bindings() -> KeyBindings:
    """Enter 发送；Esc+Enter 换行。不绑 s-enter：当前 prompt_toolkit 不认这个键。"""
    keys = KeyBindings()

    @keys.add("enter")
    def _send(event) -> None:
        event.current_buffer.validate_and_handle()

    @keys.add("escape", "enter")
    def _newline(event) -> None:
        event.current_buffer.insert_text("\n")

    return keys


def _banner(console: Console, *, config: Config, session: SessionRuntime) -> None:
    """绘制启动横幅：品牌为主，信息为辅。"""
    title = Text()
    title.append("x", style="bold #5dffa8")
    title.append("code", style="bold #e8eef4")
    title.append(f"  v{__version__}", style="dim #8b9aab")

    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="#8b9aab", justify="right")
    meta.add_column(style="#e8eef4")
    meta.add_row("model", config.model)
    meta.add_row("workspace", str(session.workspace_root))
    meta.add_row("session", f"{session.session_id}  ·  {session.meta.name}")

    console.print()
    console.print(Panel(meta, title=title, border_style="#2a9b68", padding=(1, 2)))
    console.print(
        Text(
            "  /resume 选会话  ·  Enter 发送  ·  Esc+Enter 换行  ·  "
            "Ctrl-C 停本轮 / 空输入再按退出",
            style="dim #8b9aab",
        )
    )
    console.print()


def _toolbar(config: Config, session: SessionRuntime, turns: int) -> Callable[[], Any]:
    def _get() -> Any:
        short_ws = session.workspace_root.name
        used = session.estimated_tokens or count_messages_tokens(
            session.messages, model=config.model
        )
        tok = format_token_usage(used, config.context_window)
        return HTML(
            f"<style bg='#0a0f14'>"
            f"<b><style fg='#5dffa8'> xcode </style></b>"
            f"<style fg='#8b9aab'> │ </style>"
            f"<style fg='#e8eef4'>{config.model}</style>"
            f"<style fg='#8b9aab'> │ </style>"
            f"{short_ws}"
            f"<style fg='#8b9aab'> │ </style>"
            f"turns {turns}"
            f"<style fg='#8b9aab'> │ </style>"
            f"<style fg='#e8eef4'>{tok}</style>"
            f"<style fg='#8b9aab'> │ </style>"
            f"{session.session_id[-8:]}"
            f"</style>"
        )

    return _get


def _print_replay(console: Console, session: SessionRuntime) -> None:
    """有对话才回放。空会话不打。助手段走 Markdown。"""

    blocks = replay_turns(session.messages)
    if not blocks:
        return
    console.print(Text("  ── 最近对话 ──", style="dim #2a9b68"))
    for block in blocks:
        if block.role == "user":
            console.print(Text(f"  you  {block.text}", style="#e8eef4"))
            continue
        if block.hint:
            console.print(Text(f"  · {block.hint}", style="dim"))
        if block.text.strip():
            console.print(Markdown(block.text))
    console.print()


def _build_registry(config: Config) -> PipelineRegistry | None:
    """构造长期记忆管线注册表（light_model）；无 data_home 则整段记忆功能关闭。"""
    if config.data_home is None:
        return None
    client = AsyncOpenAI(api_key=config.api_key or "missing", base_url=config.base_url)
    return PipelineRegistry(
        data_home=config.data_home,
        client=client,
        model=config.light_model or config.model,
    )


async def _confirm(prompt: PromptSession, question: str) -> bool:
    """y/N 确认；Ctrl-C / 空输入视为否。"""

    try:
        answer = await prompt.prompt_async(
            HTML(f"<b><style fg='#ffb454'>⚠ {question} [y/N] </style></b>")
        )
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}


async def _run_agent_turn(
    text: str,
    *,
    console: Console,
    config: Config,
    session: SessionRuntime,
    store: SessionStore,
    renderer: "_EventRenderer",
    ask_permission,
    cancel_event: asyncio.Event,
) -> None:
    """跑一轮 agent。SIGINT 只置 cancel_event，不让 asyncio.run 把整个 TUI 掀掉。"""

    loop = asyncio.get_running_loop()

    def _on_sigint() -> None:
        cancel_event.set()

    try:
        loop.add_signal_handler(signal.SIGINT, _on_sigint)
        previous = None
    except (NotImplementedError, RuntimeError):
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda *_args: _on_sigint())

    try:
        async for event in run_agent(
            text,
            config=config,
            session=session,
            store=store,
            ask_permission=ask_permission,
            cancel_event=cancel_event,
        ):
            renderer.render(event)
    except (KeyboardInterrupt, asyncio.CancelledError):
        cancel_event.set()
        console.print(Text("  · cancelled — 本轮已停，对话还在", style="dim #ffb454"))
    finally:
        try:
            loop.remove_signal_handler(signal.SIGINT)
        except (NotImplementedError, RuntimeError):
            if previous is not None:
                signal.signal(signal.SIGINT, previous)


def _submit_round(registry: PipelineRegistry | None, session: SessionRuntime) -> None:
    """把「刚结束的一轮」异步交给长期记忆，不阻塞下一轮输入。

    用当前 session.messages（送模视图，tool 可能已截断）切片；
    should_extract 为假则完全不入队。
    """
    if registry is None:
        return
    round_content = slice_round(
        session.messages,
        workspace=session.workspace_root,
        session_id=session.session_id,
    )
    if should_extract(round_content):
        registry.for_workspace(session.workspace_root).submit(round_content)


async def start_tui(
    *,
    config: Config,
    session: SessionRuntime,
    store: SessionStore,
) -> None:
    """启动交互 REPL：读输入 → slash 或 run_agent → 渲染事件。"""
    # --- 1) 终端与输入控件 ---
    console = Console()
    history_path = config.data_home / "prompt_history.txt"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    turns = sum(1 for m in session.messages if m.get("role") == "user")

    _banner(console, config=config, session=session)
    _print_replay(console, session)
    registry = _build_registry(config)
    completer = XcodeCompleter(session.workspace_root)
    last_renderer: _EventRenderer | None = None

    prompt = PromptSession(
        history=FileHistory(str(history_path)),
        completer=completer,
        complete_while_typing=True,
        key_bindings=_build_key_bindings(),
        multiline=True,
        style=_PT_STYLE,
        bottom_toolbar=_toolbar(config, session, turns),
        placeholder=[("class:placeholder", "描述任务，或 /resume 选会话…")],
    )

    # --- 2) 主循环：slash 本地处理，否则交给 agent ---
    try:
        while True:
            try:
                text = await prompt.prompt_async(
                    HTML("<b><style fg='#5dffa8'>›</style></b> "),
                )
            except (EOFError, KeyboardInterrupt):
                console.print(Text("bye", style="dim"))
                return
            text = (text or "").strip()
            if not text:
                continue

            if text.startswith("/"):
                result = await _handle_slash(
                    text,
                    console=console,
                    config=config,
                    session=session,
                    store=store,
                    memory_registry=registry,
                    prompt=prompt,
                    last_renderer=last_renderer,
                )
                if result.exit:
                    console.print(Text("bye", style="dim"))
                    return
                if result.session is not None:
                    session = result.session
                    completer.workspace = session.workspace_root
                    turns = sum(1 for m in session.messages if m.get("role") == "user")
                    prompt.bottom_toolbar = _toolbar(config, session, turns)
                    _print_replay(console, session)
                if result.invoke:
                    text = result.invoke
                else:
                    continue

            console.print(Text("── agent ──", style="dim #2a9b68"))
            renderer = _EventRenderer(console)
            last_renderer = renderer

            async def _ask_permission(question: str) -> bool:
                """审批回调：仅 requires_approval 的工具执行前询问。"""
                return await _confirm(prompt, question)

            cancel_event = asyncio.Event()
            await _run_agent_turn(
                text,
                console=console,
                config=config,
                session=session,
                store=store,
                renderer=renderer,
                ask_permission=_ask_permission,
                cancel_event=cancel_event,
            )
            _submit_round(registry, session)
            session.estimated_tokens = count_messages_tokens(
                session.messages, model=config.model
            )
            turns = sum(1 for m in session.messages if m.get("role") == "user")
            prompt.bottom_toolbar = _toolbar(config, session, turns)
            console.print()
    finally:
        if registry is not None:
            await registry.drain_all()


def _print_sessions(
    console: Console,
    *,
    store: SessionStore,
    session: SessionRuntime,
) -> list:
    """打印带序号的会话列表；返回 list[SessionMeta] 供 /resume 复用。"""
    items = store.list_sessions(session.workspace_root)
    if not items:
        console.print("[dim](no sessions)[/]")
        return items
    rows = rows_from_metas(items, current_id=session.session_id)
    for i, row in enumerate(rows, start=1):
        console.print("  " + format_session_line(row, index=i))
        if row.preview:
            console.print(Text(f"     {row.preview}", style="dim"))
    console.print(
        Text(
            "  直接 /resume 打开选择器  ·  或 /resume <序号|标题|id>  ·  /new",
            style="dim",
        )
    )
    return items


async def _handle_slash(
    text: str,
    *,
    console: Console,
    config: Config,
    session: SessionRuntime,
    store: SessionStore,
    memory_registry: PipelineRegistry | None = None,
    prompt: PromptSession | None = None,
    last_renderer: "_EventRenderer | None" = None,
) -> _SlashResult:
    """处理 slash；exit=True 退出 TUI；session 非空则主循环切换会话。"""
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower()
    if cmd in {"/exit", "/quit"}:
        return _SlashResult(exit=True)
    if cmd == "/help":
        lines = ["### commands", ""]
        for name, desc in SLASH_ITEMS:
            lines.append(f"- `{name}` — {desc}")
        lines.extend(
            [
                "",
                "`/resume` 打开选择器并回放该场对话；无参启动仍是空会话。",
                "",
                "Enter 发送 · Esc+Enter 换行 · Ctrl-C 停本轮",
            ]
        )
        console.print(Markdown("\n".join(lines)))
        return _SlashResult()
    if cmd in {"/resume", "/session", "/sessions"}:
        return await _handle_resume_or_pick(
            arg.strip(),
            console=console,
            session=session,
            store=store,
        )
    if cmd == "/new":
        # 先落盘当前会话指针状态，再新建
        try:
            session.save()
        except OSError:
            pass
        new_rt = store.create(session.workspace_root)
        new_rt.save()
        console.print(
            Text(
                f"new session · {new_rt.session_id}  ·  {new_rt.meta.name}",
                style="dim #5dffa8",
            )
        )
        return _SlashResult(session=new_rt)
    if cmd == "/rename":
        title = arg.strip()
        if not title:
            console.print("[dim]usage:[/] /rename <标题>")
            return _SlashResult()
        session.meta.name = title
        session.meta.default_name = False
        session.save()
        console.print(Text(f"renamed · {title}", style="dim #5dffa8"))
        return _SlashResult()
    if cmd == "/last":
        if last_renderer is None or not last_renderer.last_tool_result:
            console.print("[dim]no tool output yet[/]")
            return _SlashResult()
        console.print(Text("── last tool ──", style="dim #2a9b68"))
        console.print(last_renderer.last_tool_result)
        return _SlashResult()
    if cmd == "/clear":
        console.print(
            "[dim]v1 无 /clear 清空对话；请用[/] [bold]/new[/] "
            "[dim]或退出后[/] [bold]--new-session[/]"
        )
        return _SlashResult()
    if cmd == "/compact":
        if not session.messages:
            console.print("[dim]nothing to compact[/]")
            return _SlashResult()
        console.print("[dim]compacting…[/]")
        try:
            summary = await run_compact(session, config=config)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]compact failed:[/] {exc}")
            return _SlashResult()
        preview = summary if len(summary) <= 240 else summary[:237] + "…"
        console.print(f"[dim]compacted · {len(session.messages)} msgs in window[/]")
        console.print(Text(preview, style="dim"))
        return _SlashResult()
    if cmd == "/tools":
        names = build_registry(session.workspace_root).list_names()
        console.print(", ".join(names) if names else "(none)")
        return _SlashResult()
    if cmd == "/status":
        used = session.estimated_tokens or count_messages_tokens(
            session.messages, model=config.model
        )
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_row("model", config.model)
        table.add_row("session", session.session_id)
        table.add_row("messages", str(len(session.messages)))
        table.add_row(
            "context",
            format_token_usage(used, config.context_window),
        )
        table.add_row("events", str(session.event_count))
        console.print(table)
        return _SlashResult()
    if cmd == "/snapshot":
        return _handle_snapshot(arg.strip(), console=console, session=session)
    if cmd == "/restore":
        return await _handle_restore(
            arg.strip(),
            console=console,
            session=session,
            prompt=prompt,
        )
    if cmd == "/memory":
        await _handle_memory(
            arg,
            console=console,
            config=config,
            session=session,
            memory_registry=memory_registry,
            prompt=prompt,
        )
        return _SlashResult()
    if cmd == "/skills":
        return _handle_skills(arg, console=console, config=config, session=session)
    console.print(f"[yellow]unknown command:[/] {cmd}  (try /help)")
    _ = arg
    return _SlashResult()


def _snapshot_store(session: SessionRuntime) -> SnapshotStore:
    return SnapshotStore(session.data_dir, session.workspace_root)


def _print_restore_list(console: Console, snaps: SnapshotStore) -> None:
    rows = snaps.list_entries()
    if not rows:
        console.print("[dim]no snapshots yet — write_file/edit_file first, or /snapshot[/]")
        return
    for row in rows:
        console.print(
            f"  {row['key']}  {row['label']}  "
            f"{row['files']} files  [dim]{row['created_at']}[/]"
        )
    console.print(
        Text(
            "  /restore last  ·  /restore <名>  ·  /restore undo",
            style="dim",
        )
    )


def _handle_snapshot(
    name: str,
    *,
    console: Console,
    session: SessionRuntime,
) -> _SlashResult:
    snaps = _snapshot_store(session)
    try:
        final = snaps.save_named(name or None)
    except ValueError as exc:
        console.print(f"[yellow]snapshot failed:[/] {exc}")
        return _SlashResult()
    n = len(snaps.capture_session_now())
    console.print(Text(f"snapshot saved · {final}  ·  {n} files", style="dim #5dffa8"))
    return _SlashResult()


async def _handle_restore(
    target: str,
    *,
    console: Console,
    session: SessionRuntime,
    prompt: PromptSession | None = None,
) -> _SlashResult:
    snaps = _snapshot_store(session)
    if not target:
        console.print(
            Text(
                "usage: /restore last | <name> | undo",
                style="dim",
            )
        )
        _print_restore_list(console, snaps)
        return _SlashResult()
    if prompt is not None:
        n = len(snaps.capture_session_now())
        if not await _confirm(prompt, f"restore {target} · 将覆盖约 {n} 个已跟踪文件"):
            console.print(Text("cancelled", style="dim"))
            return _SlashResult()
    try:
        if target.lower() == "last":
            report = snaps.restore_last()
        elif target.lower() == "undo":
            report = snaps.restore_undo()
        else:
            report = snaps.restore_named(target)
    except FileNotFoundError as exc:
        console.print(f"[yellow]restore failed:[/] {exc}")
        _print_restore_list(console, snaps)
        return _SlashResult()
    except OSError as exc:
        console.print(f"[red]restore failed:[/] {exc}")
        return _SlashResult()
    console.print(Text(report.format(), style="dim #5dffa8"))
    return _SlashResult()


async def _handle_resume_or_pick(
    query: str,
    *,
    console: Console,
    session: SessionRuntime,
    store: SessionStore,
) -> _SlashResult:
    """无参打开选择器；有参按序号 / 标题 / id 切入。"""

    if not query:
        items = store.list_sessions(session.workspace_root)
        rows = rows_from_metas(items, current_id=session.session_id)
        if not rows:
            console.print("[dim](no sessions)[/]")
            return _SlashResult()
        sid = await pick_session(rows)
        if not sid:
            return _SlashResult()
        return _handle_resume(
            sid,
            console=console,
            session=session,
            store=store,
        )
    return _handle_resume(
        query,
        console=console,
        session=session,
        store=store,
    )


def _handle_resume(
    query: str,
    *,
    console: Console,
    session: SessionRuntime,
    store: SessionStore,
) -> _SlashResult:
    """按 query 切入已有会话。"""
    if not query:
        _print_sessions(console, store=store, session=session)
        return _SlashResult()
    try:
        sid = store.find_session_id(session.workspace_root, query)
    except ValueError as exc:
        console.print(f"[yellow]resume failed:[/] {exc}")
        _print_sessions(console, store=store, session=session)
        return _SlashResult()
    if sid == session.session_id:
        console.print(f"[dim]already on {sid}[/]")
        return _SlashResult()
    try:
        session.save()
    except OSError:
        pass
    try:
        new_rt = store.load(session.workspace_root, sid)
    except FileNotFoundError:
        console.print(f"[yellow]session not found:[/] {sid}")
        return _SlashResult()
    new_rt.touch()
    new_rt.save()
    n = len(new_rt.messages)
    console.print(
        Text(
            f"resumed · {new_rt.session_id}  ·  {new_rt.meta.name}  ·  {n} msgs",
            style="dim #5dffa8",
        )
    )
    return _SlashResult(session=new_rt)


def _handle_skills(
    arg: str,
    *,
    console: Console,
    config: Config,
    session: SessionRuntime,
) -> _SlashResult:
    """列表 / 启停 / 本轮强制加载 skill。"""
    registry = SkillRegistry(session.workspace_root, data_home=config.data_home)
    result = handle_skills_arg(arg, registry)
    if result.kind == "invoke":
        console.print(Text("loaded skill · running…", style="dim #5dffa8"))
        return _SlashResult(invoke=result.text)
    if result.kind == "error":
        console.print(f"[yellow]{result.text}[/]")
        return _SlashResult()
    if result.kind == "list" and not arg.strip():
        console.print(
            Text(
                "usage: /skills  ·  /skills on|off <name>  ·  /skills <name> [args]",
                style="dim",
            )
        )
    console.print(result.text)
    return _SlashResult()


async def _handle_memory(
    arg: str,
    *,
    console: Console,
    config: Config,
    session: SessionRuntime,
    memory_registry: PipelineRegistry | None = None,
    prompt: PromptSession | None = None,
) -> bool:
    """/memory 子命令：查看或清空**长期记忆**（不是会话 transcript）。

    - summary/path/show/grep：只读 MemoryStore
    - clear：PipelineRegistry.clear_workspace → epoch 作废在途任务 + 删文件重建模板
      必须先作废 pipeline，否则后台 stage2 可能把旧内容写回空目录
    项目规范请写 XCODE.md，勿把 MEMORY 当人工配置文件。
    """
    store = MemoryStore(config.data_home, session.workspace_root)
    store.ensure_layout()
    sub, _, rest = arg.partition(" ")
    sub = sub.strip().lower()
    if sub in {"", "summary"}:
        # /memory 默认读 memory_summary（注入 system 的短摘要），不是 MEMORY.md
        summary = store.read_summary() or "(empty summary)"
        console.print(summary)
        if store.summary_is_placeholder() and store.memory_has_entries():
            console.print()
            console.print(
                Text(
                    "提示：summary 仍是空模板（后台 stage2 尚未合并）；"
                    "但 MEMORY.md 已有内容——见下方。也可 /memory show memory",
                    style="dim #ffb454",
                )
            )
            console.print()
            try:
                console.print(store.read_rel("MEMORY.md"))
            except FileNotFoundError:
                pass
        elif store.summary_is_placeholder():
            console.print()
            console.print(
                Text(
                    "说明：/memory 显示 memory_summary.md；"
                    "详情注册表用 /memory show memory；"
                    "摘要由后台 stage2 防抖合并写入（≥3 信号或空闲约 5 分钟）。",
                    style="dim",
                )
            )
        return False
    if sub == "path":
        console.print(str(store.root))
        console.print("[dim]generated state — project conventions → XCODE.md[/]")
        return False
    if sub == "show":
        which = rest.strip().lower() or "summary"
        rel = "MEMORY.md" if which in {"memory", "mem"} else "memory_summary.md"
        try:
            console.print(store.read_rel(rel))
        except FileNotFoundError:
            console.print(f"[yellow]missing {rel}[/]")
        return False
    if sub == "grep" and rest.strip():
        console.print(store.grep(rest.strip()))
        return False
    if sub == "clear":
        if prompt is not None and not await _confirm(prompt, "清空本项目长期记忆"):
            console.print(Text("cancelled", style="dim"))
            return False
        if memory_registry is not None:
            memory_registry.clear_workspace(session.workspace_root)
        else:
            store.clear()
        console.print("[dim]cleared memories (pipeline invalidated, templates restored)[/]")
        return False
    console.print(
        "[yellow]usage:[/] /memory [summary|path|show memory|show summary|grep <q>|clear]\n"
        "[dim]/memory = summary（注入 system）；/memory show memory = MEMORY.md 注册表[/]\n"
        "[dim]memories are generated; put project conventions in XCODE.md[/]"
    )
    return False


def _short_tool_preview(name: str | None, args: dict[str, Any], *, limit: int = 72) -> str:
    """工具参数一行摘要：优先展示 path/command 等关键字段。"""
    if not args:
        return ""
    preferred = (
        "path",
        "command",
        "query",
        "pattern",
        "url",
        "file",
        "glob",
        "target",
    )
    for key in preferred:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            text = val.strip().replace("\n", " ")
            if len(text) > limit:
                text = text[: limit - 1] + "…"
            return text
    preview = json.dumps(args, ensure_ascii=False)
    if len(preview) > limit:
        preview = preview[: limit - 1] + "…"
    return preview


class _EventRenderer:
    """产品事件 → 终端。thinking / 回答都按 delta 流式打出。

    当场不渲染 Markdown：边长边重排会叠字。回放是完整文稿，仍走 Markdown。
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self.last_tool_result: str | None = None
        self._tool_started: float | None = None
        self._thinking_open = False
        self._answer_labeled = False

    def render(self, event: dict[str, Any]) -> None:
        et = event.get("type")
        if et == "text_delta":
            self._close_thinking()
            text = str(event.get("text") or "")
            if not text:
                return
            if not self._answer_labeled:
                self.console.print()
                self.console.print(Text("  · answer", style="dim"))
                self._answer_labeled = True
            self.console.print(text, end="")
            return
        if et == "thinking_delta":
            thinking = str(event.get("thinking") or "")
            if not thinking:
                return
            if not self._thinking_open:
                self.console.print()
                self.console.print(Text("  · thinking", style="dim italic"))
                self._thinking_open = True
            self.console.print(Text(thinking, style="dim italic"), end="")
            return
        if et == "turn_complete":
            if str(event.get("stop_reason") or "") == "compacted":
                self.console.print(
                    Text("  · compacted — 早期对话已收成摘要", style="dim #ffb454")
                )
                return
            self._finish_answer()
            return
        if et == "tool_call":
            self._finish_answer()
            name = str(event.get("name") or "?")
            args = event.get("input") or {}
            if not isinstance(args, dict):
                args = {}
            preview = _short_tool_preview(name, args)
            self._tool_started = time.monotonic()
            self.console.print()
            self.console.print(Text(f"  ⚙ tool  {name}", style="bold #5dffa8"), end="")
            if preview:
                self.console.print(Text(f"  {preview}", style="dim"))
            else:
                self.console.print()
            return
        if et == "tool_result":
            is_error = bool(event.get("is_error"))
            flag = "err" if is_error else "ok"
            style = "#ffb454" if is_error else "#2a9b68"
            result = str(event.get("result") or "")
            self.last_tool_result = result
            elapsed = ""
            if self._tool_started is not None:
                elapsed = f"{time.monotonic() - self._tool_started:.1f}s"
                self._tool_started = None
            extra = f"  · {elapsed}" if elapsed else ""
            if is_error:
                snippet = result.replace("\n", " ").strip()
                if len(snippet) > 80:
                    snippet = snippet[:77] + "…"
                self.console.print(Text(f"  ↳ {flag}: {snippet}{extra}", style=style))
            elif result:
                self.console.print(
                    Text(f"  ↳ {flag}", style=style),
                    Text(f"  · {len(result)} chars{extra}", style="dim"),
                )
            else:
                self.console.print(Text(f"  ↳ {flag}{extra}", style=style))
            return
        if et == "error":
            self._finish_answer()
            self.console.print()
            err = str(event.get("error") or "")
            if err == "cancelled":
                self.console.print(Text("  · cancelled — 本轮已停，对话还在", style="dim #ffb454"))
            else:
                self.console.print(Text(f"error: {err}", style="bold red"))
            return
        if et == "done":
            self._finish_answer()
            self.console.print()
            return

    def _close_thinking(self) -> None:
        if self._thinking_open:
            self.console.print()
            self._thinking_open = False

    def _finish_answer(self) -> None:
        """收口当前回答段：只换行，不重印正文。"""
        self._close_thinking()
        if self._answer_labeled:
            self.console.print()
            self._answer_labeled = False


async def run_once(
    prompt: str,
    *,
    config: Config,
    session: SessionRuntime,
    store: SessionStore,
    json_events: bool = False,
) -> int:
    """跑一次 `-p`：消费 run_agent 事件并打印；结束前同步等记忆抽取落库。

    输入：用户 prompt；输出：退出码（出现 error 则为 1）。
    """
    console = Console()
    code = 0
    registry = _build_registry(config)
    renderer = _EventRenderer(console)
    try:
        async for event in run_agent(prompt, config=config, session=session, store=store):
            if json_events:
                console.print_json(json.dumps(event, ensure_ascii=False))
            else:
                renderer.render(event)
            if event.get("type") == "error":
                code = 1
    finally:
        _submit_round(registry, session)
        if registry is not None:
            await registry.drain_all()
    if not json_events:
        console.print()
    return code
