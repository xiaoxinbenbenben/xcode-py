"""记忆管线：切片、琐碎轮、stage1/2、队列合批。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from xcode.memory.pipeline import (
    MemoryPipeline,
    PipelineRegistry,
    RoundContent,
    is_blacklisted,
    should_extract,
    slice_round,
)
from xcode.memory.store import MEMORY_NAME, MemoryStore


def _store(tmp_path) -> MemoryStore:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return MemoryStore(tmp_path / "home", ws)


def _messages():
    return [
        {"role": "user", "content": "上一轮"},
        {"role": "assistant", "content": "上一轮回复"},
        {"role": "user", "content": "改用 postgresql"},
        {"role": "assistant", "content": "好的，我改"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "edit_file", "arguments": '{"path": "a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "文件已更新"},
    ]


def test_slice_round_latest_only(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    rc = slice_round(_messages(), workspace=ws, session_id="sess-1")
    assert rc.user_text == "改用 postgresql"
    assert "上一轮" not in rc.user_text
    assert rc.assistant_texts == ("好的，我改",)
    assert rc.tool_calls and "edit_file" in rc.tool_calls[0]
    assert rc.tool_outputs == ("文件已更新",)


def test_slice_round_immutable(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    messages = _messages()
    rc = slice_round(messages, workspace=ws, session_id="s")
    messages.append({"role": "user", "content": "下一轮"})
    messages[3]["content"] = "被篡改"
    assert rc.user_text == "改用 postgresql"
    assert rc.assistant_texts == ("好的，我改",)


def test_should_extract():
    assert should_extract(RoundContent(workspace="/w", session_id="s", user_text="谢谢", assistant_texts=("好",))) is False
    assert should_extract(RoundContent(workspace="/w", session_id="s", user_text="项目改用 postgresql")) is True
    assert should_extract(RoundContent(workspace="/w", session_id="s", user_text="嗯", tool_calls=("bash(ls)",))) is True


def test_blacklist():
    assert is_blacklisted("密钥 sk-abcdefghijklmnopqrstuvwxyz") is True
    assert is_blacklisted("提交前跑 ruff") is False


class _FakeCompletions:
    def __init__(self, replies: list[str], *, fail: bool = False):
        self.replies = list(replies)
        self.fail = fail
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("boom")
        reply = self.replies.pop(0) if self.replies else '{"raw_bullets":[],"rollout_summary":""}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])


def _client(replies: list[str], *, fail: bool = False):
    return SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions(replies, fail=fail)))


def _run(pipeline: MemoryPipeline, items: list[RoundContent]) -> None:
    async def _main():
        for item in items:
            pipeline.submit(item)
        await pipeline.drain()

    asyncio.run(_main())


def test_pipeline_empty_stage1_skips_consolidate(tmp_path):
    store = _store(tmp_path)
    completions = _FakeCompletions(['{"raw_bullets":[],"rollout_summary":""}'])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    pipeline = MemoryPipeline(store=store, client=client, model="m")
    _run(
        pipeline,
        [RoundContent(workspace=str(store.workspace), session_id="s1", user_text="记住约定 ruff")],
    )
    assert len(completions.calls) == 1  # only stage1


def test_pipeline_stage1_and_stage2_write_files(tmp_path):
    """单次信号在 drain 时强制 consolidate。"""
    store = _store(tmp_path)
    stage1 = '{"raw_bullets":["提交前跑 ruff"],"rollout_summary":"约定 lint"}'
    stage2 = (
        '{"unchanged":false,'
        '"MEMORY_md":"v1\\n# MEMORY\\n## lint\\nkeywords: ruff\\n- 提交前跑 ruff\\n",'
        '"memory_summary_md":"v1\\n# Memory Summary\\n用 ruff\\n\\n## Whats in Memory\\n- lint\\n"}'
    )
    client = _client([stage1, stage2])
    pipeline = MemoryPipeline(store=store, client=client, model="m")
    _run(
        pipeline,
        [RoundContent(workspace=str(store.workspace), session_id="s1", user_text="记住提交前跑 ruff")],
    )
    assert "ruff" in store.read_rel(MEMORY_NAME)
    assert "ruff" in store.read_summary()
    assert "ruff" in store.read_rel("raw_memories.md")
    rollouts = list((store.root / "rollout_summaries").glob("*.md"))
    assert len(rollouts) == 1


async def _await_worker(pipeline: MemoryPipeline) -> None:
    """只等队列 worker 结束，不 force consolidation（与 drain 不同）。"""
    if pipeline._worker is not None:
        await pipeline._worker


def test_debounce_skips_stage2_until_threshold(tmp_path):
    store = _store(tmp_path)
    stage1 = '{"raw_bullets":["事实A"],"rollout_summary":""}'
    client = _client([stage1])
    pipeline = MemoryPipeline(
        store=store,
        client=client,
        model="m",
        consolidate_min_signals=3,
        consolidate_idle_seconds=3600,
    )

    async def _main():
        pipeline.submit(
            RoundContent(
                workspace=str(store.workspace),
                session_id="s",
                user_text="足够长的用户输入A",
            )
        )
        await _await_worker(pipeline)
        assert pipeline.pending_signal_count == 1
        assert len(client.chat.completions.calls) == 1
        if pipeline._idle_task and not pipeline._idle_task.done():
            pipeline._idle_task.cancel()
            try:
                await pipeline._idle_task
            except asyncio.CancelledError:
                pass

    asyncio.run(_main())
    assert "事实A" in store.read_rel("raw_memories.md")
    assert "事实A" not in store.read_rel(MEMORY_NAME)


def test_debounce_flushes_at_min_signals(tmp_path):
    store = _store(tmp_path)
    stage1 = '{"raw_bullets":["事实"],"rollout_summary":""}'
    stage2 = (
        '{"unchanged":false,"MEMORY_md":"v1\\n# M\\n三连\\n",'
        '"memory_summary_md":"v1\\n# S\\n三连\\n"}'
    )
    client = _client([stage1, stage1, stage1, stage2])
    pipeline = MemoryPipeline(
        store=store,
        client=client,
        model="m",
        consolidate_min_signals=3,
        consolidate_idle_seconds=3600,
    )

    async def _main():
        for i in range(3):
            pipeline.submit(
                RoundContent(
                    workspace=str(store.workspace),
                    session_id="s",
                    user_text=f"足够长的用户输入内容{i}",
                )
            )
            await _await_worker(pipeline)
        assert "三连" in store.read_rel(MEMORY_NAME)
        # 3 stage1 + 1 stage2
        assert len(client.chat.completions.calls) == 4

    asyncio.run(_main())


def test_debounce_idle_triggers_consolidate(tmp_path):
    store = _store(tmp_path)
    stage1 = '{"raw_bullets":["空闲合并"],"rollout_summary":""}'
    stage2 = (
        '{"unchanged":false,"MEMORY_md":"v1\\n# M\\n空闲\\n",'
        '"memory_summary_md":"v1\\n# S\\n空闲\\n"}'
    )
    client = _client([stage1, stage2])
    pipeline = MemoryPipeline(
        store=store,
        client=client,
        model="m",
        consolidate_min_signals=99,
        consolidate_idle_seconds=0.05,
    )

    async def _main():
        pipeline.submit(
            RoundContent(
                workspace=str(store.workspace),
                session_id="s",
                user_text="足够长的用户输入触发空闲",
            )
        )
        await _await_worker(pipeline)
        assert pipeline.pending_signal_count == 1
        await asyncio.sleep(0.15)
        assert "空闲" in store.read_rel(MEMORY_NAME)

    asyncio.run(_main())


def test_pipeline_blacklist_drops_secret_bullet(tmp_path):
    store = _store(tmp_path)
    stage1 = (
        '{"raw_bullets":["提交前跑 ruff","api_key = sk-abcdefghijklmnopqrstuvwxyz"],'
        '"rollout_summary":""}'
    )
    stage2 = (
        '{"unchanged":false,"MEMORY_md":"v1\\n# M\\nruff\\n",'
        '"memory_summary_md":"v1\\n# S\\nruff\\n"}'
    )
    client = _client([stage1, stage2])
    pipeline = MemoryPipeline(store=store, client=client, model="m")
    _run(
        pipeline,
        [RoundContent(workspace=str(store.workspace), session_id="s1", user_text="记住约定")],
    )
    raw = store.read_rel("raw_memories.md")
    assert "ruff" in raw
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in raw


def test_pipeline_batches_one_stage1(tmp_path):
    store = _store(tmp_path)
    # empty bullets → only one stage1, no stage2
    client = _client(['{"raw_bullets":[],"rollout_summary":""}'])
    pipeline = MemoryPipeline(store=store, client=client, model="m")
    _run(
        pipeline,
        [
            RoundContent(workspace=str(store.workspace), session_id="s", user_text="轮次一详情内容"),
            RoundContent(workspace=str(store.workspace), session_id="s", user_text="轮次二详情内容"),
        ],
    )
    assert len(client.chat.completions.calls) == 1
    prompt = client.chat.completions.calls[0]["messages"][1]["content"]
    assert "轮次一" in prompt and "轮次二" in prompt


def test_pipeline_failure_puts_back(tmp_path):
    store = _store(tmp_path)
    completions = _FakeCompletions([], fail=True)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    pipeline = MemoryPipeline(store=store, client=client, model="m")
    _run(
        pipeline,
        [RoundContent(workspace=str(store.workspace), session_id="s", user_text="有内容的一轮对话")],
    )
    assert pipeline.pending_count == 1
    completions.fail = False
    completions.replies = ['{"raw_bullets":[],"rollout_summary":""}']
    _run(
        pipeline,
        [RoundContent(workspace=str(store.workspace), session_id="s", user_text="再来一轮足够长的话")],
    )
    assert pipeline.pending_count == 0


def test_stage2_unchanged_keeps_old(tmp_path):
    store = _store(tmp_path)
    store.atomic_write(MEMORY_NAME, "v1\n# KEEP\nold content\n")
    stage1 = '{"raw_bullets":["新事实"],"rollout_summary":"s"}'
    stage2 = '{"unchanged":true}'
    client = _client([stage1, stage2])
    pipeline = MemoryPipeline(store=store, client=client, model="m")
    _run(
        pipeline,
        [RoundContent(workspace=str(store.workspace), session_id="s", user_text="新事实写入测试内容")],
    )
    assert "KEEP" in store.read_rel(MEMORY_NAME)
    assert "old content" in store.read_rel(MEMORY_NAME)


def test_registry_per_workspace(tmp_path):
    home = tmp_path / "home"
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    client = _client([])
    reg = PipelineRegistry(data_home=home, client=client, model="m")
    assert reg.for_workspace(a) is reg.for_workspace(a)
    assert reg.for_workspace(a) is not reg.for_workspace(b)


def test_stage2_failure_keeps_pending_and_drain_retries(tmp_path):
    """stage2 LLM 失败时 pending 不清空；drain 可重试直到成功。"""
    store = _store(tmp_path)
    stage1 = '{"raw_bullets":["不可丢的事实"],"rollout_summary":""}'
    stage2_ok = (
        '{"unchanged":false,"MEMORY_md":"v1\\n# M\\n不可丢的事实\\n",'
        '"memory_summary_md":"v1\\n# S\\n不可丢的事实\\n"}'
    )

    class _Seq:
        def __init__(self):
            self.n = 0

        async def create(self, **kwargs):
            self.n += 1
            if self.n == 1:
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=stage1))]
                )
            if self.n == 2:
                raise RuntimeError("stage2 boom")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=stage2_ok))]
            )

    pipeline = MemoryPipeline(
        store=store,
        client=SimpleNamespace(chat=SimpleNamespace(completions=_Seq())),
        model="m",
        consolidate_min_signals=1,
        consolidate_idle_seconds=3600,
    )

    async def _main():
        pipeline.submit(
            RoundContent(
                workspace=str(store.workspace),
                session_id="s",
                user_text="足够长的用户输入保留事实",
            )
        )
        await _await_worker(pipeline)
        assert pipeline.pending_signal_count == 1
        assert "不可丢的事实" not in store.read_summary()
        await pipeline.drain()
        assert pipeline.pending_signal_count == 0
        assert "不可丢的事实" in store.read_summary()

    asyncio.run(_main())


def test_consolidate_reads_full_memory_not_truncated(tmp_path):
    store = _store(tmp_path)
    tail = "TAIL_UNIQUE_MARKER_SHOULD_BE_SEEN"
    huge = "v1\n# MEMORY\n" + ("x" * 15000) + "\n" + tail + "\n"
    store.atomic_write(MEMORY_NAME, huge)
    stage1 = '{"raw_bullets":["新点"],"rollout_summary":""}'
    stage2 = (
        '{"unchanged":false,"MEMORY_md":"v1\\n# M\\n保留\\n",'
        '"memory_summary_md":"v1\\n# S\\nok\\n"}'
    )
    seen: list[str] = []

    class _Cap:
        async def create(self, **kwargs):
            seen.append(kwargs["messages"][1]["content"])
            n = len(seen)
            content = stage1 if n == 1 else stage2
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Cap()))
    pipeline = MemoryPipeline(
        store=store, client=client, model="m", consolidate_min_signals=1
    )
    _run(
        pipeline,
        [
            RoundContent(
                workspace=str(store.workspace),
                session_id="s",
                user_text="足够长的输入触发归并全文",
            )
        ],
    )
    assert len(seen) >= 2
    assert tail in seen[1], "stage2 prompt must include full MEMORY tail"


def test_clear_invalidates_inflight_writes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    home = tmp_path / "home"
    stage1 = '{"raw_bullets":["不该写回"],"rollout_summary":"x"}'
    stage2 = (
        '{"unchanged":false,"MEMORY_md":"v1\\n# M\\n不该写回\\n",'
        '"memory_summary_md":"v1\\n# S\\n不该写回\\n"}'
    )
    gate = asyncio.Event()

    class _Slow:
        def __init__(self):
            self.n = 0

        async def create(self, **kwargs):
            self.n += 1
            if self.n == 1:
                await gate.wait()
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=stage1))]
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=stage2))]
            )

    reg = PipelineRegistry(
        data_home=home,
        client=SimpleNamespace(chat=SimpleNamespace(completions=_Slow())),
        model="m",
    )
    pipeline = reg.for_workspace(ws)

    async def _main():
        pipeline.submit(
            RoundContent(
                workspace=str(ws.resolve()),
                session_id="s",
                user_text="足够长的输入测试 clear 竞态",
            )
        )
        await asyncio.sleep(0.05)
        reg.clear_workspace(ws)
        gate.set()
        await pipeline.drain()
        assert "不该写回" not in pipeline._store.read_summary()
        assert "不该写回" not in pipeline._store.read_rel(MEMORY_NAME)

    asyncio.run(_main())
