"""内置 coding 工具：只读 / 编辑 / Bash / Todo / Compact。"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from xcode.tools.base import Tool, ToolContext, ToolResult, resolve_workspace_path


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

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_workspace_path(ctx.workspace, str(args.get("path") or "."))
        if not path.exists():
            return ToolResult(ok=False, summary=f"missing: {path}")
        if not path.is_dir():
            return ToolResult(ok=False, summary=f"not a directory: {path}")
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        text = "\n".join(entries) or "(empty)"
        return ToolResult(ok=True, summary=f"{len(entries)} entries", content=text)


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

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = resolve_workspace_path(ctx.workspace, str(args.get("path") or "."))
        pattern = str(args["pattern"])
        matches = sorted(str(p.relative_to(ctx.workspace)) for p in root.glob(pattern) if p.is_file())
        # 限制结果量，避免刷爆上下文
        matches = matches[:200]
        return ToolResult(
            ok=True,
            summary=f"{len(matches)} files",
            content="\n".join(matches) or "(none)",
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

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
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
        return ToolResult(ok=True, summary=f"{len(hits)} hits", content="\n".join(hits) or "(none)")


class ReadTool(Tool):
    name = "Read"
    description = "Read a text file with optional line range."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "1-based start line"},
            "limit": {"type": "integer"},
        },
        "required": ["path"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_workspace_path(ctx.workspace, str(args["path"]))
        if not path.is_file():
            return ToolResult(ok=False, summary=f"not a file: {path}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        offset = max(int(args.get("offset") or 1), 1)
        limit = int(args["limit"]) if args.get("limit") is not None else len(lines)
        chunk = lines[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{offset + i}|{line}" for i, line in enumerate(chunk))
        return ToolResult(ok=True, summary=f"{len(chunk)} lines", content=numbered)


class EditTool(Tool):
    name = "Edit"
    description = "Replace an exact string occurrence in a file."
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

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_workspace_path(ctx.workspace, str(args["path"]))
        if not path.is_file():
            return ToolResult(ok=False, summary=f"not a file: {path}")
        text = path.read_text(encoding="utf-8")
        old, new = str(args["old_string"]), str(args["new_string"])
        if old not in text:
            return ToolResult(ok=False, summary="old_string not found")
        if args.get("replace_all"):
            updated = text.replace(old, new)
            count = text.count(old)
        else:
            if text.count(old) > 1:
                return ToolResult(ok=False, summary="old_string is not unique; set replace_all")
            updated = text.replace(old, new, 1)
            count = 1
        path.write_text(updated, encoding="utf-8")
        return ToolResult(ok=True, summary=f"replaced {count} occurrence(s)")


class WriteTool(Tool):
    name = "Write"
    description = "Create or overwrite a text file."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_workspace_path(ctx.workspace, str(args["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args["content"]), encoding="utf-8")
        return ToolResult(ok=True, summary=f"wrote {path.relative_to(ctx.workspace)}")


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

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args["command"])
        if ctx.ask_permission and not ctx.ask_permission(self.name, {"command": command}):
            return ToolResult(ok=False, summary="permission denied")
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
            return ToolResult(ok=False, summary=f"timeout after {timeout}s")
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        truncated = len(out) > ctx.max_output_chars
        if truncated:
            out = out[: ctx.max_output_chars] + "\n…[truncated]"
        return ToolResult(
            ok=proc.returncode == 0,
            summary=f"exit {proc.returncode}",
            content=out or "(no output)",
            truncated=truncated,
        )


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

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        todos = [
            {"content": str(t["content"]), "status": str(t["status"])}
            for t in (args.get("todos") or [])
        ]
        ctx.todos.clear()
        ctx.todos.extend(todos)
        return ToolResult(ok=True, summary=f"{len(todos)} todos", content=json.dumps(todos, ensure_ascii=False))


class CompactTool(Tool):
    name = "Compact"
    description = "Request history compaction; runtime will summarize older turns."
    parameters = {
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
        },
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # 真正压缩由 agent loop 检测 data.flag 后执行；此处只做信号。
        return ToolResult(
            ok=True,
            summary="compaction requested",
            data={"compact": True, "reason": str(args.get("reason") or "")},
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
