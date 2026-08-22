"""按 mtime 增量建索引；写文件后只重解析改过的。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

from xcode.code_index.parse import language_for_path, parse_file
from xcode.code_index.store import CodeIndexStore

SKIP_DIR_NAMES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "dist",
    "build",
    "target",
    "vendor",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "coverage",
    ".next",
    "out",
    "site-packages",
}
MAX_FILE_BYTES = 1 * 1024 * 1024


def iter_index_files(workspace: Path) -> Iterator[Path]:
    """产出可索引源文件；不跟随 symlink，跳过生成物与超大文件。"""
    root = workspace.resolve()
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
        current = Path(dirpath)
        for name in filenames:
            path = current / name
            if language_for_path(path) is None:
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path


class CodeIndexManager:
    """TUI / -p 进场时 start()；parse 丢到线程，不卡输入框。"""

    def __init__(self, *, workspace: Path, data_home: Path) -> None:
        self.workspace = workspace.resolve()
        self.data_home = data_home
        self.store = CodeIndexStore(data_home, self.workspace)

    async def start(self) -> None:
        self.store.set_status("scanning")
        await asyncio.to_thread(self._scan_sync)

    async def aclose(self) -> None:
        self.store.close()

    async def refresh_paths(self, paths: list[Path]) -> None:
        await asyncio.to_thread(self._refresh_sync, paths)

    def status_text(self) -> str:
        files, symbols, failed = self.store.counts()
        status = self.store.meta("status") or "idle"
        updated = self.store.meta("updated_at") or "-"
        return (
            f"status    {status}\n"
            f"files     {files}\n"
            f"symbols   {symbols}\n"
            f"failed    {failed}\n"
            f"updated   {updated}"
        )

    def _scan_sync(self) -> None:
        existing = self.store.file_mtimes()
        seen: set[str] = set()
        failed = 0
        for path in iter_index_files(self.workspace):
            rel = path.relative_to(self.workspace).as_posix()
            seen.add(rel)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                failed += 1
                continue
            if existing.get(rel) == mtime:
                continue
            if not self._index_one(path, rel, mtime):
                failed += 1
        for stale in set(existing) - seen:
            self.store.remove_file(stale)
        self.store.rescore()
        self.store.set_status("ready", failed=failed)

    def _refresh_sync(self, paths: list[Path]) -> None:
        failed = int(self.store.meta("failed") or "0")
        for path in paths:
            try:
                resolved = path.resolve()
                rel = resolved.relative_to(self.workspace).as_posix()
            except (OSError, ValueError):
                continue
            if not resolved.exists():
                self.store.remove_file(rel)
                continue
            try:
                mtime = resolved.stat().st_mtime
            except OSError:
                failed += 1
                continue
            if not self._index_one(resolved, rel, mtime):
                failed += 1
        self.store.rescore()
        status = self.store.meta("status") or "ready"
        if status != "scanning":
            self.store.set_status(status, failed=failed)

    def _index_one(self, path: Path, rel: str, mtime: float) -> bool:
        if language_for_path(rel) is None or any(part in SKIP_DIR_NAMES for part in Path(rel).parts):
            self.store.remove_file(rel)
            return True
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                self.store.remove_file(rel)
                return True
            symbols = parse_file(path, rel=rel)
        except Exception:
            return False
        self.store.replace_file(rel, mtime, symbols)
        return True
