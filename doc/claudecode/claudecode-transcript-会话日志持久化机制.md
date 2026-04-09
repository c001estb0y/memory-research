# Claude Code Transcript — 会话日志持久化机制

基于 `@anthropic-ai/claude-code@2.1.87` 编译版验证 + 本地 `.claude-internal` 目录实际数据分析。

---

## 一、Transcript 是什么

Transcript 是 Claude Code 的**会话完整日志持久化系统**。它将每次会话的所有交互——用户消息、模型回复、工具调用、工具结果、系统事件——实时写入本地磁盘上的 `.jsonl` 文件。

**核心定位**：

| 对比维度 | Transcript | AutoCompact 摘要 | ExtractMemories |
|---------|-----------|-----------------|-----------------|
| 存什么 | 会话的完整原始记录 | 对话的 9 段式结构化摘要 | 用户偏好/反馈/项目上下文 |
| 格式 | JSONL（每行一条 JSON 消息） | 纯文本（注入到对话中） | Markdown + YAML frontmatter |
| 生存期 | 磁盘持久化，默认保留 30 天 | 仅当前会话内存 | 永久（跨会话） |
| 写入时机 | 每条消息实时追加 | 上下文压缩时一次性生成 | 每轮查询结束时后台提取 |
| 用途 | 压缩后回溯细节、会话恢复、调试 | 替换被压缩的原始消息 | 积累可检索的长期记忆 |

**一句话总结**：Transcript 是 Claude Code 的「黑匣子」——记录一切，但平时不主动使用，只在需要时回读。

---

## 二、存储位置与目录结构

### 2.1 根目录

```
~/.claude-internal/
├── projects/                          # 按项目组织的会话数据
│   └── <sanitized-project-path>/      # 项目路径（路径分隔符替换为 -）
│       ├── <session-uuid>.jsonl       # ★ 主 Transcript 文件
│       └── <session-uuid>/            # 会话关联数据目录
│           ├── subagents/             # 子代理的独立 transcript
│           │   ├── agent-a021253546cb60f41.jsonl
│           │   ├── agent-a021253546cb60f41.meta.json
│           │   ├── agent-acompact-*.jsonl     # 压缩子代理
│           │   └── agent-aside_question-*.jsonl # 侧问子代理
│           └── tool-results/          # 持久化的工具输出
│               ├── bellqvr78.txt
│               └── ...
├── file-history/                      # 文件变更历史快照
│   └── <session-uuid>/
├── image-cache/                       # 会话图片缓存
│   └── <session-uuid>/
├── tasks/                             # 任务状态
│   └── <session-uuid>/
├── sessions/                          # 活跃会话元数据
├── backups/                           # 配置备份
├── history.jsonl                      # 命令历史
├── settings.json                      # 用户设置
└── config.json                        # 认证配置
```

### 2.2 项目路径映射

项目路径通过替换路径分隔符（`\`、`/`、`:`）为 `-` 生成目录名：

| 原始项目路径 | 映射后的目录名 |
|-------------|---------------|
| `E:\UGit\LetsGoEditor\Editor\AIKnowledge` | `E--UGit-LetsGoEditor-Editor-AIKnowledge` |
| `D:\GitHub\deer-flow` | `D--GitHub-deer-flow` |
| `D:\GitHub\kaixing-ai-game-editor` | `D--GitHub-kaixing-ai-game-editor` |

### 2.3 文件命名

- **主 Transcript**：`<session-uuid>.jsonl`（如 `bee4efb6-31cb-4bca-9d14-f4774a031b56.jsonl`）
- **子代理 Transcript**：`agent-<hash>.jsonl`（如 `agent-a021253546cb60f41.jsonl`）
- **压缩子代理**：`agent-acompact-<hash>.jsonl`（Full Compaction fork agent 的记录）
- **子代理元数据**：`agent-<hash>.meta.json`

---

## 三、JSONL 数据格式

每个 `.jsonl` 文件中，每行是一个独立的 JSON 对象。根据 `type` 字段区分消息类型。

### 3.1 消息类型一览

| type | 说明 | 出现频率 |
|------|------|---------|
| `permission-mode` | 会话权限模式声明（首行） | 每会话 1 次 |
| `file-history-snapshot` | 文件变更快照 | 每轮用户输入时 |
| `user` | 用户消息（含 tool_result） | 高频 |
| `assistant` | 模型回复（含 tool_use、文本） | 高频 |
| `system` | 系统事件（turn_duration、microcompact_boundary 等） | 中频 |

### 3.2 公共字段

所有消息共享以下字段：

```json
{
  "type": "user|assistant|system|permission-mode|file-history-snapshot",
  "uuid": "07ceecb9-2eb5-4568-b3ad-e02172ddc489",
  "parentUuid": "fdc21ca8-e3c6-4893-b940-9597b8b80216",
  "timestamp": "2026-04-07T12:29:13.748Z",
  "sessionId": "bee4efb6-31cb-4bca-9d14-f4774a031b56",
  "version": "2.1.92",
  "gitBranch": "dev_farm",
  "slug": "tidy-percolating-turing",
  "userType": "external",
  "entrypoint": "cli",
  "cwd": "E:\\UGit\\LetsGoEditor\\Editor\\AIKnowledge",
  "isSidechain": false
}
```

| 字段 | 说明 |
|------|------|
| `uuid` | 本条消息的唯一 ID |
| `parentUuid` | 父消息 UUID（形成消息链/树） |
| `isSidechain` | 是否是侧链消息（如子代理的独立分支） |
| `sessionId` | 会话 UUID（与文件名一致） |
| `version` | Claude Code 版本号 |
| `gitBranch` | 当前 Git 分支 |
| `slug` | 会话的人类可读别名（如 `tidy-percolating-turing`） |
| `entrypoint` | 入口方式（`cli` / `ide`） |
| `cwd` | 当前工作目录 |

### 3.3 permission-mode（权限声明）

每个 transcript 的第一行，声明会话的权限模式：

```json
{
  "type": "permission-mode",
  "permissionMode": "bypassPermissions",
  "sessionId": "bee4efb6-..."
}
```

### 3.4 user 消息

包含两种内容：用户直接输入和工具执行结果。

**用户直接输入**：

```json
{
  "type": "user",
  "promptId": "58dd9c15-...",
  "message": {
    "role": "user",
    "content": "@LLMwiki编译方案.md 根据这个文档只帮我编译..."
  },
  "permissionMode": "bypassPermissions"
}
```

**工具执行结果**：

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": [{
      "tool_use_id": "tooluse_kKLE9N3ngwJLLHbMr7dcM0",
      "type": "tool_result",
      "content": "The file ... has been updated successfully."
    }]
  },
  "toolUseResult": {
    "filePath": "E:/UGit/.../wiki/log.md",
    "oldString": "...",
    "newString": "...",
    "originalFile": "...",
    "structuredPatch": [...],
    "userModified": false,
    "replaceAll": false
  },
  "sourceToolAssistantUUID": "ae9d0571-..."
}
```

`toolUseResult` 字段记录了工具执行的完整上下文，包括文件编辑的 diff 信息（`structuredPatch`）。

### 3.5 assistant 消息

模型的回复，包含文本和/或工具调用：

```json
{
  "type": "assistant",
  "message": {
    "id": "msg_1775571475735_1heklxibf6d",
    "type": "message",
    "role": "assistant",
    "model": "claude-4.6-opus",
    "content": [
      {"type": "text", "text": "好的，我来..."},
      {"type": "tool_use", "id": "tooluse_xxx", "name": "FileEdit", "input": {...}}
    ],
    "stop_reason": "end_turn",
    "usage": {
      "input_tokens": 176337,
      "cache_creation_input_tokens": 836,
      "cache_read_input_tokens": 175500,
      "output_tokens": 783,
      "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
      "service_tier": "standard"
    }
  }
}
```

**关键**：`usage` 字段记录了每次 API 调用的完整 token 使用详情，包括缓存命中率（`cache_read_input_tokens`）。

### 3.6 system 消息

系统事件，如回合耗时统计：

```json
{
  "type": "system",
  "subtype": "turn_duration",
  "durationMs": 2628362,
  "messageCount": 235,
  "isMeta": false
}
```

`subtype` 已知值包括：
- `turn_duration` — 回合耗时和消息数统计
- `microcompact_boundary` — Microcompact 执行边界标记
- `local_command` — 本地命令执行

### 3.7 file-history-snapshot

文件变更快照，记录每轮操作前的文件状态：

```json
{
  "type": "file-history-snapshot",
  "messageId": "07ceecb9-...",
  "snapshot": {
    "messageId": "07ceecb9-...",
    "trackedFileBackups": {},
    "timestamp": "2026-04-07T12:29:13.948Z"
  },
  "isSnapshotUpdate": false
}
```

---

## 四、消息链与树结构

Transcript 中的消息通过 `uuid` 和 `parentUuid` 形成一棵**消息树**，而非简单的线性列表：

```
permission-mode (root)
└── file-history-snapshot
    └── user: "帮我编译 Wiki"          ← 用户输入
        └── assistant: "好的，我来..."  ← 模型初始回复
            ├── assistant: tool_use(TaskCreate)  ← 工具调用
            │   ├── user: tool_result           ← 工具结果
            │   └── user: tool_result           ← 另一个工具结果
            ├── assistant: tool_use(TaskCreate)  ← 另一个工具调用
            └── ...
```

**`isSidechain: true`** 的消息表示分叉出的侧链（如子代理的独立对话线程），不在主对话线性流程中。

---

## 五、子代理 Transcript

每个子代理（sub-agent）有自己独立的 transcript 文件，存储在 `subagents/` 目录中。

### 5.1 实际案例

你的 Wiki 编译会话产生了 **28 个子代理** transcript，包括：

| 子代理类型 | 文件名模式 | 说明 |
|-----------|-----------|------|
| 普通子代理 | `agent-a021253546cb60f41.jsonl` | 并行执行的 Task agent |
| 压缩子代理 | `agent-acompact-54227223ad8a5c7e.jsonl` | Full Compaction 时 fork 的摘要生成 agent |
| 侧问子代理 | `agent-aside_question-abc658e5697df14e.jsonl` | 侧问（side query）agent |

每个子代理同时有一个 `.meta.json` 元数据文件。

### 5.2 跳过 Transcript 的情况

源码中部分内部操作设置 `skipTranscript: true`，不写入 transcript：
- ExtractMemories（记忆提取）的 forked agent
- Prompt suggestion（提示建议）生成
- Agent summary（代理摘要）生成

---

## 六、数据规模与留存策略

### 6.1 实际数据规模

以你的 Wiki 编译会话为例：

| 指标 | 值 |
|------|----|
| 主 Transcript 大小 | 3.05 MB |
| 主 Transcript 行数 | 563 行 |
| 子代理 Transcript 数量 | 28 个 |
| 工具结果持久化文件 | 5 个 .txt |
| 会话总时长 | ~2,628 秒（约 44 分钟） |
| 总消息数 | 235 条 |

### 6.2 留存策略

源码中定义了 transcript 的留存机制（v2.1.87 确认）：

- **默认留存**：30 天
- **配置方式**：`settings.json` 中的 `retentionPeriodDays` 字段
- **禁用持久化**：设置 `retentionPeriodDays: 0` 将完全禁用——不写入 transcript，且启动时删除已有文件
- **清理时机**：Claude Code 启动时检查并删除过期的 transcript

```
源码描述：
"Number of days to retain chat transcripts (default: 30).
 Setting to 0 disables session persistence entirely:
 no transcripts are written and existing transcripts
 are deleted at startup."
```

---

## 七、Transcript 在压缩中的角色

### 7.1 作为 Full Compaction 的安全网

当 AutoCompact 触发 Full Compaction 时，所有原始消息被 9 段式摘要替换。此时 transcript 成为唯一的完整记录来源。

压缩后的摘要消息末尾会注入 transcript 路径：

```
If you need specific details from before compaction (that are not in the 
summary above but were discussed or outputted), read the full transcript 
at: C:\Users\minusjiang\.claude-internal\projects\E--UGit-LetsGoEditor-
Editor-AIKnowledge\bee4efb6-31cb-4bca-9d14-f4774a031b56.jsonl
```

### 7.2 回读的实际效果

理论上模型可以用 `FileRead` 读取 transcript 恢复被压缩掉的细节。但实际效果有限：

| 挑战 | 说明 |
|------|------|
| 文件体积 | 3MB+ 的 JSONL，远超单次 FileRead 的 token 预算 |
| 编码问题 | JSONL 中的中文内容可能出现编码问题（如 PowerShell 读取时的乱码） |
| 定位困难 | 563 行中找到特定消息需要 Grep + 多次读取 |
| 模型不主动 | 摘要看起来「足够完整」时，模型不会意识到需要回读 |

### 7.3 Hooks 系统集成

Transcript 路径也传递给 Hooks 系统（如会话启动钩子）：

```json
{
  "session_id": "bee4efb6-...",
  "transcript_path": "~/.claude-internal/projects/.../bee4efb6-....jsonl",
  "cwd": "E:\\UGit\\...",
  "permission_mode": "bypassPermissions"
}
```

外部工具可以通过 hooks 获取 transcript 路径，实现自定义的会话分析或监控。

---

## 八、与其他存储机制的关系

```
┌─────────────────────────────────────────────────────────────┐
│                  Claude Code 数据持久化全景                  │
│                                                             │
│  会话级（当前会话）                                         │
│  ├─ Transcript (.jsonl)     ← 完整原始记录                  │
│  ├─ File History            ← 文件变更快照（可 undo）       │
│  ├─ Session Memory (.md)    ← 会话笔记（供轻量压缩使用）   │
│  ├─ Tasks                   ← 任务状态                      │
│  └─ Tool Results (.txt)     ← 持久化的工具输出              │
│                                                             │
│  项目级（跨会话）                                           │
│  ├─ Auto Memory (.md)       ← 自动提取的长期记忆            │
│  ├─ CLAUDE.md               ← 项目规则文件                  │
│  └─ Team Memory (.md)       ← 团队共享记忆（可选）          │
│                                                             │
│  全局级                                                     │
│  ├─ User CLAUDE.md          ← 个人全局规则                  │
│  ├─ history.jsonl           ← 命令历史                      │
│  └─ settings.json / config.json ← 配置                     │
└─────────────────────────────────────────────────────────────┘
```

---

## 九、相关源码文件索引

| 源码路径（TypeScript 原始） | 职责 | v2.1.87 状态 |
|---------------------------|------|-------------|
| `src/utils/transcript.ts` | Transcript 读写、路径计算、JSONL 序列化 | ✅ 确认存在 |
| `src/utils/session.ts` | 会话 ID 生成、会话文件管理 | ✅ |
| `src/services/compact/compact.ts` | 压缩后注入 transcript 路径到摘要 | ✅ |
| `src/hooks/` | Hooks 系统，传递 `transcript_path` 给外部工具 | ✅ |
| `src/services/extractMemories/extractMemories.ts` | 设置 `skipTranscript: true` 跳过记忆提取 agent 的 transcript | ✅ |

---

## 十、实用操作

### 10.1 查看会话列表

```powershell
Get-ChildItem "~/.claude-internal/projects/<project-dir>" -Filter "*.jsonl" |
  Select-Object Name, @{N='SizeMB';E={[Math]::Round($_.Length/1MB,2)}}, LastWriteTime
```

### 10.2 提取特定会话的所有用户消息

```bash
rg '"type":"user"' <transcript>.jsonl | rg '"content":"[^[{]' 
```

### 10.3 统计 token 消耗

```bash
rg '"output_tokens"' <transcript>.jsonl
```

### 10.4 查看子代理列表

```powershell
Get-ChildItem "~/.claude-internal/projects/<project-dir>/<session-id>/subagents" -Filter "*.jsonl" |
  Select-Object Name, @{N='SizeKB';E={[Math]::Round($_.Length/1KB,1)}}
```
