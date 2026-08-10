"""长期记忆 LLM 的提示词与 JSON 解析。

Stage1（抽取）：输入「本轮对话摘要文本」→ JSON {raw_bullets, rollout_summary}
Stage2（合并）：输入「完整 MEMORY + 完整 summary + 本批新信号」
  → JSON {unchanged:true} 或 {MEMORY_md, memory_summary_md} 两份**完整**正文

解析容错：去掉 markdown 围栏；失败时尝试抓第一个 {...}；再失败返回空/unchanged。
Stage2 若缺字段，调用方应**保留 pending** 以便 drain 重试，而不是当成成功清空。
"""

from __future__ import annotations

import json
import re
from typing import Any

STAGE1_SYSTEM = """\
你是项目长期记忆抽取器（stage1）。从本轮对话提取**稳定、可复用**的事实。
只提取：项目决策、约定、已验证经验。
不要提取：临时状态、命令输出、密钥、可直接从代码读出的事实、一次性调试细节。

输出严格 JSON 对象（不要 markdown 围栏）：
{
  "raw_bullets": ["一句话事实", ...],  // 可空数组
  "rollout_summary": "本轮与记忆相关的短摘要，无则空字符串"
}
"""

STAGE2_SYSTEM = """\
你是项目长期记忆 consolidation 编辑器（stage2）。
你维护两份 Markdown：
1) MEMORY.md — 可搜索注册表：按主题分组，含 keywords，可指向 rollout_summaries/ 路径
2) memory_summary.md — 极短总览；首行必须是 v1；含简要画像与 ## What's in Memory 路由

规则：
- 输入中的 MEMORY.md / memory_summary.md 是**完整**正文；返回时也必须是**完整**文档
- **禁止**删除或省略你未打算修改的既有段落（尤其是文件后半部分）
- 合并重复；仅在有明确新事实时改写或删除过时内容
- 不写密钥、临时状态、大段代码
- memory_summary 必须短（建议 < 80 行）
- MEMORY 是路由层，细节可留在 rollout 指针里
- 若没有值得更新的内容，返回 {"unchanged": true}

否则返回 JSON：
{
  "unchanged": false,
  "MEMORY_md": "完整 MEMORY.md 正文（须覆盖输入中全部仍有效内容）",
  "memory_summary_md": "完整 memory_summary.md 正文（首行 v1）"
}
不要输出其它字段或围栏。
"""


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}


def parse_stage1(text: str) -> tuple[list[str], str]:
    data = parse_json_object(text)
    bullets_raw = data.get("raw_bullets") or []
    bullets: list[str] = []
    if isinstance(bullets_raw, list):
        for item in bullets_raw:
            if isinstance(item, str) and item.strip():
                bullets.append(item.strip())
    summary = data.get("rollout_summary") or ""
    if not isinstance(summary, str):
        summary = ""
    return bullets, summary.strip()


def parse_stage2(text: str) -> tuple[bool, str | None, str | None]:
    """返回 (unchanged, memory_md, summary_md)。"""
    data = parse_json_object(text)
    if data.get("unchanged") is True:
        return True, None, None
    memory = data.get("MEMORY_md") or data.get("memory_md")
    summary = data.get("memory_summary_md") or data.get("summary_md")
    if not isinstance(memory, str) or not memory.strip():
        return True, None, None
    if not isinstance(summary, str) or not summary.strip():
        return True, None, None
    summary = summary.strip()
    if not summary.startswith("v1"):
        summary = "v1\n" + summary
    return False, memory.strip(), summary
