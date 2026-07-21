"""Skills 加载：扫描 skills/*/SKILL.md 并提供 Skill 工具。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xcode.tools.base import Tool, ToolContext, ToolResult


@dataclass(slots=True)
class Skill:
    name: str
    path: Path
    description: str
    body: str


def load_skills(roots: list[Path]) -> list[Skill]:
    """从多个根目录加载 skill。

    约定：每个子目录含 SKILL.md；首段 frontmatter/首行作描述。
    """
    skills: list[Skill] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            skill_md = child / "SKILL.md"
            if not child.is_dir() or not skill_md.is_file():
                continue
            name = child.name
            if name in seen:
                continue
            text = skill_md.read_text(encoding="utf-8", errors="replace")
            description = _first_paragraph(text)
            skills.append(Skill(name=name, path=skill_md, description=description, body=text))
            seen.add(name)
    return skills


def _first_paragraph(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    return (lines[0] if lines else "skill")[:200]


class SkillTool(Tool):
    name = "Skill"
    description = "Load a skill document by name into the conversation."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "action": {"type": "string", "enum": ["list", "load"], "default": "load"},
        },
    }

    def __init__(self, skills: list[Skill]) -> None:
        self._skills = {s.name: s for s in skills}

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = str(args.get("action") or "load")
        if action == "list" or not args.get("name"):
            names = sorted(self._skills)
            return ToolResult(ok=True, summary=f"{len(names)} skills", content="\n".join(names) or "(none)")
        skill = self._skills.get(str(args["name"]))
        if skill is None:
            return ToolResult(ok=False, summary=f"unknown skill: {args['name']}")
        return ToolResult(ok=True, summary=skill.name, content=skill.body)


def skill_roots(workspace: Path, package_skills: Path | None = None) -> list[Path]:
    roots = [workspace / "skills"]
    if package_skills is not None:
        roots.append(package_skills)
    return roots
