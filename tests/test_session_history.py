"""会话 transcript / context / prune / compact / resume。"""

from __future__ import annotations

import json
from pathlib import Path

from xcode.runtime.session import (
    SessionStore,
    prune_tool_content,
    retain_last_user_groups,
    split_user_turn_groups,
)
from xcode.runtime.tokens import count_text_tokens, format_token_usage


def test_prune_tool_content_envelope():
    raw = "x" * 100
    out = prune_tool_content(raw, limit=20)
    assert "tool_output_truncated" in out
    assert 'original_chars="100"' in out
    assert 'kept_chars="20"' in out
    assert "sha256" not in out  # 截断壳故意不带哈希
    assert "x" * 20 in out


def test_user_turn_groups():
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "c"},
        {"role": "assistant", "content": "d"},
        {"role": "user", "content": "e"},
        {"role": "assistant", "content": "f"},
    ]
    groups = split_user_turn_groups(msgs)
    assert len(groups) == 2
    assert groups[0][0]["content"] == "a"
    assert len(groups[0]) == 4
    retained = retain_last_user_groups(msgs, n=1)
    assert retained[0]["content"] == "e"


def test_append_and_resume_roundtrip(tmp_path: Path):
    store = SessionStore(tmp_path / "home", tool_prune_chars=50)
    ws = tmp_path / "ws"
    ws.mkdir()
    s = store.create(ws)
    s.append_message({"role": "user", "content": "hello"})
    big = "Z" * 200
    s.append_message({"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
    ]})
    s.append_message({"role": "tool", "tool_call_id": "c1", "content": big})
    s.append_message({"role": "assistant", "content": "done"})
    s.write_context()

    # 送模侧 tool 已截断
    tool_msg = [m for m in s.messages if m.get("role") == "tool"][0]
    assert "tool_output_truncated" in tool_msg["content"]
    assert "Z" * 50 in tool_msg["content"]
    assert "Z" * 51 not in tool_msg["content"].split(">")[-1]  # 仅前缀 50 个 Z

    # JSONL 全文（未触硬顶）
    lines = s.transcript_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    disk_tool = json.loads(lines[2])
    assert disk_tool["content"] == big
    assert disk_tool["truncated"] is False

    # resume
    s2 = store.load(ws, s.session_id)
    assert len(s2.messages) == 4
    assert s2.messages[0]["content"] == "hello"
    assert s2.last_event_id == 4
    assert s2.event_count == 4
    assert s2.byte_offset == s.byte_offset


def test_resume_uses_context_when_offset_matches(tmp_path: Path):
    store = SessionStore(tmp_path / "home")
    ws = tmp_path / "ws"
    ws.mkdir()
    s = store.create(ws)
    s.append_message({"role": "user", "content": "u1"})
    s.append_message({"role": "assistant", "content": "a1"})
    s.write_context()
    offset = s.byte_offset

    s2 = store.load(ws, s.session_id)
    assert s2.byte_offset == offset
    assert [m["content"] for m in s2.messages] == ["u1", "a1"]


def test_resume_tail_incremental(tmp_path: Path):
    store = SessionStore(tmp_path / "home")
    ws = tmp_path / "ws"
    ws.mkdir()
    s = store.create(ws)
    s.append_message({"role": "user", "content": "u1"})
    s.write_context()
    # 模拟：context 已写，又追加消息未刷 context
    s.append_message({"role": "assistant", "content": "a1"})

    s2 = store.load(ws, s.session_id)
    assert len(s2.messages) == 2
    assert s2.messages[1]["content"] == "a1"


def test_compact_self_contained_rebuild(tmp_path: Path):
    store = SessionStore(tmp_path / "home")
    ws = tmp_path / "ws"
    ws.mkdir()
    s = store.create(ws)
    for i in range(8):
        s.append_message({"role": "user", "content": f"user-{i}"})
        s.append_message({"role": "assistant", "content": f"asst-{i}"})
    s.apply_compact("summary of early work")
    s.append_message({"role": "user", "content": "after"})
    s.append_message({"role": "assistant", "content": "ok"})
    s.write_context()

    # 损坏 context → 从 compact 重建
    s.context_path.unlink()
    s3 = store.load(ws, s.session_id)
    assert s3.messages[0]["content"].startswith("<compact_summary>")
    assert "summary of early work" in s3.messages[0]["content"]
    assert s3.messages[-1]["content"] == "ok"
    assert s3.messages[-2]["content"] == "after"
    # retained 最多 6 个 user group + summary + after 轮
    roles = [m["role"] for m in s3.messages]
    assert roles[0] == "user"  # summary


def test_incomplete_last_line_ignored(tmp_path: Path):
    store = SessionStore(tmp_path / "home")
    ws = tmp_path / "ws"
    ws.mkdir()
    s = store.create(ws)
    s.append_message({"role": "user", "content": "ok"})
    s.write_context()
    with s.transcript_path.open("a", encoding="utf-8") as fh:
        fh.write('{"v":1,"event_id":99,"type":"message"')  # 半行
    # 删 context 强制回放
    s.context_path.unlink()
    s2 = store.load(ws, s.session_id)
    assert len(s2.messages) == 1
    assert s2.messages[0]["content"] == "ok"


def test_tool_pairing_preserved_after_prune(tmp_path: Path):
    store = SessionStore(tmp_path / "home", tool_prune_chars=10)
    ws = tmp_path / "ws"
    ws.mkdir()
    s = store.create(ws)
    s.append_message({
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_x", "type": "function", "function": {"name": "r", "arguments": "{}"}}
        ],
    })
    s.append_message({"role": "tool", "tool_call_id": "call_x", "content": "abcdefghijklmnop"})
    assert s.messages[0]["tool_calls"][0]["id"] == "call_x"
    assert s.messages[1]["tool_call_id"] == "call_x"
    assert "tool_output_truncated" in s.messages[1]["content"]


def test_token_helpers():
    assert count_text_tokens("hello world") > 0
    assert "256k" in format_token_usage(128_000, 256_000) or "128" in format_token_usage(
        128_000, 256_000
    )


def test_needs_compact_threshold(tmp_path: Path):
    store = SessionStore(tmp_path / "home")
    ws = tmp_path / "ws"
    ws.mkdir()
    s = store.create(ws)
    s.append_message({"role": "user", "content": "hi"})
    assert s.needs_compact(
        overhead_tokens=0,
        context_window=1000,
        compact_threshold=0.7,
        reserved_output_tokens=0,
    ) is False
    # 人为灌满
    s.append_message({"role": "assistant", "content": "word " * 5000})
    assert s.needs_compact(
        overhead_tokens=0,
        context_window=1000,
        compact_threshold=0.7,
        reserved_output_tokens=100,
    ) is True
