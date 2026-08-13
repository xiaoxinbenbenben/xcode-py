"""Skills：三层披露、发现覆盖、启停、参数、catalog 预算、slash。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from xcode.context.builder import assemble_system_prompt
from xcode.skill import (
    CATALOG_CHAR_BUDGET,
    SkillRegistry,
    format_skills_list,
    handle_skills_arg,
    parse_skills_slash,
    render_invocation_user_text,
)
from xcode.tools.base import ToolContext
from xcode.tools.builtins import LoadSkillTool, ReadFileTool, WriteFileTool


def _write_skill(root: Path, name: str, description: str, body: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _registry(tmp_path: Path, *, with_project: bool = True) -> SkillRegistry:
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    builtin = tmp_path / "builtin"
    home.mkdir()
    ws.mkdir()
    builtin.mkdir()
    if with_project:
        (ws / ".xcode" / "skills").mkdir(parents=True)
    return SkillRegistry(
        ws,
        data_home=home,
        builtin_root=builtin,
        user_root=home / "skills",
        state_path=home / "skills.json",
    )


def test_scan_skips_invalid_and_foreign_dirs(tmp_path):
    reg = _registry(tmp_path)
    user = tmp_path / "home" / "skills"
    _write_skill(user, "ok-skill", "Valid skill for listing", "Do the thing.")
    (user / "bad-no-meta").mkdir(parents=True)
    (user / "bad-no-meta" / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
    _write_skill(user, "BadName", "uppercase dir", "nope")
    empty = user / "empty-desc"
    empty.mkdir()
    (empty / "SKILL.md").write_text(
        "---\nname: empty-desc\ndescription: \n---\n\nbody\n",
        encoding="utf-8",
    )
    claude = tmp_path / "ws" / ".claude" / "skills"
    _write_skill(claude, "claude-only", "Should not be discovered", "secret")
    agents = tmp_path / "ws" / ".agents" / "skills"
    _write_skill(agents, "codex-only", "Should not be discovered", "secret")

    names = [s.name for s in reg.enabled_skills()]
    assert names == ["ok-skill"]


def test_project_overrides_user_and_builtin(tmp_path):
    reg = _registry(tmp_path)
    _write_skill(tmp_path / "builtin", "review", "builtin desc", "builtin body")
    _write_skill(tmp_path / "home" / "skills", "review", "user desc", "user body")
    _write_skill(
        tmp_path / "ws" / ".xcode" / "skills",
        "review",
        "project desc",
        "project body",
    )
    skill = reg.get("review")
    assert skill is not None
    assert skill.source == "project"
    assert skill.description == "project desc"
    rendered = reg.render("review")
    assert rendered is not None
    assert "project body" in rendered
    assert "user body" not in rendered


def test_disable_hides_from_catalog_and_load(tmp_path):
    reg = _registry(tmp_path)
    _write_skill(tmp_path / "home" / "skills", "quiet", "A quiet skill", "shh")
    assert "quiet" in reg.catalog_text()
    assert reg.disable("quiet") is True
    assert "quiet" not in SkillRegistry(
        tmp_path / "ws",
        data_home=tmp_path / "home",
        builtin_root=tmp_path / "builtin",
        user_root=tmp_path / "home" / "skills",
        state_path=tmp_path / "home" / "skills.json",
    ).catalog_text()
    assert SkillRegistry(
        tmp_path / "ws",
        data_home=tmp_path / "home",
        builtin_root=tmp_path / "builtin",
        user_root=tmp_path / "home" / "skills",
        state_path=tmp_path / "home" / "skills.json",
    ).render("quiet") is None


def test_arguments_replace_or_append(tmp_path):
    reg = _registry(tmp_path)
    _write_skill(
        tmp_path / "home" / "skills",
        "with-args",
        "Takes arguments",
        "Focus: $ARGUMENTS\nDone.",
    )
    _write_skill(
        tmp_path / "home" / "skills",
        "no-slot",
        "No placeholder",
        "Static only.",
    )
    replaced = reg.render("with-args", "auth")
    assert replaced is not None
    assert "Focus: auth" in replaced
    assert "$ARGUMENTS" not in replaced
    appended = reg.render("no-slot", "please check tests")
    assert appended is not None
    assert appended.rstrip().endswith("ARGUMENTS:\nplease check tests")
    untouched = reg.render("no-slot", "")
    assert untouched is not None
    assert "ARGUMENTS:" not in untouched


def test_render_adds_base_directory(tmp_path):
    reg = _registry(tmp_path)
    _write_skill(tmp_path / "home" / "skills", "anchored", "Has a base dir", "Go.")
    text = reg.render("anchored")
    assert text is not None
    assert text.startswith("Base directory: ")
    base = (tmp_path / "home" / "skills" / "anchored").resolve()
    assert str(base) in text.splitlines()[0]


def test_catalog_shortens_then_omits(tmp_path):
    reg = _registry(tmp_path)
    user = tmp_path / "home" / "skills"
    for i in range(12):
        _write_skill(
            user,
            f"skill-{i:02d}",
            "x" * 400,
            "body",
        )
    fullish = reg.catalog_text(budget=2000)
    assert "Available skills" in fullish
    assert "load_skill" in fullish
    assert len(fullish) <= 2000
    tiny = reg.catalog_text(budget=220)
    assert "... and" in tiny
    assert "more" in tiny
    assert len(tiny) <= 220


def test_refresh_picks_up_new_skill(tmp_path):
    reg = _registry(tmp_path)
    _write_skill(tmp_path / "home" / "skills", "one", "First skill", "a")
    assert [s.name for s in reg.enabled_skills()] == ["one"]
    _write_skill(tmp_path / "home" / "skills", "two", "Second skill", "b")
    assert [s.name for s in reg.enabled_skills()] == ["one", "two"]


def test_system_prompt_lists_skills_not_bodies(tmp_path):
    reg = _registry(tmp_path)
    _write_skill(
        tmp_path / "ws" / ".xcode" / "skills",
        "review",
        "Review diffs for bugs",
        "SECRET BODY MUST NOT ENTER SYSTEM",
    )
    system = assemble_system_prompt(
        workspace=tmp_path / "ws",
        tool_names=["load_skill"],
        data_home=tmp_path / "home",
        skills=reg,
    )
    assert "review: Review diffs for bugs" in system
    assert "SECRET BODY" not in system
    assert "load_skill" in system


def test_load_skill_tool_returns_body(tmp_path):
    _registry(tmp_path)
    _write_skill(tmp_path / "home" / "skills", "review", "Review code", "Look for bugs.")
    ctx = ToolContext(workspace=tmp_path / "ws", data_home=tmp_path / "home")
    # tool constructs its own registry; point data_home at tmp home
    result = asyncio.run(LoadSkillTool().execute({"name": "review"}, ctx))
    assert result.ok
    assert "Look for bugs." in result.text
    assert "Base directory:" in result.text


def test_load_skill_missing_is_error(tmp_path):
    _registry(tmp_path)  # create home/ws layout
    ctx = ToolContext(workspace=tmp_path / "ws", data_home=tmp_path / "home")
    result = asyncio.run(LoadSkillTool().execute({"name": "nope"}, ctx))
    assert result.is_error
    assert "nope" in result.text


def test_read_file_can_open_user_skill_resource(tmp_path):
    reg = _registry(tmp_path)
    _write_skill(
        tmp_path / "home" / "skills",
        "docs",
        "Has a reference file",
        "See references/note.md",
    )
    note = tmp_path / "home" / "skills" / "docs" / "references" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("resource-secret\n", encoding="utf-8")
    ctx = ToolContext(workspace=tmp_path / "ws", data_home=tmp_path / "home")
    result = asyncio.run(ReadFileTool().execute({"path": str(note)}, ctx))
    assert result.ok
    assert "resource-secret" in result.text
    blocked = asyncio.run(
        WriteFileTool().execute({"path": str(note), "content": "hack"}, ctx)
    )
    assert blocked.is_error
    assert any(s.name == "docs" for s in reg.enabled_skills())


def test_parse_and_handle_skills_slash(tmp_path):
    assert parse_skills_slash("") == ("list", "", "")
    assert parse_skills_slash("on review") == ("on", "review", "")
    assert parse_skills_slash("off review") == ("off", "review", "")
    assert parse_skills_slash("review focus auth") == ("load", "review", "focus auth")

    reg = _registry(tmp_path)
    _write_skill(tmp_path / "home" / "skills", "review", "Review diffs", "Check $ARGUMENTS")
    listed = handle_skills_arg("", reg)
    assert listed.kind == "list"
    assert "review" in listed.text
    assert format_skills_list(reg).count("review") == 1

    off = handle_skills_arg("off review", reg)
    assert off.kind == "ok"

    fresh = SkillRegistry(
        tmp_path / "ws",
        data_home=tmp_path / "home",
        builtin_root=tmp_path / "builtin",
        user_root=tmp_path / "home" / "skills",
        state_path=tmp_path / "home" / "skills.json",
    )
    assert fresh.get("review", include_disabled=True) is not None
    assert fresh.get("review") is None

    on = handle_skills_arg("on review", fresh)
    assert on.kind == "ok"
    loaded = handle_skills_arg("review auth", fresh)
    assert loaded.kind == "invoke"
    assert "Check auth" in loaded.text
    assert "用户调用了 skill review" in render_invocation_user_text("review", "x")


def test_catalog_budget_constant():
    assert CATALOG_CHAR_BUDGET == 8000


def test_name_must_match_directory(tmp_path):
    reg = _registry(tmp_path)
    folder = tmp_path / "home" / "skills" / "folder-name"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: other-name\ndescription: mismatch\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert reg.enabled_skills() == []
