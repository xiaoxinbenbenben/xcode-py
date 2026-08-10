# xcode — pure-Python local coding agent

标准包 `xcode`，**单一入口**（CLI + TUI/REPL）。

## 安装

```bash
uv sync --extra dev
# 或
pip install -e ".[dev]"
```

准备 `.env`（可参考 `.env.example`）：

```env
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://your-compatible-endpoint/v1
OPENAI_MODEL=gpt-4o-mini
OPENAI_LIGHT_MODEL=gpt-4o-mini   # 长期记忆抽取/合并、会话 compact 摘要
```

可选会话窗口相关：`XCODE_CONTEXT_WINDOW`（默认 256000）、`XCODE_COMPACT_THRESHOLD` 等，见 `.env.example`。

## 运行

```bash
# 交互 TUI（默认）
uv run xcode

# 指定工作区 / 会话
uv run xcode --workspace /path/to/project
uv run xcode --session sess-xxxxxxxxxxxx
uv run xcode --new-session          # 新开一场对话（清空上下文请用这个）

# 单次调用后退出
uv run xcode -p "列出当前目录"

# 子命令
uv run xcode doctor
uv run xcode session list
uv run xcode session new --workspace /path/to/project
```

## TUI 命令

进入交互后，普通文本会交给 agent；以 `/` 开头的为本地命令：

| 命令 | 作用 |
|---|---|
| `/help` | 列出可用 slash 命令 |
| `/exit` / `/quit` | 退出 TUI（退出前会尽量刷完长期记忆队列） |
| `/sessions` | 列出当前项目下已保存会话；`●` 为当前会话 |
| `/compact` | **手动压缩**当前会话上下文（摘要 + 保留近端回合）；与自动 compact 同一套逻辑 |
| `/tools` | 列出当前可用内置工具名 |
| `/status` | 模型、session id、消息数、上下文占用估算、事件数 |
| `/memory` | 长期记忆（见下） |

### `/memory` 子用法

长期记忆是**跨会话**的项目级 Markdown，与当前对话 transcript 不是一回事。

| 用法 | 作用 |
|---|---|
| `/memory` 或 `/memory summary` | 打印 `memory_summary.md` |
| `/memory path` | 打印 memories 目录路径 |
| `/memory show summary` | 全文 summary |
| `/memory show memory` | 全文 `MEMORY.md` |
| `/memory grep <关键词>` | 在 MEMORY / rollout 里子串搜索 |
| `/memory clear` | 清空该项目记忆，并作废在途后台写入 |

说明：

- 项目规范请写工作区 **`XCODE.md` / `XCODE.local.md`**，不要手改 memories 当配置。
- 每轮结束后，有价值的对话会在后台做 stage1 抽取；合并进 MEMORY 有防抖（多条信号或空闲后）。
- **没有**会话级 `/clear`。要全新上下文请退出后用 `xcode --new-session`。

### 输入快捷键

| 按键 | 作用 |
|---|---|
| **Enter** | 发送 |
| **Esc+Enter** | 换行 |
| **Ctrl-C** | 退出 |

底栏会显示模型名、工作区、轮次、**上下文占用**（如 `~12.4k/256k`）、会话 id 尾缀。

## 数据落盘（简要）

默认根目录：`~/.xcode`（可用 `XCODE_HOME` 改）。

```text
projects/<project_key>/
  sessions/<session_id>/
    meta.json
    transcript.jsonl    # 会话权威流水
    context.json        # 当前送模窗口缓存
  memories/
    memory_summary.md   # 注入 system 的短摘要
    MEMORY.md
    raw_memories.md
    rollout_summaries/
```

设计说明：`docs/plan.md`、`docs/session-history.md`。
