"""上下文组装：system 提示（todo #5）。

# 挂靠（已从树中移除，待回填）：Skill→#13；SQLite→#11；压缩/mention→#6；工具→#7
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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
) -> str:
    """组装发给模型的 system 主段（身份 / 现场 / 准则 / 项目说明）。"""
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
        ]
    )
    project = _project_memory(workspace)
    if project:
        parts.extend(["", "项目说明：", project])
    return "\n".join(parts)


def build_context_bundle(
    *,
    user_input: str,
    session: SessionRuntime,
    tool_names: list[str],
    model: str | None = None,
) -> ContextBundle:
    """组装本轮上下文：仅 system + 用户原文。"""
    system = assemble_system_prompt(
        workspace=session.workspace_root,
        tool_names=tool_names,
        model=model,
    )
    return ContextBundle(
        system=system,
        user_text=user_input,
        history=list(session.messages),
    )
