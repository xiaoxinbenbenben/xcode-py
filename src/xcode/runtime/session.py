"""会话持久化：新建 / 恢复 / 列表，以及 workspace 绑定。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha1
from pathlib import Path
from typing import Any
from uuid import uuid4

_WHITESPACE_RE = re.compile(r"\s+")
_FILE_MENTION_RE = re.compile(r"@\S+")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def project_key(workspace: Path) -> str:
    """把 workspace 绝对路径映射为稳定短 key，用作数据子目录名。"""
    digest = sha1(str(workspace.resolve()).encode("utf-8")).hexdigest()[:12]
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", workspace.name).strip("-") or "project"
    return f"{name}-{digest}"


@dataclass(slots=True)
class SessionMeta:
    session_id: str
    name: str
    workspace_root: str
    created_at: str
    last_active_at: str
    default_name: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "workspace_root": self.workspace_root,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "default_name": self.default_name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionMeta:
        return cls(
            session_id=str(data["session_id"]),
            name=str(data["name"]),
            workspace_root=str(data["workspace_root"]),
            created_at=str(data["created_at"]),
            last_active_at=str(data["last_active_at"]),
            default_name=bool(data.get("default_name", True)),
        )


@dataclass
class SessionRuntime:
    """单个会话的内存视图：元数据 + 对话消息 + 辅助状态。"""

    meta: SessionMeta
    messages: list[dict[str, Any]] = field(default_factory=list)
    todos: list[dict[str, str]] = field(default_factory=list)
    summary: str | None = None
    data_dir: Path = field(default_factory=Path)

    @property
    def session_id(self) -> str:
        return self.meta.session_id

    @property
    def workspace_root(self) -> Path:
        return Path(self.meta.workspace_root)

    def touch(self) -> None:
        self.meta.last_active_at = _utc_now()

    def update_name_from_user_input(self, user_input: str) -> None:
        """首条有效用户输入生成短标题；已有自定义名则跳过。"""
        if not self.meta.default_name:
            return
        text = _FILE_MENTION_RE.sub("", user_input).strip()
        if not text:
            return
        text = _WHITESPACE_RE.sub(" ", text)[:24].strip()
        if text:
            self.meta.name = text
            self.meta.default_name = False

    def save(self) -> None:
        """把会话写入 data_dir（副作用：写磁盘）。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "meta.json").write_text(
            json.dumps(self.meta.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        payload = {
            "messages": self.messages,
            "todos": self.todos,
            "summary": self.summary,
        }
        (self.data_dir / "state.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        pointer = self.data_dir.parent / "current_session.json"
        pointer.write_text(
            json.dumps({"session_id": self.session_id}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class SessionStore:
    """按 workspace 管理会话目录。"""

    def __init__(self, data_home: Path) -> None:
        self.data_home = data_home

    def _project_dir(self, workspace: Path) -> Path:
        return self.data_home / "projects" / project_key(workspace)

    def sessions_dir(self, workspace: Path) -> Path:
        path = self._project_dir(workspace) / "sessions"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def memory_dir(self, workspace: Path) -> Path:
        path = self._project_dir(workspace) / "memory"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def create(
        self,
        workspace: Path,
        *,
        name: str | None = None,
    ) -> SessionRuntime:
        """新建会话并落盘。"""
        workspace = workspace.resolve()
        now = _utc_now()
        session_id = f"sess-{uuid4().hex[:16]}"
        meta = SessionMeta(
            session_id=session_id,
            name=name or f"未命名会话 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            workspace_root=str(workspace),
            created_at=now,
            last_active_at=now,
            default_name=name is None,
        )
        runtime = SessionRuntime(
            meta=meta,
            data_dir=self.sessions_dir(workspace) / session_id,
        )
        runtime.save()
        return runtime

    def load(self, workspace: Path, session_id: str) -> SessionRuntime:
        """按 id 加载会话；不存在则抛 FileNotFoundError。"""
        data_dir = self.sessions_dir(workspace) / session_id
        meta = SessionMeta.from_dict(
            json.loads((data_dir / "meta.json").read_text(encoding="utf-8"))
        )
        state: dict[str, Any] = {}
        state_path = data_dir / "state.json"
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
        return SessionRuntime(
            meta=meta,
            messages=list(state.get("messages") or []),
            todos=list(state.get("todos") or []),
            summary=state.get("summary"),
            data_dir=data_dir,
        )

    def list_sessions(self, workspace: Path) -> list[SessionMeta]:
        """列出某 workspace 下已保存会话，按最近活跃倒序。"""
        root = self.sessions_dir(workspace)
        items: list[SessionMeta] = []
        for child in root.iterdir():
            meta_path = child / "meta.json"
            if not meta_path.is_file():
                continue
            items.append(SessionMeta.from_dict(json.loads(meta_path.read_text(encoding="utf-8"))))
        items.sort(key=lambda m: m.last_active_at, reverse=True)
        return items

    def resolve(
        self,
        workspace: Path,
        *,
        session_id: str | None = None,
        new_session: bool = False,
    ) -> SessionRuntime:
        """按 CLI 参数解析应使用的会话。"""
        workspace = workspace.resolve()
        if new_session:
            return self.create(workspace)
        if session_id:
            return self.load(workspace, session_id)
        pointer = self.sessions_dir(workspace) / "current_session.json"
        if pointer.is_file():
            data = json.loads(pointer.read_text(encoding="utf-8"))
            sid = str(data.get("session_id") or "")
            if sid:
                try:
                    return self.load(workspace, sid)
                except FileNotFoundError:
                    pass
        existing = self.list_sessions(workspace)
        if existing:
            return self.load(workspace, existing[0].session_id)
        return self.create(workspace)
