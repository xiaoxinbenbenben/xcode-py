# 定稿决策表（2026-08-10）

## 长期记忆

| 决策点 | 选定方案 | 说明 |
|---|---|---|
| scope | 仅项目级 | `{data_home}/projects/{project_key}/memories/` |
| 存储 | Markdown，无 SQLite/向量 | `memory_summary.md` / `MEMORY.md` / `raw_memories.md` / `rollout_summaries/` |
| 注入 | 仅 summary + 读指引 | 不塞 MEMORY 正文 |
| 按需读 | `memory_read` + `memory_grep` | summary 路由 + 文档 keywords |
| 写入 | stage1 + stage2 | 权威文件只经 consolidation 原子写 |
| stage1 | 每轮可写 raw/rollout | 空结果跳过 stage2 |
| stage2 触发 | 防抖 | ≥3 条新信号 **或** 空闲 ≥5 分钟；`drain`/退出必跑 |
| 模型 | `light_model` | extract 与 consolidate 同模型 |

## 会话 / 历史

全文见 [session-history.md](./session-history.md)。

| 决策点 | 选定方案 | 说明 |
|---|---|---|
| 主目标 | 可恢复 + 长会话不炸 | 先可靠 append，再 prune/compact |
| 布局 | `meta` + `transcript.jsonl` + `context.json` | 项目下 `sessions/{id}/`；`current_session` 指针保留 |
| 权威 | transcript append-only | 有上限的产品记录，非原始字节归档 |
| 送模缓存 | `context.json` | messages + offset/count/id；可丢、可重建 |
| 对账 | `byte_offset` + `last_event_id` + `event_count` | 半行 JSON 丢弃 |
| Resume | 等长且一致 → 直用；变长 → 尾部增量；否则从最后 compact 重建 | 不扫 compact 前全历史 |
| Compact 事件 | summary + `retained_messages` + `source_through_event_id` | 自包含检查点 |
| 写入入口 | 唯一 `append_message` | 禁各处直接 `messages.append` |
| 落盘 | JSONL append+flush；context/meta 临时文件 rename | 轮末可 fsync transcript |
| 多写者 | 假定单写 | v1 **不加** session 文件锁 |
| Prune | 送模/MEMORY 截断；JSONL 尽量全文 | 固定 `<tool_output_truncated>`；不破坏 tool 配对 |
| 近端保留 | 最近 6 个 **user turn group** | 非按消息条数切片 |
| Compact 时机 | 每次即将调模型前 | 用户轮初 + 每个 tool 后 |
| 预算 | system+XCODE+memory+tools+对话+reserve | `input + reserved_output >= window * threshold` |
| `context_window` | **256000** | 可配 |
| `compact_threshold` | **0.7** | 可配 |
| Compact 模型 | `light_model` | 可与主模型相同 |
| Token | tiktoken 估 + TUI `~used/256k` | 触发与展示共用 |
| 产品 v1 | list/new/resume + auto + `/compact` | **无** `/clear`（用 `--new-session`） |
| 与 MEMORY | 正交 | MEMORY 吃截断后的本轮 messages |
