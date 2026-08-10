"""组装每轮发给模型的 system / user 文本（不负责会话落盘）。

## 本轮上下文从哪来
- system：身份准则 + 工作区 XCODE.md/XCODE.local.md + **长期记忆 summary 段**
- user_text：用户原文（@ 文件等由上游处理；此处原样）
- history：调用方传入的 session.messages 快照（实际送模仍以 session.messages 为准）

长期记忆：只通过 summary_prompt_block 注入短 summary，细节靠 memory_* 工具。
会话压缩（compact）在 runtime.session / agent 里做，不在本文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from xcode.memory import MemoryStore, summary_prompt_block
from xcode.runtime.session import SessionRuntime

_PROJECT_FILE_LIMIT = 4000
_PROJECT_TOTAL_LIMIT = 8000


@dataclass(slots=True)
class ContextBundle:
    system: str
    user_text: str
    history: list[dict[str, Any]]


def _read_optional(path: Path, *, limit: int = _PROJECT_FILE_LIMIT) -> str:
    """读可选文本文件；不存在或读失败则返回空串。"""
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _project_memory(workspace: Path) -> str:
    """拼根目录项目说明：XCODE.md + XCODE.local.md。"""
    chunks: list[str] = []
    for name in ("XCODE.md", "XCODE.local.md"):
        text = _read_optional(workspace / name)
        if text:
            chunks.append(f"## {name}\n{text}")
    return "\n\n".join(chunks)[:_PROJECT_TOTAL_LIMIT]


def assemble_system_prompt(
    *,
    workspace: Path,
    tool_names: list[str],
    model: str | None = None,
    data_home: Path | None = None,
) -> str:
    """组装发给模型的 system 主段（身份 / 现场 / 准则 / 项目说明 / 长期记忆摘要）。"""
    parts = [
        "你是 xcode，一个在用户本地仓库工作的 coding agent。",
        f"当前时间：{datetime.now().isoformat(timespec='seconds')}",
        f"工作目录：{workspace.resolve()}",
    ]
    if model:
        parts.append(f"模型：{model}")
    parts.extend(
        [
            f"可用工具：{', '.join(tool_names) if tool_names else '(无)'}",
            "",
            "准则：",
            "- 简洁、直接，以落地实现为准。",
            "- 需要时用工具查看真实文件、搜索代码、验证行为，不要凭空猜测。",
            "- 优先用确定性的本地工具，再下结论。",
            "- 改文件时保持改动范围可控。",
            "- 原样保留用户给出的 URL 与标识符，除非工具结果证明需要改。",
            "- 仅在继续会有风险时才追问澄清。",
            "- 项目说明以工作区 XCODE.md / XCODE.local.md（及代码）为准；"
            "长期记忆（memory_summary）仅为对话中沉淀的提示，冲突时以 XCODE/代码为准。",
            "- 长期记忆：system 仅含 memory_summary；细节用 memory_grep / memory_read 读 MEMORY.md 或 rollout；"
            "不要再打开 memory_summary。",
        ]
    )
    project = _project_memory(workspace)
    if project:
        parts.extend(["", "项目说明：", project])
    # 长期记忆：仅 summary + 读指引（见 memory.store.summary_prompt_block）
    if data_home is not None:
        store = MemoryStore(data_home, workspace)
        memory = summary_prompt_block(store)
        if memory:
            parts.extend(["", memory])
    return "\n".join(parts)


def build_context_bundle(
    *,
    user_input: str,
    session: SessionRuntime,
    tool_names: list[str],
    model: str | None = None,
    data_home: Path | None = None,
) -> ContextBundle:
    """组装本轮上下文：仅 system + 用户原文。"""
    system = assemble_system_prompt(
        workspace=session.workspace_root,
        tool_names=tool_names,
        model=model,
        data_home=data_home,
    )
    return ContextBundle(
        system=system,
        user_text=user_input,
        history=list(session.messages),
    )
