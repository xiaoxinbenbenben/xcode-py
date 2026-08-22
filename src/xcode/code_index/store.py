"""sqlite 索引：文件、符号、分数、search_code。"""

from __future__ import annotations

import sqlite3
import threading
from collections import defaultdict
from pathlib import Path

from xcode.code_index.parse import Symbol
from xcode.runtime.session import project_key
from xcode.runtime.tokens import count_text_tokens

SCHEMA_VERSION = "1"
_SEARCH_CAP = 20


class CodeIndexStore:
    """一个工作区一份 sqlite。"""

    def __init__(self, data_home: Path, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.db_path = data_home / "projects" / project_key(self.workspace) / "code-index.sqlite"
        self._lock = threading.RLock()
        self._conn = self._open()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA case_sensitive_like=ON")
        return conn

    def _open(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return self._ensure_schema(self._connect())

    def _ensure_schema(self, conn: sqlite3.Connection) -> sqlite3.Connection:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if existing:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row and row["value"] == SCHEMA_VERSION:
                return conn
            conn.close()
            self._unlink_db()
            conn = self._connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
              path TEXT PRIMARY KEY,
              mtime REAL NOT NULL,
              score INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS symbols (
              id INTEGER PRIMARY KEY,
              path TEXT NOT NULL,
              name TEXT NOT NULL,
              kind TEXT NOT NULL,
              line INTEGER NOT NULL,
              is_def INTEGER NOT NULL,
              parent TEXT
            );
            CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name);
            CREATE INDEX IF NOT EXISTS symbols_path ON symbols(path);
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()
        return conn

    def _unlink_db(self) -> None:
        for extra in ("", "-wal", "-shm"):
            path = self.db_path.parent / (self.db_path.name + extra)
            path.unlink(missing_ok=True)

    def replace_file(self, rel: str, mtime: float, symbols: list[Symbol]) -> None:
        """覆盖一个文件的符号行。"""
        with self._lock:
            self._conn.execute("DELETE FROM symbols WHERE path = ?", (rel,))
            self._conn.execute(
                "INSERT OR REPLACE INTO files(path, mtime, score) VALUES (?, ?, 0)",
                (rel, mtime),
            )
            self._conn.executemany(
                "INSERT INTO symbols(path, name, kind, line, is_def, parent) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (s.path, s.name, s.kind, s.line, 1 if s.is_def else 0, s.parent)
                    for s in symbols
                ],
            )
            self._conn.commit()

    def remove_file(self, rel: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM symbols WHERE path = ?", (rel,))
            self._conn.execute("DELETE FROM files WHERE path = ?", (rel,))
            self._conn.commit()

    def rescore(self) -> None:
        """跨文件「源文件 × 符号」各 +1，加到定义所在文件。"""
        with self._lock:
            self._conn.execute("UPDATE files SET score = 0")
            rows = self._conn.execute(
                """
                SELECT d.path AS def_path, r.path AS ref_path, d.name AS name
                FROM symbols d
                JOIN symbols r ON r.name = d.name AND r.is_def = 0 AND d.is_def = 1
                WHERE r.path != d.path
                """
            ).fetchall()
            pairs = {(row["ref_path"], row["name"], row["def_path"]) for row in rows}
            scores: dict[str, int] = {}
            for _, _, def_path in pairs:
                scores[def_path] = scores.get(def_path, 0) + 1
            for path, score in scores.items():
                self._conn.execute("UPDATE files SET score = ? WHERE path = ?", (score, path))
            self._conn.commit()

    def file_score(self, rel: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT score FROM files WHERE path = ?", (rel,)
            ).fetchone()
        return int(row["score"]) if row else 0

    def set_file_score(self, rel: str, score: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO files(path, mtime, score) VALUES (?, 0, ?) "
                "ON CONFLICT(path) DO UPDATE SET score = excluded.score",
                (rel, score),
            )
            self._conn.commit()

    def file_mtime(self, rel: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT mtime FROM files WHERE path = ?", (rel,)
            ).fetchone()
        return float(row["mtime"]) if row else None

    def file_mtimes(self) -> dict[str, float]:
        with self._lock:
            rows = self._conn.execute("SELECT path, mtime FROM files").fetchall()
        return {row["path"]: float(row["mtime"]) for row in rows}

    def set_status(self, status: str, *, failed: int | None = None) -> None:
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('status', ?)",
                (status,),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('updated_at', ?)",
                (now,),
            )
            if failed is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('failed', ?)",
                    (str(failed),),
                )
            self._conn.commit()

    def meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def counts(self) -> tuple[int, int, int]:
        with self._lock:
            files = self._conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
            symbols = self._conn.execute("SELECT COUNT(*) AS n FROM symbols").fetchone()["n"]
            failed = int(self.meta("failed") or "0")
        return int(files), int(symbols), failed

    def def_rows(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT s.path, s.name, s.kind, s.line, s.parent, f.score "
                    "FROM symbols s JOIN files f ON f.path = s.path "
                    "WHERE s.is_def = 1 ORDER BY f.score DESC, s.path, s.line"
                )
            )

    def search_code(self, name: str) -> str:
        """按名字查定义和引用；未就绪时叫用 grep。"""
        name = name.strip()
        if not name:
            return "search_code error: name is required"
        status = self.meta("status")
        _, symbols, _ = self.counts()
        if status != "ready" and symbols == 0:
            return "search_code: index not ready; use grep"
        with self._lock:
            exact = list(
                self._conn.execute(
                    "SELECT path, name, kind, line, is_def, parent FROM symbols "
                    "WHERE name = ? ORDER BY is_def DESC, path, line",
                    (name,),
                )
            )
            rows = exact
            if not rows:
                rows = list(
                    self._conn.execute(
                        "SELECT path, name, kind, line, is_def, parent FROM symbols "
                        "WHERE name LIKE ? ESCAPE '\\' ORDER BY is_def DESC, path, line",
                        (_like_prefix(name),),
                    )
                )
        defs = [r for r in rows if r["is_def"]]
        refs = [r for r in rows if not r["is_def"]]
        if not defs and not refs:
            return f'search_code "{name}": no symbols named {name}'
        lines = [f'search_code "{name}"']
        lines.append("definitions:")
        lines.extend(_format_hits(defs[:_SEARCH_CAP], is_def=True))
        extra_defs = len(defs) - _SEARCH_CAP
        if extra_defs > 0:
            lines.append(f"... and {extra_defs} more")
        if not defs:
            lines.append("  (none)")
        lines.append("references:")
        lines.extend(_format_hits(refs[:_SEARCH_CAP], is_def=False))
        extra_refs = len(refs) - _SEARCH_CAP
        if extra_refs > 0:
            lines.append(f"... and {extra_refs} more")
        if not refs:
            lines.append("  (none)")
        return "\n".join(lines)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _like_prefix(name: str) -> str:
    escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def _format_hits(rows, *, is_def: bool) -> list[str]:
    lines: list[str] = []
    for row in rows:
        if is_def:
            label = row["name"]
            if row["kind"] == "method" and row["parent"]:
                label = f"{row['parent']}.{row['name']}"
            lines.append(f"  {row['path']}:{row['line']} {row['kind']} {label}")
        else:
            lines.append(f"  {row['path']}:{row['line']} call {row['name']}")
    return lines


MAP_TOKEN_LIMIT = 1500


def render_repo_map(
    store: CodeIndexStore,
    *,
    max_tokens: int = MAP_TOKEN_LIMIT,
    model: str = "",
) -> str:
    """按分数从高到低拼路径 + 定义名；不拆半个类。空则返回空串。"""
    by_file: dict[str, list] = defaultdict(list)
    scores: dict[str, int] = {}
    for row in store.def_rows():
        path = row["path"]
        scores[path] = int(row["score"])
        by_file[path].append(row)
    ordered = sorted(by_file, key=lambda p: (-scores.get(p, 0), p))
    pieces: list[str] = []
    for path in ordered:
        blocks = _file_blocks(by_file[path])
        if not blocks:
            continue
        if not _fits("\n".join(pieces + [path, blocks[0]]), max_tokens, model):
            continue
        pieces.append(path)
        for block in blocks:
            if not _fits("\n".join([*pieces, block]), max_tokens, model):
                return "\n".join(pieces)
            pieces.append(block)
    return "\n".join(pieces)


def _fits(text: str, max_tokens: int, model: str) -> bool:
    return count_text_tokens(text, model=model) <= max_tokens


def _file_blocks(rows) -> list[str]:
    methods_by_parent: dict[str, list] = defaultdict(list)
    top: list = []
    for row in rows:
        if row["kind"] == "method" and row["parent"]:
            methods_by_parent[row["parent"]].append(row)
        else:
            top.append(row)
    top.sort(key=lambda r: int(r["line"]))
    for items in methods_by_parent.values():
        items.sort(key=lambda r: int(r["line"]))
    blocks: list[str] = []
    for row in top:
        if row["kind"] == "class":
            lines = [f"  class {row['name']}"]
            for method in methods_by_parent.get(row["name"], []):
                lines.append(f"    {method['name']}")
            blocks.append("\n".join(lines))
        else:
            blocks.append(f"  {row['name']}")
    return blocks
