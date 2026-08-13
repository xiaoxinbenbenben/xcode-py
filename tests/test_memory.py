"""Codex 式 Markdown 记忆库：目录、读写、注入、grep。"""

from __future__ import annotations

import pytest

from xcode.memory.store import (
    MEMORY_NAME,
    SUMMARY_NAME,
    MemoryStore,
    summary_prompt_block,
)


def _store(tmp_path, name: str = "proj") -> MemoryStore:
    ws = tmp_path / name
    ws.mkdir(exist_ok=True)
    return MemoryStore(tmp_path / "home", ws)


def test_ensure_layout_creates_templates(tmp_path):
    store = _store(tmp_path)
    store.ensure_layout()
    assert (store.root / SUMMARY_NAME).is_file()
    assert (store.root / MEMORY_NAME).is_file()
    assert (store.root / "raw_memories.md").is_file()
    assert (store.root / "rollout_summaries").is_dir()
    assert store.read_summary().startswith("v1")


def test_scope_isolation(tmp_path):
    a = _store(tmp_path, "a")
    b = _store(tmp_path, "b")
    a.ensure_layout()
    b.ensure_layout()
    a.atomic_write(MEMORY_NAME, "v1\n# A only\n")
    assert "A only" in a.read_rel(MEMORY_NAME)
    assert "A only" not in b.read_rel(MEMORY_NAME)


def test_path_traversal_rejected(tmp_path):
    store = _store(tmp_path)
    store.ensure_layout()
    with pytest.raises(PermissionError):
        store.resolve_rel("../secret")
    with pytest.raises(PermissionError):
        store.resolve_rel("/etc/passwd")


def test_atomic_write_and_append_raw(tmp_path):
    store = _store(tmp_path)
    store.atomic_write(MEMORY_NAME, "v1\n# body\n")
    store.append_raw("\n## chunk\n- fact\n")
    assert "# body" in store.read_rel(MEMORY_NAME)
    raw = store.read_rel("raw_memories.md")
    assert "fact" in raw


def test_write_rollout_returns_rel(tmp_path):
    store = _store(tmp_path)
    rel = store.write_rollout("sess-abc", "hello rollout")
    assert rel.startswith("rollout_summaries/")
    assert "hello rollout" in store.read_rel(rel)


def test_grep_and_query(tmp_path):
    store = _store(tmp_path)
    store.atomic_write(MEMORY_NAME, "v1\n# MEMORY\n使用 SQLite 存会话\n提交前跑 ruff\n")
    hits = store.grep("SQLite")
    assert "SQLite" in hits
    assert store.grep("完全不存在的词xyz") == "(no matches)"


def test_clear_restores_templates(tmp_path):
    store = _store(tmp_path)
    store.atomic_write(MEMORY_NAME, "v1\n# junk\n")
    store.write_rollout("s1", "x")
    store.clear()
    assert "junk" not in store.read_rel(MEMORY_NAME)
    assert store.read_summary().startswith("v1")


def test_summary_prompt_block(tmp_path):
    store = _store(tmp_path)
    store.atomic_write(SUMMARY_NAME, "v1\n# Memory Summary\n项目用 pytest\n")
    block = summary_prompt_block(store)
    assert "## 长期记忆" in block
    assert "memory_summary.md" in block
    assert "pytest" in block
    assert "MEMORY.md" in block


def test_summary_placeholder_and_memory_entries(tmp_path):
    store = _store(tmp_path)
    store.ensure_layout()
    assert store.summary_is_placeholder()
    assert not store.memory_has_entries()
    store.atomic_write(MEMORY_NAME, "v1\n# MEMORY\n\n## 开发约定\n- 注释要写清楚\n")
    assert store.memory_has_entries()
    assert store.summary_is_placeholder()  # MEMORY 有内容但 summary 仍空
    store.atomic_write(SUMMARY_NAME, "v1\n# Memory Summary\n有注释约定\n\n## What's in Memory\n- 约定\n")
    assert not store.summary_is_placeholder()
