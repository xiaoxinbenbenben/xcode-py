# 定稿决策表（2026-08-10）

## 长期记忆

| 决策点 | 选定方案 | 说明 |
|---|---|---|
| scope | 仅项目级 | `{data_home}/projects/{project_key}/memories/` |
| 存储 | Markdown，无 SQLite/向量 | `memory_summary.md` / `MEMORY.md` / `raw_memories.md` / `rollout_summaries/` |
| 注入 | 仅 summary + 读指引 | 不塞 MEMORY 正文 |
| 按需读 | `memory_read` + `memory_grep` | summary 路由 + 文档 keywords |
| 写入 | stage1 + stage2 | 权威文件只经 consolidation 原子写 |
| stage1 | 每轮可写 raw/rollout | 空结果跳过 stage2 |
| stage2 触发 | 防抖 | ≥3 条新信号 **或** 空闲 ≥5 分钟；`drain`/退出必跑 |
| 模型 | `light_model` | extract 与 consolidate 同模型 |

## 会话 / 历史

全文见 [session-history.md](./session-history.md)。

| 决策点 | 选定方案 | 说明 |
|---|---|---|
| 主目标 | 可恢复 + 长会话不炸 | 先可靠 append，再 prune/compact |
| 布局 | `meta` + `transcript.jsonl` + `context.json` | 项目下 `sessions/{id}/`；`current_session` 指针保留 |
| 权威 | transcript append-only | 有上限的产品记录，非原始字节归档 |
| 送模缓存 | `context.json` | messages + offset/count/id；可丢、可重建 |
| 对账 | `byte_offset` + `last_event_id` + `event_count` | 半行 JSON 丢弃 |
| Resume | 等长且一致 → 直用；变长 → 尾部增量；否则从最后 compact 重建 | 不扫 compact 前全历史 |
| Compact 事件 | summary + `retained_messages` + `source_through_event_id` | 自包含检查点 |
| 写入入口 | 唯一 `append_message` | 禁各处直接 `messages.append` |
| 落盘 | JSONL append+flush；context/meta 临时文件 rename | 轮末可 fsync transcript |
| 多写者 | 假定单写 | v1 **不加** session 文件锁 |
| Prune | 送模/MEMORY 截断；JSONL 尽量全文 | 固定 `<tool_output_truncated>`；不破坏 tool 配对 |
| 近端保留 | 最近 6 个 **user turn group** | 非按消息条数切片 |
| Compact 时机 | 每次即将调模型前 | 用户轮初 + 每个 tool 后 |
| 预算 | system+XCODE+memory+tools+对话+reserve | `input + reserved_output >= window * threshold` |
| `context_window` | **256000** | 可配 |
| `compact_threshold` | **0.7** | 可配 |
| Compact 模型 | `light_model` | 可与主模型相同 |
| Token | tiktoken 估 + TUI `~used/256k` | 触发与展示共用 |
| 产品 v1 | list/new/resume + auto + `/compact` | **无** `/clear`（用 `--new-session`） |
| 与 MEMORY | 正交 | MEMORY 吃截断后的本轮 messages |

## 快照 / restore

拍板：2026-08-10。只撤文件、不撤对话；不靠用户 git。

### 决策表

| 决策点   | 选定方案                         | 说明                                                                           |
| ----- | ---------------------------- | ---------------------------------------------------------------------------- |
| 拍什么   | 仅 `write_file` / `edit_file` | bash / 人手 / 测试进程改盘不进快照                                                       |
| 何时拍   | 某文件**本轮第一次改之前**拷原文           | 没改文件不拍                                                                       |
| 一轮    | 一次用户输入 → agent `done`        | 与 `_submit_round` 同一边界                                                       |
| 撤什么   | **只撤文件**                     | 对话 / transcript 不动                                                           |
| 能撤多远  | **上一轮** + 命名档                | 无完整 prompt 时间线                                                               |
| 命名档范围 | 本会话被上述两工具碰过的文件的**此刻内容**      | `/snapshot`；未碰过的不管                                                           |
| 新建文件  | 记 `missing`                  | revert 时删除该路径                                                                |
| 冲突    | 直接覆盖                         | 人手或 bash 又改过也对不上就盖                                                           |
| 反悔    | 撤回前先存 `__pre_restore`（1 份）   | `/restore undo` 回到撤回前                                                        |
| 审批    | `revert_turn` 要 y/N          | `/restore` 是用户命令，不再问；`-p` 无回调则拒绝                                             |
| 存放    | 当前**会话**目录                   | `/resume` 仍在；`/new` 看不见；非项目级                                                 |
| bash  | 软限制                          | system：改文件走 write/edit；拦 `sed -i` / `perl -pi` / `ruby -i` 等抢活写法；测试/git/构建照常 |

### 磁盘

```
{data_home}/projects/{project_key}/sessions/{session_id}/snapshots/
  last_turn.json       # 上一轮：路径 → blob | missing
  session_files.json   # 本会话碰过的相对路径（有序）
  pre_restore.json     # 最近一次撤回前的「现在」
  named/<name>.json    # 命名档
  blobs/<sha256>       # 文件正文，按内容去重
```

一条快照记录：`path → { "sha256": "…" } | { "missing": true }`。  
正文只存在 `blobs/`；JSON 只挂引用。

### 调用链

```
write_file / edit_file
  → 若本轮尚未备份该路径：读盘（无文件则 missing）→ 写入进行中的 turn 档
  → 路径记入 session_files
  → 再真正改盘

用户回合结束（agent done）
  → 进行中的 turn 档封成 last_turn（覆盖上一份）
  → 新回合开始时清空「本轮已备份」集合

/snapshot [名]
  → 按 session_files 读此刻磁盘（缺则 missing；>5MB 跳过）
  → 写入 named/<名>.json
  → 命名档 >20 个则删最旧

/restore last        == revert_turn 的文件效果（slash 免批）
/restore <名>
/restore undo
  → 先把当前变更集（session_files 的此刻）存成 pre_restore
  → 按目标快照写回正文或删除 missing 路径
  → >5MB / 无 blob 的条目跳过并打印

revert_turn（工具）
  → requires_approval；通过后同 /restore last

bash
  → 命中抢活规则则拒绝，提示改用 write_file / edit_file
  → 未命中则照常执行；其改盘不进快照
```

## Skills

拍板：2026-08-13。标准 3 层渐进披露；L3 不另造加载器。

### 决策表

| 决策点 | 选定方案 | 说明 |
|---|---|---|
| 格式 | `agentskills.io` | `<name>/SKILL.md`；frontmatter 要 `name` + `description` |
| 披露 | **3 层** | L1 名单常驻；L2 激活后才读正文；L3 附属文件按需 |
| L3 | 现有 `read_file` / `bash` | 正文里的相对路径；文档用读、脚本用跑。无 `load_skill_resource` |
| 发现 | builtin < 用户 < 项目 | 包内 `builtin_skills/`、`~/.xcode/skills/`、`<ws>/.xcode/skills/`。不扫 `.claude/skills`、`.agents/skills`（要共用就 symlink） |
| 同名 | 后者覆盖前者 | 项目盖个人，个人盖内置 |
| 非法 | 跳过，不进 catalog | 缺 frontmatter / `name` 不合法 / `description` 空 |
| L1 | system 只塞 `name: description` | 默认 8000 字符预算；先截 description，再整条省略，末尾 `... and N more` |
| L2 | `load_skill(name, args?)` | 正文进 **tool result**，不写回 system |
| 用户入口 | `/skills` | 列表；`/skills on\|off <name>`；`/skills <name> [args]` 本轮强制加载。不给每个 skill 挂顶级 `/name`（避免撞 `/help` `/memory` `/restore`） |
| 隐式 | 模型看 catalog 自己 `load_skill` | 无向量、无关键词路由 |
| 启停 | 用户级 disabled 集合 | 落 `~/.xcode/skills.json`；关掉后不进 catalog、不能 load。文件夹还在，只是名单里没了 |
| 参数 | `$ARGUMENTS` | 正文有占位就替换；没有且 args 非空则末尾追加 `ARGUMENTS:` |
| 锚点 | 返回前加 `Base directory:` | skill 根目录，给 L3 相对路径当锚 |
| 驻留 | 无单独缓冲 | 正文就是对话里的一条 tool 结果。compact 只压对话窗口，不通知、不强制重载；还要用就靠 catalog 再 `load_skill`，和第一次一样 |
| 刷新 | 用到再看 mtime | 每轮组 catalog、`/skills`、`load_skill` 时比修改时间；变了重扫。不 watch、不要求用户先敲 `/skills` |

