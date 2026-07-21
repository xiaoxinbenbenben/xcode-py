## Why

`xx-coding` 已具备本地 coding agent 的核心能力，但依赖 OpenAI Agents SDK、包结构松散（`src/` 非标准包名），且 CLI（Python）与 TUI（React/Ink）双入口割裂。需要用纯 Python、标准包 `xcode`、单一入口重写，降低复杂度并便于维护与测试。

## What Changes

- **新建** 标准 Python 包 `xcode/`（`src/xcode` 布局 + `pyproject.toml`），纯 Python 实现 xx-coding 核心功能
- **单一入口**：`xcode` / `python -m xcode`；无子命令进入交互 TUI/REPL，有子命令或 `-p` 走 CLI（参考 PaiCLI-Python，不照搬）
- **移除双入口**：不再维护独立 Node/Ink TUI；交互界面改为 Python（prompt-toolkit + rich）
- **功能对齐**：session、workspace、runtime events、工具集、上下文分层/压缩、skills、任务/子代理、权限与 hooks 等核心能力
- **依赖简化**：用 openai 兼容客户端 + 自研 agent loop，不依赖 `openai-agents` SDK
- **测试与文档**：基础单元测试 + 静态汇报页

## Capabilities

### New Capabilities

- `single-entry`: 单一 CLI 入口；无参进 TUI/REPL，子命令/`-p` 走非交互模式
- `agent-runtime`: 会话、workspace 绑定、流式 runtime events、配置与 tracing
- `tooling`: 只读/编辑/Bash/Todo/Compact/Task/Skill 等工具与统一响应协议
- `context-memory`: 分层上下文、@file 预处理、压缩、workspace 长期记忆

### Modified Capabilities

- （无：仓库尚无主 specs）

## Impact

- 新代码位于仓库根目录 `src/xcode/`、`tests/`、`pyproject.toml`；参考目录 `xx-coding/`、`PaiCLI-Python/` 只读，不修改
- 运行时依赖：`openai`、`typer`、`rich`、`prompt-toolkit`、`python-dotenv`、`tiktoken`（可选）
- 用户侧：**BREAKING** 相对 xx-coding：入口从 `scripts/cli.py` + `npm tui` 变为统一 `xcode`；会话目录默认 `~/.xcode/`
