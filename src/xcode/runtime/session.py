"""会话历史：transcript.jsonl 权威流水 + context.json 送模缓存。见 docs/session-history.md。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha1
from pathlib import Path
from typing import Any
from uuid import uuid4

from xcode.runtime.tokens import count_messages_tokens

_WHITESPACE_RE = re.compile(r"\s+")
_FILE_MENTION_RE = re.compile(r"@\S+")

TRANSCRIPT_NAME = "transcript.jsonl"
CONTEXT_NAME = "context.json"
META_NAME = "meta.json"

DEFAULT_TOOL_PRUNE_CHARS = 16_000
DEFAULT_TRANSCRIPT_HARD_CAP = 2_000_000
DEFAULT_RETAINED_USER_GROUPS = 6


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def project_key(workspace: Path) -> str:
    """把 workspace 绝对路径映射为稳定短 key，用作数据子目录名。

    这里的 sha1 仅用于「路径 → 短目录名」，避免绝对路径里的斜杠/中文把目录打爆；
    与 tool 截断、内容校验无关。
    """
    digest = sha1(str(workspace.resolve()).encode("utf-8")).hexdigest()[:12]
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", workspace.name).strip("-") or "project"
    return f"{name}-{digest}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """临时文件 + rename 写 JSON，避免写到一半进程挂掉留下半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def prune_tool_content(content: str, *, limit: int = DEFAULT_TOOL_PRUNE_CHARS) -> str:
    """送模侧截断 tool 输出。

    只砍 content 字符串，不删消息本身，避免破坏 assistant.tool_calls ↔ tool.tool_call_id 配对。
    壳里只写 original_chars / kept_chars，方便人和模型看出「被截过」；不做内容哈希。
    """
    if not isinstance(content, str):
        content = str(content)
    if len(content) <= limit:
        return content
    kept = content[:limit]
    return (
        f'<tool_output_truncated original_chars="{len(content)}" '
        f'kept_chars="{limit}">\n'
        f"{kept}\n"
        f"</tool_output_truncated>"
    )


def _maybe_hard_cap(content: str, *, hard_cap: int) -> tuple[str, bool, int, int]:
    """JSONL 单事件硬顶：超过则截断并标记 truncated（仍禁止静默）。

    返回 (content, truncated, original_chars, kept_chars)。
    """
    if not isinstance(content, str):
        content = str(content)
    original = len(content)
    if original <= hard_cap:
        return content, False, original, original
    return content[:hard_cap], True, original, hard_cap

def prune_message_for_model(
    msg: dict[str, Any],
    *,
    tool_prune_chars: int = DEFAULT_TOOL_PRUNE_CHARS,
) -> dict[str, Any]:
    """生成送模用消息：tool content 截断，其它原样。"""
    out: dict[str, Any] = {"role": msg["role"]}
    if "content" in msg:
        out["content"] = msg["content"]
    if msg.get("tool_calls") is not None:
        out["tool_calls"] = msg["tool_calls"]
    if msg.get("tool_call_id") is not None:
        out["tool_call_id"] = msg["tool_call_id"]
    if msg.get("name") is not None:
        out["name"] = msg["name"]
    if out.get("role") == "tool" and isinstance(out.get("content"), str):
        out["content"] = prune_tool_content(out["content"], limit=tool_prune_chars)
    return out


def _user_previews(messages: list[Any]) -> list[str]:
    previews: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or content.startswith("<compact_summary>"):
            continue
        previews.append(" ".join(content.split()))
    return previews


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def split_user_turn_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """按「用户回合组」切分，不是按消息条数。

    一组 = 一条 user + 后面连续的 assistant/tool（可能多轮 tool 循环）+ 最终 assistant。
    compact 保留「最近 N 组」时必须用这个切法，否则会把 tool 配对切碎。
    """
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "user":
            if current:
                groups.append(current)
            current = [msg]
        else:
            if not current:
                # 前缀（如 compact summary 也可能是 user）——无 user 时自成一组
                current = [msg]
            else:
                current.append(msg)
    if current:
        groups.append(current)
    return groups


def retain_last_user_groups(
    messages: list[dict[str, Any]],
    *,
    n: int = DEFAULT_RETAINED_USER_GROUPS,
) -> list[dict[str, Any]]:
    """保留最近 n 个 user turn group。"""
    if n <= 0:
        return []
    groups = split_user_turn_groups(messages)
    if not groups:
        return []
    return [msg for group in groups[-n:] for msg in group]


def summary_message(summary: str) -> dict[str, Any]:
    """Compact 摘要作为一条 user 消息注入窗口。"""
    text = summary.strip()
    return {
        "role": "user",
        "content": f"<compact_summary>\n{text}\n</compact_summary>",
    }


@dataclass(slots=True)
class SessionMeta:
    session_id: str
    name: str
    workspace_root: str
    created_at: str
    last_active_at: str
    default_name: bool = True
    user_turns: int = 0
    message_count: int = 0
    preview: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "workspace_root": self.workspace_root,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "default_name": self.default_name,
            "user_turns": self.user_turns,
            "message_count": self.message_count,
            "preview": self.preview,
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
            user_turns=int(data.get("user_turns") or 0),
            message_count=int(data.get("message_count") or 0),
            preview=str(data.get("preview") or ""),
        )


@dataclass
class SessionRuntime:
    """单个会话的运行时状态。

    字段分工：
    - messages：当前送模窗口（已 prune / 可能已 compact）
    - last_event_id / event_count / byte_offset：与 transcript 对齐的书签
    - estimated_tokens：本地估算，给 TUI 和调试用（非账单精确值）
    """

    meta: SessionMeta
    messages: list[dict[str, Any]] = field(default_factory=list)
    data_dir: Path = field(default_factory=Path)
    last_event_id: int = 0
    event_count: int = 0
    byte_offset: int = 0
    estimated_tokens: int = 0
    tool_prune_chars: int = DEFAULT_TOOL_PRUNE_CHARS
    transcript_hard_cap: int = DEFAULT_TRANSCRIPT_HARD_CAP
    retained_user_groups: int = DEFAULT_RETAINED_USER_GROUPS

    @property
    def session_id(self) -> str:
        return self.meta.session_id

    @property
    def workspace_root(self) -> Path:
        return Path(self.meta.workspace_root)

    @property
    def transcript_path(self) -> Path:
        return self.data_dir / TRANSCRIPT_NAME

    @property
    def context_path(self) -> Path:
        return self.data_dir / CONTEXT_NAME

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

    def _write_meta_and_pointer(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.data_dir / META_NAME, self.meta.as_dict())
        pointer = self.data_dir.parent / "current_session.json"
        _atomic_write_json(pointer, {"session_id": self.session_id})

    def write_context(self, *, model: str = "", fsync_transcript: bool = False) -> None:
        """把「当前送模窗口 + 书签」落到 context.json。

        过程：
        1. 用 tiktoken 估 messages 的 token，写入 estimated_tokens
        2. 带上 event_count / last_event_id / byte_offset，供下次 load 对账
        3. 临时文件 + rename（原子替换）
        4. 同步写 meta 与 current_session 指针
        5. 可选 fsync transcript，降低整轮结束时丢尾部风险
        """
        self.refresh_meta_stats()
        self.estimated_tokens = count_messages_tokens(self.messages, model=model)
        payload = {
            "schema_version": 1,
            "session_id": self.session_id,
            "transcript_event_count": self.event_count,
            "transcript_last_event_id": self.last_event_id,
            "transcript_byte_offset": self.byte_offset,
            "messages": self.messages,
            "estimated_tokens": self.estimated_tokens,
        }
        _atomic_write_json(self.context_path, payload)
        self._write_meta_and_pointer()
        if fsync_transcript and self.transcript_path.is_file():
            with self.transcript_path.open("rb") as fh:
                fh.flush()
                try:
                    import os

                    os.fsync(fh.fileno())
                except OSError:
                    pass

    def refresh_meta_stats(self) -> None:
        """用当前送模窗口刷新列表要用的轮次 / 预览。"""
        self.meta.message_count = len(self.messages)
        previews = _user_previews(self.messages)
        self.meta.user_turns = len(previews)
        self.meta.preview = (previews[-1] if previews else "")[:80]

    def save(self) -> None:
        """兼容旧调用：写 context + meta（不重复写 transcript）。"""
        self.touch()
        self.write_context(fsync_transcript=True)

    def append_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """会话消息的唯一写入入口（agent 禁止直接 messages.append）。

        过程逐步：
        1. 规范化 role / content
        2. 若内容过大 → JSONL 侧硬顶截断，并标 truncated=true（禁止静默）
        3. event_id += 1，拼 type=message 事件，append 到 transcript.jsonl 并 flush
        4. 更新 byte_offset = 文件当前大小（给 context 书签用）
        5. 生成送模版：tool content 再 prune 到 tool_prune_chars，append 到 self.messages
        6. 返回送模版消息

        注意：JSONL 里尽量是「更完整」的 content；内存 messages 是「更短」的送模视图。
        """
        role = str(message.get("role") or "")
        if role not in {"user", "assistant", "tool", "system"}:
            raise ValueError(f"unsupported role: {role}")

        content = message.get("content")
        content_str: str | None
        if content is None:
            content_str = None
        elif isinstance(content, str):
            content_str = content
        else:
            content_str = json.dumps(content, ensure_ascii=False)

        truncated = False
        original_chars: int | None = None
        kept_chars: int | None = None
        disk_content = content_str
        # 大 tool / 异常大文本：JSONL 也有硬顶（有上限的产品记录，不是原始冷归档）
        if isinstance(content_str, str) and (
            role == "tool" or len(content_str) > self.transcript_hard_cap
        ):
            disk_content, truncated, original_chars, kept_chars = _maybe_hard_cap(
                content_str, hard_cap=self.transcript_hard_cap
            )

        self.last_event_id += 1
        self.event_count += 1
        event: dict[str, Any] = {
            "v": 1,
            "event_id": self.last_event_id,
            "type": "message",
            "created_at": _utc_now(),
            "role": role,
            "content": disk_content,
            "tool_calls": message.get("tool_calls"),
            "tool_call_id": message.get("tool_call_id"),
            "truncated": truncated,
            "original_chars": original_chars if truncated else None,
            "kept_chars": kept_chars if truncated else None,
        }
        if message.get("name") is not None:
            event["name"] = message["name"]

        line = json.dumps(event, ensure_ascii=False) + "\n"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self.transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
        self.byte_offset = self.transcript_path.stat().st_size

        model_msg = prune_message_for_model(
            {
                "role": role,
                "content": disk_content if role == "tool" else content_str,
                "tool_calls": message.get("tool_calls"),
                "tool_call_id": message.get("tool_call_id"),
                **({"name": message["name"]} if message.get("name") is not None else {}),
            },
            tool_prune_chars=self.tool_prune_chars,
        )
        # assistant content None 保持
        if role == "assistant" and content is None:
            model_msg["content"] = None
        self.messages.append(model_msg)
        return model_msg

    def apply_compact(self, summary: str, *, model: str = "") -> None:
        """执行一次上下文压缩（摘要文本由调用方用 light_model 生成）。

        过程：
        1. 从当前 messages 取出最近 retained_user_groups 个 user turn group（已 prune）
        2. 写一条 type=compact 事件到 JSONL，内含：
           - summary：handoff 摘要
           - retained_messages：当时的近端窗口（自包含，重建不必重放更早历史）
           - source_through_event_id：压缩覆盖到的最后一个旧 event_id
        3. 内存 messages 替换为 [summary_message] + retained_messages

        transcript 里 compact 之前的 message 行仍保留（审计用），只是送模不再带上。
        """
        retained = retain_last_user_groups(
            self.messages, n=self.retained_user_groups
        )
        # 确保 retained 内 tool 已 prune
        retained = [
            prune_message_for_model(m, tool_prune_chars=self.tool_prune_chars) for m in retained
        ]
        source_through = self.last_event_id
        self.last_event_id += 1
        self.event_count += 1
        event = {
            "v": 1,
            "event_id": self.last_event_id,
            "type": "compact",
            "created_at": _utc_now(),
            "source_through_event_id": source_through,
            "summary": summary.strip(),
            "retained_messages": retained,
            "estimated_tokens": 0,
        }
        new_messages = [summary_message(summary), *retained]
        event["estimated_tokens"] = count_messages_tokens(new_messages, model=model)

        line = json.dumps(event, ensure_ascii=False) + "\n"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self.transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
        self.byte_offset = self.transcript_path.stat().st_size
        self.messages = new_messages
        self.estimated_tokens = event["estimated_tokens"]

    def needs_compact(
        self,
        *,
        overhead_tokens: int,
        context_window: int,
        compact_threshold: float,
        reserved_output_tokens: int,
        model: str = "",
    ) -> bool:
        """budget = overhead + messages + reserve；>= window * threshold 则该 compact。"""
        if context_window <= 0:
            return False
        used = overhead_tokens + count_messages_tokens(self.messages, model=model)
        return (used + reserved_output_tokens) >= int(context_window * compact_threshold)

    # --- 加载 / 回放 ---

    @staticmethod
    def _read_transcript_events(path: Path) -> list[dict[str, Any]]:
        """读 JSONL；丢弃最后一行不完整 JSON。"""
        if not path.is_file():
            return []
        raw = path.read_bytes()
        return _parse_jsonl(raw.decode("utf-8", errors="replace")) if raw else []

    @staticmethod
    def _events_from_offset(path: Path, offset: int) -> list[dict[str, Any]]:
        if not path.is_file() or offset < 0:
            return []
        if offset >= path.stat().st_size:
            return []
        with path.open("rb") as fh:
            fh.seek(offset)
            return _parse_jsonl(fh.read().decode("utf-8", errors="replace"))

    def _message_from_event(
        self, event: dict[str, Any], *, for_model: bool
    ) -> dict[str, Any] | None:
        if event.get("type") != "message":
            return None
        msg: dict[str, Any] = {
            "role": event.get("role"),
            "content": event.get("content"),
        }
        if event.get("tool_calls") is not None:
            msg["tool_calls"] = event["tool_calls"]
        if event.get("tool_call_id") is not None:
            msg["tool_call_id"] = event["tool_call_id"]
        if event.get("name") is not None:
            msg["name"] = event["name"]
        if for_model:
            return prune_message_for_model(msg, tool_prune_chars=self.tool_prune_chars)
        return msg

    def _rebuild_from_events(self, events: list[dict[str, Any]]) -> None:
        """从事件列表重建 messages 与计数器。"""
        last_compact_idx = -1
        for i, ev in enumerate(events):
            if ev.get("type") == "compact":
                last_compact_idx = i

        if last_compact_idx >= 0:
            compact = events[last_compact_idx]
            summary = str(compact.get("summary") or "")
            retained = list(compact.get("retained_messages") or [])
            retained = [
                prune_message_for_model(m, tool_prune_chars=self.tool_prune_chars)
                for m in retained
                if isinstance(m, dict)
            ]
            source_through = int(compact.get("source_through_event_id") or 0)
            messages = [summary_message(summary), *retained]
            for ev in events[last_compact_idx + 1 :]:
                if ev.get("type") != "message":
                    continue
                eid = int(ev.get("event_id") or 0)
                if eid <= source_through:
                    continue
                msg = self._message_from_event(ev, for_model=True)
                if msg is not None:
                    messages.append(msg)
            self.messages = messages
        else:
            messages = []
            for ev in events:
                msg = self._message_from_event(ev, for_model=True)
                if msg is not None:
                    messages.append(msg)
            self.messages = messages

        if events:
            self.last_event_id = max(int(e.get("event_id") or 0) for e in events)
            self.event_count = len(events)
        else:
            self.last_event_id = 0
            self.event_count = 0
        if self.transcript_path.is_file():
            self.byte_offset = self.transcript_path.stat().st_size
        else:
            self.byte_offset = 0

    def _apply_message_events(self, events: list[dict[str, Any]]) -> None:
        """增量应用 message / compact 事件到当前窗口。"""
        for ev in events:
            et = ev.get("type")
            if et == "compact":
                summary = str(ev.get("summary") or "")
                retained = list(ev.get("retained_messages") or [])
                retained = [
                    prune_message_for_model(m, tool_prune_chars=self.tool_prune_chars)
                    for m in retained
                    if isinstance(m, dict)
                ]
                self.messages = [summary_message(summary), *retained]
            elif et == "message":
                msg = self._message_from_event(ev, for_model=True)
                if msg is not None:
                    self.messages.append(msg)
            eid = int(ev.get("event_id") or 0)
            if eid > self.last_event_id:
                self.last_event_id = eid
            self.event_count += 1
        if self.transcript_path.is_file():
            self.byte_offset = self.transcript_path.stat().st_size

    def load_state_from_disk(self) -> None:
        """打开已有 session 时恢复 messages（SessionStore.load 调用）。

        决策树：
        A. context 存在且 session_id 匹配
           A1. 文件 size == byte_offset 且末 event_id/count 一致 → 信任 context.messages
           A2. 文件更长 → 以 context 为底，只回放 offset 之后的新事件
           A3. 对不上 → 走 B
        B. 从 transcript 全量事件重建：优先最后一个 compact 检查点 + 其后 message
        半行 JSON 在读事件时已丢弃，不会污染 messages。
        """
        path = self.transcript_path
        events_all = self._read_transcript_events(path)
        # 半行丢弃后，真实权威长度以「完整事件重写后的逻辑」对齐：用当前完整文件 size
        file_size = path.stat().st_size if path.is_file() else 0

        ctx: dict[str, Any] | None = None
        if self.context_path.is_file():
            try:
                ctx = json.loads(self.context_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                ctx = None

        if ctx and str(ctx.get("session_id") or "") == self.session_id:
            offset = int(ctx.get("transcript_byte_offset") or 0)
            last_id = int(ctx.get("transcript_last_event_id") or 0)
            count = int(ctx.get("transcript_event_count") or 0)
            cached_messages = list(ctx.get("messages") or [])

            if file_size == offset and last_id == (
                max((int(e.get("event_id") or 0) for e in events_all), default=0)
            ) and count == len(events_all):
                self.messages = cached_messages
                self.last_event_id = last_id
                self.event_count = count
                self.byte_offset = offset
                self.estimated_tokens = int(ctx.get("estimated_tokens") or 0)
                return

            if file_size > offset >= 0 and count == len(
                [e for e in events_all if int(e.get("event_id") or 0) <= last_id]
            ):
                # 尾部增量：信任 context messages 作为到 last_id 的状态
                self.messages = cached_messages
                self.last_event_id = last_id
                self.event_count = count
                self.byte_offset = offset
                tail = self._events_from_offset(path, offset)
                # 仅接受 event_id > last_id
                tail = [e for e in tail if int(e.get("event_id") or 0) > last_id]
                if tail:
                    self._apply_message_events(tail)
                self.estimated_tokens = count_messages_tokens(self.messages)
                return

        # 完整从 compact 重建
        self._rebuild_from_events(events_all)
        self.estimated_tokens = count_messages_tokens(self.messages)


class SessionStore:
    """按 workspace 管理会话目录：create / load / list / resolve。

    resolve 优先级（与 CLI 一致）：
      --new-session → 指定 --session id → current_session.json → 最近活跃 → 新建
    不负责迁移旧 state.json；新格式只认 meta + transcript + context。
    """

    def __init__(
        self,
        data_home: Path,
        *,
        tool_prune_chars: int = DEFAULT_TOOL_PRUNE_CHARS,
        transcript_hard_cap: int = DEFAULT_TRANSCRIPT_HARD_CAP,
    ) -> None:
        self.data_home = data_home
        self.tool_prune_chars = tool_prune_chars
        self.transcript_hard_cap = transcript_hard_cap

    def _project_dir(self, workspace: Path) -> Path:
        return self.data_home / "projects" / project_key(workspace)

    def sessions_dir(self, workspace: Path) -> Path:
        path = self._project_dir(workspace) / "sessions"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _runtime_kwargs(self) -> dict[str, Any]:
        return {
            "tool_prune_chars": self.tool_prune_chars,
            "transcript_hard_cap": self.transcript_hard_cap,
        }

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
            **self._runtime_kwargs(),
        )
        runtime.data_dir.mkdir(parents=True, exist_ok=True)
        if not runtime.transcript_path.exists():
            runtime.transcript_path.write_text("", encoding="utf-8")
        runtime.write_context(fsync_transcript=False)
        return runtime

    def load(self, workspace: Path, session_id: str) -> SessionRuntime:
        """按 id 加载会话；不存在则抛 FileNotFoundError。"""
        data_dir = self.sessions_dir(workspace) / session_id
        meta_path = data_dir / META_NAME
        if not meta_path.is_file():
            raise FileNotFoundError(f"session not found: {session_id}")
        meta = SessionMeta.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
        runtime = SessionRuntime(
            meta=meta,
            data_dir=data_dir,
            **self._runtime_kwargs(),
        )
        runtime.load_state_from_disk()
        return runtime

    def list_sessions(self, workspace: Path) -> list[SessionMeta]:
        """列出某 workspace 下已保存会话，按最近活跃倒序。"""
        root = self.sessions_dir(workspace)
        items: list[SessionMeta] = []
        for child in root.iterdir():
            if not child.is_dir():
                continue
            meta_path = child / META_NAME
            if not meta_path.is_file():
                continue
            try:
                meta = SessionMeta.from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, KeyError):
                continue
            if meta.message_count == 0 and meta.preview == "":
                self._hydrate_meta_from_context(child, meta)
            items.append(meta)
        items.sort(key=lambda m: m.last_active_at, reverse=True)
        return items

    @staticmethod
    def _hydrate_meta_from_context(child: Path, meta: SessionMeta) -> None:
        """旧会话 meta 没有轮次字段时，从 context.json 补一版给列表用。"""

        ctx_path = child / CONTEXT_NAME
        if not ctx_path.is_file():
            return
        try:
            data = json.loads(ctx_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            return
        meta.message_count = len(messages)
        previews = _user_previews(messages)
        meta.user_turns = len(previews)
        if previews and not meta.preview:
            meta.preview = previews[-1][:80]

    def find_session_id(self, workspace: Path, query: str) -> str:
        """按完整 id / 唯一前缀 / 唯一后缀 / 列表序号（1-based）解析会话 id。

        找不到或多义时抛 ValueError（带可读说明）。
        """
        q = (query or "").strip()
        if not q:
            raise ValueError("empty session query")
        items = self.list_sessions(workspace)
        if not items:
            raise ValueError("no sessions in this workspace")

        # 1-based index from /sessions listing order
        if q.isdigit():
            idx = int(q)
            if 1 <= idx <= len(items):
                return items[idx - 1].session_id
            raise ValueError(f"index out of range: {idx} (1..{len(items)})")

        # exact
        for meta in items:
            if meta.session_id == q:
                return meta.session_id

        # unique prefix (e.g. sess-3ab0) or unique suffix (toolbar shows last 8)
        prefix_hits = [m.session_id for m in items if m.session_id.startswith(q)]
        if len(prefix_hits) == 1:
            return prefix_hits[0]
        if len(prefix_hits) > 1:
            raise ValueError(
                "ambiguous prefix; matches: " + ", ".join(prefix_hits[:5])
            )

        suffix_hits = [m.session_id for m in items if m.session_id.endswith(q)]
        if len(suffix_hits) == 1:
            return suffix_hits[0]
        if len(suffix_hits) > 1:
            raise ValueError(
                "ambiguous suffix; matches: " + ", ".join(suffix_hits[:5])
            )

        needle = q.casefold()
        name_hits = [m for m in items if needle in (m.name or "").casefold()]
        if len(name_hits) == 1:
            return name_hits[0].session_id
        if len(name_hits) > 1:
            exact = [m for m in name_hits if (m.name or "").casefold() == needle]
            if len(exact) == 1:
                return exact[0].session_id
            raise ValueError(
                "ambiguous title; matches: "
                + ", ".join(f"{m.name} ({m.session_id[-8:]})" for m in name_hits[:5])
            )

        raise ValueError(f"session not found: {q}")

    def resolve(
        self,
        workspace: Path,
        *,
        session_id: str | None = None,
        new_session: bool = False,
    ) -> SessionRuntime:
        """按 CLI 参数解析应使用的会话。

        优先级：强制新建 → 指定 id → current 指针 → 最近会话 → 新建。
        """
        workspace = workspace.resolve()
        if new_session:
            return self.create(workspace)
        if session_id:
            # 允许前缀/后缀/序号，与 TUI /resume 一致
            try:
                sid = self.find_session_id(workspace, session_id)
            except ValueError:
                sid = session_id
            return self.load(workspace, sid)
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
