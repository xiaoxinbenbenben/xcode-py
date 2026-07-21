## ADDED Requirements

### Requirement: Unified tool response envelope

工具执行 MUST 返回统一信封，包含 `status`（success | partial | error）、`text`、`data`、`stats`（至少含 `time_ms`）、`context`（至少含 `cwd` 与 `params_input`）；error 状态 MUST 含 `error.code` 与 `error.message`。

#### Scenario: Successful tool call serializes envelope

- **WHEN** 工具成功执行
- **THEN** 发给模型的 tool message JSON 含 status=success、stats.time_ms、context.cwd

### Requirement: Large output spill to disk

当工具文本输出超过配置阈值时，系统 MUST 将完整输出写入会话 `tool-output/`，并在返回文本中提供可 Read 的相对路径预览提示。

#### Scenario: Oversized bash output is truncated with path

- **WHEN** Bash 输出超过阈值
- **THEN** 返回 partial 或 success 带 truncated 预览，且完整文件存在于 tool-output
