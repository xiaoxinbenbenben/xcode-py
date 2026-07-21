## ADDED Requirements

### Requirement: Single CLI entrypoint
系统 MUST 通过统一入口 `xcode`（或 `python -m xcode`）提供所有交互方式，不得再提供独立的 Node/Ink TUI 启动脚本作为正式入口。

#### Scenario: No-args opens interactive mode
- **WHEN** 用户执行 `xcode` 且未指定子命令
- **THEN** 系统进入交互式 TUI/REPL

#### Scenario: Prompt flag runs once
- **WHEN** 用户执行 `xcode -p "列出文件"`
- **THEN** 系统以非交互方式运行一次 agent 并打印结果后退出

#### Scenario: Subcommand stays in CLI mode
- **WHEN** 用户执行 `xcode session list` 或 `xcode doctor`
- **THEN** 系统执行对应子命令且不进入交互 REPL
