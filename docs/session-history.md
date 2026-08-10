# 会话 / 历史方案（定稿 v2）

日期：2026-08-10  
状态：**已实现**（`src/xcode/runtime/session.py` / `agent.py` / `tokens.py`）  
目标：**可恢复（resume）** + **长会话不炸（prune + compact）**  
非目标：旧 `state.json` 迁移、`/clear`、fork/archive、SQLite 索引、多进程同 session 写锁、artifacts 外置大对象

---

## 1. 背景与选型

调研对照：Claude Code（项目 JSONL）、Codex（rollout + SQLite + compact 窗口）、OpenCode（MessageV2 + auto compact）、Aider（md 历史 + summarize）。

**采用**：Claude 式 append-only transcript + OpenCode/Aider 式 compact；**不抄** Codex 全量事件类型与永不修剪的巨型 rollout。

| 维度 | 选定 |
|---|---|
| 主目标 | 可恢复 + 长会话（实现顺序：先可靠 append，再 compact） |
| 权威存储 | `transcript.jsonl` |
| 送模缓存 | `context.json`（可校验、可重建） |
| 名片 | `meta.json`（现有语义） |
| 旧会话 | 不迁移；旧 `state.json` 可删 |
| 多写者 | 假定单进程单写；v1 不加文件锁 |

---

## 2. 磁盘布局

```text
{data_home}/projects/{project_key}/sessions/{session_id}/
  meta.json              # id / name / workspace / 时间戳
  transcript.jsonl       # 权威流水（有上限的产品记录，append-only）
  context.json           # 当前送模窗口缓存
{data_home}/projects/{project_key}/sessions/current_session.json
```

| 文件 | 职责 |
|---|---|
| `meta.json` | 列表、标题、workspace 绑定 |
| `transcript.jsonl` | 审计、resume 重建、完整（受上限）轨迹 |
| `context.json` | 下一轮 API 使用的 messages + 对账书签 |
| `current_session.json` | 项目默认会话指针 |

**与 MEMORY 正交**：长期记忆仍在 `memories/`；会话不承担跨 session 记忆。

---

## 3. 写入时序

| 时机 | 动作 |
|---|---|
| 每条 user / assistant / tool 消息 | **唯一入口** `session.append_message(...)`：分配 `event_id` → append JSONL + `flush` → 更新内存送模 messages（tool 已 prune） |
| 即将调用模型前 | 估 token；若超阈 → **compact** 再请求（见 §6） |
| 整轮结束（DONE / ERROR） | 写 `context.json`（临时文件 + rename）；更新 `meta`；transcript 可 `fsync` |

禁止在 `agent.py` 等处直接 `session.messages.append(...)`。

`context.json` / `meta.json`：写临时文件再 **rename** 保证原子替换。

---

## 4. Transcript 事件

每行一个 JSON 对象，会话内 **`event_id` 单调递增整数**。

### 4.1 message

```json
{
  "v": 1,
  "event_id": 10,
  "type": "message",
  "created_at": "2026-08-10T12:00:00Z",
  "role": "user|assistant|tool",
  "content": "...",
  "tool_calls": null,
  "tool_call_id": null,
  "truncated": false,
  "original_chars": null,
  "kept_chars": null
}
```

- tool 配对必须完整：`assistant.tool_calls` 与对应 `role=tool` + `tool_call_id` 不可丢消息，只可截断 content。
- 触顶截断时：`truncated=true`，写入 `original_chars` / `kept_chars`，**禁止静默截断**（不写内容哈希）。
- 定性：transcript 是 **「有上限的权威产品记录」**，不是原始字节冷归档。v1 可有硬顶（如单事件 ~2MB）；完整审计日后可加 `artifacts/`。

### 4.2 compact（自包含检查点）

```json
{
  "v": 1,
  "event_id": 124,
  "type": "compact",
  "created_at": "...",
  "source_through_event_id": 123,
  "summary": "...",
  "retained_messages": [],
  "estimated_tokens": 18000
}
```

- `summary`：handoff 摘要文本。  
- `retained_messages`：检查点时刻的 **送模用** 近端消息（已 prune），不是全文 tool  dump。  
- 回放不必重放 `source_through_event_id` 之前的历史。

---

## 5. context.json

```json
{
  "schema_version": 1,
  "session_id": "sess-...",
  "transcript_event_count": 123,
  "transcript_last_event_id": 123,
  "transcript_byte_offset": 456789,
  "messages": [],
  "estimated_tokens": 42000
}
```

| 字段 | 含义 |
|---|---|
| `transcript_byte_offset` | 生成该 context 时 transcript 文件字节长度（书签） |
| `transcript_last_event_id` / `event_count` | 与 JSONL 对齐的轻量元数据 |
| `messages` | 当前送模窗口（含 compact 后的形态） |
| `estimated_tokens` | 本地 tokenizer 估算 |

**不做** `last_event_sha256`（单写场景收益有限）。

### Resume 算法

1. 读 `context.json`（若无 → 从 transcript 全量按 compact 规则重建）。  
2. 若 `transcript` 大小 **等于** `byte_offset`，且末条 `event_id` 与 count 一致 → **直接使用** `messages`。  
3. 若文件 **大于** offset → 只解析 offset 之后的新行，增量合并进 messages，刷新 context。  
4. 若文件 **小于** offset、id/count 对不上、或元数据损坏 → 从 **最后一个合法 compact** 重建（见下）。  
5. JSONL **最后一行不是完整 JSON** → 丢弃该半行，再按 2–4 处理。

### 从 compact 重建

1. 找最后一个合法 `type=compact` 事件。  
2. `messages = [summary 消息] + retained_messages`。  
3. 应用所有 `event_id > source_through_event_id` 的 message 事件。  
4. 对 tool content **重新应用 prune**（规则升级时仍安全）。  
5. 写回 `context.json`（更新 offset / count / id / estimated_tokens）。

无任何 compact 时：从文件头回放全部 message（新会话或尚未压过）。

---

## 6. Prune 与 Compact

### 6.1 Tool prune（写入口即时）

- **JSONL**：尽量保留全文；触硬顶则截断并打 `truncated` 元数据。  
- **内存 messages / context / MEMORY 输入**：送模侧截断，默认 **> 16_000 字符**。  
- 固定壳，保持协议完整：

```text
<tool_output_truncated
 original_chars="83420"
 kept_chars="16000">
...保留前缀...
</tool_output_truncated>
```

### 6.2 近端保留：user turn group

一组 =

```text
user
→ (assistant 含 tool_calls + tool)*
→ 最终 assistant（若有）
```

Compact 时保留最近 **6 组**（不是最近 6 条 message）。

### 6.3 Compact 触发

**检查点（两处，缺一不可）：**

1. 新用户输入、**第一次**送模前  
2. 每个 tool 执行完、**再次**调用模型前  

**预算（输入侧估算 + 输出预留）：**

```text
system
+ XCODE / 边界说明等
+ memory_summary
+ tool schemas
+ conversation（含 compact summary + messages）
+ 本轮已纳入、即将送出的内容
+ reserved_output_tokens    # 可配，默认如 8192
```

**触发条件：**

```text
estimated_input_tokens + reserved_output_tokens
    >= context_window * compact_threshold
```

| 参数 | 默认 |
|---|---|
| `context_window` | **256_000** |
| `compact_threshold` | **0.7** |
| compact 模型 | **`light_model`** |
| 手动 | TUI **`/compact`**，与 auto 同一套逻辑 |

Compact 成功后：append `type=compact` 事件；用新窗口替换内存 messages；轮末照常写 context。

---

## 7. Token 估算与展示

- 使用 **tiktoken**（未知模型 fallback 如 `o200k_base` / `cl100k_base`）。  
- 与 compact 触发共用同一套估算。  
- TUI 展示当前送模窗口占用：`~used / 256k`（非 OpenAI 后端允许近似，用 `~`）。  
- API `usage` 若有，可作旁注对照，**不**作为唯一触发源（需在发送前可知）。

---

## 8. 产品面 v1

| 做 | 不做 |
|---|---|
| 现有 `session list` / `new` / `--session` / current 指针 | `/clear`（请用 `--new-session`） |
| auto compact + `/compact` | fork / archive / rename 产品化 |
| resume 校验与重建 | 旧 `state.json` 迁移 |
| tiktoken 占用展示 | 多进程同 session 锁 |
| 唯一 `append_message` | SQLite session 索引 |

---

## 9. 与 MEMORY 的边界

| | 会话历史 | 长期记忆 |
|---|---|---|
| 路径 | `sessions/...` | `memories/...` |
| 生命周期 | 单 thread | 跨会话 |
| 输入 | — | stage1 吃 **截断后的** 本轮 messages |
| 注入 | 全量/压缩后的对话 | 仅 `memory_summary` + 工具按需读 |

---

## 10. 实现要点（落地时）

1. `SessionRuntime`：`append_message`、`write_context`、`load`（校验 / 增量 / compact 重建）、`compact()`。  
2. `run_agent`：所有消息走 append；**每次** `chat.completions` 前 `ensure_context_budget(...)`。  
3. `build_context_bundle` 的固定前缀（system / XCODE / memory / tools）纳入 token 预算。  
4. 配置：`context_window`、`compact_threshold`、`reserved_output_tokens`、tool prune 字符阈、JSONL 硬顶。  
5. 测试：append 顺序与 id；半行 JSONL；offset 增量；compact 后回放与 retained 一致；tool 配对不丢；超阈 mid-turn compact。  
6. 删除或忽略仓库/本机测试用旧 `state.json` 会话数据即可。

---

## 11. 决策摘要表

见 [plan.md](./plan.md)「会话 / 历史」一节。
