## ADDED Requirements

### Requirement: Branded interactive banner

无参启动 TUI 时 MUST 展示品牌 banner（名称/版本/model/workspace/session）与可用 slash 提示。

#### Scenario: TUI starts with banner

- **WHEN** 用户运行 `xcode` 进入交互
- **THEN** 首屏包含 xcode 品牌与 session 信息

### Requirement: Slash commands with completion

TUI MUST 支持至少 `/help` `/exit` `/sessions` `/clear` `/tools` `/status`，并对 slash 提供补全。

#### Scenario: Help lists commands

- **WHEN** 用户输入 `/help`
- **THEN** 列出可用 slash 命令

### Requirement: Structured tool rendering

流式文本与工具调用 MUST 以可区分样式渲染（工具名与结果摘要可见）。

#### Scenario: Tool call visible in stream

- **WHEN** agent 发出 tool_call 事件
- **THEN** 终端显示工具名与参数摘要
