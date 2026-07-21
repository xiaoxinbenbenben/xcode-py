"""任务系统 MVP：持久化任务图 + TaskRun 一次性子任务记录。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from xcode.tools.base import Tool, ToolContext, ToolResult


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class TaskStore:
    """把任务列表存在会话目录下的 tasks.json。"""

    path: Path
    tasks: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, session_data_dir: Path) -> TaskStore:
        path = session_data_dir / "tasks.json"
        tasks: list[dict[str, Any]] = []
        if path.is_file():
            tasks = json.loads(path.read_text(encoding="utf-8"))
        return cls(path=path, tasks=tasks)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.tasks, ensure_ascii=False, indent=2), encoding="utf-8")


class TaskCreateTool(Tool):
    name = "TaskCreate"
    description = "Create a durable task item."
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["title"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        store = TaskStore.load(ctx.session_data_dir)
        task = {
            "id": f"task-{uuid4().hex[:10]}",
            "title": str(args["title"]),
            "description": str(args.get("description") or ""),
            "status": "pending",
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        }
        store.tasks.append(task)
        store.save()
        return ToolResult(ok=True, summary=task["id"], data=task)


class TaskUpdateTool(Tool):
    name = "TaskUpdate"
    description = "Update a task status or title."
    parameters = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "status": {"type": "string"},
            "title": {"type": "string"},
        },
        "required": ["id"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        store = TaskStore.load(ctx.session_data_dir)
        for task in store.tasks:
            if task["id"] == args["id"]:
                if args.get("status"):
                    task["status"] = str(args["status"])
                if args.get("title"):
                    task["title"] = str(args["title"])
                task["updated_at"] = _utc_now()
                store.save()
                return ToolResult(ok=True, summary="updated", data=task)
        return ToolResult(ok=False, summary="task not found")


class TaskListTool(Tool):
    name = "TaskList"
    description = "List durable tasks."
    parameters = {"type": "object", "properties": {}}

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        store = TaskStore.load(ctx.session_data_dir)
        return ToolResult(
            ok=True,
            summary=f"{len(store.tasks)} tasks",
            content=json.dumps(store.tasks, ensure_ascii=False, indent=2),
        )


class TaskGetTool(Tool):
    name = "TaskGet"
    description = "Get one task by id."
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        store = TaskStore.load(ctx.session_data_dir)
        for task in store.tasks:
            if task["id"] == args["id"]:
                return ToolResult(ok=True, summary=task["id"], data=task)
        return ToolResult(ok=False, summary="task not found")


class TaskRunTool(Tool):
    name = "TaskRun"
    description = "Record a one-shot analysis sub-task prompt (MVP: store only)."
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "label": {"type": "string"},
        },
        "required": ["prompt"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # MVP：记录子任务意图，完整子代理循环可后续扩展
        store = TaskStore.load(ctx.session_data_dir)
        task = {
            "id": f"run-{uuid4().hex[:10]}",
            "title": str(args.get("label") or "TaskRun"),
            "description": str(args["prompt"]),
            "status": "queued",
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "kind": "task_run",
        }
        store.tasks.append(task)
        store.save()
        return ToolResult(ok=True, summary=task["id"], data=task)


def task_tools() -> list[Tool]:
    return [
        TaskCreateTool(),
        TaskUpdateTool(),
        TaskListTool(),
        TaskGetTool(),
        TaskRunTool(),
    ]
