"""工具基类、统一响应协议与文件 snapshot。"""

from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

ToolStatus = Literal["success", "partial", "error"]


@dataclass(slots=True)
class FileSnapshot:
    """文件乐观锁快照：mtime + size + 内容哈希（防同尺寸同毫秒篡改）。"""

    mtime_ms: int
    size_bytes: int
    sha1: str

    def as_dict(self) -> dict[str, Any]:
        return {"mtime_ms": self.mtime_ms, "size_bytes": self.size_bytes, "sha1": self.sha1}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileSnapshot:
        return cls(
            mtime_ms=int(data["mtime_ms"]),
            size_bytes=int(data["size_bytes"]),
            sha1=str(data.get("sha1") or ""),
        )

    @classmethod
    def of(cls, path: Path) -> FileSnapshot:
        """读取磁盘上路径的当前 snapshot。"""
        raw = path.read_bytes()
        st = path.stat()
        return cls(
            mtime_ms=int(st.st_mtime_ns // 1_000_000),
            size_bytes=st.st_size,
            sha1=hashlib.sha1(raw).hexdigest(),
        )


@dataclass(slots=True)
class ToolResponse:
    """工具统一信封（对齐 xx-coding 思路，字段更干净）。

    status: success | partial | error
    text: 给模型看的主文本
    data: 结构化载荷（summary / truncated / compact 等）
    stats: 至少含 time_ms
    context: 至少含 cwd、params_input
    error: 仅 status=error 时使用
    """

    status: ToolStatus
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"success", "partial"}

    @property
    def content(self) -> str:
        return self.text

    @property
    def summary(self) -> str:
        if self.error:
            return self.error.get("message") or self.status
        return str(self.data.get("summary") or self.status)

    @property
    def truncated(self) -> bool:
        return bool(self.data.get("truncated"))

    def to_message_content(self, *, max_chars: int) -> str:
        """序列化为发给模型的 tool message JSON。"""
        body = self.text
        truncated = self.truncated
        if len(body) > max_chars:
            body = body[:max_chars] + "\n…[truncated]"
            truncated = True
        payload = {
            "status": self.status,
            "text": body,
            "data": {**self.data, "truncated": truncated},
            "stats": self.stats,
            "context": self.context,
            "error": self.error,
        }
        return json.dumps(payload, ensure_ascii=False)


# 兼容旧测试/导入名
ToolResult = ToolResponse


def _base_context(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    return {"cwd": str(ctx.workspace), "params_input": params}


def success(
    ctx: ToolContext,
    params: dict[str, Any],
    *,
    text: str = "",
    summary: str = "",
    data: dict[str, Any] | None = None,
    time_ms: int = 0,
    truncated: bool = False,
) -> ToolResponse:
    """构造成功响应。"""
    payload = dict(data or {})
    if summary:
        payload["summary"] = summary
    if truncated:
        payload["truncated"] = True
    return ToolResponse(
        status="success",
        text=text,
        data=payload,
        stats={"time_ms": time_ms},
        context=_base_context(ctx, params),
    )


def partial(
    ctx: ToolContext,
    params: dict[str, Any],
    *,
    text: str,
    summary: str = "",
    data: dict[str, Any] | None = None,
    time_ms: int = 0,
) -> ToolResponse:
    """构造成功但内容被截断/不完整的响应。"""
    payload = {"truncated": True, **(data or {})}
    if summary:
        payload["summary"] = summary
    return ToolResponse(
        status="partial",
        text=text,
        data=payload,
        stats={"time_ms": time_ms},
        context=_base_context(ctx, params),
    )


def failure(
    ctx: ToolContext,
    params: dict[str, Any],
    *,
    code: str,
    message: str,
    text: str | None = None,
    time_ms: int = 0,
    data: dict[str, Any] | None = None,
) -> ToolResponse:
    """构造错误响应。"""
    return ToolResponse(
        status="error",
        text=text if text is not None else message,
        data=dict(data or {}),
        stats={"time_ms": time_ms},
        context=_base_context(ctx, params),
        error={"code": code, "message": message},
    )


@dataclass
class ToolContext:
    """工具执行时可见的运行时上下文。"""

    workspace: Path
    session_data_dir: Path
    todos: list[dict[str, str]]
    max_output_chars: int
    memory_dir: Path
    snapshots: dict[str, FileSnapshot] = field(default_factory=dict)
    ask_permission: Callable[[str, dict[str, Any]], bool] | None = None

    def rel_key(self, path: Path) -> str:
        """workspace 相对路径（posix），用作 snapshot 键。"""
        return path.resolve().relative_to(self.workspace.resolve()).as_posix()

    def remember_snapshot(self, path: Path) -> FileSnapshot:
        """登记路径当前磁盘状态。"""
        snap = FileSnapshot.of(path)
        self.snapshots[self.rel_key(path)] = snap
        return snap

    def check_lock(self, path: Path) -> str | None:
        """若有 snapshot 且磁盘已变，返回错误信息；无 snapshot 则放行。"""
        key = self.rel_key(path)
        expected = self.snapshots.get(key)
        if expected is None:
            return None
        if not path.exists():
            return f"FILE_CHANGED: '{key}' 在 Read 后被删除"
        current = FileSnapshot.of(path)
        if (
            current.mtime_ms != expected.mtime_ms
            or current.size_bytes != expected.size_bytes
            or (expected.sha1 and current.sha1 != expected.sha1)
        ):
            return (
                f"FILE_CHANGED: '{key}' 在 Read 后被外部修改 "
                f"(expected mtime={expected.mtime_ms} size={expected.size_bytes}, "
                f"got mtime={current.mtime_ms} size={current.size_bytes})"
            )
        return None


class Tool(ABC):
    name: str
    description: str
    parameters: dict[str, Any]

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        """执行工具。输入：模型 args + ToolContext；输出：ToolResponse。"""


def resolve_workspace_path(workspace: Path, raw: str) -> Path:
    """把相对/绝对路径规范到绝对路径，并校验仍在 workspace 内。"""
    workspace = workspace.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = workspace / path
    path = path.resolve()
    try:
        path.relative_to(workspace)
    except ValueError as exc:
        raise PermissionError(f"path outside workspace: {path}") from exc
    return path


def timed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
