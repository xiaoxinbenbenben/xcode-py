## ADDED Requirements

### Requirement: Layered context assembly
系统 MUST 在每次运行前组装分层上下文（系统提示 / 项目与记忆 / 会话历史与本轮输入），再交给模型。

#### Scenario: Context includes workspace cues
- **WHEN** workspace 存在 `AGENTS.md` 或等价项目说明
- **THEN** 组装后的系统/项目层包含其摘要或内容

### Requirement: File mention preprocessing
用户输入中的 `@path` MUST 被预处理为文件提及，并在上下文中注入相关 reminder。

#### Scenario: At-file expands mention
- **WHEN** 用户输入包含 `@README.md` 且文件存在
- **THEN** 上下文标记该文件为 mentioned，并可注入摘要或路径提醒

### Requirement: History compaction
长会话 MUST 支持压缩（自动阈值触发或显式 Compact 工具），以控制历史长度。

#### Scenario: Compact summarizes older turns
- **WHEN** Compact 被调用或历史超过阈值
- **THEN** 旧轮次被摘要替换，会话仍可继续

### Requirement: Workspace-scoped long-term memory
系统 MUST 按 workspace 持久化长期记忆条目，并在后续会话组装时可读入。

#### Scenario: Memory persists across sessions
- **WHEN** 在某 workspace 写入记忆后新建同 workspace 会话
- **THEN** 新会话上下文可读取到该记忆
