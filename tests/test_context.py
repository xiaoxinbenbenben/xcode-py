"""@file 提及与压缩测试。"""

from pathlib import Path

from xcode.context.compaction import (
    compact_messages,
    estimate_chars,
    should_auto_compact,
)
from xcode.context.mentions import preprocess_mentions
from xcode.context.memory import MemoryStore


def test_mentions(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# hi", encoding="utf-8")
    result = preprocess_mentions("请看 @README.md 然后总结", tmp_path)
    assert "README.md" in result.mentioned_files
    assert result.reminders


def test_compaction() -> None:
    messages = [{"role": "user", "content": f"m{i}"} for i in range(12)]
    assert should_auto_compact(messages, threshold_turns=10)
    compacted, summary = compact_messages(messages, keep_last=4)
    assert len(compacted) == 4
    assert "user:" in summary


def test_compaction_preserves_tool_pairs() -> None:
    messages = [
        {"role": "user", "content": "u0"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "file"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "done"},
    ]
    # keep_last=2 会切到 tool 附近；应把 assistant+tool 整组保留或整组摘要掉
    compacted, summary = compact_messages(messages, keep_last=2)
    assert not (compacted and compacted[0].get("role") == "tool")
    assert "tools=Read" in summary or "assistant:tools" in summary or len(compacted) >= 2


def test_compaction_char_threshold() -> None:
    messages = [{"role": "user", "content": "x" * 1000} for _ in range(5)]
    assert estimate_chars(messages) >= 5000
    assert should_auto_compact(messages, threshold_turns=100, threshold_chars=4000)


def test_memory(tmp_path: Path) -> None:
    store = MemoryStore.for_dir(tmp_path / "mem")
    store.add("prefer pytest")
    block = store.as_prompt_block()
    assert "prefer pytest" in block
