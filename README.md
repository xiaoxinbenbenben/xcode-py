# xcode — pure-Python local coding agent

标准包 `xcode`，**单一入口**（CLI + TUI/REPL）。

## 安装

```bash
uv sync --extra dev
# 或
pip install -e ".[dev]"
```

准备 API 配置（变量名必须是 **`OPENAI_*`**，不要用业务项目里的 `API_KEY`）：

```env
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://your-compatible-endpoint/v1
OPENAI_MODEL=deepseek-v4-flash
OPENAI_LIGHT_MODEL=deepseek-v4-flash   # 长期记忆 / compact 摘要
```

推荐把上述内容放在 **`~/.xcode/.env`**（任意目录启动都能读到）。  
也可放在当前工作区 `.env`，或 `export` 到 shell。  
在别的业务仓库启动时，若该仓库 `.env` 没有 `OPENAI_*`，会继续从 `~/.xcode/.env` / 源码树 `.env` 补缺。

可选：`XCODE_CONTEXT_WINDOW` 等见 `.env.example`。自检：`uv run xcode doctor`。

## 运行

```bash
# 交互 TUI（默认）
uv run xcode

# 指定工作区 / 会话
uv run xcode --workspace /path/to/project
uv run xcode --session sess-xxxx    # 打开指定旧会话（完整 id / 前缀 / 后缀均可）
uv run xcode --new-session          # 与无参 TUI 一样：新开一场

# 单次调用后退出
uv run xcode -p "列出当前目录"

# 子命令
uv run xcode doctor
uv run xcode session list
uv run xcode session new --workspace /path/to/project
```

## TUI 命令

普通文本交给 agent；`/` 开头为本地命令。无参启动是**空会话**。`/resume` 切入旧场并回放对话（Markdown 渲染）。无 `/clear`：新对话用 `/new`。

`/resume`（及 `/sessions`）**无参打开选择器**：输入过滤标题/预览，`↑↓` 选择，Enter 切入，不必抄 session id。有参仍可用：`/resume 2`、`/resume 修 compact`、`/resume 650948cf`。

| 命令 | 作用 |
|---|---|
| `/help` | 列出命令 |
| `/exit` `/quit` | 退出（会尽量刷完记忆队列） |
| `/resume` `/sessions` | 选择器切入会话；有参按序号 / 标题 / id |
| `/new` | 新建并切入 |
| `/rename <标题>` | 给当前会话起名（选择器里更好找） |
| `/snapshot [名]` | 把本会话 `write_file`/`edit_file` 碰过的文件打成命名档；无名用时间戳 |
| `/restore` | 见下表（执行前会确认） |
| `/last` | 展开上一条工具输出 |
| `/compact` | 强制压缩当前对话（摘要 + 近端回合） |
| `/tools` | 列出内置工具 |
| `/status` | 模型、session、消息数、上下文占用、事件数 |
| `/memory` | 见下表 |
| `/skills` | 见下表 |

`/restore`（只撤**文件**，对话不动）：

| 用法 | 作用 |
|---|---|
| `/restore` | 列出上一轮、命名档、撤回前 |
| `/restore last` | 撤上一轮有改文件的回合（= `revert_turn`，slash 免批） |
| `/restore <名>` | 回到该命名档 |
| `/restore undo` | 回到上一次撤回之前 |

`/memory`（跨会话项目记忆，不是当前对话）：

| 用法 | 作用 |
|---|---|
| `/memory` `/memory summary` | 打印 `memory_summary.md`（注入 system 的短摘要） |
| `/memory path` | memories 目录路径 |
| `/memory show summary` | 全文 summary |
| `/memory show memory` | 全文 `MEMORY.md`（主题注册表） |
| `/memory grep <关键词>` | 在 MEMORY / rollout 里搜 |
| `/memory clear` | 清空本项目记忆并作废在途写入 |

`/skills`（说明书按需加载，不是把所有 SKILL.md 塞进 system）：

| 用法 | 作用 |
|---|---|
| `/skills` | 列出 builtin / 用户 / 项目 skill（含来源、开关） |
| `/skills on\|off <名>` | 启停（记在 `~/.xcode/skills.json`，不删文件夹） |
| `/skills <名> [args]` | 本轮强制加载正文并交给 agent |

模型也会看名单自己 `load_skill`。正文里的相对路径用 `read_file` / `bash`。skill 目录：包内 `builtin_skills/`、`~/.xcode/skills/<名>/`、项目 `.xcode/skills/<名>/`。

`/memory` ≠ `/memory show memory`。摘要靠后台 stage2 防抖写（约 ≥3 条信号或空闲 5 分钟）；summary 仍空但 MEMORY 已有内容时会提示并附带展示。规范写工作区 `XCODE.md`，不要手改 memories。

**显示：** thinking 与最终回答按流式 delta 直接打出。工具输出一行摘要，`/last` 看上一条全文。自动 compact 会打一行提示。

**按键：** Enter 发送 · Esc+Enter 换行 · 输入时 `@` 补全工作区路径 · Ctrl-C 取消本轮（空输入再按退出）。底栏：模型、工作区、轮次、`~used/256k`、session 尾缀。

## 数据

`~/.xcode`（`XCODE_HOME` 可改）。设计：`docs/plan.md`、`docs/session-history.md`。

```text
projects/<project_key>/
  sessions/<session_id>/
    meta.json  transcript.jsonl  context.json
    snapshots/          # /snapshot /restore
  memories/
    memory_summary.md   MEMORY.md   raw_memories.md   rollout_summaries/
skills/<name>/SKILL.md   # 用户级
skills.json              # /skills on|off
```