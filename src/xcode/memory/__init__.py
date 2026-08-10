"""长期记忆包（跨会话；与 runtime.session 的「单场对话历史」正交）。

## 设计要点（读代码前先看这里）
- **只存项目级** Markdown：memory_summary.md / MEMORY.md / raw_memories.md / rollout_summaries/
- **注入模型**：system 里只塞 summary + 读指引，不塞 MEMORY 全文
- **按需读**：工具 memory_read / memory_grep
- **写入两阶段**：
  stage1 每轮抽取 bullets + rollout 摘要 → 落 raw / rollout 文件
  stage2 防抖合并进 MEMORY + summary（≥3 信号或空闲 5 分钟，退出 drain）
- **权威文件** MEMORY/summary 只经 consolidation 原子写；禁止业务代码直接改

对外常用：MemoryStore、MemoryPipeline、PipelineRegistry、slice_round、should_extract。
"""

from xcode.memory.pipeline import (
    MemoryPipeline,
    PipelineRegistry,
    RoundContent,
    is_blacklisted,
    should_extract,
    slice_round,
)
from xcode.memory.store import MemoryStore, summary_prompt_block

__all__ = [
    "MemoryPipeline",
    "MemoryStore",
    "PipelineRegistry",
    "RoundContent",
    "is_blacklisted",
    "should_extract",
    "slice_round",
    "summary_prompt_block",
]
