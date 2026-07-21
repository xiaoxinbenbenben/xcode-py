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
from rich.prompt import Confirm

from xcode import __version__
from xcode.config import load_config
from xcode.entrypoints.tui import run_once, start_tui
from xcode.runtime.session import SessionStore

app = typer.Typer(
    name="xcode",
    help="xcode — pure-Python local coding agent (single entry)",
    invoke_without_command=True,
    no_args_is_help=False,
)
session_app = typer.Typer(help="Session management")
app.add_typer(session_app, name="session")
console = Console()


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
    store = SessionStore(config.data_home)
    # --workspace 仅在新建时强制绑定；恢复时以会话内记录为准
    runtime = store.resolve(root, session_id=session_id, new_session=new_session)
    if workspace is not None and new_session:
        runtime.meta.workspace_root = str(root)
        runtime.save()

    def ask(tool_name: str, params: dict) -> bool:
        console.print(f"[yellow]Permission[/] {tool_name}: {params}")
        return Confirm.ask("Allow?", default=True)

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

    asyncio.run(start_tui(config=config, session=runtime, store=store, ask_permission=ask))


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
        "api_key": "configured" if config.api_key else "missing",
        "base_url": config.base_url,
        "model": config.model,
        "cwd": str(root),
        "data_home": str(config.data_home),
        "version": __version__,
    }
    console.print_json(json.dumps(checks, ensure_ascii=False))


@session_app.command("list")
def session_list(
    workspace: Annotated[Optional[Path], typer.Option("--workspace")] = None,
) -> None:
    """列出当前 workspace 下的会话。"""
    root = (workspace or Path.cwd()).resolve()
    config = load_config(project_root=root)
    store = SessionStore(config.data_home)
    items = store.list_sessions(root)
    if not items:
        console.print("(no sessions)")
        return
    for meta in items:
        console.print(
            f"{meta.session_id}\t{meta.name}\t{meta.workspace_root}\t{meta.last_active_at}"
        )


@session_app.command("new")
def session_new(
    workspace: Annotated[Optional[Path], typer.Option("--workspace")] = None,
) -> None:
    """新建会话并打印 id。"""
    root = (workspace or Path.cwd()).resolve()
    config = load_config(project_root=root)
    store = SessionStore(config.data_home)
    runtime = store.create(root)
    console.print(runtime.session_id)
