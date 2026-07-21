"""任务工具测试。"""

from pathlib import Path

from xcode.tasks.store import TaskCreateTool, TaskListTool
from xcode.tools.base import ToolContext


def test_task_create_list(tmp_path: Path) -> None:
    ctx = ToolContext(
        workspace=tmp_path,
        session_data_dir=tmp_path / "sess",
        todos=[],
        max_output_chars=1000,
        memory_dir=tmp_path / "mem",
    )
    created = TaskCreateTool().execute({"title": "ship", "description": "mvp"}, ctx)
    assert created.ok
    listed = TaskListTool().execute({}, ctx)
    assert "ship" in listed.content
