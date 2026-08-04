"""运行时配置：从环境变量与可选 .env 加载 LLM / 路径设置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(slots=True)
class Config:
    """Agent 运行所需的最小配置快照。"""

    api_key: str
    base_url: str
    model: str
    light_model: str
    data_home: Path


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
    return Config(
        api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip(),
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        light_model=os.getenv("OPENAI_LIGHT_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip(),
        data_home=data_home,
    )
