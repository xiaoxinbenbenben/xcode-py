# Handoff：xcode-py（接手前必读）

更新：2026-07-26  
仓库：https://github.com/xiaoxinbenbenben/xcode-py  
标尺：**PaiCLI-Python/**（勿用 xx-coding 完成度当进度）

---

## 1. 目标与原则（已定）

- 从 0 做「像 PaiCLI」的 coding agent，包名 `xcode`
- **自研 ReAct loop**，不上 OpenAI Agents SDK
- 暂不对齐 xx 的 Team/Worktree/BackgroundRun；PaiCLI 有 `/plan`、`/team`，无 worktree
- 联网 / 索引 / `save_memory` / `load_skill` = **Tools 内容包**，不单开史诗
- 工具协议可以对内更结构化，但**工具面与产品行为**应对齐 PaiCLI
- 路线按 `docs/todo.md`：**L0–L6 共 24 点，按序号 1→24 执行**（已废除 P0–P7 写法）

---

## 2. 进度（接手前状态）

| 序号 | 项 | 状态 | 说明 |
|------|----|------|------|
| **1** | 安装 | ✅ 视为完成 | 只做包装好；**不做**分层配置与 doctor 方面；配置继续读 `.env` |
| **2** | 事件协议 | ✅ 已实现（工作区有未提交改动） | 扁平产品事件；见下节 |
| **3+** | LLM Provider 起 | ⬜ 未开始 | 下一步从这里继续 |

说明：`todo.md` 完成表里 2 打了 ✅；1 按约定验收即可勾，未强制改表。

**未提交的本地改动（交接时请先 `git status`）：**

- 事件协议：`runtime/events.py`、`runtime/agent.py`、`entrypoints/tui.py`、`tests/test_events.py`
- 可读性/注释：`compaction.py`、`session.py`、`permissions/engine.py`、`tools/builtins.py`
- 规则：`.cursor/rules/code-comment-flow-simplicity.mdc`（新/改）
- 文档：`docs/todo.md`、`README.md`（README 早前已去掉 xx 对比/测试/汇报页）

旧 OpenSpec（`p0-config-and-events` / `p1-react-workspace-loop`）与 `docs/paicli-p0*` / `p1*` **已删**，勿再捡回。

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
- 测试：`tests/test_events.py`（假 LLM，可注入 `client=`）

关键实现文件：

- `src/xcode/runtime/events.py` — `make_event` / `map_finish_reason`
- `src/xcode/runtime/agent.py` — ReAct + 收流分流 + `_iter_tool_executions`
- `src/xcode/entrypoints/tui.py` — 消费扁平事件

---

## 4. 工程约定（接手前必守）

规则文件：`.cursor/rules/code-comment-flow-simplicity.mdc`（alwaysApply）

要点：

1. 注释写「做什么 / 输入输出」，禁套话与逐行旁白  
2. **长函数（约 >40 行或多阶段）必须有阶段注释**（`# --- 1) … ---`）  
3. 架构简洁；完成一小批后要在回复里串调用链  
4. 改完要能讲清谁调谁，不要只丢文件列表  

近期教训：只写 docstring、百行主流程零阶段注释 = 不合格（已在 `agent.py` 整改）。

---

## 5. 现状能力快照（别误判进度）

已有但**不能**当成「序号已完成」：

- ReAct 循环、工具（仍是 `Read`/`Write`/`Bash` 等 PascalCase）、权限/Hooks/压缩/会话/TUI — 多是 xx 血统骨架  
- LLM 仍直接在 `agent.py` 里用 `AsyncOpenAI`，**尚未**独立 `llm/` Provider（序号 3）  
- 工具面**尚未**对齐 PaiCLI（`read_file` 等）— 属序号 7  

配置：继续 `load_config` + `.env`（`OPENAI_*` / `XCODE_HOME` 等），不要擅自做用户/项目 JSON 分层，除非产品改口。

---

## 6. 建议下一步（序号 3）

**LLM Provider**

- 抽出 `src/xcode/llm/`（接口 + OpenAI-compatible 流式）  
- 按配置创建客户端；缺 key 早失败  
- `run_agent` 只依赖客户端抽象（可继续注入假 client 测）  
- 多 provider key / 能力探测按 `todo.md` 第 3 条做，别膨胀进 Plan/MCP  

做完 3 再按序：4 ReAct 收口 → 5 Prompt → …；**7 工具面改名**别提前搅进 3。

---

## 7. 怎么跑 / 怎么验

```bash
uv sync --extra dev
uv run xcode --version
uv run python -m xcode --version
uv run pytest -q

# 实跑（需有效 .env）
uv run xcode --new-session --workspace . -p "你的问题"
uv run xcode   # 交互 TUI
```

参考：同目录下已有 `PaiCLI-Python/`、`xx-coding/`（对照用，不是进度标尺）。

---

## 8. 给下一位的检查清单

- [ ] `git status`：提交或暂存本次事件协议 + 注释规则改动（若尚未提交）  
- [ ] 读完 `docs/todo.md` 执行顺序与完成定义  
- [ ] 遵守 `.cursor/rules/code-comment-flow-simplicity.mdc`  
- [ ] 从 **序号 3 LLM Provider** 开工；不要复活已删的 P0/P1 OpenSpec  
- [ ] 标尺始终是 PaiCLI，不是 xx-coding  

---

## 9. 一句话现状

**安装约定已收窄完成；事件协议已扁平定稿并接上 TUI/`-p`；注释规则已加严。下一棒：抽出 LLM Provider（序号 3）。**
