"""TUI 事件渲染：默认紧凑折叠 thinking / tool 细节。"""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from xcode.entrypoints.tui import (
    _EventRenderer,
    _build_key_bindings,
    _confirm,
    _short_tool_preview,
)


def _console() -> tuple[Console, StringIO]:
    buf = StringIO()
    return Console(file=buf, force_terminal=False, width=120, highlight=False), buf


def test_build_key_bindings_does_not_register_unknown_keys():
    """prompt_toolkit 3.x 没有 s-enter；启动时注册未知键会直接炸。"""
    keys = _build_key_bindings()
    sequences = {
        tuple(getattr(part, "value", part) for part in binding.keys) for binding in keys.bindings
    }
    assert ("c-m",) in sequences or ("enter",) in sequences
    assert ("escape", "c-m") in sequences or ("escape", "enter") in sequences
    assert ("s-enter",) not in sequences


def test_short_tool_preview_prefers_path_and_command():
    assert _short_tool_preview("memory_read", {"path": "MEMORY.md"}) == "MEMORY.md"
    assert "echo hi" in _short_tool_preview("bash", {"command": "echo hi"})
    long_cmd = "x" * 200
    preview = _short_tool_preview("bash", {"command": long_cmd}, limit=40)
    assert len(preview) <= 40
    assert preview.endswith("…")


def test_default_streams_thinking_and_text_but_folds_tool_body():
    console, buf = _console()
    r = _EventRenderer(console)
    r.render({"type": "thinking_delta", "thinking": "I should plan carefully "})
    r.render({"type": "thinking_delta", "thinking": "more secret reasoning"})
    r.render(
        {
            "type": "text_delta",
            "text": "I'll check MEMORY.md first then write the note.",
        }
    )
    r.render({"type": "turn_complete", "stop_reason": "tool_use"})
    r.render({"type": "tool_call", "name": "memory_read", "input": {"path": "MEMORY.md"}})
    r.render(
        {
            "type": "tool_result",
            "name": "memory_read",
            "result": "# MEMORY\n" + ("detail line\n" * 50),
            "is_error": False,
        }
    )
    r.render({"type": "text_delta", "text": "已写入长期记忆。"})
    r.render({"type": "turn_complete", "stop_reason": "end_turn"})
    r.render({"type": "done"})
    out = buf.getvalue()
    assert "· thinking" in out
    assert "I should plan carefully" in out
    assert "more secret reasoning" in out
    assert "I'll check MEMORY.md first then write the note." in out
    assert "detail line" not in out
    assert "⚙ tool" in out
    assert "memory_read" in out
    assert "MEMORY.md" in out
    assert "· answer" in out
    assert "已写入长期记忆" in out
    assert "↳ ok" in out or "ok" in out


def test_tool_body_stays_in_last_tool_not_on_screen():
    console, buf = _console()
    r = _EventRenderer(console)
    r.render({"type": "thinking_delta", "thinking": "secret plan"})
    r.render({"type": "text_delta", "text": "hello"})
    r.render({"type": "turn_complete", "stop_reason": "end_turn"})
    r.render(
        {
            "type": "tool_result",
            "name": "bash",
            "result": "full stdout body here",
            "is_error": False,
        }
    )
    r.render({"type": "done"})
    out = buf.getvalue()
    assert "· thinking" in out
    assert "secret plan" in out
    assert "· answer" in out
    assert "hello" in out
    assert "full stdout body here" not in out
    assert r.last_tool_result == "full stdout body here"


def test_compact_notifies_and_shows_tool_elapsed(monkeypatch):
    console, buf = _console()
    r = _EventRenderer(console)
    times = iter([100.0, 101.4])
    monkeypatch.setattr("xcode.entrypoints.tui.time.monotonic", lambda: next(times))
    r.render({"type": "turn_complete", "stop_reason": "compacted"})
    r.render({"type": "tool_call", "name": "bash", "input": {"command": "sleep 1"}})
    r.render({"type": "tool_result", "name": "bash", "result": "ok\n" * 20, "is_error": False})
    out = buf.getvalue()
    assert "compact" in out.lower()
    assert "1.4s" in out
    assert r.last_tool_result is not None
    assert "ok" in r.last_tool_result


def test_answer_streams_each_delta_once():
    console, buf = _console()
    r = _EventRenderer(console)
    r.render({"type": "text_delta", "text": "Review 完成。"})
    assert "Review 完成。" in buf.getvalue()
    r.render({"type": "text_delta", "text": "## 代码审查报告\n"})
    r.render({"type": "turn_complete", "stop_reason": "end_turn"})
    out = buf.getvalue()
    assert "· answer" in out
    assert out.count("Review 完成。") == 1
    assert out.count("## 代码审查报告") == 1


def test_confirm_uses_compact_session_without_toolbar(monkeypatch):
    """审批必须另开一行 prompt。复用主会话的 bottom_toolbar 会按光标到屏底撑出一整块空白。"""
    import asyncio

    seen: list[dict] = []

    class _FakeSession:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        async def prompt_async(self, *args, **kwargs):
            _ = args, kwargs
            return "y"

    monkeypatch.setattr("xcode.entrypoints.tui.PromptSession", _FakeSession)
    assert asyncio.run(_confirm("允许工具 webSearchStd 执行？")) is True
    assert seen
    kw = seen[0]
    assert not kw.get("bottom_toolbar")
    assert kw.get("multiline") is False
    assert kw.get("reserve_space_for_menu", 0) == 0
    assert kw.get("completer") is None
