# Handoff：xcode-py（接手前必读）

更新：2026-08-04  
仓库：https://github.com/xiaoxinbenbenben/xcode-py  
标尺：**PaiCLI-Python/**（勿用 xx-coding 完成度当进度）

---

## 1. 目标与原则（已定）

- 从 0 做「像 PaiCLI」的 coding agent，包名 `xcode`，纯 Python 单入口
- **自研 ReAct loop**，不上 OpenAI Agents SDK
- 暂不对齐 xx 的 Team/Worktree/BackgroundRun；PaiCLI 有 `/plan`、`/team`，无 worktree
- 联网 / 索引 / `save_memory` / `load_skill` = **Tools 内容包**，不单开史诗
- 路线按 `docs/todo.md`：**L0–L6 共 24 点，按序号 1→24 执行**（已废除 P0–P7 写法；实际执行中 4/5/10/20 已跳序落地，7 的改名已提前完成）
- 工具面已对齐 PaiCLI snake_case（`read_file` / `write_file` / `bash` / `web_search` …），改名**已完成**，不再属序号 7 的前置

---

## 2. 进度（接手前状态）

| 序号 | 项 | 状态 | 说明 |
|------|----|------|------|
| **1** | 安装 | ✅ | `xcode` / `python -m xcode` 可启动（0.2.0）；配置继续读 `.env`，不做分层配置与 doctor 方面 |
| **2** | 事件协议 | ✅（已提交） | 扁平产品事件，fd050d9 起稳定；见第 3 节 |
| **3** | LLM Provider | ⬜ 未开始 | 仍直接在 `agent.py` 用 `AsyncOpenAI`，未抽 `llm/` |
| **4** | ReAct Loop | 🟢 已实现、未勾表 | `agent.py` 全新实现：24 轮上限、收流分流、usage/thinking、stop 映射 |
| **5** | Prompt 组装 | ✅ | `context/builder.py`：身份/cwd/时间/工具/准则 + 项目说明（`XCODE.md` + `XCODE.local.md`） |
| **6** | 上下文治理 | ⬜ | `compaction.py` / `memory.py` / `mentions.py` 已删，待回填 |
| **7** | 工具体系 | 🟡 大部完成 | Registry/执行器/结果模型就绪；**9/13** 工具已实现（工作区 6 + bash + 联网 2）；缺 `save_memory` / `load_skill` / `search_code` / `revert_turn` |
| **8** | 安全策略 | ⬜（雏形） | 仅 `bash` 极简 deny-list + `resolve_workspace_path` 越界拦截；HITL/审计未做 |
| **9** | Hooks | ⬜ | `hooks/` 已删待回填 |
| **10** | 会话/历史 | 🟢 已实现、未勾表 | `session.py`：新建/恢复/列表/续会、cwd 绑定、`/clear` `/sessions` |
| **11–19** | 记忆/扩展/编排 | ⬜ | 相关模块已删，按序回填（SQLite 记忆→11、Skills→13、MCP→14 …） |
| **20** | CLI/REPL | 🟢 已实现、未勾表 | 单入口：无参进 TUI、`-p`、`--json-events`、slash、`session list/new`、`doctor` |
| **21–24** | SDK/Runtime/可观测 | ⬜ | `web.py` 是**联网内容包**，不是 `serve`（22） |

说明：`todo.md` 完成表只勾了 **1 / 2 / 5**；4 / 10 / 20 的实现已落地但未勾表，按完成定义验收后即可勾。6+ 的旧 xx 血统模块已从树中移除，回填时按 todo 挂靠，**不要从 git 历史捡回**。

**工作区有大量未提交改动（接手时请先 `git status`）：**

- 整体收拢重建：32 文件，+646 / −2104（删 hooks/permissions/skills/tasks/tracing/compaction/memory/mentions/output + 全部旧测试；重写 agent/session/builder/builtins/cli/tui）
- 新增：`src/xcode/web.py`（联网内容包）、`XCODE.md`（项目记忆）、`.claude/`（OpenSpec 工作流 skills/commands）、`.idea/`
- `pyproject.toml` 加 `httpx` 依赖（web.py 用）；`.gitignore` 加 `XCODE.local.md`
- 参考仓库 `PaiCLI-Python/`、`xx-coding/` 已 gitignore（`# Reference clones — do not publish`）

---

## 3. 事件协议（序号 2）定稿

对外**扁平** dict，无 `payload` 包装。产品流类型：

| type | 字段 |
|------|------|
| `text_delta` | `text` |
| `thinking_delta` | `thinking`（有才发） |
| `usage` | `usage.{input_tokens,output_tokens}`（有才发） |
| `turn_complete` | `turn`, `stop_reason` |
| `tool_call` | `name`, `input` |
| `tool_result` | `name`, `result`, `is_error` |
| `error` | `error` |
| `done` | `total_turns`, `total_tokens` |

- 流式 tool 分片只在内部拼，**不对外发** `tool_call_delta`
- 废弃产品流：`run_started` / `run_finished` / `compacted`（压缩可写 trace）
- 主路径：`run_agent` → yield 事件 → `tui._render_event` / `-p` / `--json-events`
- 测试模式：`run_agent` 可注入 `client=`（假 LLM）

关键实现文件：

- `src/xcode/runtime/events.py` — `make_event` / `map_finish_reason`
- `src/xcode/runtime/agent.py` — ReAct + 收流分流 + `_iter_tool_executions`
- `src/xcode/entrypoints/tui.py` — 消费扁平事件

---

## 4. 工程约定（接手前必守）

规则文件：`.cursor/rules/code-comment-flow-simplicity.mdc`（已提交，alwaysApply）

要点：

1. 注释写「做什么 / 输入输出」，禁套话与逐行旁白  
2. **长函数（约 >40 行或多阶段）必须有阶段注释**（`# --- 1) … ---`）  
3. 架构简洁；完成一小批后要在回复里串调用链  
4. 改完要能讲清谁调谁，不要只丢文件列表  

近期教训：只写 docstring、百行主流程零阶段注释 = 不合格（已在 `agent.py` 整改）。

---

## 5. 现状能力快照（别误判进度）

运行时链路已通：`cli.py`/`tui.py` → `run_agent`（ReAct）→ `build_context_bundle` + `builtin_tools`（registry）→ 扁平事件落回渲染；会话落盘 `~/.xcode/projects/<key>/sessions/`。

已有但**不能**当成「序号已完成」：

- **3**：LLM 仍直接在 `agent.py:131` 用 `AsyncOpenAI`，未独立 `llm/` Provider，`light_model` 配置尚无用武之地  
- **6**：压缩 / mention / @file 注入未回填；`builder.py` 的 `history` 字段当前未进 prompt  
- **8**：只有 `bash` deny-list 雏形与路径越界拦截，无 HITL / 审计落盘  
- **21–24**：SDK / `serve` / 可观测 / 中断取消均未开始；`web.py` 的 SSRF 防护（只放行公网 http(s)，放行 Clash fake-ip 段）已就位

**测试现状（重要）：`tests/` 目录已整体删除，`uv run pytest -q` 目前是坏的** —— 会跑去收集被 gitignore 的 `PaiCLI-Python/tests/` 与 `xx-coding/tests/`，报 `ModuleNotFoundError: paicli`（34 个 collection error）。重建 `tests/`（假 LLM 注入 `client=` 的方式仍在）或先给 pytest 限定收集范围。

配置：继续 `load_config` + `.env`（`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENAI_LIGHT_MODEL` / `XCODE_HOME`），不要擅自做用户/项目 JSON 分层，除非产品改口。

---

## 6. 建议下一步

**0) 先提交当前重建**（整个收拢工作还在工作区），并把 `tests/` 修好或明确重建计划；否则后续改动没有基线。

**1) 序号 3：LLM Provider**

- 抽出 `src/xcode/llm/`（接口 + OpenAI-compatible 流式）  
- 按配置创建客户端；缺 key 早失败  
- `run_agent` 只依赖客户端抽象（保持可注入假 client 测）  
- 多 provider key / 能力探测按 `todo.md` 第 3 条做，别膨胀进 Plan/MCP

**2) 序号 6：上下文治理**（回填 `context/`）

- 窗口预算、历史裁剪/压缩、@file / mention 注入、侧车历史与 Loop 的交接；`builder.history` 目前没进 prompt，正好从这里接手

**3) 序号 7 补齐**：`save_memory`（入口，后端等 11）、`load_skill`（等 13）、`search_code`（可接轻量索引）、`revert_turn`（与 12 快照配套）

做完这些再按序：8 安全策略 → 9 Hooks → 11/12/13 → …；**3 与 6 别互相搅进**（3 只管发请求，6 只管塞多少）。

---

## 7. 怎么跑 / 怎么验

```bash
uv sync --extra dev
uv run xcode --version        # 0.2.0
uv run python -m xcode --version
uv run xcode doctor           # 环境自检
# ⚠️ pytest 暂不可用：tests/ 已删，会误收 PaiCLI-Python/、xx-coding/ 的测试

# 实跑（需有效 .env）
uv run xcode --new-session --workspace . -p "你的问题"
uv run xcode --new-session --workspace . -p "你的问题" --json-events
uv run xcode                  # 交互 TUI
uv run xcode session list     # 会话管理
```

参考：同目录下 `PaiCLI-Python/`、`xx-coding/` 为 gitignore 的对照克隆（不是进度标尺）。

---

## 8. 给下一位的检查清单

- [ ] `git status`：确认 / 提交整体收拢重建（32 文件未提交）；`XCODE.md`、`.claude/`、`.idea/` 是否入库按团队约定定  
- [ ] 修好 `pytest`：重建 `tests/`（假 LLM 注入 `client=`），或限定收集范围；`testpaths` 目前指向不存在的 `tests/`  
- [ ] 读完 `docs/todo.md` 执行顺序与完成定义；4/10/20 验收后按约定勾表  
- [ ] 遵守 `.cursor/rules/code-comment-flow-simplicity.mdc`  
- [ ] 从 **序号 3 LLM Provider** 开工（或先做 6 上下文治理回填）；**不要**从 git 历史捡回已删的 hooks/permissions/skills/tasks/tracing/compaction 模块  
- [ ] 标尺始终是 PaiCLI，不是 xx-coding  

---

## 9. 一句话现状

**底座已重建收口：事件协议定稿、ReAct/会话/单入口 CLI-TUI/9 个 snake_case 工具（含联网内容包）都已落地未提交；todo 勾选 1/2/5。下一棒：先提交并修好 pytest，再抽 LLM Provider（序号 3）。**
