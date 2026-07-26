"""内置 coding 工具：只读 / 编辑 / Bash / Todo / Compact。"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from xcode.tools.base import (
    Tool,
    ToolContext,
    ToolResponse,
    failure,
    partial,
    resolve_workspace_path,
    success,
    timed_ms,
)
from xcode.tools.output import spill_large_output

PRIVILEGED_WORDS = {
    "sudo",
    "su",
    "doas",
    "mkfs",
    "fdisk",
    "dd",
    "shutdown",
    "reboot",
    "poweroff",
    "halt",
}


def _shell_command_words(command: str) -> list[str]:
    """提取 shell 命令词（用于校验 / 权限）。"""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    words: list[str] = []
    expecting = True
    for token in tokens:
        if token in {";", "&", "&&", "||", "|", "(", ")"}:
            expecting = True
            continue
        if not expecting:
            continue
        words.append(token)
        expecting = False
    return words


def validate_bash_command(command: str) -> str | None:
    """硬拒绝危险 Bash；返回错误信息或 None。"""
    normalized = " ".join(command.strip().split()).lower()
    if normalized in {"rm -rf /", "rm -rf /*"}:
        return "不允许删除系统根目录"
    for word in _shell_command_words(command):
        if word.lower() in PRIVILEGED_WORDS:
            return f"拒绝特权命令: {word}"
    return None


def resolve_readable_path(ctx: ToolContext, raw: str) -> Path:
    """解析可读路径：workspace 内，或会话 tool-output 下的绝对/相对路径。"""
    try:
        return resolve_workspace_path(ctx.workspace, raw)
    except PermissionError:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = ctx.session_data_dir / path
        path = path.resolve()
        session_root = ctx.session_data_dir.resolve()
        try:
            path.relative_to(session_root)
        except ValueError as exc:
            raise PermissionError(f"path outside workspace/session: {path}") from exc
        return path


# --- 只读：列表 / 匹配 / 搜索 / 读文件 ---


class LSTool(Tool):
    name = "LS"
    description = "List files and directories under a workspace-relative path."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path relative to workspace"},
        },
        "required": ["path"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        started = time.perf_counter()
        path = resolve_workspace_path(ctx.workspace, str(args.get("path") or "."))
        if not path.exists():
            return failure(ctx, args, code="NOT_FOUND", message=f"missing: {path}", time_ms=timed_ms(started))
        if not path.is_dir():
            return failure(ctx, args, code="NOT_A_DIR", message=f"not a directory: {path}", time_ms=timed_ms(started))
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        text = "\n".join(entries) or "(empty)"
        return success(
            ctx,
            args,
            text=text,
            summary=f"{len(entries)} entries",
            time_ms=timed_ms(started),
        )


class GlobTool(Tool):
    name = "Glob"
    description = "Find files matching a glob pattern under workspace."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Optional subdirectory"},
        },
        "required": ["pattern"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        started = time.perf_counter()
        root = resolve_workspace_path(ctx.workspace, str(args.get("path") or "."))
        pattern = str(args["pattern"])
        matches = sorted(str(p.relative_to(ctx.workspace)) for p in root.glob(pattern) if p.is_file())
        matches = matches[:200]
        return success(
            ctx,
            args,
            text="\n".join(matches) or "(none)",
            summary=f"{len(matches)} files",
            time_ms=timed_ms(started),
        )


class GrepTool(Tool):
    name = "Grep"
    description = "Search file contents with a regex pattern."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "glob": {"type": "string"},
        },
        "required": ["pattern"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        started = time.perf_counter()
        root = resolve_workspace_path(ctx.workspace, str(args.get("path") or "."))
        pattern = re.compile(str(args["pattern"]))
        file_glob = str(args.get("glob") or "**/*")
        hits: list[str] = []
        paths = [root] if root.is_file() else list(root.glob(file_glob))
        for path in paths:
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    rel = path.relative_to(ctx.workspace)
                    hits.append(f"{rel}:{i}:{line}")
                    if len(hits) >= 100:
                        break
            if len(hits) >= 100:
                break
        return success(
            ctx,
            args,
            text="\n".join(hits) or "(none)",
            summary=f"{len(hits)} hits",
            time_ms=timed_ms(started),
        )


class ReadTool(Tool):
    name = "Read"
    description = "Read a text file with optional line range. Records a snapshot for Edit/Write locking."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "1-based start line"},
            "limit": {"type": "integer"},
        },
        "required": ["path"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        started = time.perf_counter()
        try:
            path = resolve_readable_path(ctx, str(args["path"]))
        except PermissionError as exc:
            return failure(ctx, args, code="PATH_DENIED", message=str(exc), time_ms=timed_ms(started))
        if not path.is_file():
            return failure(ctx, args, code="NOT_A_FILE", message=f"not a file: {path}", time_ms=timed_ms(started))
        head = path.read_bytes()[:8192]
        if b"\x00" in head:
            return failure(ctx, args, code="BINARY_FILE", message=f"binary file: {path}", time_ms=timed_ms(started))
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        # 仅 workspace 内文件登记乐观锁；会话 spill 文件不锁
        try:
            path.relative_to(ctx.workspace.resolve())
            ctx.remember_snapshot(path)
        except ValueError:
            pass
        offset = max(int(args.get("offset") or 1), 1)
        limit = int(args["limit"]) if args.get("limit") is not None else len(lines)
        chunk = lines[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{offset + i}|{line}" for i, line in enumerate(chunk))
        return success(
            ctx,
            args,
            text=numbered,
            summary=f"{len(chunk)} lines",
            time_ms=timed_ms(started),
        )


# --- 写入：Edit / Write ---


class EditTool(Tool):
    name = "Edit"
    description = "Replace an exact string occurrence in a file. Uses Read snapshot as optimistic lock."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        started = time.perf_counter()
        path = resolve_workspace_path(ctx.workspace, str(args["path"]))
        if not path.is_file():
            return failure(ctx, args, code="NOT_A_FILE", message=f"not a file: {path}", time_ms=timed_ms(started))
        lock_err = ctx.check_lock(path)
        if lock_err:
            return failure(ctx, args, code="FILE_CHANGED", message=lock_err, time_ms=timed_ms(started))
        text = path.read_text(encoding="utf-8")
        old, new = str(args["old_string"]), str(args["new_string"])
        if old not in text:
            return failure(ctx, args, code="NOT_FOUND", message="old_string not found", time_ms=timed_ms(started))
        if args.get("replace_all"):
            updated = text.replace(old, new)
            count = text.count(old)
        else:
            if text.count(old) > 1:
                return failure(
                    ctx,
                    args,
                    code="NOT_UNIQUE",
                    message="old_string is not unique; set replace_all",
                    time_ms=timed_ms(started),
                )
            updated = text.replace(old, new, 1)
            count = 1
        path.write_text(updated, encoding="utf-8")
        ctx.remember_snapshot(path)
        return success(
            ctx,
            args,
            text="",
            summary=f"replaced {count} occurrence(s)",
            time_ms=timed_ms(started),
        )


class WriteTool(Tool):
    name = "Write"
    description = "Create or overwrite a text file. Uses Read snapshot as optimistic lock when present."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        started = time.perf_counter()
        path = resolve_workspace_path(ctx.workspace, str(args["path"]))
        if path.exists():
            lock_err = ctx.check_lock(path)
            if lock_err:
                return failure(ctx, args, code="FILE_CHANGED", message=lock_err, time_ms=timed_ms(started))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args["content"]), encoding="utf-8")
        ctx.remember_snapshot(path)
        return success(
            ctx,
            args,
            text="",
            summary=f"wrote {path.relative_to(ctx.workspace)}",
            time_ms=timed_ms(started),
        )


# --- 执行：Bash ---


class BashTool(Tool):
    name = "Bash"
    description = "Run a shell command in the workspace directory."
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout_sec": {"type": "integer"},
        },
        "required": ["command"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        started = time.perf_counter()
        command = str(args["command"])
        denied = validate_bash_command(command)
        if denied:
            return failure(ctx, args, code="COMMAND_DENIED", message=denied, time_ms=timed_ms(started))
        if ctx.ask_permission and not ctx.ask_permission(self.name, {"command": command}):
            return failure(ctx, args, code="PERMISSION_DENIED", message="permission denied", time_ms=timed_ms(started))
        timeout = int(args.get("timeout_sec") or 60)
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=ctx.workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return failure(
                ctx,
                args,
                code="TIMEOUT",
                message=f"timeout after {timeout}s",
                time_ms=timed_ms(started),
            )
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        if not out:
            out = "(no output)"
        spill = spill_large_output(
            tool_name=self.name,
            full_output=out,
            session_data_dir=ctx.session_data_dir,
            max_chars=ctx.max_output_chars,
        )
        elapsed = timed_ms(started)
        summary = f"exit {proc.returncode}"
        if spill:
            text = spill.preview + f"\n(full file: {spill.full_path})"
            if proc.returncode == 0:
                return partial(
                    ctx,
                    args,
                    text=text,
                    summary=summary,
                    data={"exit_code": proc.returncode, "spill_path": spill.full_path},
                    time_ms=elapsed,
                )
            return failure(
                ctx,
                args,
                code="EXIT_NONZERO",
                message=summary,
                text=text,
                time_ms=elapsed,
                data={"exit_code": proc.returncode, "spill_path": spill.full_path},
            )
        if proc.returncode != 0:
            return failure(
                ctx,
                args,
                code="EXIT_NONZERO",
                message=summary,
                text=out,
                time_ms=elapsed,
                data={"exit_code": proc.returncode},
            )
        return success(ctx, args, text=out, summary=summary, time_ms=elapsed)


# --- 会话辅助：Todo / Compact ---


class TodoWriteTool(Tool):
    name = "TodoWrite"
    description = "Replace the session todo list."
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        started = time.perf_counter()
        todos = [
            {"content": str(t["content"]), "status": str(t["status"])}
            for t in (args.get("todos") or [])
        ]
        ctx.todos.clear()
        ctx.todos.extend(todos)
        return success(
            ctx,
            args,
            text=json.dumps(todos, ensure_ascii=False),
            summary=f"{len(todos)} todos",
            time_ms=timed_ms(started),
        )


class CompactTool(Tool):
    name = "Compact"
    description = "Request history compaction; runtime will summarize older turns."
    parameters = {
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
        },
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        started = time.perf_counter()
        return success(
            ctx,
            args,
            text="compaction requested",
            summary="compaction requested",
            data={"compact": True, "reason": str(args.get("reason") or "")},
            time_ms=timed_ms(started),
        )


def builtin_tools() -> list[Tool]:
    return [
        LSTool(),
        GlobTool(),
        GrepTool(),
        ReadTool(),
        EditTool(),
        WriteTool(),
        BashTool(),
        TodoWriteTool(),
        CompactTool(),
    ]
