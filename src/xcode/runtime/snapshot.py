"""工作区文件快照：只跟踪 write_file / edit_file，不靠用户 git。

见 docs/plan.md「快照 / restore」。

  {session_dir}/snapshots/
    last_turn.json / session_files.json / pre_restore.json
    named/<name>.json
    blobs/<sha256>
    open_turn.json     # 本轮进行中（崩溃时 begin_turn 会封成 last_turn）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from xcode.tools.base import resolve_workspace_path

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_NAMED = 20
RESERVED_NAMES = frozenset({"last", "undo", "pre_restore"})
_NAME_MAX = 80

_DIR_NAME = "snapshots"
_LAST = "last_turn.json"
_OPEN = "open_turn.json"
_SESSION_FILES = "session_files.json"
_PRE = "pre_restore.json"
_NAMED = "named"
_BLOBS = "blobs"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def normalize_snapshot_name(raw: str | None) -> str:
    """空名 → UTC 时间戳；拒绝保留字与路径分隔符。"""
    name = (raw or "").strip()
    if not name:
        return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if name.lower() in RESERVED_NAMES:
        raise ValueError(f"reserved snapshot name: {name}")
    if any(sep in name for sep in ("/", "\\", "\0")):
        raise ValueError("snapshot name must not contain path separators")
    if name in {".", ".."}:
        raise ValueError("invalid snapshot name")
    return name[:_NAME_MAX]


def _safe_named_path(named_dir: Path, name: str) -> Path:
    """命名档文件名：去掉文件系统不安全字符。"""
    slug = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("._") or "snap"
    return named_dir / f"{slug}.json"


@dataclass
class RestoreReport:
    """一次 restore 的结果摘要。"""

    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def format(self) -> str:
        lines: list[str] = []
        if self.restored:
            lines.append("restored: " + ", ".join(self.restored))
        if self.deleted:
            lines.append("deleted: " + ", ".join(self.deleted))
        if self.skipped:
            bits = [f"{p} ({why})" for p, why in self.skipped]
            lines.append("skipped: " + ", ".join(bits))
        if not lines:
            return "nothing to restore"
        return "\n".join(lines)


class SnapshotStore:
    """绑定一个 session 目录的快照存取。"""

    def __init__(self, session_dir: Path, workspace: Path) -> None:
        self.session_dir = session_dir
        self.workspace = workspace.resolve()
        self.root = session_dir / _DIR_NAME
        self._backed: set[str] = set()
        self._open_files: dict[str, dict[str, Any]] = {}
        self._adopt_crashed_open()

    def _adopt_crashed_open(self) -> None:
        """未封存的 open_turn 提升为 last_turn，便于崩溃后立刻 /restore last。"""
        leftover = _read_json(self.root / _OPEN)
        if leftover and leftover.get("files"):
            self._write_record(self.root / _LAST, leftover, kind="last_turn")

    def begin_turn(self) -> None:
        """新用户回合：清空本轮已备份集合。崩溃残留的 open_turn 先封成 last_turn。"""
        self._adopt_crashed_open()
        self._open_files = {}
        self._backed.clear()
        self._save_open()

    def seal_turn(self) -> None:
        """回合结束：有改动才覆盖 last_turn。"""
        if self._open_files:
            self._write_record(
                self.root / _LAST,
                {"files": self._open_files, "created_at": _utc_now()},
                kind="last_turn",
            )
        self._open_files = {}
        self._backed.clear()
        self._save_open()
        self._gc_blobs()

    def note_before_write(self, rel: str) -> None:
        """本轮第一次改该路径前：拷原文（或记 missing）。"""
        rel = rel.strip().replace("\\", "/")
        if not rel or rel in self._backed:
            return
        entry = self._capture(rel)
        self._open_files[rel] = entry
        self._backed.add(rel)
        self._save_open()
        self._remember_path(rel)

    def save_named(self, name: str | None) -> str:
        """按 session_files 拍此刻，写入命名档；返回最终名字。"""
        final = normalize_snapshot_name(name)
        files = self.capture_session_now()
        record = {
            "v": 1,
            "kind": "named",
            "name": final,
            "created_at": _utc_now(),
            "files": files,
        }
        path = _safe_named_path(self.root / _NAMED, final)
        _atomic_write_json(path, record)
        self._evict_named()
        self._gc_blobs()
        return final

    def restore_last(self) -> RestoreReport:
        data = _read_json(self.root / _LAST)
        files = (data or {}).get("files") or {}
        if not files:
            return RestoreReport()
        return self._restore(files)

    def restore_named(self, name: str) -> RestoreReport:
        path = self._find_named(name)
        if path is None:
            raise FileNotFoundError(f"named snapshot not found: {name}")
        data = _read_json(path) or {}
        files = data.get("files") or {}
        if not files:
            return RestoreReport()
        return self._restore(files)

    def restore_undo(self) -> RestoreReport:
        data = _read_json(self.root / _PRE)
        files = (data or {}).get("files") or {}
        if not files:
            return RestoreReport()
        return self._restore(files, save_pre=False)

    def list_entries(self) -> list[dict[str, Any]]:
        """列出可 restore 的条目（last / undo / named），供 /restore 无参打印。"""
        rows: list[dict[str, Any]] = []
        last = _read_json(self.root / _LAST)
        if last and last.get("files"):
            rows.append(
                {
                    "key": "last",
                    "label": "上一轮",
                    "files": len(last["files"]),
                    "created_at": last.get("created_at") or "",
                }
            )
        pre = _read_json(self.root / _PRE)
        if pre and pre.get("files"):
            rows.append(
                {
                    "key": "undo",
                    "label": "撤回前",
                    "files": len(pre["files"]),
                    "created_at": pre.get("created_at") or "",
                }
            )
        named_dir = self.root / _NAMED
        if named_dir.is_dir():
            items: list[dict[str, Any]] = []
            for child in named_dir.glob("*.json"):
                data = _read_json(child)
                if not data:
                    continue
                items.append(
                    {
                        "key": str(data.get("name") or child.stem),
                        "label": "named",
                        "files": len(data.get("files") or {}),
                        "created_at": str(data.get("created_at") or ""),
                    }
                )
            items.sort(key=lambda r: r["created_at"], reverse=True)
            rows.extend(items)
        return rows

    def capture_session_now(self) -> dict[str, dict[str, Any]]:
        """本会话变更集的此刻切片。"""
        files: dict[str, dict[str, Any]] = {}
        for rel in self._session_paths():
            files[rel] = self._capture(rel)
        return files

    def _restore(
        self,
        files: dict[str, Any],
        *,
        save_pre: bool = True,
    ) -> RestoreReport:
        if save_pre:
            pre_files = self.capture_session_now()
            for rel in files:
                if rel not in pre_files:
                    pre_files[rel] = self._capture(rel)
            _atomic_write_json(
                self.root / _PRE,
                {
                    "v": 1,
                    "kind": "pre_restore",
                    "created_at": _utc_now(),
                    "files": pre_files,
                },
            )
        report = RestoreReport()
        for rel, entry in files.items():
            if not isinstance(entry, dict):
                report.skipped.append((rel, "bad entry"))
                continue
            if entry.get("skip"):
                report.skipped.append((rel, str(entry.get("skip"))))
                continue
            try:
                dest = resolve_workspace_path(self.workspace, rel)
            except PermissionError as exc:
                report.skipped.append((rel, str(exc)))
                continue
            if entry.get("missing"):
                if dest.is_file():
                    try:
                        dest.unlink()
                        report.deleted.append(rel)
                    except OSError as exc:
                        report.skipped.append((rel, str(exc)))
                self._remember_path(rel)
                continue
            digest = entry.get("sha256")
            if not isinstance(digest, str) or not digest:
                report.skipped.append((rel, "no blob"))
                continue
            blob = self.root / _BLOBS / digest
            if not blob.is_file():
                report.skipped.append((rel, "blob missing"))
                continue
            try:
                data = blob.read_bytes()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                report.restored.append(rel)
                self._remember_path(rel)
            except OSError as exc:
                report.skipped.append((rel, str(exc)))
        return report

    def _capture(self, rel: str) -> dict[str, Any]:
        try:
            path = resolve_workspace_path(self.workspace, rel)
        except PermissionError as exc:
            return {"skip": str(exc)}
        if not path.exists():
            return {"missing": True}
        if path.is_dir():
            return {"skip": "is_directory"}
        try:
            size = path.stat().st_size
        except OSError as exc:
            return {"skip": str(exc)}
        if size > MAX_FILE_BYTES:
            return {"skip": "too_large", "size": size}
        try:
            data = path.read_bytes()
        except OSError as exc:
            return {"skip": str(exc)}
        digest = sha256(data).hexdigest()
        blob = self.root / _BLOBS / digest
        if not blob.is_file():
            blob.parent.mkdir(parents=True, exist_ok=True)
            tmp = blob.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(blob)
        return {"sha256": digest}

    def _session_paths(self) -> list[str]:
        data = _read_json(self.root / _SESSION_FILES) or {}
        raw = data.get("paths") or []
        if not isinstance(raw, list):
            return []
        return [str(p) for p in raw if isinstance(p, str) and p]

    def _remember_path(self, rel: str) -> None:
        paths = self._session_paths()
        if rel not in paths:
            paths.append(rel)
            _atomic_write_json(self.root / _SESSION_FILES, {"v": 1, "paths": paths})

    def _save_open(self) -> None:
        _atomic_write_json(
            self.root / _OPEN,
            {
                "v": 1,
                "kind": "open_turn",
                "created_at": _utc_now(),
                "files": self._open_files,
            },
        )

    def _write_record(self, path: Path, data: dict[str, Any], *, kind: str) -> None:
        files = data.get("files") or {}
        _atomic_write_json(
            path,
            {
                "v": 1,
                "kind": kind,
                "created_at": data.get("created_at") or _utc_now(),
                "files": files,
            },
        )

    def _find_named(self, name: str) -> Path | None:
        named_dir = self.root / _NAMED
        if not named_dir.is_dir():
            return None
        exact = _safe_named_path(named_dir, name)
        if exact.is_file():
            return exact
        for child in named_dir.glob("*.json"):
            data = _read_json(child)
            if data and data.get("name") == name:
                return child
        return None

    def _evict_named(self) -> None:
        named_dir = self.root / _NAMED
        if not named_dir.is_dir():
            return
        items: list[tuple[str, Path]] = []
        for child in named_dir.glob("*.json"):
            data = _read_json(child) or {}
            items.append((str(data.get("created_at") or ""), child))
        if len(items) <= MAX_NAMED:
            return
        items.sort(key=lambda it: it[0])
        for _, path in items[: len(items) - MAX_NAMED]:
            path.unlink(missing_ok=True)

    def _gc_blobs(self) -> None:
        blob_dir = self.root / _BLOBS
        if not blob_dir.is_dir():
            return
        live: set[str] = set()
        for path in (
            self.root / _LAST,
            self.root / _OPEN,
            self.root / _PRE,
        ):
            live.update(self._shas_in(_read_json(path)))
        named_dir = self.root / _NAMED
        if named_dir.is_dir():
            for child in named_dir.glob("*.json"):
                live.update(self._shas_in(_read_json(child)))
        for blob in blob_dir.iterdir():
            if blob.is_file() and blob.name not in live:
                blob.unlink(missing_ok=True)

    @staticmethod
    def _shas_in(data: dict[str, Any] | None) -> set[str]:
        out: set[str] = set()
        if not data:
            return out
        files = data.get("files") or {}
        if not isinstance(files, dict):
            return out
        for entry in files.values():
            if isinstance(entry, dict):
                digest = entry.get("sha256")
                if isinstance(digest, str) and digest:
                    out.add(digest)
        return out
