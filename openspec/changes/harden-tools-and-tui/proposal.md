## Why

对照 xx-coding 后，xcode 的工具协议、文件冲突检测、压缩与权限/hooks 仍偏 MVP，影响可靠性；同时 TUI 仅有裸 REPL，缺少可感知的产品交互层。现在补齐「协议 / 可靠性 / 上下文」与简约入口体验，刻意不做 Team/Worktree/BackgroundRun/TaskRun。

## What Changes

- 引入更完整的 `ToolResponse`（status / stats / context / error），替换简陋 ok 信封
- Edit/Write 增加基于 Read snapshot 的乐观锁冲突检测
- 增强本地启发式 compaction（按体积触发、保持 tool 成对、结构化摘要）
- Bash：危险命令硬拒绝 + 大输出截断并落盘可回查
- 权限：支持 settings.json 规则文件 + hard deny；默认 hooks（trace）
- CLI/TUI：简约大气的 banner / prompt / toolbar / slash / 工具渲染
- **不做**：Team / Worktree / BackgroundRun / TaskRun 编排扩展；不引入 OpenAI Agents SDK

## Capabilities

### New Capabilities

- `tool-protocol`: 统一工具响应信封与输出截断落盘
- `file-locking`: Read 记录 snapshot，Edit/Write 冲突检测
- `context-compaction`: 更可靠的本地历史压缩
- `permissions-hooks`: 规则文件权限与默认生命周期 hooks
- `terminal-ux`: 单入口下的产品级终端交互层

### Modified Capabilities

- （无既有 main specs 需 delta；本 change 以新能力为主）

## Impact

- `src/xcode/tools/*`、`context/compaction.py`、`permissions/*`、`hooks/*`、`runtime/agent.py`、`entrypoints/*`
- 测试：`tests/test_tools.py`、`test_context.py`、新增权限/协议/TUI 冒烟
- 依赖：无新第三方依赖（继续 prompt-toolkit / rich / typer）
