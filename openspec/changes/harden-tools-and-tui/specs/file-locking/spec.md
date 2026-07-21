## ADDED Requirements

### Requirement: Read remembers file snapshot

成功的 Read MUST 记录目标文件的 `mtime_ms` 与 `size_bytes` 到会话级 snapshot 表（相对 workspace 路径为键）。

#### Scenario: Read stores snapshot

- **WHEN** Read 成功读取文件
- **THEN** ToolContext 中存在该相对路径的 snapshot

### Requirement: Edit/Write detect concurrent changes

若存在 snapshot 且磁盘文件的 mtime/size 与 snapshot 不一致，Edit/Write MUST 失败并返回 `error.code=FILE_CHANGED`，不得写入。

#### Scenario: Stale edit is rejected

- **WHEN** Read 后外部修改文件再 Edit
- **THEN** Edit 返回 error FILE_CHANGED 且文件内容未被该 Edit 改写

### Requirement: Write without prior snapshot is allowed

无 snapshot 时可 Write/Edit；成功后 MUST 更新 snapshot。

#### Scenario: Fresh write succeeds

- **WHEN** 对无 snapshot 的路径 Write
- **THEN** 写入成功并登记新 snapshot
