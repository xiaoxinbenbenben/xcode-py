## 1. Tool protocol

- [x] 1.1 实现 `ToolResponse` / `FileSnapshot` / 输出截断落盘辅助
- [x] 1.2 迁移 `Tool` / builtins / tasks / skills 到新协议
- [x] 1.3 更新 agent 消费与序列化；补协议测试

## 2. File locking

- [x] 2.1 ToolContext 持有 snapshots；Read 登记
- [x] 2.2 Edit/Write 冲突检测；补测试

## 3. Compaction

- [x] 3.1 双阈值 + 成对切分 + 结构化摘要
- [x] 3.2 补压缩测试

## 4. Bash / permissions / hooks

- [x] 4.1 Bash 校验与大输出落盘
- [x] 4.2 规则文件权限引擎 + hard deny
- [x] 4.3 默认 hooks；接入 agent

## 5. Terminal UX

- [x] 5.1 banner / prompt / slash / 补全 / 工具渲染
- [x] 5.2 CLI 入口微调与冒烟测试

## 6. Verify

- [x] 6.1 pytest 全绿；doctor / -p / TUI 冒烟
- [x] 6.2 分阶段 commit；标记 tasks 完成
