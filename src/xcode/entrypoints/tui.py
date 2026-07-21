"""交互层：prompt-toolkit 输入 + rich 渲染 runtime events。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.markdown import Markdown

from xcode import __version__
from xcode.config import Config
from xcode.runtime.agent import run_agent
from xcode.runtime.session import SessionRuntime, SessionStore


async def start_tui(
    *,
    config: Config,
    session: SessionRuntime,
    store: SessionStore,
    ask_permission: Callable[[str, dict[str, Any]], bool] | None = None,
) -> None:
    """启动交互 REPL（即本项目的 Python TUI）。"""
    console = Console()
    history_path = config.data_home / "prompt_history.txt"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    prompt = PromptSession(history=FileHistory(str(history_path)))

    console.print(
        f"[bold]xcode[/] v{__version__}  model={config.model}\n"
        f"workspace={session.workspace_root}\n"
        f"session={session.session_id} ({session.meta.name})\n"
        f"commands: /help /exit /sessions /clear"
    )

    while True:
        try:
            text = await asyncio.to_thread(prompt.prompt, "you> ")
        except (EOFError, KeyboardInterrupt):
            console.print("bye")
            return
        text = (text or "").strip()
        if not text:
            continue
        if text in {"/exit", "/quit"}:
            console.print("bye")
            return
        if text == "/help":
            console.print("Enter 发送；/exit 退出；/sessions 列会话；/clear 清空本地历史视图")
            continue
        if text == "/sessions":
            for meta in store.list_sessions(session.workspace_root):
                mark = "*" if meta.session_id == session.session_id else " "
                console.print(f"{mark} {meta.session_id}  {meta.name}  {meta.last_active_at}")
            continue
        if text == "/clear":
            session.messages.clear()
            session.summary = None
            session.save()
            console.print("cleared")
            continue

        console.print("[dim]agent>[/]")
        async for event in run_agent(
            text,
            config=config,
            session=session,
            store=store,
            ask_permission=ask_permission,
        ):
            _render_event(console, event)
        console.print()


def _render_event(console: Console, event: dict[str, Any]) -> None:
    et = event.get("type")
    payload = event.get("payload") or {}
    if et == "text_delta":
        console.print(payload.get("text", ""), end="")
    elif et == "tool_call":
        console.print(f"\n[cyan]⚙ {payload.get('name')}[/] {payload.get('arguments')}")
    elif et == "tool_result":
        flag = "ok" if payload.get("ok") else "err"
        console.print(f"[dim]↳ {flag}: {payload.get('summary')}[/]")
    elif et == "error":
        console.print(f"\n[red]error:[/] {payload.get('message')}")
    elif et == "run_finished":
        text = payload.get("text") or ""
        if text and not event.get("_streamed"):
            # 若全程无 delta（部分兼容端），补打全文
            pass
    elif et == "compacted":
        console.print(f"\n[dim]compacted ({payload.get('reason')})[/]")


async def run_once(
    prompt: str,
    *,
    config: Config,
    session: SessionRuntime,
    store: SessionStore,
    json_events: bool = False,
) -> int:
    """非交互单次运行；json_events 时打印 JSONL。"""
    console = Console()
    code = 0
    async for event in run_agent(prompt, config=config, session=session, store=store):
        if json_events:
            import json

            console.print_json(json.dumps(event, ensure_ascii=False))
        else:
            _render_event(console, event)
        if event.get("type") == "error":
            code = 1
    if not json_events:
        console.print()
    return code
