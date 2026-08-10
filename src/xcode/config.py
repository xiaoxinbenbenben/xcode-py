"""运行时配置：从环境变量与可选 .env 加载 LLM / 路径设置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(slots=True)
class Config:
    """Agent 运行所需的配置快照（环境变量见 load_config / .env.example）。

    LLM：api_key / base_url / model / light_model
    路径：data_home（会话 + 记忆落盘根目录，默认 ~/.xcode）
    会话窗口（会话/历史子系统）：
      context_window          标称上下文长度，默认 256k
      compact_threshold       达到 window 的该比例则 compact，默认 0.7
      reserved_output_tokens  预算里预留给模型输出，默认 8k
      tool_prune_chars        送模时单条 tool 输出保留字符数
      transcript_hard_cap     JSONL 单事件硬顶（超则标记 truncated）
    light_model 同时用于：长期记忆 stage1/2、会话 compact 摘要。
    """

    api_key: str
    base_url: str
    model: str
    light_model: str
    data_home: Path
    context_window: int = 256_000
    compact_threshold: float = 0.7
    reserved_output_tokens: int = 8192
    tool_prune_chars: int = 16_000
    transcript_hard_cap: int = 2_000_000


def default_data_home() -> Path:
    """返回用户级数据根目录（会话落盘处）。"""
    return Path.home() / ".xcode"


def load_config(
    *,
    project_root: Path | None = None,
    env_file: Path | None = None,
) -> Config:
    """加载配置。

    输入：可选项目根与 .env 路径；副作用：若存在则 load_dotenv。
    输出：Config 实例（缺 key 时 api_key 为空字符串，由 doctor/运行时再提示）。
    """
    root = (project_root or Path.cwd()).resolve()
    candidates = [
        env_file,
        root / ".env",
        Path.cwd() / ".env",
    ]
    for path in candidates:
        if path is not None and path.is_file():
            load_dotenv(path, override=False)
            break

    data_home = Path(os.getenv("XCODE_HOME", str(default_data_home()))).expanduser()

    def _int(name: str, default: int) -> int:
        raw = os.getenv(name, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def _float(name: str, default: float) -> float:
        raw = os.getenv(name, "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    return Config(
        api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip(),
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        light_model=os.getenv(
            "OPENAI_LIGHT_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        ).strip(),
        data_home=data_home,
        context_window=_int("XCODE_CONTEXT_WINDOW", 256_000),
        compact_threshold=_float("XCODE_COMPACT_THRESHOLD", 0.7),
        reserved_output_tokens=_int("XCODE_RESERVED_OUTPUT_TOKENS", 8192),
        tool_prune_chars=_int("XCODE_TOOL_PRUNE_CHARS", 16_000),
        transcript_hard_cap=_int("XCODE_TRANSCRIPT_HARD_CAP", 2_000_000),
    )
