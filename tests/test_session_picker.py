"""会话浏览：相对时间、过滤、标题解析、回放切片。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from xcode.entrypoints.session_picker import (
    SessionRow,
    filter_rows,
    format_session_line,
    relative_time,
    replay_turns,
    rows_from_metas,
)
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from xcode.entrypoints.complete import XcodeCompleter
from xcode.runtime.session import SessionMeta, SessionStore


def test_relative_time_buckets():
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    assert relative_time("2026-08-13T11:59:20Z", now=now) == "just now"
    assert relative_time("2026-08-13T11:48:00Z", now=now) == "12m ago"
    assert relative_time("2026-08-13T09:00:00Z", now=now) == "3h ago"
    assert relative_time("2026-08-10T12:00:00Z", now=now) == "3d ago"


def test_filter_rows_matches_title_id_and_preview():
    rows = [
        SessionRow(
            session_id="sess-aaa11111bbbb2222",
            name="修 compact 阈值",
            preview="把 threshold 调到 0.7",
            last_active_at="2026-08-13T10:00:00Z",
            user_turns=4,
            message_count=12,
            current=True,
        ),
        SessionRow(
            session_id="sess-cccc3333dddd4444",
            name="未命名会话",
            preview="列出当前目录",
            last_active_at="2026-08-12T10:00:00Z",
            user_turns=1,
            message_count=2,
            current=False,
        ),
    ]
    assert [r.session_id for r in filter_rows(rows, "compact")] == [rows[0].session_id]
    assert [r.session_id for r in filter_rows(rows, "dddd4444")] == [rows[1].session_id]
    assert [r.session_id for r in filter_rows(rows, "列出")] == [rows[1].session_id]
    assert filter_rows(rows, "zzzz") == []


def test_format_session_line_shows_title_not_full_id():
    row = SessionRow(
        session_id="sess-aaa11111bbbb2222",
        name="修 TUI 选择器",
        preview="学 grok 的 /resume",
        last_active_at="2026-08-13T11:48:00Z",
        user_turns=3,
        message_count=9,
        current=True,
    )
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    line = format_session_line(row, now=now, index=1)
    assert "修 TUI 选择器" in line
    assert "3 turns" in line
    assert "12m ago" in line
    assert "●" in line
    assert "sess-aaa11111bbbb2222" not in line
    assert "bbbb2222" in line


def test_find_session_id_by_title(tmp_path: Path):
    store = SessionStore(tmp_path / "home")
    ws = tmp_path / "ws"
    ws.mkdir()
    a = store.create(ws, name="修 compact 阈值")
    store.create(ws, name="别的话题")
    assert store.find_session_id(ws, "修 compact") == a.session_id
    assert store.find_session_id(ws, "COMPACT") == a.session_id


def test_rows_from_metas_skips_zero_turn_sessions():
    metas = [
        SessionMeta(
            session_id="sess-empty",
            name="未命名会话",
            workspace_root="/tmp",
            created_at="2026-08-13T00:00:00Z",
            last_active_at="2026-08-13T12:00:00Z",
            user_turns=0,
            message_count=0,
            preview="",
        ),
        SessionMeta(
            session_id="sess-real",
            name="有内容",
            workspace_root="/tmp",
            created_at="2026-08-13T00:00:00Z",
            last_active_at="2026-08-13T11:00:00Z",
            user_turns=2,
            message_count=6,
            preview="hello",
        ),
    ]
    rows = rows_from_metas(metas)
    assert [r.session_id for r in rows] == ["sess-real"]


def test_rows_from_metas_marks_current_and_sorts():
    metas = [
        SessionMeta(
            session_id="sess-old",
            name="旧",
            workspace_root="/tmp",
            created_at="2026-08-01T00:00:00Z",
            last_active_at="2026-08-01T00:00:00Z",
            user_turns=1,
            message_count=2,
            preview="hello",
        ),
        SessionMeta(
            session_id="sess-new",
            name="新",
            workspace_root="/tmp",
            created_at="2026-08-13T00:00:00Z",
            last_active_at="2026-08-13T00:00:00Z",
            user_turns=5,
            message_count=10,
            preview="world",
        ),
    ]
    rows = rows_from_metas(metas, current_id="sess-old")
    assert [r.session_id for r in rows] == ["sess-new", "sess-old"]
    assert rows[1].current is True
    assert rows[0].current is False


def test_replay_turns_keeps_last_user_and_final_answer():
    messages = [
        {"role": "user", "content": "先读 README"},
        {
            "role": "assistant",
            "content": "我去读一下",
            "tool_calls": [{"id": "1"}],
        },
        {"role": "tool", "tool_call_id": "1", "content": "# title\n" + ("x" * 200)},
        {"role": "assistant", "content": "README 里写了安装步骤。"},
        {"role": "user", "content": "继续改 TUI"},
        {"role": "assistant", "content": "## 计划\n\n- 选择器\n- 回放"},
    ]
    blocks = replay_turns(messages, max_turns=2)
    assert [b.role for b in blocks] == ["user", "assistant", "user", "assistant"]
    assert blocks[0].text == "先读 README"
    assert "1 tool" in (blocks[1].hint or "")
    assert "README 里写了安装步骤" in blocks[1].text
    assert "计划" in blocks[3].text
    assert "x" * 50 not in blocks[1].text


def test_replay_turns_skips_compact_summary_user():
    messages = [
        {"role": "user", "content": "<compact_summary>\nhandoff\n</compact_summary>"},
        {"role": "user", "content": "下一问"},
        {"role": "assistant", "content": "答"},
    ]
    blocks = replay_turns(messages, max_turns=4)
    assert [b.role for b in blocks] == ["user", "assistant"]
    assert blocks[0].text == "下一问"


def test_slash_completer_shows_resume_description():
    completer = XcodeCompleter(Path("."))
    hits = list(
        completer.get_completions(Document("/re"), CompleteEvent())
    )
    names = [c.text for c in hits]
    assert "/resume" in names
    resume = next(c for c in hits if c.text == "/resume")
    assert "选择" in (resume.display_meta or "") or "会话" in str(resume.display_meta)


def test_meta_counts_update_on_save(tmp_path: Path):
    store = SessionStore(tmp_path / "home")
    ws = tmp_path / "ws"
    ws.mkdir()
    s = store.create(ws)
    s.append_message({"role": "user", "content": "列出目录并解释"})
    s.append_message({"role": "assistant", "content": "好"})
    s.write_context()
    listed = store.list_sessions(ws)
    assert listed[0].user_turns == 1
    assert listed[0].message_count == 2
    assert "列出目录" in listed[0].preview
