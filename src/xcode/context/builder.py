"""组装发给模型的 system 文本（身份 / XCODE / 仓库地图 / memory_summary / skills）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from xcode.memory import MemoryStore, summary_prompt_block
from xcode.runtime.session import SessionRuntime
from xcode.skill import SkillRegistry

_PROJECT_FILE_LIMIT = 4000
_PROJECT_TOTAL_LIMIT = 8000


@dataclass(slots=True)
class ContextBundle:
    system: str
    user_text: str


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
    skills: SkillRegistry | None = None,
) -> str:
    """组装发给模型的 system 主段（身份 / 现场 / 准则 / 项目说明 / 长期记忆摘要 / skills）。"""
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
            "- 长期记忆由后台管线从对话自动抽取并合并；不要用 bash/write 改 memories 目录下的文件。"
            "用户要求「记住某事」时直接确认即可，下一轮后可 memory_read 核对。",
            "- 创建或修改源码用 write_file / edit_file，不要用 bash（sed -i、echo >、perl -pi）。"
            "bash 留给测试、git、构建。用户要求撤回上一轮文件改动时用 revert_turn。",
            "- 任务明显匹配 Available skills 中某条时，先 load_skill 再按说明书做；不要一次预载全部。"
            "说明书里的相对路径以 Base directory 为根，用 read_file 读文档、bash 跑脚本。",
            "- 找函数、类、方法名用 search_code；找字符串或正则用 grep。"
            "未覆盖的语言、注释和字符串字面量仍用 grep。",
        ]
    )
    project = _project_memory(workspace)
    if project:
        parts.extend(["", "项目说明：", project])
    if data_home is not None:
        from xcode.code_index import CodeIndexStore, render_repo_map

        index = CodeIndexStore(data_home, workspace)
        try:
            repo_map = render_repo_map(index)
        finally:
            index.close()
        if repo_map:
            parts.extend(["", "仓库地图：", repo_map])
    # 长期记忆：仅 summary + 读指引（见 memory.store.summary_prompt_block）
    if data_home is not None:
        store = MemoryStore(data_home, workspace)
        memory = summary_prompt_block(store)
        if memory:
            parts.extend(["", memory])
    registry = skills or SkillRegistry(workspace, data_home=data_home)
    catalog = registry.catalog_text()
    if catalog:
        parts.extend(["", catalog])
    return "\n".join(parts)


def build_context_bundle(
    *,
    user_input: str,
    session: SessionRuntime,
    tool_names: list[str],
    model: str | None = None,
    data_home: Path | None = None,
    skills: SkillRegistry | None = None,
) -> ContextBundle:
    """组装本轮上下文：system + 用户原文。"""
    system = assemble_system_prompt(
        workspace=session.workspace_root,
        tool_names=tool_names,
        model=model,
        data_home=data_home,
        skills=skills,
    )
    return ContextBundle(
        system=system,
        user_text=user_input,
    )
