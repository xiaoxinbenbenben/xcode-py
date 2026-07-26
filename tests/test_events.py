"""产品事件协议：扁平事件形状与 agent 产出顺序。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from xcode.config import Config
from xcode.runtime import events as ev
from xcode.runtime.agent import run_agent
from xcode.runtime.session import SessionStore


def test_make_event_is_flat() -> None:
    event = ev.make_event(ev.TEXT_DELTA, text="hi")
    assert event == {"type": "text_delta", "text": "hi"}
    assert "payload" not in event


def test_map_finish_reason() -> None:
    assert ev.map_finish_reason("tool_calls") == "tool_use"
    assert ev.map_finish_reason("stop") == "end_turn"
    assert ev.map_finish_reason("length") == "max_tokens"


class _FakeCompletions:
    def __init__(self, scripts: list[list[Any]]) -> None:
        self._scripts = list(scripts)
        self.calls = 0

    async def create(self, **kwargs: Any) -> Any:
        _ = kwargs
        idx = self.calls
        self.calls += 1
        chunks = self._scripts[idx] if idx < len(self._scripts) else self._scripts[-1]

        async def _gen():
            for chunk in chunks:
                yield chunk

        return _gen()


class _FakeChat:
    def __init__(self, scripts: list[list[Any]]) -> None:
        self.completions = _FakeCompletions(scripts)


class _FakeClient:
    def __init__(self, scripts: list[list[Any]]) -> None:
        self.chat = _FakeChat(scripts)


def _delta_chunk(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str | None = None,
    usage: Any | None = None,
) -> SimpleNamespace:
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    usage_obj = None
    if usage is not None:
        usage_obj = SimpleNamespace(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
    return SimpleNamespace(choices=[choice], usage=usage_obj)


def _tool_call_delta(*, index: int, id: str = "", name: str = "", arguments: str = "") -> Any:
    return SimpleNamespace(
        index=index,
        id=id or None,
        function=SimpleNamespace(name=name or None, arguments=arguments or None),
    )


def _runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XCODE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = Config(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="fake-model",
        light_model="fake-model",
        data_home=tmp_path / "home",
        trace_enabled=False,
    )
    store = SessionStore(config.data_home)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    session = store.create(ws)
    return config, session, store


async def _collect(client, *, config, session, store, prompt: str) -> list[dict[str, Any]]:
    return [
        event
        async for event in run_agent(
            prompt,
            config=config,
            session=session,
            store=store,
            client=client,
        )
    ]


def test_no_tool_run_emits_text_turn_done(tmp_path, monkeypatch) -> None:
    config, session, store = _runtime(tmp_path, monkeypatch)
    client = _FakeClient(
        [
            [
                _delta_chunk(content="hello "),
                _delta_chunk(content="world", finish_reason="stop"),
            ]
        ]
    )
    events = asyncio.run(_collect(client, config=config, session=session, store=store, prompt="hi"))
    types = [e["type"] for e in events]
    assert "payload" not in events[0]
    assert types.count("text_delta") == 2
    assert "turn_complete" in types
    assert types[-1] == "done"
    assert "run_finished" not in types
    assert "tool_call_delta" not in types
    done = events[-1]
    assert done["total_turns"] == 1
    assert "total_tokens" in done


def test_tool_run_emits_call_then_result(tmp_path, monkeypatch) -> None:
    config, session, store = _runtime(tmp_path, monkeypatch)
    (session.workspace_root / "note.txt").write_text("alpha", encoding="utf-8")

    client = _FakeClient(
        [
            [
                _delta_chunk(
                    tool_calls=[
                        _tool_call_delta(
                            index=0,
                            id="call_1",
                            name="Read",
                            arguments='{"path":"note.txt"}',
                        )
                    ],
                    finish_reason="tool_calls",
                )
            ],
            [
                _delta_chunk(content="done reading", finish_reason="stop"),
            ],
        ]
    )
    events = asyncio.run(
        _collect(client, config=config, session=session, store=store, prompt="read it")
    )
    types = [e["type"] for e in events]
    assert "tool_call" in types
    assert "tool_result" in types
    assert types[-1] == "done"
    assert "run_finished" not in types

    call = next(e for e in events if e["type"] == "tool_call")
    assert call["name"] == "Read"
    assert call["input"] == {"path": "note.txt"}
    assert "arguments" not in call

    result = next(e for e in events if e["type"] == "tool_result")
    assert result["name"] == "Read"
    assert result["is_error"] is False
    assert "alpha" in result["result"]


def test_thinking_and_usage_when_present(tmp_path, monkeypatch) -> None:
    config, session, store = _runtime(tmp_path, monkeypatch)
    client = _FakeClient(
        [
            [
                _delta_chunk(reasoning="step1"),
                _delta_chunk(
                    content="ok",
                    finish_reason="stop",
                    usage={"prompt_tokens": 3, "completion_tokens": 2},
                ),
            ]
        ]
    )
    events = asyncio.run(
        _collect(client, config=config, session=session, store=store, prompt="think")
    )
    types = [e["type"] for e in events]
    assert "thinking_delta" in types
    assert "usage" in types
    thinking = next(e for e in events if e["type"] == "thinking_delta")
    assert thinking["thinking"] == "step1"
    usage = next(e for e in events if e["type"] == "usage")
    assert usage["usage"]["input_tokens"] == 3
    assert usage["usage"]["output_tokens"] == 2
