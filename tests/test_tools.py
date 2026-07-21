"""内置工具、协议与乐观锁测试。"""

from pathlib import Path

import pytest

from xcode.tools.base import ToolContext, resolve_workspace_path
from xcode.tools.builtins import BashTool, EditTool, LSTool, ReadTool, WriteTool, validate_bash_command
from xcode.tools.output import spill_large_output


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
    assert write.status == "success"
    assert "time_ms" in write.stats
    read = ReadTool().execute({"path": "a.txt"}, ctx)
    assert "hello" in read.content
    edit = EditTool().execute(
        {"path": "a.txt", "old_string": "hello", "new_string": "hola"},
        ctx,
    )
    assert edit.ok
    assert "hola" in (ws / "a.txt").read_text(encoding="utf-8")


def test_optimistic_lock_rejects_stale_edit(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    ctx = _ctx(ws)
    (ws / "a.txt").write_text("v1\n", encoding="utf-8")
    ReadTool().execute({"path": "a.txt"}, ctx)
    # 外部修改
    (ws / "a.txt").write_text("v2\n", encoding="utf-8")
    result = EditTool().execute(
        {"path": "a.txt", "old_string": "v2", "new_string": "v3"},
        ctx,
    )
    assert not result.ok
    assert result.error and result.error["code"] == "FILE_CHANGED"
    assert (ws / "a.txt").read_text(encoding="utf-8") == "v2\n"


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


def test_bash_hard_deny() -> None:
    assert validate_bash_command("sudo rm -rf /tmp/x") is not None
    assert validate_bash_command("rm -rf /") is not None
    assert validate_bash_command("echo hi") is None


def test_bash_deny_via_tool(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    result = BashTool().execute({"command": "sudo true"}, _ctx(ws))
    assert not result.ok
    assert result.error and result.error["code"] == "COMMAND_DENIED"


def test_spill_large_output(tmp_path: Path) -> None:
    session = tmp_path / "sess"
    session.mkdir()
    spill = spill_large_output(
        tool_name="Bash",
        full_output="x" * 500,
        session_data_dir=session,
        max_chars=100,
    )
    assert spill is not None
    assert Path(spill.full_path).is_file()
    assert len(spill.preview) < 500
