### **L0 工程底座**

1. **安装** — 标准包装好，`xcode` / `python -m xcode` 可全局启动。配置维持现有读 `.env`，不做分层配置与 `doctor` 方面
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

## **执行顺序**

按方面合集序号推进，不另开 P0–P7 阶段：

```
1 安装
 → 2 事件协议
 → 3 LLM Provider
 → 4 ReAct Loop
 → 5 Prompt 组装
 → 6 上下文治理
 → 7 工具体系（先工作区+Bash；其余内容包按需挂）
 → 8 安全策略
 → 9 Hooks
 → 10 会话/历史
 → 11 长期记忆存储
 → 12 快照
 → 13 Skills
 → 14 MCP
 → 15 代码索引（可选）
 → 16 多模态
 → 17 Plan-and-Execute
 → 18 Multi-Agent / Team
 → 19 后台 Task
 → 20 CLI / REPL 收口
 → 21 SDK
 → 22 Runtime API
 → 23 可观测
 → 24 中断取消与评测回归
```

### **完成定义（按序号）**

| **序号** | **完成标准** |
| :----- | :----------------------------- |
| **1** | `uv sync` / `pip install -e .` 后可用 `xcode` 与 `python -m xcode` 启动（配置继续用 `.env`） |
| **2** | 事件类型与字段定稿，入口只认这一套 ✅ |
| **3** | OpenAI-compatible 流式可用；按配置创建客户端 |
| **4** | 纯 ReAct 多轮 tool，停止条件明确 |
| **5** | 系统提示含 cwd/工具/项目说明文件 |
| **6** | 长对话可控（预算/压缩/mention 生效） |
| **7** | Registry/执行器就绪；工作区+Bash 能读改跑；其它工具作内容包可挂 |
| **8** | 危险路径/命令可拦；HITL/审计可落盘 |
| **9** | turn/tool 前后（及错误）钩子可观察、可拦截 |
| **10** | 会话可续；cwd 绑定；可清空/展示上下文 |
| **11** | SQLite 长期记忆按 scope 可用 |
| **12** | turn 快照与 `/snapshot` `/restore` 可用 |
| **13** | 内置/用户/项目 Skills 启停与懒注入可用 |
| **14** | MCP Client（及可选 Server）与内置工具同一 Loop |
| **15** | （可选）索引可供 `search_code` |
| **16** | 图片输入可预处理，不支持时降级 |
| **17** | `/plan`（或等价）真跑通 |
| **18** | `/team`（Planner/Worker/Reviewer）真跑通 |
| **19** | 后台 Task 可投递与查询 |
| **20** | 单入口 CLI/REPL：`-p`、渲染、slash 收口 |
| **21** | SDK 与 CLI 同一能力面 |
| **22** | `serve`：threads / turns / events / tasks |
| **23** | 事件/审计可展示（如 `/audit`） |
| **24** | 可中断取消；有最小评测回归 |

### **灵活挂靠（不变）**

| **能力** | **挂法** |
| :----- | :----- |
| 联网 / 部分检索 | Tools 内容包，挂在 **7**；也可以在 **14/15** 前后补 |
| `save_memory` / `load_skill` | Tools 入口在 **7**；后端分别靠 **11**、**13** |
| 审计 JSONL | **8** 落盘；**23** 做展示 |
| Hooks 与追踪 | **9** 埋钩子；**23** 消费事件做可视化 |
| 上下文治理 vs Prompt | **5** 负责「组什么」；**6** 负责「塞多少、何时压缩」 |
| 最小 `-p` 冒烟 | 正式收口在 **20**；**4/7** 落地后可先薄挂入口做验收，不另开方面 |
