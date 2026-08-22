"""长期记忆磁盘层：memories/ 下的 Markdown 读写，不调 LLM。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from xcode.runtime.session import project_key

SUMMARY_NAME = "memory_summary.md"
MEMORY_NAME = "MEMORY.md"
RAW_NAME = "raw_memories.md"
ROLLOUTS_DIR = "rollout_summaries"

SUMMARY_INJECT_LIMIT = 4000
READ_LIMIT = 12000

_EMPTY_SUMMARY = """\
v1
# Memory Summary

（尚无长期记忆）

## What's in Memory
- （空）
"""

_EMPTY_MEMORY = """\
v1
# MEMORY

按主题组织的项目长期记忆注册表。模型按需检索本文件；细节见 rollout_summaries/。
"""


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class MemoryStore:
    """绑定「一个 workspace」的 memories 根目录上的全部文件操作。"""

    def __init__(self, data_home: Path, workspace: Path) -> None:
        self.data_home = data_home
        self.workspace = workspace.resolve()
        # 与 session 共用 project_key，保证同一项目的会话与记忆落在同一 project 桶下
        self.root = data_home / "projects" / project_key(self.workspace) / "memories"

    def ensure_layout(self) -> None:
        """缺啥补啥：目录 + 空模板文件，幂等可重复调用。"""
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ROLLOUTS_DIR).mkdir(parents=True, exist_ok=True)
        summary = self.root / SUMMARY_NAME
        memory = self.root / MEMORY_NAME
        if not summary.is_file():
            summary.write_text(_EMPTY_SUMMARY, encoding="utf-8")
        if not memory.is_file():
            memory.write_text(_EMPTY_MEMORY, encoding="utf-8")
        raw = self.root / RAW_NAME
        if not raw.is_file():
            raw.write_text("# raw_memories\n\n", encoding="utf-8")

    def resolve_rel(self, rel: str) -> Path:
        """解析相对 memories 根的路径；拒绝越界与绝对路径。"""
        rel = rel.strip().replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise PermissionError(f"invalid memory path: {rel!r}")
        path = (self.root / rel).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError as exc:
            raise PermissionError(f"path outside memories: {rel}") from exc
        return path

    def read_rel(self, rel: str, *, limit: int | None = READ_LIMIT) -> str:
        """读相对路径文件。limit=None 表示全文（consolidation 必须全文）。"""
        self.ensure_layout()
        path = self.resolve_rel(rel)
        if not path.is_file():
            raise FileNotFoundError(rel)
        text = path.read_text(encoding="utf-8", errors="replace")
        if limit is None:
            return text
        return text[:limit]

    def read_summary(self, *, limit: int | None = SUMMARY_INJECT_LIMIT) -> str:
        self.ensure_layout()
        try:
            text = (self.root / SUMMARY_NAME).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        if limit is None:
            return text
        return text[:limit]

    def summary_is_placeholder(self) -> bool:
        """summary 是否仍是空模板（stage2 尚未合并出有效摘要）。"""
        text = self.read_summary(limit=None)
        return not text.strip() or text.strip() == _EMPTY_SUMMARY.strip()

    def memory_has_entries(self) -> bool:
        """MEMORY.md 是否已有超过空模板的内容。"""
        try:
            text = self.read_rel(MEMORY_NAME, limit=None)
        except (FileNotFoundError, OSError, PermissionError):
            return False
        body = text.strip()
        return bool(body) and body != _EMPTY_MEMORY.strip()

    def append_raw(self, body: str) -> None:
        self.ensure_layout()
        path = self.root / RAW_NAME
        with path.open("a", encoding="utf-8") as fh:
            fh.write(body)
            if not body.endswith("\n"):
                fh.write("\n")

    def write_rollout(self, session_id: str, body: str) -> str:
        """写入 rollout 文件，返回相对路径。"""
        self.ensure_layout()
        safe_sid = re.sub(r"[^a-zA-Z0-9_-]+", "-", session_id)[:40] or "sess"
        rel = f"{ROLLOUTS_DIR}/{safe_sid}-{_utc_stamp()}.md"
        path = self.resolve_rel(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.strip() + "\n", encoding="utf-8")
        return rel

    def atomic_write(self, rel: str, body: str) -> None:
        """原子覆盖（tmp + rename）；用于 MEMORY.md / memory_summary.md。"""
        self.ensure_layout()
        path = self.resolve_rel(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body if body.endswith("\n") else body + "\n", encoding="utf-8")
        tmp.replace(path)

    def clear(self) -> None:
        """清空记忆目录并重建空模板。"""
        if self.root.is_dir():
            for child in self.root.rglob("*"):
                if child.is_file():
                    child.unlink(missing_ok=True)
            for child in sorted(self.root.rglob("*"), reverse=True):
                if child.is_dir():
                    try:
                        child.rmdir()
                    except OSError:
                        pass
        self.ensure_layout()

    def grep(self, query: str, *, limit: int = 30) -> str:
        """在 MEMORY.md 与 rollout_summaries 中做子串搜索。"""
        self.ensure_layout()
        words = [w.lower() for w in query.split() if w]
        if not words:
            return "(empty query)"
        hits: list[str] = []
        files = [self.root / MEMORY_NAME]
        rollouts = self.root / ROLLOUTS_DIR
        if rollouts.is_dir():
            files.extend(sorted(rollouts.glob("*.md"), reverse=True)[:50])
        for path in files:
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel = path.relative_to(self.root).as_posix()
            for i, line in enumerate(lines, start=1):
                lower = line.lower()
                if all(w in lower for w in words):
                    hits.append(f"{rel}:{i}: {line.strip()}")
                    if len(hits) >= limit:
                        return "\n".join(hits)
        return "\n".join(hits) if hits else "(no matches)"


def summary_prompt_block(store: MemoryStore) -> str:
    """组装塞进 system 的长期记忆段落。

    故意不注入 MEMORY 全文，避免每轮烧大量 token。
    模型若需要细节，按指引调用 memory_grep / memory_read。
    """
    store.ensure_layout()
    summary = store.read_summary()
    if not summary.strip() or summary.strip() == _EMPTY_SUMMARY.strip():
        # 仍注入指引，便于模型知道如何用工具
        body = "（尚无浓缩摘要；需要时 memory_read MEMORY.md）"
    else:
        body = summary
    root = str(store.root)
    return (
        "## 长期记忆（系统生成，勿当项目规范；规范见 XCODE.md）\n"
        f"记忆目录（memory_read / memory_grep 的根）：`{root}`\n"
        f"- 下方已提供 `{SUMMARY_NAME}`，**不要**再 memory_read 它。\n"
        f"- 需要细节：先 `memory_grep` 或 `memory_read(\"{MEMORY_NAME}\")`；\n"
        f"  仅当 MEMORY 指向具体 rollout 时再读 `rollout_summaries/...`（最多 1～2 个）。\n"
        f"\n### {SUMMARY_NAME}\n{body}"
    )


def format_raw_append(
    *,
    session_id: str,
    bullets: list[str],
    rollout_rel: str | None,
) -> str:
    lines = [
        "\n---\n",
        f"## {_utc_now()}  session={session_id}\n",
    ]
    if bullets:
        lines.append("### bullets\n")
        for b in bullets:
            lines.append(f"- {b}\n")
    if rollout_rel:
        lines.append(f"\nrollout: {rollout_rel}\n")
    return "".join(lines)
