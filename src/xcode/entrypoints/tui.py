"""交互层：TUI banner / prompt / slash / 流式渲染。

## 与会话、记忆的接线（读 slash 与主循环时对照）
- 启动：外部传入已 resolve 的 SessionRuntime（transcript/context 已 load）
- 每轮用户输入非 slash → run_agent（内部 append_message + 可能 compact）
- 一轮结束后 → _submit_round：slice_round → MemoryPipeline.submit（后台 stage1/2）
- 退出 finally → registry.drain_all：尽量把记忆队列刷完
- /compact：强制会话压缩；/memory *：读或 clear 长期记忆；无 /clear 会话
"""

from __future__ import annotations

import json
from typing import Any, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
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
from xcode.memory import MemoryStore, PipelineRegistry, should_extract, slice_round
from xcode.runtime.agent import build_registry, run_agent, run_compact
from xcode.runtime.session import SessionRuntime, SessionStore
from xcode.runtime.tokens import count_messages_tokens, format_token_usage

SLASH_COMMANDS = [
    "/help",
    "/exit",
    "/quit",
    "/sessions",
    "/compact",
    "/tools",
    "/status",
    "/memory",
]

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
    """Enter 发送；Esc+Enter 换行。"""
    keys = KeyBindings()

    @keys.add("enter")
    def _(event) -> None:
        event.current_buffer.validate_and_handle()

    @keys.add("escape", "enter")
    def _(event) -> None:
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
        Text("  /help  ·  Enter 发送  ·  Esc+Enter 换行  ·  Ctrl-C 退出", style="dim #8b9aab")
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
    registry = _build_registry(config)

    prompt = PromptSession(
        history=FileHistory(str(history_path)),
        completer=WordCompleter(SLASH_COMMANDS, sentence=True),
        key_bindings=_build_key_bindings(),
        multiline=True,
        style=_PT_STYLE,
        bottom_toolbar=_toolbar(config, session, turns),
        placeholder=[("class:placeholder", "描述任务，或 /help…")],
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
                should_exit = await _handle_slash(
                    text,
                    console=console,
                    config=config,
                    session=session,
                    store=store,
                    memory_registry=registry,
                )
                if should_exit:
                    console.print(Text("bye", style="dim"))
                    return
                continue

            console.print(Text("── agent ──", style="dim #2a9b68"))

            async def _ask_permission(text: str) -> bool:
                """审批回调（传给 run_agent）：仅高危工具（requires_approval）
                执行前由运行时调用；TUI 内 y/N 确认，Ctrl-C 视为拒绝。"""
                try:
                    answer = await prompt.prompt_async(
                        HTML(f"<b><style fg='#ffb454'>⚠ {text} [y/N] </style></b>")
                    )
                except (EOFError, KeyboardInterrupt):
                    return False
                return answer.strip().lower() in {"y", "yes"}

            async for event in run_agent(
                text,
                config=config,
                session=session,
                store=store,
                ask_permission=_ask_permission,
            ):
                _render_event(console, event)
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


async def _handle_slash(
    text: str,
    *,
    console: Console,
    config: Config,
    session: SessionRuntime,
    store: SessionStore,
    memory_registry: PipelineRegistry | None = None,
) -> bool:
    """处理 slash；返回 True 表示退出。"""
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower()
    if cmd in {"/exit", "/quit"}:
        return True
    if cmd == "/help":
        console.print(Markdown(
            "### commands\n"
            + "\n".join(f"- `{c}`" for c in SLASH_COMMANDS)
            + "\n\nEnter 发送 · Esc+Enter 换行"
        ))
        return False
    if cmd == "/sessions":
        for meta in store.list_sessions(session.workspace_root):
            mark = "●" if meta.session_id == session.session_id else "○"
            console.print(f"  {mark} {meta.session_id}  {meta.name}  [dim]{meta.last_active_at}[/]")
        return False
    if cmd == "/clear":
        console.print(
            "[dim]v1 无 /clear；请退出后用[/] [bold]--new-session[/] [dim]开新会话[/]"
        )
        return False
    if cmd == "/compact":
        if not session.messages:
            console.print("[dim]nothing to compact[/]")
            return False
        console.print("[dim]compacting…[/]")
        try:
            summary = await run_compact(session, config=config)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]compact failed:[/] {exc}")
            return False
        preview = summary if len(summary) <= 240 else summary[:237] + "…"
        console.print(f"[dim]compacted · {len(session.messages)} msgs in window[/]")
        console.print(Text(preview, style="dim"))
        return False
    if cmd == "/tools":
        names = build_registry(session.workspace_root).list_names()
        console.print(", ".join(names) if names else "(none)")
        return False
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
        return False
    if cmd == "/memory":
        return _handle_memory(
            arg,
            console=console,
            config=config,
            session=session,
            memory_registry=memory_registry,
        )
    console.print(f"[yellow]unknown command:[/] {cmd}  (try /help)")
    _ = arg
    return False


def _handle_memory(
    arg: str,
    *,
    console: Console,
    config: Config,
    session: SessionRuntime,
    memory_registry: PipelineRegistry | None = None,
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
        console.print(store.read_summary() or "(empty summary)")
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
        if memory_registry is not None:
            memory_registry.clear_workspace(session.workspace_root)
        else:
            store.clear()
        console.print("[dim]cleared memories (pipeline invalidated, templates restored)[/]")
        return False
    console.print(
        "[yellow]usage:[/] /memory [summary|path|show memory|show summary|grep <q>|clear]\n"
        "[dim]memories are generated; put project conventions in XCODE.md[/]"
    )
    return False


def _render_event(console: Console, event: dict[str, Any]) -> None:
    """把一条产品事件画到终端。

    输入：扁平事件 dict；副作用：向 console 打印（text/thinking 不换行）。
    """
    et = event.get("type")
    if et == "text_delta":
        console.print(event.get("text", ""), end="")
    elif et == "thinking_delta":
        console.print(Text(str(event.get("thinking") or ""), style="dim italic"), end="")
    elif et == "tool_call":
        args = event.get("input") or {}
        preview = json.dumps(args, ensure_ascii=False)
        if len(preview) > 120:
            preview = preview[:117] + "…"
        console.print()
        console.print(Text(f"⚙ {event.get('name')}", style="bold #5dffa8"), end=" ")
        console.print(Text(preview, style="dim"))
    elif et == "tool_result":
        is_error = bool(event.get("is_error"))
        flag = "err" if is_error else "ok"
        style = "#ffb454" if is_error else "#2a9b68"
        result = str(event.get("result") or "")
        summary = result if len(result) <= 160 else result[:157] + "…"
        console.print(Text(f"  ↳ {flag}: {summary}", style=style))
    elif et == "error":
        console.print()
        console.print(Text(f"error: {event.get('error')}", style="bold red"))
    elif et == "done":
        console.print()


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
    try:
        async for event in run_agent(prompt, config=config, session=session, store=store):
            if json_events:
                console.print_json(json.dumps(event, ensure_ascii=False))
            else:
                _render_event(console, event)
            if event.get("type") == "error":
                code = 1
    finally:
        _submit_round(registry, session)
        if registry is not None:
            await registry.drain_all()
    if not json_events:
        console.print()
    return code
