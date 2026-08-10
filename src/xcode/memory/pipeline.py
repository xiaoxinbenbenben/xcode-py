"""长期记忆写入管线：对话轮次 → stage1 抽取 → 防抖 stage2 合并。

## 端到端时序（TUI 一轮结束后）
1. tui 调用 slice_round(session.messages) 切出「最后一个 user 及其后」的快照
2. should_extract 过滤寒暄/过短轮
3. PipelineRegistry.for_workspace → MemoryPipeline.submit 入队
4. 后台 worker 取批（一次尽量掏空队列合并多轮）：
   a. stage1 LLM → bullets + rollout_summary
   b. 密钥黑名单过滤
   c. 写 rollout 文件（可选）+ append raw_memories
   d. 拼一条 signal 放进 pending_signals
   e. 若 pending ≥3 → 立刻 stage2；否则 arm 5 分钟 idle 定时器
5. stage2：读**完整** MEMORY + summary + 本批 signals → LLM → 原子写两文件
   - 成功或 unchanged 才从 pending 丢掉对应 signals
   - 失败/解析失败：**保留 pending**，等 drain 或下次再试
6. 退出 TUI：registry.drain_all 有界重试强制 flush

## 并发与 clear 安全
- 每 workspace 一个 Pipeline（单 worker 串行），避免两路同时改 MEMORY
- _epoch：/memory clear 时 +1 并清空 queue/pending；在途 stage1/2 看到 epoch 变了就丢弃写盘

## 与会话历史
stage1 吃的是**送模侧** messages（tool 可能已 prune），不是 JSONL 全文。
这是有意的：记忆只要稳定事实，不需要巨型 tool 原文。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xcode.memory.prompts import (
    STAGE1_SYSTEM,
    STAGE2_SYSTEM,
    parse_stage1,
    parse_stage2,
)
from xcode.memory.store import (
    MEMORY_NAME,
    SUMMARY_NAME,
    MemoryStore,
    format_raw_append,
)

logger = logging.getLogger(__name__)

TOOL_OUTPUT_PER_ITEM = 200
TOOL_OUTPUT_TOTAL = 2000
EXTRACT_MAX_TOKENS = 1024
CONSOLIDATE_MAX_TOKENS = 16384
CONSOLIDATE_MIN_SIGNALS = 3
CONSOLIDATE_IDLE_SECONDS = 300.0
DRAIN_MAX_ROUNDS = 8
_TRIVIAL_USER = 8
_TRIVIAL_TOTAL = 16

_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|api[_-]?key\s*[=:]\s*\S+|"
    r"password\s*[=:]\s*\S+|secret\s*[=:]\s*\S+|token\s*[=:]\s*\S+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RoundContent:
    """一轮对话的不可变快照。"""

    workspace: str
    session_id: str
    user_text: str
    assistant_texts: tuple[str, ...] = ()
    tool_calls: tuple[str, ...] = ()
    tool_outputs: tuple[str, ...] = ()


def slice_round(
    messages: list[dict[str, Any]],
    *,
    workspace: Path,
    session_id: str,
) -> RoundContent:
    """从完整 messages 切出「最近一轮用户回合」供 stage1。

    从最后一条 user 起，收集该 user、后续 assistant 文本、tool_calls 摘要、tool 输出片段。
    tool 输出有总预算，避免 stage1 提示本身过长。
    """
    last_user = -1
    for index, message in enumerate(messages):
        if message.get("role") == "user":
            last_user = index
    if last_user < 0:
        last_user = 0

    user_text = ""
    assistant_texts: list[str] = []
    tool_calls: list[str] = []
    tool_outputs: list[str] = []
    out_budget = TOOL_OUTPUT_TOTAL

    for message in messages[last_user:]:
        role = message.get("role")
        if role == "user":
            user_text = str(message.get("content") or "")
        elif role == "assistant":
            text = str(message.get("content") or "")
            if text:
                assistant_texts.append(text)
            for tc in message.get("tool_calls") or []:
                function = tc.get("function") or {}
                name = str(function.get("name") or "?")
                arguments = str(function.get("arguments") or "")
                if len(arguments) > 80:
                    arguments = arguments[:79] + "…"
                tool_calls.append(f"{name}({arguments})")
        elif role == "tool":
            snippet = str(message.get("content") or "").strip()[:TOOL_OUTPUT_PER_ITEM]
            if snippet and out_budget > 0:
                tool_outputs.append(snippet)
                out_budget -= len(snippet)

    return RoundContent(
        workspace=str(workspace.resolve()),
        session_id=session_id,
        user_text=user_text,
        assistant_texts=tuple(assistant_texts),
        tool_calls=tuple(tool_calls),
        tool_outputs=tuple(tool_outputs),
    )


def should_extract(round_content: RoundContent) -> bool:
    """是否值得跑 stage1：有 tool、或用户话够长、或总文本够长。"""
    if round_content.tool_calls:
        return True
    text_len = len(round_content.user_text) + sum(
        len(text) for text in round_content.assistant_texts
    )
    if len(round_content.user_text.strip()) >= _TRIVIAL_USER:
        return True
    return text_len >= _TRIVIAL_TOTAL


def is_blacklisted(content: str) -> bool:
    return bool(_SECRET_RE.search(content))


def _combine_rounds(batch: list[RoundContent]) -> str:
    parts: list[str] = []
    for index, rc in enumerate(batch, start=1):
        parts.append(f"--- 轮次 {index} ---")
        parts.append(f"用户：{rc.user_text}")
        if rc.assistant_texts:
            parts.append("助手：" + "\n".join(rc.assistant_texts))
        if rc.tool_calls:
            parts.append("工具调用：" + "；".join(rc.tool_calls))
        if rc.tool_outputs:
            parts.append("工具输出（截断）：" + "；".join(rc.tool_outputs))
    return "\n".join(parts)


async def _chat_text(client: Any, *, model: str, system: str, user: str, max_tokens: int) -> str:
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
        temperature=0,
    )
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    return str(choices[0].message.content or "")


class MemoryPipeline:
    """单个 workspace 的写入状态机：队列 + 单 worker + pending 信号 + idle 定时器。"""

    def __init__(
        self,
        *,
        store: MemoryStore,
        client: Any,
        model: str,
        consolidate_min_signals: int = CONSOLIDATE_MIN_SIGNALS,
        consolidate_idle_seconds: float = CONSOLIDATE_IDLE_SECONDS,
    ) -> None:
        self._store = store
        self._client = client
        self._model = model
        self._consolidate_min_signals = consolidate_min_signals
        self._consolidate_idle_seconds = consolidate_idle_seconds
        self._queue: asyncio.Queue[RoundContent] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._pending_signals: list[str] = []
        self._last_signal_at: float | None = None
        self._epoch = 0

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    @property
    def pending_signal_count(self) -> int:
        return len(self._pending_signals)

    @property
    def is_running(self) -> bool:
        return self._worker is not None and not self._worker.done()

    @property
    def epoch(self) -> int:
        return self._epoch

    def submit(self, item: RoundContent) -> None:
        """非阻塞入队；若 worker 没在跑则拉起。不在这里 await LLM。"""
        self._queue.put_nowait(item)
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())

    def invalidate(self) -> None:
        """作废在途任务：清空 queue/pending，bump epoch（/memory clear 用）。"""
        self._epoch += 1
        self._pending_signals.clear()
        self._last_signal_at = None
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
            self._idle_task = None

    def clear_files_and_invalidate(self) -> None:
        """先作废在途写入，再清空磁盘记忆目录。"""
        self.invalidate()
        self._store.clear()

    async def drain(self) -> None:
        """进程退出前尽量把 queue 与 pending 刷完。

        最多 DRAIN_MAX_ROUNDS 轮：等 worker → 强制 _flush_consolidate → 再看队列。
        仍失败则打 warning；pending 留在内存，raw 已在磁盘，不会无限重试。
        """
        for _ in range(DRAIN_MAX_ROUNDS):
            await self._cancel_idle()
            if self._worker is not None and not self._worker.done():
                await self._worker
            if not self._queue.empty():
                self._ensure_worker()
                continue
            if self._pending_signals:
                try:
                    await self._flush_consolidate()
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "memory drain consolidate failed for %s",
                        self._store.root,
                        exc_info=True,
                    )
                    # pending 保留，下一轮再试
                    continue
                continue
            return
        if self._queue.qsize() or self._pending_signals:
            logger.warning(
                "memory drain incomplete for %s: queue=%s pending=%s",
                self._store.root,
                self._queue.qsize(),
                len(self._pending_signals),
            )

    async def _cancel_idle(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
        self._idle_task = None

    async def _worker_loop(self) -> None:
        """串行消费：每次尽量把队列掏成一批，减少 stage1 调用次数。"""
        while True:
            batch = [await self._queue.get()]
            while not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await self._process_batch(batch)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "memory pipeline failed for %s", self._store.root, exc_info=True
                )
                # 整批塞回队列，结束本 worker；后续 submit/drain 会再拉起
                for item in batch:
                    self._queue.put_nowait(item)
                return
            if self._queue.empty():
                return

    async def _process_batch(self, batch: list[RoundContent]) -> None:
        """一批轮次的 stage1 + 可能触发的 stage2；全程用 epoch 防 clear 竞态。"""
        epoch = self._epoch
        round_text = _combine_rounds(batch)
        stage1_user = f"## 本轮对话\n{round_text}"
        stage1_text = await _chat_text(
            self._client,
            model=self._model,
            system=STAGE1_SYSTEM,
            user=stage1_user,
            max_tokens=EXTRACT_MAX_TOKENS,
        )
        if epoch != self._epoch:
            return

        bullets, rollout_summary = parse_stage1(stage1_text)
        bullets = [b for b in bullets if not is_blacklisted(b)]
        if is_blacklisted(rollout_summary):
            rollout_summary = ""

        if not bullets and not rollout_summary:
            return

        session_id = batch[-1].session_id
        rollout_rel: str | None = None
        if rollout_summary:
            body = (
                f"# Rollout summary\n\nsession: {session_id}\n\n{rollout_summary}\n"
            )
            if bullets:
                body += "\n## bullets\n" + "\n".join(f"- {b}" for b in bullets) + "\n"
            if epoch != self._epoch:
                return
            rollout_rel = self._store.write_rollout(session_id, body)

        if epoch != self._epoch:
            return
        self._store.append_raw(
            format_raw_append(
                session_id=session_id,
                bullets=bullets,
                rollout_rel=rollout_rel,
            )
        )

        signal = _format_new_signal(bullets, rollout_summary, rollout_rel)
        if epoch != self._epoch:
            return
        self._pending_signals.append(signal)
        self._last_signal_at = time.monotonic()
        if len(self._pending_signals) >= self._consolidate_min_signals:
            await self._flush_consolidate()
        else:
            self._arm_idle_flush()

    def _arm_idle_flush(self) -> None:
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(self._idle_flush())

    async def _idle_flush(self) -> None:
        try:
            await asyncio.sleep(self._consolidate_idle_seconds)
        except asyncio.CancelledError:
            return
        if self._pending_signals:
            try:
                await self._flush_consolidate()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "memory idle consolidate failed for %s",
                    self._store.root,
                    exc_info=True,
                )

    async def _flush_consolidate(self) -> None:
        """成功写盘或 unchanged 后才消费 pending；失败保留 pending。"""
        if not self._pending_signals:
            return
        await self._cancel_idle()
        epoch = self._epoch
        signals = list(self._pending_signals)
        new_signal = "\n\n---\n\n".join(signals)

        self._store.ensure_layout()
        try:
            # 全文读入：禁止截断，否则整文件重写会丢尾部
            memory_md = self._store.read_rel(MEMORY_NAME, limit=None)
        except FileNotFoundError:
            memory_md = ""
        summary_md = self._store.read_summary(limit=None)

        user = (
            f"## 现有 MEMORY.md（完整，禁止丢弃未见部分）\n{memory_md}\n\n"
            f"## 现有 memory_summary.md（完整）\n{summary_md}\n\n"
            f"## 本批新信号（可含多轮）\n{new_signal}\n"
        )
        # 输出 token 随注册表增大，避免写不全
        max_tokens = max(
            CONSOLIDATE_MAX_TOKENS,
            min(32000, len(memory_md) // 2 + 2048),
        )
        try:
            text = await _chat_text(
                self._client,
                model=self._model,
                system=STAGE2_SYSTEM,
                user=user,
                max_tokens=max_tokens,
            )
        except Exception:
            # pending 原样保留
            raise

        if epoch != self._epoch:
            return

        unchanged, new_memory, new_summary = parse_stage2(text)
        if unchanged:
            self._drop_pending_prefix(signals)
            return
        if not new_memory or not new_summary:
            # 解析失败：保留 pending 供 drain 重试
            logger.warning(
                "memory consolidate parse failed for %s; keeping pending",
                self._store.root,
            )
            return

        if epoch != self._epoch:
            return
        self._store.atomic_write(MEMORY_NAME, new_memory)
        self._store.atomic_write(SUMMARY_NAME, new_summary)
        self._drop_pending_prefix(signals)

    def _drop_pending_prefix(self, signals: list[str]) -> None:
        """消费已成功处理的信号；并发追加的新信号保留。"""
        if self._pending_signals[: len(signals)] == signals:
            del self._pending_signals[: len(signals)]
            return
        for signal in signals:
            try:
                self._pending_signals.remove(signal)
            except ValueError:
                pass


def _format_new_signal(
    bullets: list[str], rollout_summary: str, rollout_rel: str | None
) -> str:
    parts: list[str] = []
    if bullets:
        parts.append("bullets:\n" + "\n".join(f"- {b}" for b in bullets))
    if rollout_summary:
        parts.append("rollout_summary:\n" + rollout_summary)
    if rollout_rel:
        parts.append(f"rollout_path: {rollout_rel}")
    return "\n\n".join(parts) if parts else "(none)"


class PipelineRegistry:
    """TUI/CLI 持有的多 workspace 管线表（绝对路径 → MemoryPipeline）。

    for_workspace 懒创建；drain_all 在退出时并发等各管线 drain。
    """

    def __init__(
        self,
        *,
        data_home: Path,
        client: Any,
        model: str,
    ) -> None:
        self._data_home = data_home
        self._client = client
        self._model = model
        self._pipelines: dict[str, MemoryPipeline] = {}

    def for_workspace(self, workspace: Path) -> MemoryPipeline:
        key = str(workspace.resolve())
        pipeline = self._pipelines.get(key)
        if pipeline is None:
            store = MemoryStore(self._data_home, workspace)
            store.ensure_layout()
            pipeline = MemoryPipeline(
                store=store, client=self._client, model=self._model
            )
            self._pipelines[key] = pipeline
        return pipeline

    def clear_workspace(self, workspace: Path) -> None:
        """作废在途任务并清空该 workspace 记忆文件。"""
        self.for_workspace(workspace).clear_files_and_invalidate()

    async def drain_all(self) -> None:
        """排空各 workspace 队列并强制 consolidation flush。"""
        tasks = [p.drain() for p in self._pipelines.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
