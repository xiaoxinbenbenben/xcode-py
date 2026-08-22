"""Ctrl-C / cancel_event 应停在本轮，而不是把 user 消息丢掉。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from xcode.config import Config
from xcode.runtime.agent import run_agent
from xcode.runtime.session import SessionStore


class _HangStream:
    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    def __aiter__(self) -> _HangStream:
        return self

    async def __anext__(self):
        await self._gate.wait()
        raise StopAsyncIteration


class _HangCompletions:
    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def create(self, **_kwargs):
        return _HangStream(self._gate)


def _config(tmp_path: Path) -> Config:
    return Config(
        api_key="test",
        base_url="http://127.0.0.1:9",
        model="dummy",
        light_model="dummy",
        data_home=tmp_path / "home",
    )


def test_run_agent_stops_when_cancel_event_set(tmp_path: Path):
    config = _config(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    store = SessionStore(config.data_home)
    session = store.create(ws)
    gate = asyncio.Event()
    client = SimpleNamespace(chat=SimpleNamespace(completions=_HangCompletions(gate)))
    cancel = asyncio.Event()

    async def _body() -> list[dict]:
        events: list[dict] = []

        async def _trip() -> None:
            await asyncio.sleep(0.05)
            cancel.set()
            gate.set()

        trip = asyncio.create_task(_trip())
        async for event in run_agent(
            "hello cancel",
            config=config,
            session=session,
            client=client,
            cancel_event=cancel,
        ):
            events.append(event)
        await trip
        return events

    events = asyncio.run(_body())
    assert any(e.get("type") == "error" and e.get("error") == "cancelled" for e in events)
    assert session.messages[0]["role"] == "user"
    assert "hello cancel" in str(session.messages[0]["content"])