"""会话列表 / 过滤 / 回放切片 / 交互选择器。

/resume 无参时用 pick_session：输入过滤标题与预览，方向键选中，Enter 切入。
不依赖完整 session id。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.styles import Style

from xcode.runtime.session import SessionMeta

_PICKER_STYLE = Style.from_dict(
    {
        "title": "bold #5dffa8 bg:#111820",
        "filter": "#e8eef4 bg:#111820",
        "selected": "bold #0a0f14 bg:#5dffa8",
        "row": "#e8eef4 bg:#111820",
        "dim": "#8b9aab bg:#111820",
        "preview": "#8b9aab bg:#111820",
    }
)


@dataclass(slots=True)
class SessionRow:
    session_id: str
    name: str
    preview: str
    last_active_at: str
    user_turns: int
    message_count: int
    current: bool = False


@dataclass(slots=True)
class ReplayBlock:
    """回放用的一轮切片：用户原文 + 该轮最终回答。"""

    role: str
    text: str
    hint: str | None = None


def _parse_iso(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def relative_time(iso: str, *, now: datetime | None = None) -> str:
    """把 ISO 时间收成 just now / 12m ago / 3h ago / 3d ago。"""

    stamp = _parse_iso(iso)
    if stamp is None:
        return iso or "?"
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    delta = current - stamp.astimezone(UTC)
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def filter_rows(rows: list[SessionRow], query: str) -> list[SessionRow]:
    """按标题 / session id / 预览做不区分大小写的子串过滤。"""

    needle = (query or "").strip().lower()
    if not needle:
        return list(rows)
    hits: list[SessionRow] = []
    for row in rows:
        hay = " ".join((row.name, row.session_id, row.preview)).lower()
        if needle in hay:
            hits.append(row)
    return hits


def format_session_line(
    row: SessionRow,
    *,
    now: datetime | None = None,
    index: int | None = None,
) -> str:
    """一行给人看：标题为主，id 只留尾缀。"""

    mark = "●" if row.current else "○"
    prefix = f"{mark} {index}  " if index is not None else f"{mark}  "
    turns = f"{row.user_turns} turn" if row.user_turns == 1 else f"{row.user_turns} turns"
    return (
        f"{prefix}{row.name}  ·  {relative_time(row.last_active_at, now=now)}"
        f"  ·  {turns}  ·  {row.session_id[-8:]}"
    )


def rows_from_metas(
    metas: list[SessionMeta],
    *,
    current_id: str | None = None,
) -> list[SessionRow]:
    """SessionMeta → 选择器行；按最近活跃倒序。"""

    ordered = sorted(metas, key=lambda m: m.last_active_at, reverse=True)
    return [
        SessionRow(
            session_id=meta.session_id,
            name=meta.name,
            preview=meta.preview,
            last_active_at=meta.last_active_at,
            user_turns=meta.user_turns,
            message_count=meta.message_count,
            current=meta.session_id == current_id,
        )
        for meta in ordered
        if meta.user_turns > 0
    ]


def replay_turns(
    messages: list[dict[str, Any]],
    *,
    max_turns: int = 4,
) -> list[ReplayBlock]:
    """从送模窗口抽出最近若干用户轮：用户原文 + 该轮最后一段助手回答。不截断正文。"""

    turns: list[tuple[str, str, int]] = []
    user_text: str | None = None
    assistant_text = ""
    tool_count = 0

    def _flush() -> None:
        nonlocal user_text, assistant_text, tool_count
        if user_text is None:
            return
        turns.append((user_text, assistant_text, tool_count))
        user_text = None
        assistant_text = ""
        tool_count = 0

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        text = content if isinstance(content, str) else ""
        if role == "user":
            if text.startswith("<compact_summary>"):
                continue
            _flush()
            user_text = text
            continue
        if user_text is None:
            continue
        if role == "assistant":
            if text.strip():
                assistant_text = text
            calls = msg.get("tool_calls") or []
            if isinstance(calls, list):
                tool_count += len(calls)
    _flush()

    blocks: list[ReplayBlock] = []
    for user, answer, tools in turns[-max_turns:]:
        blocks.append(ReplayBlock(role="user", text=user))
        if answer.strip() or tools:
            hint = None
            if tools:
                hint = f"{tools} tool" if tools == 1 else f"{tools} tools"
            blocks.append(ReplayBlock(role="assistant", text=answer.strip(), hint=hint))
    return blocks


async def pick_session(rows: list[SessionRow]) -> str | None:
    """交互选择会话。返回 session_id；Esc / Ctrl-C 返回 None。"""

    if not rows:
        return None

    state: dict[str, Any] = {"index": 0, "result": None}

    def _filtered() -> list[SessionRow]:
        return filter_rows(rows, buf.text)

    def _clamp() -> list[SessionRow]:
        items = _filtered()
        if not items:
            state["index"] = 0
            return items
        state["index"] = max(0, min(int(state["index"]), len(items) - 1))
        return items

    def _header() -> StyleAndTextTuples:
        return [
            (
                "class:title",
                " resume  ·  输入过滤  ↑↓ 选择  enter 切入  esc 取消 ",
            )
        ]

    def _body() -> StyleAndTextTuples:
        items = _clamp()
        if not items:
            return [("class:dim", "  (no matches)\n")]
        now = datetime.now(UTC)
        out: StyleAndTextTuples = []
        for i, row in enumerate(items):
            line = format_session_line(row, now=now, index=i + 1)
            extra = f"\n     {row.preview}" if row.preview and i == state["index"] else ""
            style = "class:selected" if i == state["index"] else "class:row"
            out.append((style, line + extra + "\n"))
        return out

    def _on_change(_buf: Buffer) -> None:
        state["index"] = 0

    buf = Buffer(multiline=False, on_text_changed=_on_change)
    keys = KeyBindings()

    @keys.add("up")
    @keys.add("c-p")
    def _up(_event) -> None:
        items = _clamp()
        if items:
            state["index"] = (state["index"] - 1) % len(items)

    @keys.add("down")
    @keys.add("c-n")
    def _down(_event) -> None:
        items = _clamp()
        if items:
            state["index"] = (state["index"] + 1) % len(items)

    @keys.add("enter")
    def _enter(event) -> None:
        items = _clamp()
        if items:
            state["result"] = items[state["index"]].session_id
        event.app.exit()

    @keys.add("escape")
    @keys.add("c-c")
    @keys.add("c-g")
    def _cancel(event) -> None:
        state["result"] = None
        event.app.exit()

    def _body_height() -> int:
        return min(10, max(2, len(_clamp()) + 1))

    search = Window(BufferControl(buffer=buf), height=1, style="class:filter")
    root = HSplit(
        [
            Window(FormattedTextControl(_header), height=1),
            search,
            Window(FormattedTextControl(_body), height=_body_height),
        ]
    )
    app: Application[None] = Application(
        layout=Layout(root, focused_element=search),
        key_bindings=keys,
        style=_PICKER_STYLE,
        full_screen=False,
        mouse_support=False,
    )
    await app.run_async()
    result = state["result"]
    return result if isinstance(result, str) else None
