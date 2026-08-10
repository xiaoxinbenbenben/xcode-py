"""兼容旧 import 路径的薄封装。

历史代码可能 ``from xcode.ingest import MemoryIngester``；
实现已迁到 ``xcode.memory.pipeline``，此处仅 re-export，勿再加业务逻辑。
"""

from xcode.memory.pipeline import (
    MemoryPipeline,
    PipelineRegistry,
    RoundContent,
    is_blacklisted,
    should_extract,
    slice_round,
)

# 旧名兼容
MemoryIngester = MemoryPipeline
IngesterRegistry = PipelineRegistry

__all__ = [
    "IngesterRegistry",
    "MemoryIngester",
    "MemoryPipeline",
    "PipelineRegistry",
    "RoundContent",
    "is_blacklisted",
    "should_extract",
    "slice_round",
]
