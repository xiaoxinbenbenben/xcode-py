## ADDED Requirements

### Requirement: Dual-threshold auto compact

系统 MUST 在用户轮次或估算消息体积超过阈值时触发自动压缩。

#### Scenario: Large history triggers compact

- **WHEN** 消息估算字符数超过阈值
- **THEN** `should_auto_compact` 为真

### Requirement: Preserve tool message pairing

压缩切分 recent 窗口时 MUST 不留下没有对应 assistant tool_calls 的孤立 `role=tool` 消息。

#### Scenario: Cut avoids orphan tool messages

- **WHEN** keep_last 边界落在 tool 消息中间
- **THEN** recent 从成对边界开始，不出现孤立 tool
