## Context

`xx-coding` 是基于 OpenAI Agents SDK 的本地 coding agent：Python CLI（`scripts/cli.py`）+ React/Ink TUI（`tui/`）双入口；核心分布在 `src/runtime|tools|context|tasks|permissions|hooks`。`PaiCLI-Python` 展示了更好的单入口模式：Typer `invoke_without_command=True`，无子命令进 REPL，`-p` 走单次调用。本仓库将以纯 Python 包 `xcode` 重建能力，参考目录只读。

## Goals / Non-Goals

**Goals:**

- 标准包 `src/xcode/`，可 `pip/uv install -e .`，入口 `xcode` / `python -m xcode`
- 单一入口：交互 TUI/REPL 与 CLI 子命令共存于同一 Typer app
- 功能对齐 xx-coding 核心（见下方对齐表），代码更简洁、层次清晰、可测
- 自研 agent loop（openai chat completions + tool calling），不依赖 openai-agents SDK

**Non-Goals:**

- 不移植 React/Ink TUI；不 1:1 复制 xx-coding 文件结构
- 不做完整 AgentTeam / worktree 多进程编排的生产级实现（提供 MVP：可列队、可派发轻量子任务）
- 不迁移旧 `artifacts/` 会话数据到 `~/.xcode/`

## Decisions

### 1. 包布局

```text
src/xcode/
  __main__.py          # python -m xcode
  entrypoints/
    cli.py             # Typer 单入口
    tui.py             # prompt-toolkit + rich 交互层
  config.py            # 环境变量 / .env
  runtime/
    session.py         # 会话持久化
    events.py          # 结构化事件
    agent.py           # agent loop（流式）
    tracing.py         # 可选本地 trace
  tools/
    base.py / registry.py / builtins.py
  context/
    builder.py / compaction.py / mentions.py / memory.py
  tasks/               # TaskCreate/List + TaskRun MVP
  permissions/ hooks/ skills/
```

**理由**：边界清晰——entrypoints 只做 I/O，runtime 编排，tools 无状态执行。

**备选**：沿用 `src/` 无包名 → 拒绝（导入与发布体验差）。

### 2. 单入口（参考 PaiCLI，不照搬）

| 调用 | 行为 |
|------|------|
| `xcode` | 启动交互 TUI/REPL |
| `xcode -p "..."` | 单次 prompt，打印结果后退出 |
| `xcode session list` 等 | 子命令 CLI |
| `xcode doctor` | 环境自检 |

实现：`typer.Typer(invoke_without_command=True)` + callback 判断 `ctx.invoked_subcommand`。

### 3. 功能对齐表（xx-coding → xcode）

| xx-coding | xcode 模块 |
|-----------|------------|
| CLI `scripts/cli.py` + Ink TUI | `entrypoints/cli.py` + `entrypoints/tui.py` |
| Session / workspace | `runtime/session.py` |
| Runtime events + streaming | `runtime/events.py` + `runtime/agent.py` |
| Local tracing | `runtime/tracing.py` |
| Context L1/L2/L3 + compaction | `context/builder.py` + `compaction.py` |
| @file 预处理 | `context/mentions.py` |
| Long-term memory | `context/memory.py` |
| LS/Glob/Grep/Read/Edit/Write/Bash/Todo | `tools/builtins.py` |
| Compact tool | `tools/builtins.py` Compact |
| TaskCreate/Update/List/Get/Run | `tasks/` + task tools |
| BackgroundRun | Bash + 后台任务 MVP |
| Skills | `skills/` + Skill tool |
| Permissions / Hooks | `permissions/` / `hooks/` |
| AgentTeam / Worktree | MVP stub（可扩展） |

### 4. Agent loop

自研：组装 messages → 调用 chat.completions（stream）→ 解析 tool_calls → 经 permission → 执行 tool → 追加 tool result → 循环，直到无 tool_calls。产出 `run_started` / `text_delta` / `tool_call` / `tool_result` / `run_finished` 等事件供 TUI 渲染。

**备选**：继续用 openai-agents → 拒绝（与「纯 Python 简洁架构」目标冲突）。

### 5. 数据目录

- 用户态：`~/.xcode/projects/<key>/sessions|memory|todos`
- 项目可选：`.xcode/` 本地覆盖

## Risks / Trade-offs

- [功能面广] → 先交付可运行核心（session + tools + loop + 单入口），Team/Worktree 为 MVP stub
- [与 xx-coding 行为微差] → 对齐表文档化；关键路径写测试
- [无旧会话迁移] → 明确 Non-Goal，用户新建 session

## Migration Plan

1. 安装 `uv sync` / `pip install -e .`
2. 配置 `.env`（`OPENAI_API_KEY` 等）
3. 运行 `xcode doctor` → `xcode`
4. 回滚：继续使用参考目录中的 `xx-coding`（未改动）

## Open Questions

- 无阻塞项；Team/Worktree 深度可在后续 change 增强
