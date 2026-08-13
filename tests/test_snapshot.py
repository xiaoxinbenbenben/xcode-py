"""文件快照：write/edit 挂钩、封轮、restore、配额、bash 软拦。"""

from __future__ import annotations

import asyncio

from xcode.runtime.snapshot import MAX_FILE_BYTES, MAX_NAMED, SnapshotStore
from xcode.tools.base import ToolContext
from xcode.tools.builtins import (
    BashTool,
    EditFileTool,
    RevertTurnTool,
    WriteFileTool,
    _bash_overlap,
)


def _store(tmp_path) -> tuple[SnapshotStore, ToolContext]:
    ws = tmp_path / "ws"
    ws.mkdir()
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    snaps = SnapshotStore(session_dir, ws)
    snaps.begin_turn()
    ctx = ToolContext(workspace=ws, snapshot=snaps)
    return snaps, ctx


def test_edit_then_restore_last(tmp_path):
    snaps, ctx = _store(tmp_path)
    path = ctx.workspace / "a.py"
    path.write_text("old\n", encoding="utf-8")
    asyncio.run(
        EditFileTool().execute(
            {"path": "a.py", "old_string": "old", "new_string": "new"},
            ctx,
        )
    )
    assert path.read_text(encoding="utf-8") == "new\n"
    snaps.seal_turn()
    report = snaps.restore_last()
    assert "a.py" in report.restored
    assert path.read_text(encoding="utf-8") == "old\n"


def test_new_file_restore_deletes(tmp_path):
    snaps, ctx = _store(tmp_path)
    asyncio.run(
        WriteFileTool().execute({"path": "born.py", "content": "hi"}, ctx)
    )
    assert (ctx.workspace / "born.py").is_file()
    snaps.seal_turn()
    report = snaps.restore_last()
    assert "born.py" in report.deleted
    assert not (ctx.workspace / "born.py").exists()


def test_two_edits_same_turn_restore_first_before(tmp_path):
    snaps, ctx = _store(tmp_path)
    path = ctx.workspace / "a.py"
    path.write_text("v0", encoding="utf-8")
    asyncio.run(
        EditFileTool().execute(
            {"path": "a.py", "old_string": "v0", "new_string": "v1"}, ctx
        )
    )
    asyncio.run(
        EditFileTool().execute(
            {"path": "a.py", "old_string": "v1", "new_string": "v2"}, ctx
        )
    )
    snaps.seal_turn()
    snaps.restore_last()
    assert path.read_text(encoding="utf-8") == "v0"


def test_empty_turn_does_not_wipe_last(tmp_path):
    snaps, ctx = _store(tmp_path)
    path = ctx.workspace / "a.py"
    path.write_text("keep-before", encoding="utf-8")
    asyncio.run(
        EditFileTool().execute(
            {"path": "a.py", "old_string": "keep-before", "new_string": "after"},
            ctx,
        )
    )
    snaps.seal_turn()
    snaps.begin_turn()
    snaps.seal_turn()
    path.write_text("after", encoding="utf-8")
    snaps.restore_last()
    assert path.read_text(encoding="utf-8") == "keep-before"


def test_named_snapshot_and_undo(tmp_path):
    snaps, ctx = _store(tmp_path)
    path = ctx.workspace / "a.py"
    path.write_text("one", encoding="utf-8")
    asyncio.run(
        EditFileTool().execute(
            {"path": "a.py", "old_string": "one", "new_string": "two"}, ctx
        )
    )
    snaps.seal_turn()
    name = snaps.save_named("ok")
    assert name == "ok"
    path.write_text("three", encoding="utf-8")
    snaps.restore_named("ok")
    assert path.read_text(encoding="utf-8") == "two"
    snaps.restore_undo()
    assert path.read_text(encoding="utf-8") == "three"


def test_too_large_skipped(tmp_path):
    snaps, ctx = _store(tmp_path)
    path = ctx.workspace / "big.bin"
    path.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    snaps.note_before_write("big.bin")
    snaps.seal_turn()
    report = snaps.restore_last()
    assert any(p == "big.bin" and "too_large" in why for p, why in report.skipped)


def test_named_eviction(tmp_path):
    snaps, ctx = _store(tmp_path)
    (ctx.workspace / "a.py").write_text("x", encoding="utf-8")
    snaps.note_before_write("a.py")
    snaps.seal_turn()
    for i in range(MAX_NAMED + 3):
        snaps.save_named(f"n{i:02d}")
    named = list((snaps.root / "named").glob("*.json"))
    assert len(named) == MAX_NAMED


def test_reserved_name_rejected(tmp_path):
    snaps, _ctx = _store(tmp_path)
    try:
        snaps.save_named("last")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "reserved" in str(exc)


def test_bash_overlap_sed():
    assert _bash_overlap("sed -i 's/a/b/' foo.py") == "sed-inplace"
    assert _bash_overlap("sed -n 'p' foo.py") is None
    assert _bash_overlap("pytest -q") is None


def test_bash_tool_blocks_sed(tmp_path):
    _snaps, ctx = _store(tmp_path)
    result = asyncio.run(BashTool().execute({"command": "sed -i s/a/b/ a.py"}, ctx))
    assert result.is_error
    assert "write_file" in result.text


def test_revert_turn_tool(tmp_path):
    snaps, ctx = _store(tmp_path)
    path = ctx.workspace / "a.py"
    path.write_text("old", encoding="utf-8")
    asyncio.run(
        EditFileTool().execute(
            {"path": "a.py", "old_string": "old", "new_string": "new"}, ctx
        )
    )
    snaps.seal_turn()
    result = asyncio.run(RevertTurnTool().execute({}, ctx))
    assert not result.is_error
    assert path.read_text(encoding="utf-8") == "old"
