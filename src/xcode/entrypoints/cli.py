"""统一 CLI 入口：无子命令进 TUI；-p / 子命令走 CLI。"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from xcode import __version__
from xcode.config import load_config
from xcode.entrypoints.tui import run_once, start_tui
from xcode.runtime.session import SessionStore

app = typer.Typer(
    name="xcode",
    help="xcode — local coding agent · 无参进 TUI · -p 单次 · 子命令管理",
    invoke_without_command=True,
    no_args_is_help=False,
)
session_app = typer.Typer(help="Session management")
app.add_typer(session_app, name="session")
console = Console()


def pick_runtime(
    store: SessionStore,
    root: Path,
    *,
    prompt: str | None,
    session_id: str | None,
    new_session: bool,
):
    """TUI 无 --session 时新建空会话；-p / --session 走 resolve。"""
    if prompt is None and session_id is None:
        return store.create(root)
    return store.resolve(root, session_id=session_id, new_session=new_session)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"xcode {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    prompt: Annotated[
        Optional[str],
        typer.Option("-p", "--prompt", help="Print mode: single prompt, then exit"),
    ] = None,
    workspace: Annotated[
        Optional[Path],
        typer.Option("--workspace", help="Workspace root for new/current session"),
    ] = None,
    session_id: Annotated[
        Optional[str],
        typer.Option("--session", help="Restore a specific session id"),
    ] = None,
    new_session: Annotated[
        bool,
        typer.Option("--new-session", help="Force create a new session"),
    ] = False,
    json_events: Annotated[
        bool,
        typer.Option("--json-events", help="Emit runtime events as JSON lines"),
    ] = False,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """无子命令时：有 -p 则单次运行，否则进入交互 TUI。"""
    _ = version
    if ctx.invoked_subcommand is not None:
        return

    root = (workspace or Path.cwd()).resolve()
    config = load_config(project_root=root)
    if not config.api_key:
        console.print(
            "[red]OPENAI_API_KEY 未配置[/]\n"
            "[dim]xcode 只认环境变量/`.env` 里的[/] [bold]OPENAI_API_KEY[/]"
            "[dim]（不是业务项目的 API_KEY）。[/]\n"
            "[dim]可任选：[/]\n"
            "  1) 在 [bold]~/.xcode/.env[/] 写入 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL\n"
            "  2) 在当前目录 `.env` 写入同上\n"
            "  3) shell: export OPENAI_API_KEY=...\n"
            "[dim]自检：[/] uv run xcode doctor"
        )
        raise typer.Exit(2)
    store = SessionStore(
        config.data_home,
        tool_prune_chars=config.tool_prune_chars,
        transcript_hard_cap=config.transcript_hard_cap,
    )
    runtime = pick_runtime(
        store,
        root,
        prompt=prompt,
        session_id=session_id,
        new_session=new_session,
    )
    if workspace is not None and (new_session or (prompt is None and session_id is None)):
        runtime.meta.workspace_root = str(root)
        runtime.save()

    if prompt is not None:
        code = asyncio.run(
            run_once(
                prompt,
                config=config,
                session=runtime,
                store=store,
                json_events=json_events,
            )
        )
        raise typer.Exit(code)

    asyncio.run(start_tui(config=config, session=runtime, store=store))


@app.command("doctor")
def doctor(
    workspace: Annotated[Optional[Path], typer.Option("--workspace")] = None,
) -> None:
    """环境自检。"""
    root = (workspace or Path.cwd()).resolve()
    config = load_config(project_root=root)
    checks = {
        "python": sys.version.split()[0],
        "uv": shutil.which("uv") or "missing",
        "api_key": "configured" if config.api_key else "MISSING — set OPENAI_API_KEY",
        "base_url": config.base_url,
        "model": config.model,
        "light_model": config.light_model,
        "cwd": str(root),
        "data_home": str(config.data_home),
        "version": __version__,
        "hint": (
            "xcode reads OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL from env "
            "or layered .env (cwd, ~/.xcode/.env). Business project API_KEY is ignored."
        ),
    }
    console.print_json(json.dumps(checks, ensure_ascii=False))
    if not config.api_key:
        raise typer.Exit(2)


def _store_from_config(config) -> SessionStore:
    return SessionStore(
        config.data_home,
        tool_prune_chars=config.tool_prune_chars,
        transcript_hard_cap=config.transcript_hard_cap,
    )


@session_app.command("list")
def session_list(
    workspace: Annotated[Optional[Path], typer.Option("--workspace")] = None,
) -> None:
    """列出当前 workspace 下的会话。"""
    root = (workspace or Path.cwd()).resolve()
    config = load_config(project_root=root)
    store = _store_from_config(config)
    items = store.list_sessions(root)
    if not items:
        console.print("(no sessions)")
        return
    from xcode.entrypoints.session_picker import format_session_line, rows_from_metas

    for i, row in enumerate(rows_from_metas(items), start=1):
        console.print(format_session_line(row, index=i))
        if row.preview:
            console.print(f"     {row.preview}")


@session_app.command("new")
def session_new(
    workspace: Annotated[Optional[Path], typer.Option("--workspace")] = None,
) -> None:
    """新建会话并打印 id。"""
    root = (workspace or Path.cwd()).resolve()
    config = load_config(project_root=root)
    store = _store_from_config(config)
    runtime = store.create(root)
    console.print(runtime.session_id)
