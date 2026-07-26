"""交互层：简约大气的终端体验（banner / prompt / slash / 流式渲染）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
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

from xcode import __version__
from xcode.config import Config
from xcode.runtime.agent import build_registry, run_agent
from xcode.runtime.session import SessionRuntime, SessionStore

SLASH_COMMANDS = [
    "/help",
    "/exit",
    "/quit",
    "/sessions",
    "/clear",
    "/tools",
    "/status",
    "/compact",
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
            f"{session.session_id[-8:]}"
            f"</style>"
        )

    return _get


async def start_tui(
    *,
    config: Config,
    session: SessionRuntime,
    store: SessionStore,
    ask_permission: Callable[[str, dict[str, Any]], bool] | None = None,
) -> None:
    """启动交互 REPL：读输入 → slash 或 run_agent → 渲染事件。"""
    # --- 1) 终端与输入控件 ---
    console = Console()
    history_path = config.data_home / "prompt_history.txt"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    turns = sum(1 for m in session.messages if m.get("role") == "user")

    _banner(console, config=config, session=session)

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
                text, console=console, config=config, session=session, store=store
            )
            if should_exit:
                console.print(Text("bye", style="dim"))
                return
            continue

        console.print(Text("── agent ──", style="dim #2a9b68"))
        async for event in run_agent(
            text,
            config=config,
            session=session,
            store=store,
            ask_permission=ask_permission,
        ):
            _render_event(console, event)
        turns = sum(1 for m in session.messages if m.get("role") == "user")
        prompt.bottom_toolbar = _toolbar(config, session, turns)
        console.print()


async def _handle_slash(
    text: str,
    *,
    console: Console,
    config: Config,
    session: SessionRuntime,
    store: SessionStore,
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
        session.messages.clear()
        session.summary = None
        session.save()
        console.print("[dim]cleared conversation[/]")
        return False
    if cmd == "/tools":
        names = build_registry(session.workspace_root).list_names()
        console.print(", ".join(names))
        return False
    if cmd == "/status":
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_row("model", config.model)
        table.add_row("session", session.session_id)
        table.add_row("messages", str(len(session.messages)))
        table.add_row("todos", str(len(session.todos)))
        table.add_row("snapshots", str(len(session.snapshots)))
        table.add_row("summary", "yes" if session.summary else "no")
        console.print(table)
        return False
    if cmd == "/compact":
        from xcode.context.compaction import compact_messages

        session.messages, session.summary = compact_messages(
            session.messages, existing_summary=session.summary
        )
        session.save()
        console.print(f"[dim]compacted → {len(session.messages)} messages kept[/]")
        return False
    console.print(f"[yellow]unknown command:[/] {cmd}  (try /help)")
    _ = arg
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
    """跑一次 `-p`：消费 run_agent 事件并打印。

    输入：用户 prompt；输出：退出码（出现 error 则为 1）。
    """
    console = Console()
    code = 0
    async for event in run_agent(prompt, config=config, session=session, store=store):
        if json_events:
            console.print_json(json.dumps(event, ensure_ascii=False))
        else:
            _render_event(console, event)
        if event.get("type") == "error":
            code = 1
    if not json_events:
        console.print()
    return code
