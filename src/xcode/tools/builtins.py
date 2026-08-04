"""内置工具表（todo #7 按目标清单回填）。

所有 execute 均为 async（供子进程 / HTTP 类工具复用）。
已实现：read_file / write_file / edit_file / list_dir / glob / grep / bash / web_search / web_fetch
待回填：save_memory / load_skill / search_code / revert_turn
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from xcode.tools.base import Tool, ToolContext, ToolResult, resolve_workspace_path
from xcode.web import fetch_url, search_web

_DEFAULT_LIMIT = 500
_MAX_LIMIT = 2000
_MAX_WRITE_BYTES = 5 * 1024 * 1024
_DEFAULT_HIDDEN_BASENAMES = {".git", "node_modules", "__pycache__", ".venv"}
_PER_FILE_MATCH_LIMIT = 50
_MAX_GREP_FILE_BYTES = 1 * 1024 * 1024

_BASH_TIMEOUT_DEFAULT = 30.0
_BASH_TIMEOUT_MAX = 120.0
_BASH_OUTPUT_MAX = 20_000
# 极简危险命令黑名单（#8 才正式化 CommandGuard；此处只拦明显灾难级）
_BASH_DENY_RULES: list[tuple[str, str]] = [
    ("sudo", r"\bsudo\b"),
    ("mkfs", r"\bmkfs(?:\s|\.|$)"),
    ("dd", r"\bdd\b"),
    ("root-rm", r"\brm\s+-[a-z]*r[a-z]*\s+/\s*\*?\s*$"),
    ("raw-disk-write", r">\s*/dev/sd[a-z]*"),
    ("curl-pipe-sh", r"(?:curl|wget)\b[^|]*\|\s*(?:ba)?sh\b"),
    ("fork-bomb", r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;?\s*:"),
    ("shutdown", r"\b(?:shutdown|reboot|poweroff|halt)\b"),
]


def _bash_denied(command: str) -> str | None:
    """命中黑名单返回规则名，否则 None。"""
    for rule_name, pattern in _BASH_DENY_RULES:
        if re.search(pattern, command):
            return rule_name
    return None


def _require_path(args: dict[str, Any], tool_name: str) -> str | ToolResult:
    """取出非空 path；不合法则返回错误 ToolResult。"""
    raw_path = args.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return ToolResult(f"{tool_name} error: path is required", is_error=True)
    return raw_path


def _resolved_path(ctx: ToolContext, raw_path: str, tool_name: str):
    """解析到 workspace 内；越界返回错误 ToolResult。"""
    try:
        return resolve_workspace_path(ctx.workspace, raw_path)
    except PermissionError as exc:
        return ToolResult(f"{tool_name} error: {exc}", is_error=True)


def _rel(ctx: ToolContext, path) -> str:
    """workspace 相对路径，供成功提示。"""
    return path.relative_to(ctx.workspace.resolve()).as_posix()


def _has_ignored_segment(rel: Path) -> bool:
    """相对路径任一段是否属于默认隐藏名。"""
    return any(part in _DEFAULT_HIDDEN_BASENAMES for part in rel.parts)

class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a text file from the current workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to read"},
            "offset": {
                "type": "number",
                "description": "Start line, 1-based (default 1)",
            },
            "limit": {
                "type": "number",
                "description": f"Maximum number of lines (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT})",
            },
        },
        "required": ["path"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """读 workspace 内文本文件；输出带行号。"""
        raw = _require_path(args, self.name)
        if isinstance(raw, ToolResult):
            return raw

        path = _resolved_path(ctx, raw, self.name)
        if isinstance(path, ToolResult):
            return path

        if not path.exists():
            return ToolResult(f"read_file error: file not found: {raw}", is_error=True)
        if path.is_dir():
            return ToolResult(f"read_file error: not a file: {raw}", is_error=True)

        offset = max(int(args.get("offset") or 1), 1)
        limit = int(args.get("limit") or _DEFAULT_LIMIT)
        limit = max(1, min(limit, _MAX_LIMIT))

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return ToolResult(f"read_file error: {exc}", is_error=True)

        selected = lines[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{idx + offset}: {line}" for idx, line in enumerate(selected))
        return ToolResult(numbered)


class WriteFileTool(Tool):
    name = "write_file"
    description = "Write a UTF-8 text file inside the current workspace (overwrite or append)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to write"},
            "content": {"type": "string", "description": "File content"},
            "append": {
                "type": "boolean",
                "description": "Append instead of overwrite (default false)",
            },
        },
        "required": ["path", "content"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """整文件写入或追加；自动创建父目录。"""
        raw = _require_path(args, self.name)
        if isinstance(raw, ToolResult):
            return raw
        # 校验 content 以及防止其超过 5MB
        if "content" not in args or not isinstance(args.get("content"), str):
            return ToolResult("write_file error: content is required", is_error=True)
        content = args["content"]
        if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
            return ToolResult("write_file error: content exceeds 5MB", is_error=True)

        path = _resolved_path(ctx, raw, self.name)
        if isinstance(path, ToolResult):
            return path
        if path.exists() and path.is_dir():
            return ToolResult(f"write_file error: is a directory: {raw}", is_error=True)

        append = bool(args.get("append"))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a" if append else "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            return ToolResult(f"write_file error: {exc}", is_error=True)

        return ToolResult(f"Wrote {_rel(ctx, path)}")


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Replace an exact unique string in a workspace file. "
        "old_string must match exactly once."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to edit"},
            "old_string": {"type": "string", "description": "Exact text to find (must be unique)"},
            "new_string": {"type": "string", "description": "Replacement text"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """精确替换一段唯一原文；0 次或多次匹配均拒绝。"""
        raw = _require_path(args, self.name)
        if isinstance(raw, ToolResult):
            return raw

        old = args.get("old_string")
        new = args.get("new_string")
        if not isinstance(old, str) or old == "":
            return ToolResult("edit_file error: old_string is required and must be non-empty", is_error=True)
        if not isinstance(new, str):
            return ToolResult("edit_file error: new_string is required", is_error=True)

        path = _resolved_path(ctx, raw, self.name)
        if isinstance(path, ToolResult):
            return path
        if not path.exists():
            return ToolResult(f"edit_file error: file not found: {raw}", is_error=True)
        if path.is_dir():
            return ToolResult(f"edit_file error: not a file: {raw}", is_error=True)

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(f"edit_file error: {exc}", is_error=True)

        count = text.count(old)
        if count == 0:
            return ToolResult(
                "edit_file error: old_string not found; re-read the file and use exact text",
                is_error=True,
            )
        if count > 1:
            return ToolResult(
                f"edit_file error: old_string matched {count} times; make it longer/more unique",
                is_error=True,
            )

        try:
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
        except OSError as exc:
            return ToolResult(f"edit_file error: {exc}", is_error=True)

        return ToolResult(f"Updated {_rel(ctx, path)}")


class ListDirTool(Tool):
    name = "list_dir"
    description = (
        "List entries in a directory inside the current workspace. "
        "Hides .git, node_modules, __pycache__, and .venv unless include_ignored=true."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path"},
            "include_ignored": {
                "type": "boolean",
                "description": "Include .git, node_modules, __pycache__, and .venv (default false)",
            },
            "limit": {
                "type": "number",
                "description": f"Maximum entries (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT})",
            },
        },
        "required": ["path"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """列出 workspace 内目录的一层条目；不递归，超出 limit 时提示剩余数量。"""
        raw = _require_path(args, self.name)
        if isinstance(raw, ToolResult):
            return raw

        path = _resolved_path(ctx, raw, self.name)
        if isinstance(path, ToolResult):
            return path
        if not path.exists():
            return ToolResult(f"list_dir error: directory not found: {raw}", is_error=True)
        if not path.is_dir():
            return ToolResult(f"list_dir error: not a directory: {raw}", is_error=True)

        include_ignored = args.get("include_ignored", False)
        if not isinstance(include_ignored, bool):
            return ToolResult("list_dir error: include_ignored must be a boolean", is_error=True)

        try:
            limit = int(args.get("limit") or _DEFAULT_LIMIT)
        except (TypeError, ValueError):
            return ToolResult("list_dir error: limit must be a number", is_error=True)
        limit = max(1, min(limit, _MAX_LIMIT))

        try:
            entries = [
                (child, child.is_dir())
                for child in path.iterdir()
                if include_ignored or child.name not in _DEFAULT_HIDDEN_BASENAMES
            ]
        except OSError as exc:
            return ToolResult(f"list_dir error: {exc}", is_error=True)

        entries.sort(key=lambda item: (not item[1], item[0].name.lower(), item[0].name))
        visible = entries[:limit]
        rows = [f"{child.name}{'/' if is_dir else ''}" for child, is_dir in visible]
        if len(entries) > limit:
            rows.append(f"... {len(entries) - limit} more entries not shown")
        return ToolResult("\n".join(rows) or "(empty directory)")


class GlobTool(Tool):
    name = "glob"
    description = (
        "Find files by glob pattern inside the current workspace. "
        "Returns files only. Skips paths under .git, node_modules, __pycache__, "
        "and .venv unless include_ignored=true."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern relative to workspace"},
            "include_ignored": {
                "type": "boolean",
                "description": "Include matches under .git, node_modules, __pycache__, .venv (default false)",
            },
            "limit": {
                "type": "number",
                "description": f"Maximum results (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT})",
            },
        },
        "required": ["pattern"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """按 pattern 递归找文件；过滤噪音路径；截断时提示剩余数。"""
        # --- 1) 校验 pattern / 开关 / limit ---
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return ToolResult("glob error: pattern is required", is_error=True)
        pattern = pattern.strip()
        pattern_path = Path(pattern)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            return ToolResult(
                "glob error: pattern must stay inside workspace (no absolute path or ..)",
                is_error=True,
            )

        include_ignored = args.get("include_ignored", False)
        if not isinstance(include_ignored, bool):
            return ToolResult("glob error: include_ignored must be a boolean", is_error=True)

        try:
            limit = int(args.get("limit") or _DEFAULT_LIMIT)
        except (TypeError, ValueError):
            return ToolResult("glob error: limit must be a number", is_error=True)
        limit = max(1, min(limit, _MAX_LIMIT))

        # --- 2) 匹配 → 仅文件 → 过滤噪音 / 越界 ---
        root = ctx.workspace.resolve()
        rels: list[str] = []
        try:
            for match in root.glob(pattern):
                try:
                    resolved = match.resolve()
                    rel = resolved.relative_to(root)
                except (OSError, ValueError):
                    continue
                if not resolved.is_file():
                    continue
                if not include_ignored and _has_ignored_segment(rel):
                    continue
                rels.append(rel.as_posix())
        except OSError as exc:
            return ToolResult(f"glob error: {exc}", is_error=True)

        # --- 3) 排序与截断 ---
        rels.sort(key=lambda item: item.lower())
        visible = rels[:limit]
        if len(rels) > limit:
            visible = [*visible, f"... {len(rels) - limit} more entries not shown"]
        return ToolResult("\n".join(visible) or "(no matches)")


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search text in workspace files with a regex pattern. "
        "Skips .git, node_modules, __pycache__, and .venv unless include_ignored=true."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to match"},
            "path": {
                "type": "string",
                "description": "File or directory to search (default workspace root)",
            },
            "include_ignored": {
                "type": "boolean",
                "description": "Include paths under .git, node_modules, __pycache__, .venv (default false)",
            },
            "limit": {
                "type": "number",
                "description": f"Maximum matches (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT})",
            },
        },
        "required": ["pattern"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """按正则全树搜文本；单文件命中截断到 50 条，全局按 limit 截断。"""
        # --- 1) 校验 pattern / path / 开关 / limit ---
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return ToolResult("grep error: pattern is required", is_error=True)
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return ToolResult(f"grep error: invalid regex: {exc}", is_error=True)

        raw_path = args.get("path") or "."
        if not isinstance(raw_path, str) or not raw_path.strip():
            return ToolResult("grep error: path must be a non-empty string", is_error=True)
        start = _resolved_path(ctx, raw_path, self.name)
        if isinstance(start, ToolResult):
            return start
        if not start.exists():
            return ToolResult(f"grep error: path not found: {raw_path}", is_error=True)

        include_ignored = args.get("include_ignored", False)
        if not isinstance(include_ignored, bool):
            return ToolResult("grep error: include_ignored must be a boolean", is_error=True)

        try:
            limit = int(args.get("limit") or _DEFAULT_LIMIT)
        except (TypeError, ValueError):
            return ToolResult("grep error: limit must be a number", is_error=True)
        limit = max(1, min(limit, _MAX_LIMIT))

        # --- 2) 收文件列表：单文件直搜，目录 rglob 过滤隐藏段（尊重显式 path）---
        root = ctx.workspace.resolve()
        if start.is_file():
            files = [start]
        else:
            files = []
            for p in start.rglob("*"):
                if not p.is_file():
                    continue
                if not include_ignored and _has_ignored_segment(p.relative_to(start)):
                    continue
                files.append(p)
        files.sort(key=lambda p: p.relative_to(root).as_posix().lower())

        # --- 3) 逐文件正则搜索，每文件最多 _PER_FILE_MATCH_LIMIT 条 + 溢出提示 ---
        rows: list[str] = []
        for file_path in files:
            try:
                if file_path.stat().st_size > _MAX_GREP_FILE_BYTES:
                    continue
                lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel = file_path.relative_to(root).as_posix()
            file_rows: list[str] = []
            extra = 0
            for lineno, line in enumerate(lines, start=1):
                if compiled.search(line):
                    if len(file_rows) < _PER_FILE_MATCH_LIMIT:
                        file_rows.append(f"{rel}:{lineno}: {line.strip()}")
                    else:
                        extra += 1
            if extra:
                file_rows.append(f"... {extra} more matches in this file")
            rows.extend(file_rows)

        # --- 4) 全局截断收尾 ---
        if len(rows) > limit:
            rows = rows[:limit] + [f"... {len(rows) - limit} more matches not shown"]
        return ToolResult("\n".join(rows) or "(no matches)")


class BashTool(Tool):
    name = "bash"
    description = (
        "Execute a shell command in the current workspace. "
        "Captures stdout and stderr; the workspace is the working directory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout": {
                "type": "number",
                "description": f"Timeout in seconds (default {_BASH_TIMEOUT_DEFAULT:.0f}, max {_BASH_TIMEOUT_MAX:.0f})",
            },
        },
        "required": ["command"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """在 workspace 里跑一条 shell 命令；截断输出、超时杀进程。"""
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult("bash error: command is required", is_error=True)
        denied = _bash_denied(command)
        if denied is not None:
            return ToolResult(
                f"bash error: command blocked by deny-list ({denied})", is_error=True
            )

        try:
            timeout = float(args.get("timeout") or _BASH_TIMEOUT_DEFAULT)
        except (TypeError, ValueError):
            return ToolResult("bash error: timeout must be a number", is_error=True)
        timeout = max(1.0, min(timeout, _BASH_TIMEOUT_MAX))

        # --- 1) 起子进程，超时则杀 ---
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=ctx.workspace.resolve(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(
                f"bash error: command timed out after {timeout:.0f}s", is_error=True
            )

        # --- 2) 收输出：截断 + 无输出兜底 ---
        output = (stdout + stderr).decode("utf-8", errors="replace")
        if len(output) > _BASH_OUTPUT_MAX:
            output = output[:_BASH_OUTPUT_MAX] + "\n... [output truncated]"
        if not output:
            output = f"(exit {proc.returncode}, no output)"
        return ToolResult(output, is_error=proc.returncode != 0)


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web for current information. Returns titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {
                "type": "number",
                "description": "Maximum result count (default 5, max 20)",
            },
        },
        "required": ["query"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """DDG 检索，输出编号的 标题/链接/摘要 块。"""
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult("web_search error: query is required", is_error=True)
        try:
            max_results = int(args.get("max_results") or 5)
        except (TypeError, ValueError):
            return ToolResult("web_search error: max_results must be a number", is_error=True)
        max_results = max(1, min(max_results, 20))

        try:
            results = await search_web(query, max_results=max_results)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(f"web_search error: {exc}", is_error=True)
        if not results:
            return ToolResult(f'No search results found for "{query}".')
        blocks = [
            f"{idx}. {r.title}\n{r.url}\n{r.snippet}"
            for idx, r in enumerate(results, start=1)
        ]
        return ToolResult("\n\n".join(blocks))


class WebFetchTool(Tool):
    name = "web_fetch"
    description = "Fetch a public HTTP/HTTPS page and return readable text."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "max_length": {
                "type": "number",
                "description": "Maximum returned characters (default 10000)",
            },
        },
        "required": ["url"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """抓取公网页面，HTML 转纯文本，按 max_length 截断。"""
        url = args.get("url")
        if not isinstance(url, str) or not url.strip():
            return ToolResult("web_fetch error: url is required", is_error=True)
        try:
            max_length = int(args.get("max_length") or 10_000)
        except (TypeError, ValueError):
            return ToolResult("web_fetch error: max_length must be a number", is_error=True)
        max_length = max(1, min(max_length, 100_000))

        try:
            text = await fetch_url(url, max_length=max_length)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(f"web_fetch error: {exc}", is_error=True)
        return ToolResult(text)


def builtin_tools() -> list[Tool]:
    """返回当前已实现的内置工具。"""
    return [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListDirTool(),
        GlobTool(),
        GrepTool(),
        BashTool(),
        WebSearchTool(),
        WebFetchTool(),
    ]
