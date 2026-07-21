## ADDED Requirements

### Requirement: Built-in coding tools
系统 MUST 提供与 xx-coding 对齐的核心工具：LS、Glob、Grep、Read、Edit、Write、Bash、TodoWrite。

#### Scenario: Read tool returns file content
- **WHEN** agent 调用 Read 且路径在 workspace 内
- **THEN** 工具返回文件内容（可带行号）并使用统一响应结构

#### Scenario: Path outside workspace is rejected
- **WHEN** 工具路径解析后落在 workspace 之外
- **THEN** 工具返回错误且不读写该路径

### Requirement: Unified tool response protocol
所有工具 MUST 返回统一结构（至少包含 ok/error、摘要与可截断的正文），超长输出 MUST 截断。

#### Scenario: Long bash output truncated
- **WHEN** Bash 输出超过截断阈值
- **THEN** 返回截断后的内容并标明已截断

### Requirement: Skills loading
系统 MUST 能从项目 `skills/` 目录加载 skill 描述，并经由 Skill 工具按需注入。

#### Scenario: List available skills
- **WHEN** 存在有效 skill 目录
- **THEN** Skill 工具或 slash 命令可列出可用 skill 名称
