## ADDED Requirements

### Requirement: Session and workspace binding
系统 MUST 支持可恢复会话，并将每个会话绑定到一个 workspace 根目录。

#### Scenario: Create session with workspace
- **WHEN** 用户新建会话并指定 `--workspace <path>`
- **THEN** 会话元数据记录该 workspace，后续工具在该根目录下解析路径

#### Scenario: Restore latest session
- **WHEN** 用户启动且未指定 `--new-session`
- **THEN** 系统恢复最近活跃会话（若存在）

#### Scenario: List sessions
- **WHEN** 用户执行 session 列表命令
- **THEN** 系统输出已保存会话的 id、名称、workspace 与最近活跃时间

### Requirement: Streaming runtime events
Agent 运行 MUST 产出结构化 runtime events，供 CLI/TUI 渲染。

#### Scenario: One-shot emits events
- **WHEN** 以 `-p` 运行且启用 JSON 事件模式
- **THEN** stdout 以 JSONL 形式输出至少包含 run_started 与 run_finished 的事件

### Requirement: Runtime configuration
系统 MUST 从环境变量与可选 `.env` 加载 API key、base URL、model 等配置。

#### Scenario: Doctor reports config
- **WHEN** 用户执行 `xcode doctor`
- **THEN** 输出是否配置 api_key、当前 model、cwd 等关键检查项
