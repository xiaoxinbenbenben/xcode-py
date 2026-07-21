"""@file 提及与压缩测试。"""

from pathlib import Path

from xcode.context.compaction import compact_messages, should_auto_compact
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


def test_memory(tmp_path: Path) -> None:
    store = MemoryStore.for_dir(tmp_path / "mem")
    store.add("prefer pytest")
    block = store.as_prompt_block()
    assert "prefer pytest" in block
