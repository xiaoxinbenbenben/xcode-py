"""内置工具测试。"""

from pathlib import Path

from xcode.tools.base import ToolContext, resolve_workspace_path
from xcode.tools.builtins import EditTool, LSTool, ReadTool, WriteTool
import pytest


def _ctx(ws: Path) -> ToolContext:
    return ToolContext(
        workspace=ws,
        session_data_dir=ws / ".sess",
        todos=[],
        max_output_chars=1000,
        memory_dir=ws / ".mem",
    )


def test_read_write_edit(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    ctx = _ctx(ws)
    write = WriteTool().execute({"path": "a.txt", "content": "hello\nworld\n"}, ctx)
    assert write.ok
    read = ReadTool().execute({"path": "a.txt"}, ctx)
    assert "hello" in read.content
    edit = EditTool().execute(
        {"path": "a.txt", "old_string": "hello", "new_string": "hola"},
        ctx,
    )
    assert edit.ok
    assert "hola" in (ws / "a.txt").read_text(encoding="utf-8")


def test_path_outside_workspace_rejected(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    with pytest.raises(PermissionError):
        resolve_workspace_path(ws, str(tmp_path / "other.txt"))


def test_ls(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "f.py").write_text("x", encoding="utf-8")
    result = LSTool().execute({"path": "."}, _ctx(ws))
    assert result.ok
    assert "f.py" in result.content
