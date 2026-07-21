"""分层上下文组装：L1 系统 / L2 项目与记忆 / L3 会话历史。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xcode.context.memory import MemoryStore
from xcode.context.mentions import MentionResult, preprocess_mentions
from xcode.runtime.session import SessionRuntime


SYSTEM_PROMPT = """你是 xcode，一个在用户本地仓库工作的 coding agent。
遵守：
- 优先用工具查看真实文件，再提出修改
- 路径相对于 workspace；不要越界访问
- 回答简洁，修改时用 Edit/Write；执行命令用 Bash
- 复杂任务用 TodoWrite 跟踪进度
"""


@dataclass(slots=True)
class ContextBundle:
    system: str
    mention: MentionResult
    history: list[dict[str, Any]]


def _read_optional(path: Path, *, limit: int = 8000) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:limit]


def build_context_bundle(
    *,
    user_input: str,
    session: SessionRuntime,
    tool_names: list[str],
    memory_dir: Path,
) -> ContextBundle:
    """组装本轮发给模型的系统提示与历史视图。"""
    workspace = session.workspace_root
    mention = preprocess_mentions(user_input, workspace)
    agents_md = _read_optional(workspace / "AGENTS.md")
    memory_block = MemoryStore.for_dir(memory_dir).as_prompt_block()

    layers = [
        SYSTEM_PROMPT.strip(),
        f"Workspace: {workspace}",
        f"Available tools: {', '.join(tool_names)}",
    ]
    if agents_md:
        layers.append("## AGENTS.md\n" + agents_md)
    if memory_block:
        layers.append(memory_block)
    if session.summary:
        layers.append("## Conversation summary\n" + session.summary)
    if mention.reminders:
        layers.append("## Mentions\n" + "\n".join(mention.reminders))

    history = list(session.messages)
    return ContextBundle(system="\n\n".join(layers), mention=mention, history=history)
