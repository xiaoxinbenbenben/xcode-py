"""任务系统 MVP：持久化任务图 + TaskRun 占位（不扩展编排）。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from xcode.tools.base import Tool, ToolContext, ToolResponse, failure, success, timed_ms


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

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        started = time.perf_counter()
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
        return success(ctx, args, text=json.dumps(task, ensure_ascii=False), summary=task["id"], data=task, time_ms=timed_ms(started))


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

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        started = time.perf_counter()
        store = TaskStore.load(ctx.session_data_dir)
        for task in store.tasks:
            if task["id"] == args["id"]:
                if args.get("status"):
                    task["status"] = str(args["status"])
                if args.get("title"):
                    task["title"] = str(args["title"])
                task["updated_at"] = _utc_now()
                store.save()
                return success(ctx, args, text=json.dumps(task, ensure_ascii=False), summary="updated", data=task, time_ms=timed_ms(started))
        return failure(ctx, args, code="NOT_FOUND", message="task not found", time_ms=timed_ms(started))


class TaskListTool(Tool):
    name = "TaskList"
    description = "List durable tasks."
    parameters = {"type": "object", "properties": {}}

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        started = time.perf_counter()
        store = TaskStore.load(ctx.session_data_dir)
        text = json.dumps(store.tasks, ensure_ascii=False, indent=2)
        return success(ctx, args, text=text, summary=f"{len(store.tasks)} tasks", time_ms=timed_ms(started))


class TaskGetTool(Tool):
    name = "TaskGet"
    description = "Get one task by id."
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        started = time.perf_counter()
        store = TaskStore.load(ctx.session_data_dir)
        for task in store.tasks:
            if task["id"] == args["id"]:
                return success(ctx, args, text=json.dumps(task, ensure_ascii=False), summary=task["id"], data=task, time_ms=timed_ms(started))
        return failure(ctx, args, code="NOT_FOUND", message="task not found", time_ms=timed_ms(started))


class TaskRunTool(Tool):
    name = "TaskRun"
    description = "Record a one-shot analysis sub-task prompt (MVP placeholder; no subagent)."
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "label": {"type": "string"},
        },
        "required": ["prompt"],
    }

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        # 占位：只落盘意图，不跑子代理（Team/Background 后续再对齐）
        started = time.perf_counter()
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
        return success(ctx, args, text=json.dumps(task, ensure_ascii=False), summary=task["id"], data=task, time_ms=timed_ms(started))


def task_tools() -> list[Tool]:
    return [
        TaskCreateTool(),
        TaskUpdateTool(),
        TaskListTool(),
        TaskGetTool(),
        TaskRunTool(),
    ]
