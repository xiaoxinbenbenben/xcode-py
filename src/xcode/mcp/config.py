"""读 ~/.xcode/mcp.json 与 <ws>/.xcode/mcp.json；项目同名整条盖用户。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONNECT_TIMEOUT = 30.0
DEFAULT_CALL_TIMEOUT = 60.0


@dataclass(slots=True)
class McpServerSpec:
    """一条 MCP server 配置（展开后的值）。"""

    name: str
    type: str = "stdio"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout: float = DEFAULT_CALL_TIMEOUT


def load_mcp_server_specs(
    *,
    workspace: Path,
    data_home: Path,
) -> dict[str, McpServerSpec]:
    """合并用户 + 项目 mcp.json。只认这两处，不扫 .claude / 根 .mcp.json。"""
    root = workspace.resolve()
    merged: dict[str, Any] = {}
    for path in [data_home / "mcp.json", root / ".xcode" / "mcp.json"]:
        data = _read_json(path)
        if not data:
            continue
        servers = data.get("mcpServers", data)
        if isinstance(servers, dict):
            merged.update(servers)
    specs: dict[str, McpServerSpec] = {}
    for name, raw in merged.items():
        if not isinstance(raw, dict):
            continue
        spec = _spec_from_raw(name, raw, root)
        if spec.enabled:
            specs[name] = spec
    return specs


def _read_json(path: Path) -> dict[str, Any] | None:
    """读一个 JSON 对象；缺文件或坏 JSON 返回 None，不抛。"""
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _spec_from_raw(name: str, raw: dict[str, Any], project_root: Path) -> McpServerSpec:
    """把 mcpServers 里一条原始 dict 收成 McpServerSpec，并展开 ${…}。"""
    default_type = "http" if raw.get("url") else "stdio"
    raw_type = str(raw.get("type") or raw.get("transport") or default_type)
    if raw_type in {"streamable_http", "streamable-http"}:
        raw_type = "http"
    timeout_raw = raw.get("timeout", DEFAULT_CALL_TIMEOUT)
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError):
        timeout = DEFAULT_CALL_TIMEOUT
    if timeout <= 0:
        timeout = DEFAULT_CALL_TIMEOUT
    return McpServerSpec(
        name=name,
        type=raw_type,
        command=_expand(str(raw["command"]), project_root) if raw.get("command") else None,
        args=[_expand(str(arg), project_root) for arg in raw.get("args") or []],
        env={
            key: _expand(str(value), project_root)
            for key, value in dict(raw.get("env") or {}).items()
        },
        cwd=_expand(str(raw["cwd"]), project_root) if raw.get("cwd") else None,
        url=_expand(str(raw["url"]), project_root) if raw.get("url") else None,
        headers={
            key: _expand(str(value), project_root)
            for key, value in dict(raw.get("headers") or {}).items()
        },
        enabled=bool(raw.get("enabled", True)),
        timeout=timeout,
    )


def _expand(value: str, project_root: Path) -> str:
    """展开 ${HOME} / ${PROJECT_DIR} / 环境变量；缺键变空串。"""
    replacements = {
        "PROJECT_DIR": str(project_root),
        "HOME": str(Path.home()),
    }

    def replace(match: re.Match[str]) -> str:
        """单个 ${NAME}：先 HOME/PROJECT_DIR，再环境变量，没有则空串。"""
        name = match.group(1)
        return replacements.get(name, os.environ.get(name, ""))

    return re.sub(r"\$\{([^}]+)\}", replace, value)
