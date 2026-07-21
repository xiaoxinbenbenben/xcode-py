## 1. Project scaffolding

- [x] 1.1 创建 `pyproject.toml`、`.gitignore`、`README.md`，配置包名 `xcode` 与入口脚本
- [x] 1.2 搭建 `src/xcode/` 包骨架（`__init__`、`__main__`、空模块目录）

## 2. Config and session runtime

- [x] 2.1 实现 `config.py`：加载 `.env` / 环境变量（API key、base URL、model）
- [x] 2.2 实现 `runtime/session.py`：新建/恢复/列出会话与 workspace 绑定
- [x] 2.3 实现 `runtime/events.py` 与可选 `runtime/tracing.py`

## 3. Tools and permissions

- [x] 3.1 实现工具基类、统一响应协议与 registry
- [x] 3.2 实现内置工具：LS、Glob、Grep、Read、Edit、Write、Bash、TodoWrite、Compact
- [x] 3.3 实现权限引擎 MVP 与 hooks 注册表 MVP
- [x] 3.4 实现 skills 加载与 Skill 工具；任务系统 MVP（TaskCreate/List/Get/Update/Run）

## 4. Context and agent loop

- [x] 4.1 实现 context builder、@file mentions、memory、compaction
- [x] 4.2 实现 `runtime/agent.py` 流式 agent loop（openai chat completions + tools）

## 5. Single entry CLI/TUI

- [x] 5.1 实现 Typer 单入口：无参进 TUI，`-p` 单次运行，子命令 `doctor`/`session`
- [x] 5.2 实现 `entrypoints/tui.py`（prompt-toolkit + rich 渲染 events）

## 6. Tests, docs, report

- [x] 6.1 编写并跑通基础单元测试（session、tools、entry 路由、mentions）
- [x] 6.2 用 frontend-design 编写 `docs/report/index.html` 汇报页
- [x] 6.3 分阶段 git commit（scaffolding / core / entry / report）
