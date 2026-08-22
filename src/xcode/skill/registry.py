"""Skills 扫描、启停、catalog、正文展开。

发现顺序 builtin < 用户 < 项目；同名后者覆盖。
L1 只暴露 name + description；L2 由 load_skill / /skills <name> 展开正文。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from xcode.config import default_data_home

SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CATALOG_CHAR_BUDGET = 8000
_DESC_SHORT = 80

_PACKAGE_BUILTIN = Path(__file__).resolve().parents[1] / "builtin_skills"


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    path: Path
    base_dir: Path
    body: str
    source: str
    enabled: bool


@dataclass(slots=True)
class SkillsCommandResult:
    kind: str
    text: str


class SkillStateStore:
    """用户级 disabled 集合：``{data_home}/skills.json``。"""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def disabled(self) -> set[str]:
        if self.path is None or not self.path.is_file():
            return set()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        values = data.get("disabled") if isinstance(data, dict) else None
        if not isinstance(values, list):
            return set()
        return {str(item).strip() for item in values if str(item).strip()}

    def disable(self, name: str) -> None:
        values = self.disabled()
        values.add(name)
        self._write(values)

    def enable(self, name: str) -> None:
        values = self.disabled()
        values.discard(name)
        self._write(values)

    def _write(self, disabled: set[str]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"disabled": sorted(disabled)}, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)


class SkillRegistry:
    """从 builtin / 用户 / 项目目录加载 ``<name>/SKILL.md``。"""

    def __init__(
        self,
        workspace: str | Path,
        *,
        data_home: str | Path | None = None,
        builtin_root: str | Path | None = None,
        user_root: str | Path | None = None,
        state_path: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.data_home = Path(data_home).expanduser().resolve() if data_home else None
        home = self.data_home or default_data_home()
        self.builtin_root = Path(builtin_root or _PACKAGE_BUILTIN).resolve()
        if user_root is not None:
            self.user_root = Path(user_root).expanduser().resolve()
        else:
            self.user_root = (home / "skills").resolve()
        if state_path is not None:
            self.state_path = Path(state_path).expanduser()
        else:
            self.state_path = home / "skills.json"
        self.project_root = (self.workspace / ".xcode" / "skills").resolve()
        self._state = SkillStateStore(self.state_path)
        self._skills: dict[str, Skill] = {}
        self._last_marker = -1.0

    def read_roots(self) -> list[Path]:
        """read_file 可读的 skill 根目录（不含工作区本身）。"""
        return [self.builtin_root, self.user_root, self.project_root]

    def refresh_if_stale(self) -> None:
        marker = self._scan_marker()
        if marker != self._last_marker:
            self.scan()

    def scan(self) -> list[Skill]:
        disabled = self._state.disabled()
        found: dict[str, Skill] = {}
        for source, root in (
            ("builtin", self.builtin_root),
            ("user", self.user_root),
            ("project", self.project_root),
        ):
            if not root.is_dir():
                continue
            for skill_file in sorted(root.glob("*/SKILL.md")):
                skill = _load_skill_file(skill_file, source, disabled)
                if skill is not None:
                    found[skill.name] = skill
        self._skills = found
        self._last_marker = self._scan_marker()
        return self.all_skills()

    def all_skills(self) -> list[Skill]:
        self.refresh_if_stale()
        return [self._skills[name] for name in sorted(self._skills)]

    def enabled_skills(self) -> list[Skill]:
        return [skill for skill in self.all_skills() if skill.enabled]

    def get(self, name: str, *, include_disabled: bool = False) -> Skill | None:
        self.refresh_if_stale()
        skill = self._skills.get(name)
        if skill is None:
            return None
        if not include_disabled and not skill.enabled:
            return None
        return skill

    def render(self, name: str, args: str = "") -> str | None:
        skill = self.get(name)
        if skill is None:
            return None
        body = expand_arguments(skill.body, args)
        return f"Base directory: {skill.base_dir.resolve()}\n\n{body}".rstrip() + "\n"

    def catalog_text(self, budget: int = CATALOG_CHAR_BUDGET) -> str:
        skills = self.enabled_skills()
        if not skills:
            return ""
        header = (
            "Available skills:\n"
            "Use load_skill(name) when a description matches. Do not preload all.\n"
        )
        return _fit_catalog(header, skills, budget)

    def enable(self, name: str) -> bool:
        if self.get(name, include_disabled=True) is None:
            return False
        self._state.enable(name)
        self.scan()
        return True

    def disable(self, name: str) -> bool:
        if self.get(name, include_disabled=True) is None:
            return False
        self._state.disable(name)
        self.scan()
        return True

    def _scan_marker(self) -> float:
        marker = 0.0
        for root in (self.builtin_root, self.user_root, self.project_root):
            if not root.exists():
                continue
            try:
                marker = max(marker, root.stat().st_mtime)
            except OSError:
                continue
            for path in root.glob("*/SKILL.md"):
                try:
                    marker = max(marker, path.stat().st_mtime)
                except OSError:
                    continue
        if self.state_path is not None and self.state_path.exists():
            try:
                marker = max(marker, self.state_path.stat().st_mtime)
            except OSError:
                pass
        return marker


def expand_arguments(body: str, args: str) -> str:
    """正文有 ``$ARGUMENTS`` 则替换；否则 args 非空时末尾追加。"""
    if "$ARGUMENTS" in body:
        return body.replace("$ARGUMENTS", args)
    if not str(args).strip():
        return body
    return f"{body.rstrip()}\n\nARGUMENTS:\n{args}\n"


def parse_skills_slash(arg: str) -> tuple[str, str, str]:
    """解析 ``/skills`` 参数 → (list|on|off|load, name, extra)。"""
    raw = arg.strip()
    if not raw:
        return "list", "", ""
    first, _, rest = raw.partition(" ")
    key = first.lower()
    if key in {"on", "off"}:
        name = rest.strip().split(None, 1)[0] if rest.strip() else ""
        if name:
            return key, name, ""
        return "load", first, rest.strip()
    return "load", first, rest.strip()


def format_skills_list(registry: SkillRegistry) -> str:
    skills = registry.all_skills()
    if not skills:
        return "(no skills)"
    lines: list[str] = []
    for skill in skills:
        flag = "on" if skill.enabled else "off"
        desc = " ".join(skill.description.split())
        if len(desc) > 80:
            desc = desc[:79] + "…"
        lines.append(f"  {flag:<3} {skill.name:<22} {skill.source:<8} {desc}")
    return "\n".join(lines)


def render_invocation_user_text(name: str, rendered: str) -> str:
    return (
        f'<loaded_skill name="{name}">\n{rendered.rstrip()}\n</loaded_skill>\n\n'
        f"用户调用了 skill {name}。请按说明书执行。"
    )


def handle_skills_arg(arg: str, registry: SkillRegistry) -> SkillsCommandResult:
    action, name, extra = parse_skills_slash(arg)
    if action == "list":
        return SkillsCommandResult(kind="list", text=format_skills_list(registry))
    if action == "on":
        if not registry.enable(name):
            return SkillsCommandResult(kind="error", text=f"unknown skill: {name}")
        return SkillsCommandResult(kind="ok", text=f"skill enabled: {name}")
    if action == "off":
        if not registry.disable(name):
            return SkillsCommandResult(kind="error", text=f"unknown skill: {name}")
        return SkillsCommandResult(kind="ok", text=f"skill disabled: {name}")
    rendered = registry.render(name, extra)
    if rendered is None:
        return SkillsCommandResult(
            kind="error", text=f"skill not found or disabled: {name}"
        )
    return SkillsCommandResult(
        kind="invoke",
        text=render_invocation_user_text(name, rendered),
    )


def _valid_name(name: str) -> bool:
    return bool(name) and len(name) <= 64 and SKILL_NAME_PATTERN.fullmatch(name) is not None


def _load_skill_file(path: Path, source: str, disabled: set[str]) -> Skill | None:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    parsed = parse_frontmatter(content)
    if parsed is None:
        return None
    metadata, body = parsed
    name = (metadata.get("name") or "").strip()
    description = (metadata.get("description") or "").strip()
    if not _valid_name(name) or not description:
        return None
    if name != path.parent.name:
        return None
    return Skill(
        name=name,
        description=description,
        path=path,
        base_dir=path.parent,
        body=body,
        source=source,
        enabled=name not in disabled,
    )


def parse_frontmatter(content: str) -> tuple[dict[str, str], str] | None:
    if not content.startswith("---"):
        return None
    rest = content[3:]
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]
    else:
        return None
    end = rest.find("\n---")
    if end < 0:
        return None
    header = rest[:end]
    body = rest[end + 4 :]
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    metadata: dict[str, str] = {}
    lines = header.splitlines()
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        if ":" not in raw_line:
            index += 1
            continue
        key, value = raw_line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value in {"|", ">"}:
            index += 1
            block: list[str] = []
            while index < len(lines) and (
                lines[index].startswith((" ", "\t")) or not lines[index].strip()
            ):
                block.append(lines[index].strip())
                index += 1
            metadata[key] = " ".join(part for part in block if part)
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        metadata[key] = value
        index += 1
    return metadata, body


def _fit_catalog(header: str, skills: list[Skill], budget: int) -> str:
    def line_for(skill: Skill, desc_limit: int | None) -> str:
        desc = " ".join(skill.description.split())
        if desc_limit is not None and len(desc) > desc_limit:
            desc = desc[: max(desc_limit - 1, 1)] + "…"
        return f"- {skill.name}: {desc}"

    def join(lines: list[str], omitted: int) -> str:
        body = "\n".join(lines)
        if omitted:
            suffix = f"... and {omitted} more"
            return f"{header}{body}\n{suffix}" if body else f"{header}{suffix}"
        return f"{header}{body}" if body else header.rstrip()

    full_lines = [line_for(skill, None) for skill in skills]
    text = join(full_lines, 0)
    if len(text) <= budget:
        return text

    short_lines = [line_for(skill, _DESC_SHORT) for skill in skills]
    text = join(short_lines, 0)
    if len(text) <= budget:
        return text

    kept: list[str] = []
    for index, line in enumerate(short_lines):
        omitted = len(short_lines) - (index + 1)
        trial = join([*kept, line], omitted)
        if len(trial) > budget:
            omitted_now = len(short_lines) - len(kept)
            fallback = join(kept, omitted_now)
            if len(fallback) <= budget:
                return fallback
            # 预算极小时只留 header + 省略数
            bare = join([], len(short_lines))
            return bare if len(bare) <= budget else bare[:budget]
        kept.append(line)
    return join(kept, 0)
