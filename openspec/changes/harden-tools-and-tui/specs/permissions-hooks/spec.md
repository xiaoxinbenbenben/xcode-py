## ADDED Requirements

### Requirement: Settings-based permission rules

系统 MUST 从全局 `~/.xcode/settings.json` 与项目 `.xcode/settings.json` 加载 `permissions.rules`；规则字段含 tool_name、field、pattern、decision（allow|deny|ask）。

#### Scenario: Project deny rule blocks bash pattern

- **WHEN** 项目规则 deny Bash command_word=rm
- **THEN** 对应 Bash 调用被拒绝

### Requirement: Hard deny privileged commands

Bash 对特权命令词与 `rm -rf /` 类命令 MUST hard deny，不可被 ask 绕过。

#### Scenario: sudo is hard denied

- **WHEN** Bash command 含 sudo
- **THEN** 返回拒绝且不执行

### Requirement: Default lifecycle hooks

默认 hook 注册表 MUST 在 USER_PROMPT_SUBMIT / BEFORE_TOOL / AFTER_TOOL / RUN_FINISHED 记录到会话 trace（若 TRACE_ENABLED）。

#### Scenario: Trace hook fires on tool

- **WHEN** TRACE_ENABLED 且工具执行
- **THEN** 产生对应 hook 轨迹事件或 trace 行
