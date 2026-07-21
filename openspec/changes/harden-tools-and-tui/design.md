## Context

xcode 已有自研 agent loop 与基础工具。本设计对齐 xx-coding 的可靠性思路，但保持更干净的数据结构，并自研终端体验（不照搬 PaiCLI）。

## Goals / Non-Goals

**Goals**

- 工具结果可被模型与 UI 稳定消费（status/stats/context）
- 文件编辑防踩踏（snapshot 乐观锁）
- 长会话可压缩且不打断 tool_call / tool 成对消息
- Bash 与权限有底线；hooks 默认可追踪
- TUI 有品牌层次与清晰动线

**Non-Goals**

- Team / Worktree / BackgroundRun / 真 TaskRun 子代理
- OpenAI Agents SDK
- MCP / LSP / RAG（留给后续与 PaiCLI 对齐时再做）

## Decisions

1. **ToolResponse**：`status ∈ {success, partial, error}` + `text` + `data` + `stats{time_ms,...}` + `context{cwd,params_input}` + optional `error`。`ToolResult` 改为薄别名/工厂，避免双轨。
2. **Snapshot**：`ToolContext.snapshots: dict[rel_path, FileSnapshot]`；Read 成功后写入；Edit/Write 校验 mtime_ms+size，冲突返回 `error.code=FILE_CHANGED`。
3. **Compaction**：以估算字符数 + 用户轮次双阈值；切分时不把 orphan tool message 留在 recent；摘要按 role/工具名结构化，硬上限截断。
4. **Bash**：特权词 / `rm -rf /` hard deny；超限输出写入 `session_data_dir/tool-output/`，text 仅预览 + 回查路径。
5. **Permissions**：`~/.xcode/settings.json` 与 `.xcode/settings.json` 的 `permissions.rules`；决策 allow/deny/ask；`-p` 仍可 `auto_allow`。
6. **TUI 美学**：工业蓝图感——克制的 mono 层次、信号绿强调、少装饰；slash 补全 + 底栏状态（model/session/turns）。

## Risks / Trade-offs

- 乐观锁依赖 Read 先发生；未 Read 直接 Edit 时允许（无锁）或要求？→ **无 snapshot 时放行并写后登记**，避免打断现有流程。
- 本地 compaction 不如 LLM 摘要精准 → 可接受；后续可选用 light_model 增强。

## Migration

- 对模型可见的 tool message JSON 形状变化（ok→status）；测试覆盖序列化。
