### **L0 工程底座**

1. **配置与安装** — 默认 / 用户 / 项目 / env / CLI；`doctor`；可全局启动
2. **事件协议** — text / thinking / tool\_call / tool\_result / done / error / usage

### **L1 单智能体内核**

3. **LLM Provider** — OpenAI-compatible 流式、多 provider key、能力探测
4. **ReAct Loop** — 主循环、多轮 tool、停止条件
5. **Prompt 组装** — 系统提示、工具说明、项目记忆文件（`PAI.md` 等）
6. **上下文治理** — 窗口预算、历史裁剪/压缩、@file / mention 注入、侧车历史与 Loop 的交接
7. **工具体系** — Registry / 执行器 / 结果模型；内置工具为内容包（不另开方面）：
   * 工作区：读/写/列表/glob/grep/bash
   * 记忆：`save_memory`
   * 技能：`load_skill`
   * 联网：`web_search` / `web_fetch`
   * 代码检索：`search_code`（可接轻量索引）
   * 恢复：`revert_turn`
8. **安全策略** — PathGuard、CommandGuard、HITL、审计日志落盘
9. **Hooks** — turn/tool 前后（及错误）回调：可观察、可拦截、可打点；与事件协议、审计衔接

### **L2 状态与记忆**

10. **会话 / 历史** — 多轮消息、cwd 绑定、清空/展示上下文
11. **长期记忆存储** — SQLite + scope（工具只是写入入口）
12. **快照** — turn 前后快照、`/snapshot` `/restore`（与 `revert_turn` 配套）

### **L3 扩展**

13. **Skills** — 内置/用户/项目、启停、懒注入缓冲
14. **MCP** — Client + 自身 Server + `/mcp`
15. **代码索引（可选）** — 供 `search_code`；非向量 RAG 必选项
16. **多模态** — 图片预处理与降级

### **L4 编排**

17. **Plan-and-Execute**
18. **Multi-Agent / Team**
19. **后台 Task**

### **L5 产品壳与嵌入**

20. **CLI / REPL** — 单入口、`-p`、渲染、slash
21. **SDK**
22. **Runtime API** — `serve`：threads / turns / events / tasks

### **L6 质量**

23. **可观测** — 事件/审计展示（`/audit` → 可视化）
24. **中断取消与评测回归**

**附录（不进主合集）：** LSP、Git Worktree、向量 RAG。

***

## **执行顺序（P0–P7）**

P0  配置与安装 + 事件协议

&#x20;    ↓

P1  LLM → ReAct → 工具体系(工作区+Bash) → Prompt → 最小 REPL/\`-p\`

&#x20;    ↓

P2  安全策略 + Hooks（先打点/可拦工具）+ 会话历史

&#x20;    ↓

P3  上下文治理（预算、压缩、@file）

&#x20;    长期记忆 + save\_memory

&#x20;    快照 + revert\_turn

&#x20;    Skills + load\_skill

&#x20;    ↓

P4  MCP Client

&#x20;    （顺手）web\_\* 、search\_code(+可选索引)、多模态

&#x20;    MCP Server（可略后）

&#x20;    ↓

P5  Plan-and-Execute → Team → 后台 Task

&#x20;    ↓

P6  SDK → Runtime API → REPL 收口

&#x20;    ↓

P7  可观测展示 + 中断取消 + 评测

### **完成定义（含新两项）**

| **阶段** | **完成标准**                       |
| :----- | :----------------------------- |
| **P0** | 可安装、可配置、事件类型定稿                 |
| **P1** | 任意目录 `-p` 能读改跑（纯 ReAct）        |
| **P2** | 危险操作可拦；Hooks 能在 tool 前后触发；会话可续 |
| **P3** | 长对话可控（治理生效）；记忆/技能/快照可用         |
| **P4** | MCP 与内置工具同一 Loop；联网等按需挂上       |
| **P5** | `/plan`、`/team` 真跑通            |
| **P6** | SDK / HTTP / REPL 三条入口一致       |
| **P7** | 可审计展示、可中断、有最小回归                |

### **灵活挂靠（不变）**

| **能力**                       | **挂法**                                             |
| :--------------------------- | :------------------------------------------------- |
| 联网 / 部分检索                    | Tools 内容包，P4 前后加即可                                 |
| `save_memory` / `load_skill` | Tools 入口 + P3 后端                                   |
| 审计 JSONL                     | P2 安全策略落盘；P7 做展示                                   |
| Hooks 与追踪                    | P2 埋钩子；P7 消费事件做可视化                                 |
| 上下文治理 vs Prompt              | Prompt 负责「组什么」；治理负责「塞多少、何时压缩」——P1 先有 Prompt，P3 上治理 |

