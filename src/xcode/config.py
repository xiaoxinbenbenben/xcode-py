"""运行时配置：从环境变量与可选 .env 加载 LLM / 路径设置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(slots=True)
class Config:
    """LLM + 数据目录 + 窗口预算。环境变量见 load_config / .env.example。"""

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


def _load_dotenv_layers(
    *,
    project_root: Path,
    env_file: Path | None = None,
) -> list[Path]:
    """分层加载 .env：``override=False``，已存在的变量不被后文件覆盖。

    加载顺序（**先加载的优先**，后加载的只补还没设置的键）：
    1. 显式 ``env_file``
    2. 工作区 ``project_root/.env``、``cwd/.env``（业务项目常有自己的 .env）
    3. ``$XCODE_HOME/.env``、``~/.xcode/.env``（推荐放 OPENAI_*）
    4. 源码树 ``xcode-py/.env``（editable 开发时兜底）

    旧逻辑「找到第一个文件就 break」的问题：在 ``knowledge_search`` 下启动时，
    会只读到业务项目的 .env（只有 API_KEY / EMBEDDING_*，没有 OPENAI_*），
    于是 model 默认 gpt-4o-mini、api_key 为空，请求变成字面量 key ``missing`` 的 401。
    """
    loaded: list[Path] = []
    xcode_home = Path(os.getenv("XCODE_HOME", str(default_data_home()))).expanduser()
    # Path(__file__) = .../src/xcode/config.py → parents[2] = 仓库根（editable 安装）
    repo_env = Path(__file__).resolve().parents[2] / ".env"
    candidates = [
        env_file,
        project_root / ".env",
        Path.cwd() / ".env",
        xcode_home / ".env",
        default_data_home() / ".env",
        repo_env,
    ]
    seen: set[Path] = set()
    for path in candidates:
        if path is None:
            continue
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        load_dotenv(resolved, override=False)
        loaded.append(resolved)
    return loaded


def load_config(
    *,
    project_root: Path | None = None,
    env_file: Path | None = None,
) -> Config:
    """加载配置。

    输入：可选项目根与 .env 路径；副作用：分层 load_dotenv。
    输出：Config 实例（缺 key 时 api_key 为空字符串，启动时应明确报错）。
    """
    root = (project_root or Path.cwd()).resolve()
    _load_dotenv_layers(project_root=root, env_file=env_file)

    data_home = Path(os.getenv("XCODE_HOME", str(default_data_home()))).expanduser()

    def _num(name: str, default, conv):
        raw = os.getenv(name, "").strip()
        if not raw:
            return default
        try:
            return conv(raw)
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
        context_window=_num("XCODE_CONTEXT_WINDOW", 256_000, int),
        compact_threshold=_num("XCODE_COMPACT_THRESHOLD", 0.7, float),
        reserved_output_tokens=_num("XCODE_RESERVED_OUTPUT_TOKENS", 8192, int),
        tool_prune_chars=_num("XCODE_TOOL_PRUNE_CHARS", 16_000, int),
        transcript_hard_cap=_num("XCODE_TRANSCRIPT_HARD_CAP", 2_000_000, int),
    )
