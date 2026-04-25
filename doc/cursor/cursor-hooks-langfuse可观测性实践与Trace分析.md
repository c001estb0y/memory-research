# Cursor Hooks Langfuse 可观测性实践与 Trace 分析

基于 `memory-research` 项目中 `.cursor/hooks/` 实现的深度分析，涵盖架构设计、子代理 Trace 追踪、与 Claude Code hooks 的对比，以及已知盲区。

---

## 一、系统总览

### 1.1 做了什么

在 Cursor IDE 的 Agent 工作流中，通过 hooks 机制拦截每一个关键生命周期事件，将其上报到 Langfuse 平台，实现：

- **全量行为追踪**：Prompt 提交、Agent 思考、文件读写、Shell 执行、MCP 调用
- **会话级聚合**：同一 `conversation_id` 的所有事件归入一条 Langfuse Trace
- **多 IDE 适配**：通过 Adapter 层同时支持 Cursor / CodeBuddy / Claude Code（预留）
- **效率评分**：Agent 结束时自动计算 `completion_status` 和 `efficiency` 分数

### 1.2 架构

```
Cursor IDE (每个 hook 事件)
  │
  ▼ stdin JSON
.cursor/hooks/hook-handler.js        ← 统一入口
  │
  ├── lib/adapters/cursor.js          ← Cursor 字段标准化
  ├── lib/adapters/codebuddy.js       ← CodeBuddy 字段标准化
  ├── lib/adapters/claude.js          ← Claude Code（预留）
  │       ↓
  │   normalize(rawInput) → 标准格式
  │
  ├── lib/langfuse-client.js          ← Langfuse SDK 封装
  │       ↓
  │   getOrCreateTrace(input) → Langfuse Trace
  │
  ├── lib/handlers.js                 ← 12 种事件处理器
  │       ↓
  │   routeHookHandler() → span/generation/event/score
  │
  └── lib/utils.js                    ← 工具函数
```

---

## 二、12 种 Hook 事件映射

Cursor 的 IDE 事件名经过 Adapter 层标准化为 canonical 名称：


| Cursor 事件名             | Canonical 名称       | Langfuse 类型   | 说明                           |
| ---------------------- | ------------------ | ------------- | ---------------------------- |
| `beforeSubmitPrompt`   | `before_prompt`    | generation    | 用户提交 Prompt                  |
| `afterAgentResponse`   | `after_response`   | generation    | Agent 回复文本                   |
| `afterAgentThought`    | `after_thought`    | span          | Agent 思维链（extended thinking） |
| `beforeShellExecution` | `before_shell`     | span          | Shell 命令执行前                  |
| `afterShellExecution`  | `after_shell`      | span          | Shell 命令执行后（含输出）             |
| `beforeMCPExecution`   | `before_mcp`       | span          | MCP 工具调用前                    |
| `afterMCPExecution`    | `after_mcp`        | span          | MCP 工具返回后                    |
| `beforeReadFile`       | `before_file_read` | span          | 文件读取前                        |
| `afterFileEdit`        | `after_file_edit`  | span          | 文件编辑后（含 diff 统计）             |
| `stop`                 | `stop`             | event + score | Agent 回合结束                   |
| `beforeTabFileRead`    | `before_tab_read`  | span          | Tab 补全读取文件                   |
| `afterTabFileEdit`     | `after_tab_edit`   | span          | Tab 补全编辑文件                   |


---

## 三、子代理 (Subagent) Trace 的行为特征

### 3.1 发现过程

在 Langfuse 中观测到一条异常 Trace：

- **名称**：`Cursor composer-2-fast`
- **Input/Output**：均为 `undefined`
- **Observations**：只有 10 个 `Read: xxx.py` span + 1 个 `Agent Thinking` span
- **无** `beforeSubmitPrompt`、`afterAgentResponse`、`stop` 事件

### 3.2 根因

该 Trace 来自 Cursor Agent 的 **Task 工具**派生的子代理（`subagent_type: "explore"`）：

```
父会话 eaa42113-... (2026-04-08 20:23 创建)
  │
  ├── 用户提问 → 分析数据 → 读文件 → ...
  │
  ├── [Line 52] 调用 Task 工具:
  │     description: "Study mempalace source code pipeline"
  │     subagent_type: "explore"
  │     readonly: true
  │         │
  │         ▼
  │     子代理 1021f39b-... (2026-04-09 23:45 执行)
  │         ├── Read: convo_miner.py
  │         ├── Read: miner.py
  │         ├── ... (共 10 个文件)
  │         ├── Agent Thinking
  │         └── Read: normalize.py
  │         → 结果返回父会话
  │
  └── 继续后续处理 ...
```

**证据链**：

- agent-transcripts 中 `eaa42113-*/subagents/1021f39b-*.jsonl` 确认了父子关系
- 子代理 transcript 第 1 行的 prompt 正是父会话 Task 工具的完整指令
- 父会话 transcript 第 52 行的 Task 调用生成了该子代理

### 3.3 子代理 Trace 的 3 个盲区


| 盲区            | 原因                                                      | 影响                   |
| ------------- | ------------------------------------------------------- | -------------------- |
| **Input 为空**  | 子代理的 prompt 由 Task 工具内部注入，不经过 `beforeSubmitPrompt` hook | Trace 没有 `input` 字段  |
| **Output 为空** | 子代理的结果直接返回给父 Agent，不经过 `afterAgentResponse` hook        | Trace 没有 `output` 字段 |
| **无 stop 事件** | 子代理的生命周期结束不触发 `stop` hook                               | 无完成状态和效率评分           |


只有**中间过程**的 hook（`beforeReadFile`、`afterAgentThought` 等）能正常触发。

### 3.4 父子 Trace 跨天问题

父会话可能跨天运行。本案例中：

- 父 Trace 时间戳：**04-08** 20:23
- 子 Trace 时间戳：**04-09** 23:45

在 Langfuse 按日期筛选时容易漏掉父 Trace。

---

## 四、Agent Thinking 的输出内容

### 4.1 常见误解

看到 `Agent Thinking` span 的输出是一段自然语言的规划文本，而非结构化的 `{ text, tool_calls }` JSON。

### 4.2 原因

Cursor 将一次 LLM 调用的完整响应**拆解**为多个独立的 hook 事件：

```
一次 LLM API 调用的完整响应
│
├── thinking 部分  →  afterAgentThought   ← 纯文本思维链
├── text 部分      →  afterAgentResponse   ← 回复文本
└── tool_calls     →  每个工具各自的 before/after hook
        ├── beforeReadFile
        ├── beforeShellExecution
        └── beforeMCPExecution
```

无法通过任何单一 hook 获取完整的结构化响应。`afterAgentThought` 捕获的就是 Claude 的 extended thinking 内容——模型在决定做什么之前的内部推理。

### 4.3 Metadata 中的有效信息

```json
{
  "generation_id": "8284124d-170b-4aba-ab79-d3dc0cc33e24",
  "duration_ms": 10077,
  "duration_formatted": "10.1s",
  "thinking_length": 1107
}
```

可用于分析：思考时长、思维链长度、每轮思考的效率。

---

## 五、与 Claude Code Hooks 的对比

### 5.1 设计哲学差异


| 维度              | Cursor Hooks                                             | Claude Code Hooks                                                          |
| --------------- | -------------------------------------------------------- | -------------------------------------------------------------------------- |
| **核心抽象**        | **模型输出生命周期**（prompt → thinking → response → tool → stop） | **工具执行生命周期**（PreToolUse → PostToolUse → Stop）                              |
| **Thinking 捕获** | `afterAgentThought` 直接拿到思维链文本                            | **无对应事件**（thinking 对 hooks 不可见）                                            |
| **Response 捕获** | `afterAgentResponse` 拿到回复文本                              | **无对应事件**（通过 PostSampling 内部 hook 处理）                                      |
| **文件读取**        | `beforeReadFile`（读取前拦截）                                  | 统一走 `PreToolUse` + `matcher: "Read"`                                       |
| **子代理事件**       | 子代理的中间 hook 能触发（但缺首尾）                                    | `SubagentStart` / `SubagentStop` / `TaskCreated` / `TaskCompleted`（完整生命周期） |


### 5.2 Claude Code 的 Hooks 是否也这样拆分？

**不是**。Claude Code 的设计完全不同：

1. **Claude Code 不暴露模型内部状态**：没有 `afterAgentThought`、`afterAgentResponse` 这样的 hook。模型的 thinking 和 text 输出对 hooks 系统完全不可见。
2. **Claude Code 只关注工具动作**：所有可观测性围绕 `PreToolUse` / `PostToolUse` 展开。你能看到"Agent 调用了 Write 工具写了什么文件"，但看不到"Agent 在想什么"或"Agent 回复了什么文字"。
3. **Claude Code 对子代理有完整事件**：`SubagentStart`、`SubagentStop`、`TaskCreated`、`TaskCompleted` 提供了子代理完整的生命周期观测。而 Cursor 的子代理只能通过中间的工具 hook 间接观测。
4. **Claude Code 的内部 hook 是隐藏的**：`PostSampling Hooks`（如 `extractSessionMemory`）在模型采样后执行，但不暴露给用户。这些内部 hook 处理了 Cursor 中由 `afterAgentThought` / `afterAgentResponse` 承担的职责。

### 5.3 事件覆盖对比表


| 能力     | Cursor Hook                                                      | Claude Code Hook                           |
| ------ | ---------------------------------------------------------------- | ------------------------------------------ |
| 用户输入   | `beforeSubmitPrompt`                                             | `UserPromptSubmit`                         |
| 模型思维链  | `afterAgentThought` ✅                                            | ❌ 无                                        |
| 模型回复文本 | `afterAgentResponse` ✅                                           | ❌ 无（内部 PostSampling 处理）                    |
| 工具调用前  | `beforeReadFile` / `beforeShellExecution` / `beforeMCPExecution` | `PreToolUse`（统一入口 + matcher）               |
| 工具调用后  | `afterFileEdit` / `afterShellExecution` / `afterMCPExecution`    | `PostToolUse`（统一入口）                        |
| 回合结束   | `stop`                                                           | `Stop`                                     |
| 子代理开始  | ❌（仅中间 hook 可见）                                                   | `SubagentStart` ✅                          |
| 子代理结束  | ❌                                                                | `SubagentStop` ✅                           |
| 异步任务   | ❌                                                                | `TaskCreated` / `TaskCompleted` ✅          |
| 上下文压缩  | ❌                                                                | `PreCompact` / `PostCompact` ✅             |
| 权限管理   | ❌                                                                | `PermissionRequest` / `PermissionDenied` ✅ |
| Tab 补全 | `beforeTabFileRead` / `afterTabFileEdit` ✅                       | ❌（CLI 无 Tab）                               |


### 5.4 可观测性总结

- **Cursor 更深**：能看到模型的 thinking 和 response 文本，对分析模型推理过程和回复质量有价值
- **Claude Code 更广**：覆盖 27 种事件，包括子代理生命周期、上下文压缩、权限管理、文件/配置变更等系统级事件
- **两者互补**：Cursor 擅长"模型在想什么"，Claude Code 擅长"系统在做什么"

---

## 六、多 IDE Adapter 层设计

### 6.1 目的

让同一套 handler 和 Langfuse 上报逻辑支持不同 IDE 的 hook 格式。

### 6.2 Adapter 接口

每个 Adapter 实现 4 个方法：

```javascript
{
  name: 'cursor',              // 标识名
  detect(input) {},            // 自动检测输入是否属于该 IDE
  normalize(input) {},         // 将 IDE 特有字段标准化为 canonical 格式
  transformResponse(event, response) {},  // 将 canonical 响应转回 IDE 格式
}
```

### 6.3 已实现的 Adapter


| Adapter       | 检测条件                                 | 字段映射                                                                                                    | 响应转换                          |
| ------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------- | ----------------------------- |
| **cursor**    | `conversation_id` + `cursor_version` | 直传（字段名一致）                                                                                               | 直传                            |
| **codebuddy** | `session_id` + `agent_version`       | `session_id→conversation_id`、`message_id→generation_id`、`workspace→workspace_roots`、`user_name→user_id` | `true/false → "allow"/"deny"` |
| **claude**    | 预留（`detect` 始终返回 false）              | 直传                                                                                                      | 直传                            |


### 6.4 CodeBuddy 的特殊适配

- `**afterFileEdit`**：CodeBuddy 的 edits 格式为 `{ code_edit: "..." }`，需转换为 `{ old_string: "", new_string: "..." }`
- `**afterSearchRplaceFileEdit`**：CodeBuddy 独有的事件名（注意 `Rplace` 拼写），映射到 `after_search_replace_edit`
- **响应格式**：CodeBuddy 的 `beforeSubmitPrompt` 期望 `{ continue: "allow"/"deny" }` 而非 `{ continue: true/false }`

---

## 七、test-trace.js 测试套件

`.cursor/hooks/test-trace.js` 是 hooks 系统的完整测试套件，分 6 个部分：


| Part  | 名称               | 测试内容                                                                                                                             |
| ----- | ---------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| **1** | 工具函数单元测试         | `generateTraceName`、`generateSessionId`、`generateTags`、`calculateEditStats`、`getFileExtension`、`formatDuration`、`determineLevel` |
| **2** | Adapter 测试       | Cursor/CodeBuddy adapter 的 detect、normalize、transformResponse 逻辑，含 CodeBuddy 的 edit 格式适配和自动检测                                    |
| **3** | Handler 路由测试     | 14 种 canonical 事件名全量路由，验证每个 handler 都正确调用了 Langfuse trace 方法                                                                     |
| **4** | 端到端 Cursor 路径    | 从 Cursor 原始输入 → adapter.normalize → routeHookHandler → adapter.transformResponse 的完整链路                                           |
| **5** | 端到端 CodeBuddy 路径 | 同上，覆盖 CodeBuddy 的 beforeSubmitPrompt、afterSearchRplaceFileEdit、afterFileRead 等场景                                                 |
| **6** | Langfuse 集成测试    | 需要 `.env` 中配置 API Key，向 Langfuse 实际发送 Cursor 和 CodeBuddy 两条测试 trace                                                              |


运行方式：`node .cursor/hooks/test-trace.js`

---

> **延伸阅读**：关于 LLM API 代理层（LiteLLM / Gateway）的概念介绍、与 IDE Hooks 层的可观测性差距分析、以及三种架构（CodeBuddy + LiteLLM / Claude Code SDK 直连 / Cursor 封闭客户端）的全流程对比图，请参阅独立文档：**[LLM API 代理层与可观测性差距分析](./LLM-API代理层与可观测性差距分析.md)**。

---

## 八、已知问题与改进方向

### 8.1 子代理 Trace 缺失首尾

**现状**：子代理的 `beforeSubmitPrompt`、`afterAgentResponse`、`stop` 不触发 hook。
**影响**：子代理 Trace 的 Input/Output 为空，无完成状态评分。
**改进思路**：在 `stop` handler 中检测是否有 `generation_id` 关联的子代理 Trace，主动补充 metadata；或在 handler 层缓存父 Task 调用的 prompt，在子代理的首个 hook 事件中回填。

### 8.2 单次 LLM 调用不可见

**现状**：无法获取底层 API 调用的 token 用量和成本。
**影响**：所有 observation 的 `inputUsage`/`outputUsage`/`totalCost` 均为 0。
**根因**：Cursor hooks 是**事件级**（IDE 行为层），不是**API 代理级**（LLM 请求层）。

### 8.3 父子 Trace 关联

**现状**：父子 Trace 在 Langfuse 中是独立的两条记录，无 `parentObservationId` 关联。
**改进思路**：在子代理首个 hook 事件中注入父 `conversation_id` 到 metadata，或利用 Langfuse 的 session 机制在同一 session 下展示关联 trace。

### 8.4 跨天会话追踪

**现状**：Langfuse Trace 时间戳取决于首次 hook 事件时间。长时间运行的会话（跨天）在按日期筛选时容易被遗漏。
**改进思路**：在每个 hook 事件中更新 trace 的 `updatedAt`，或在 Langfuse 中按 session 而非时间筛选。
