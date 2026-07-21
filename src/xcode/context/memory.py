"""Workspace 级长期记忆：简单 JSONL 条目。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class MemoryStore:
    path: Path

    @classmethod
    def for_dir(cls, memory_dir: Path) -> MemoryStore:
        memory_dir.mkdir(parents=True, exist_ok=True)
        return cls(path=memory_dir / "memory.jsonl")

    def add(self, text: str, *, tags: list[str] | None = None) -> None:
        entry = {"ts": _utc_now(), "text": text, "tags": tags or []}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def list(self, *, limit: int = 50) -> list[dict]:
        if not self.path.is_file():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        items = [json.loads(line) for line in lines if line.strip()]
        return items[-limit:]

    def as_prompt_block(self, *, limit: int = 20) -> str:
        items = self.list(limit=limit)
        if not items:
            return ""
        body = "\n".join(f"- {it.get('text', '')}" for it in items)
        return f"## Long-term memory\n{body}"
