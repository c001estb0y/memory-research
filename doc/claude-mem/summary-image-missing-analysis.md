# 为什么 Summary 里看不到图片语义 —— 源码级分析（v3 · cloudboyguo 分支）

> 背景：Summary #1855（Claude Code + codebuddy-mem 产生）对用户上传的报错截图完全"看不见"；
> 而 Cursor 产生的 Summary #1881 却能清晰复述任务状态，仿佛"看懂了图片"。
> 本文基于 cloudboyguo 分支源码（`d:\GitHub\agent-memory\src\`）逐行走读，给出**精确到行号**的链路分析。

---

## 1. 现象

| 来源 | Summary ID | 是否反映图片语义 | 表现 |
|---|---|---|---|
| Claude Code + codebuddy-mem | #1855 | ❌ 完全缺失 | `learned` 字段明确写"没有保留具体命令、输出或图片中的报错文本"，`notes: null` |
| Cursor + codebuddy-mem | #1881 | ✅ 反映任务语义 | `completed` 直接复述"已完成 hughesli 增量蒸馏、正式库落地、备份与结果校验" |

---

## 2. 核心结论（一句话）

> **问题不是"hook 没实现"，而是 CodeBuddy IDE / Claude Code / CodeBuddy 适配器均没有注册 `afterAgentResponse` 事件。**
> Cursor 适配器是唯一注册了该事件的适配器，因此助手的文字回复（含图片描述）能被捕获为 observation 进入 summary；
> 其余三个适配器的事件列表里**缺少**该事件，助手回复从未被录入。
> 代码中虽然有一个"补偿机制"——在 `beforeSubmitPrompt` 中为这些适配器额外录入 user_prompt，但录入的是**用户提问文本**，不是**助手回复**，因此图片语义仍然丢失。

---

## 3. 五套适配器的事件注册对比（源码证据）

### 3.1 Cursor 适配器 ✅ 唯一支持 afterAgentResponse

```5:18:d:\GitHub\agent-memory\src\adapters\cursor.ts
const SUPPORTED_EVENTS = new Set([
  'beforeShellExecution',
  'beforeMCPExecution',
  'beforeSubmitPrompt',
  'afterShellExecution',
  'afterMCPExecution',
  'afterSearchReplaceFileEdit',
  'afterFileEdit',
  'afterAgentResponse',      // ✅ 支持
  'afterAgentThought',       // ✅ 支持
  'sessionStart',
  'sessionEnd',
  'stop',
]);
```

```20:28:d:\GitHub\agent-memory\src\adapters\cursor.ts
const HOOKS_EVENTS: Array<{ event: string; timeout: number }> = [
  { event: 'beforeSubmitPrompt', timeout: 10 },
  { event: 'afterShellExecution', timeout: 10 },
  { event: 'afterMCPExecution', timeout: 10 },
  { event: 'afterFileEdit', timeout: 10 },
  { event: 'afterAgentResponse', timeout: 10 },  // ✅ 已注册
  { event: 'afterAgentThought', timeout: 10 },    // ✅ 已注册
  { event: 'stop', timeout: 30 },
];
```

### 3.2 CodeBuddy IDE 适配器 ❌

```6:13:d:\GitHub\agent-memory\src\adapters\codebuddy-ide.ts
const EVENT_MAP: Record<string, string> = {
  'UserPromptSubmit': 'beforeSubmitPrompt',
  'PreToolUse': 'beforeShellExecution',
  'PostToolUse': 'afterToolUse',
  'Stop': 'stop',
  'SessionStart': 'sessionStart',
  'SessionEnd': 'sessionEnd',
  // ❌ 没有 afterAgentResponse
};
```

```20:27:d:\GitHub\agent-memory\src\adapters\codebuddy-ide.ts
const HOOKS_EVENTS: Array<{ ideEvent: string; timeout: number; matcher?: string }> = [
  { ideEvent: 'UserPromptSubmit', timeout: 10000 },
  { ideEvent: 'PostToolUse', timeout: 10000 },
  { ideEvent: 'PreToolUse', timeout: 10000, matcher: 'Bash' },
  { ideEvent: 'Stop', timeout: 30000 },
  { ideEvent: 'SessionStart', timeout: 15000 },
  { ideEvent: 'SessionEnd', timeout: 10000 },
  // ❌ 没有 afterAgentResponse
];
```

### 3.3 Claude Code 适配器 ❌

```12:19:d:\GitHub\agent-memory\src\adapters\claude-code.ts
const EVENT_MAP: Record<string, string> = {
  'UserPromptSubmit': 'beforeSubmitPrompt',
  'SessionStart': 'sessionStart',
  'SessionEnd': 'sessionEnd',
  'PreToolUse': 'beforeShellExecution',
  'PostToolUse': 'afterToolUse',
  'Stop': 'stop',
  // ❌ 没有 afterAgentResponse
};
```

```36:43:d:\GitHub\agent-memory\src\adapters\claude-code.ts
const HOOKS_EVENTS: Array<{ ideEvent: string; timeout: number; matcher?: string }> = [
  { ideEvent: 'UserPromptSubmit', timeout: 10000 },
  { ideEvent: 'SessionStart', timeout: 15000 },
  { ideEvent: 'PostToolUse', timeout: 10000 },
  { ideEvent: 'PreToolUse', timeout: 10000, matcher: 'Bash' },
  { ideEvent: 'Stop', timeout: 30000 },
  { ideEvent: 'SessionEnd', timeout: 10000 },
  // ❌ 没有 afterAgentResponse
];
```

### 3.4 Claude Internal 适配器 ❌（继承 ClaudeCodeAdapter）

```175:179:d:\GitHub\agent-memory\src\adapters\claude-code.ts
export class ClaudeInternalAdapter extends ClaudeCodeAdapter {
  override id = 'claude-internal';
  override displayName = 'Claude Internal';
  override configDir = path.join(os.homedir(), '.claude-internal');
}
```

继承自 `ClaudeCodeAdapter`，事件列表完全一致，同样**无** `afterAgentResponse`。

### 3.5 CodeBuddy (插件版) 适配器 ⚠️ 支持但未注册

```5:18:d:\GitHub\agent-memory\src\adapters\codebuddy.ts
const SUPPORTED_EVENTS = new Set([
  // ... 包含 afterAgentResponse ...
  'afterAgentResponse',      // ⚠️ SUPPORTED_EVENTS 里有
  'afterAgentThought',
]);
```

```20:26:d:\GitHub\agent-memory\src\adapters\codebuddy.ts
const HOOKS_EVENTS = [
  'beforeSubmitPrompt',
  'afterShellExecution',
  'afterMCPExecution',
  'afterFileEdit',
  'stop',
  // ❌ 但 HOOKS_EVENTS 里没有注册 afterAgentResponse
];
```

`SUPPORTED_EVENTS` 中声明了支持，但 `HOOKS_EVENTS` 中未注册，意味着 `mapEventName` 能识别该事件，但 `generateHooksConfig` 不会为它生成 hook 命令。如果 IDE 主动发送该事件，理论上能处理；但不会主动监听。

### 3.6 总览表

| 适配器 | `afterAgentResponse` 在事件映射中 | `afterAgentResponse` 在 HOOKS 注册中 | 效果 |
|---|---|---|---|
| Cursor | ✅ | ✅ | 助手回复被完整捕获 |
| CodeBuddy IDE | ❌ | ❌ | 助手回复丢失 |
| Claude Code | ❌ | ❌ | 助手回复丢失 |
| Claude Internal | ❌ | ❌ | 助手回复丢失 |
| CodeBuddy (插件) | ✅ (mapEventName) | ❌ (generateHooksConfig) | 被动支持但不主动监听 |

---

## 4. 代码中的"补偿机制"分析

### 4.1 beforeSubmitPrompt 中的 user_prompt 补偿

```587:606:d:\GitHub\agent-memory\src\hooks-cli.ts
    // For IDEs that lack afterAgentResponse (e.g. CodeBuddy IDE, Claude Code),
    // record the user prompt as an observation so the session has data for summary.
    const sourceAdapter = detectAdapterByEvent(process.argv[2] || '');
    const lacksAgentResponse = sourceAdapter?.id === 'codebuddy-ide' || sourceAdapter?.id === 'claude-code' || sourceAdapter?.id === 'claude-internal';
    if (lacksAgentResponse && input.prompt) {
      try {
        await client.addObservation({
          sessionId,
          projectPath,
          timestamp: Date.now(),
          type: 'agent_response',
          toolName: 'user_prompt',
          toolInput: { prompt: truncateString(input.prompt, 2000) },
          toolOutput: { recorded: true }
        });
```

**问题**：
- 虽然 `type` 标记为 `'agent_response'`，但 `toolName` 是 `'user_prompt'`
- 录入的内容是 `input.prompt`（用户提问文本），**不是**助手的回复
- 因此这个补偿只能确保"session 里有至少一条 observation"，但**无法**捕获助手对图片的分析内容
- 且 `input.prompt` 是纯文本，`input.attachments`（含图片）**从未被读取**

### 4.2 Stop hook 中的 response 补偿

```693:731:d:\GitHub\agent-memory\src\hooks-cli.ts
async function handleStop(input: StopInput & { response?: string; text?: string }): Promise<MonitorResult> {
  // ...
    const responseText = input.text || input.response || '';
    if (responseText) {
      try {
        await client.addObservation({
          sessionId,
          projectPath,
          timestamp: Date.now(),
          type: 'agent_response',
          toolName: 'agent_response',
          toolInput: { event: 'stop', reason: input.reason },
          toolOutput: { response: truncateString(responseText, 5000) }
        });
```

**问题**：
- `StopInput` 接口（第 207-212 行）只有 `session_id`、`conversation_id`、`reason`、`cwd`
- `response` 和 `text` 是通过 `& { response?: string; text?: string }` 额外附加的类型
- **这些字段是否有值，完全取决于 IDE 端的实现**
- Claude Code 的 Stop 事件规范中**不包含**助手回复文本
- CodeBuddy IDE 的 Stop 事件**可能也不提供**
- 因此这个兜底方案在大多数情况下 `responseText` 为空字符串，不会录入任何内容

---

## 5. 数据流对比：同一张图片在两条链路中的命运

### 5.1 Cursor 链路（能反映图片语义） ✅

```
用户消息（含 image block）
    │
    ├─► [beforeSubmitPrompt] → initSession + 记录 user_prompt 文本到 sdk_sessions
    │                          （注意：image block 本身未被处理，只存文本部分）
    │                          （Cursor 不满足 lacksAgentResponse 条件，不做补偿录入）
    │
    ▼
多模态 LLM 看到图片 + 文本，产出文字回复（含图片内容的描述）
    │
    ├─► [afterAgentResponse] ← ✅ Cursor 触发此 hook（hooks-cli.ts:646）
    │       │
    │       ▼
    │   responseContent = input.text || input.response    (第648行)
    │   client.recordResponse(sessionId, responseContent)  (第659行)
    │       │
    │       ▼
    │   client.ts:141-152  POST /api/observation
    │     { toolName: 'agent_response', toolOutput: { response: "助手文字回复..." } }
    │       │
    │       ▼
    │   WorkerService.ts:335-398  handleObservation → 后台异步
    │   SDKAgent.ts:140  processObservation(...)
    │       │
    │       ▼
    │   SDKAgent.ts:155-169  toolName === 'agent_response'
    │     → buildResponsePrompt(responseContent, memorySessionId)
    │     → callAI() → parseObservations() → insertObservation()
    │       │
    │       ▼
    │   observations 表中有一条记录，narrative 包含:
    │   "助手分析了报错截图，发现 XXX 错误..."
    │
    ▼
[stop / sessionEnd] 触发 summary 生成
    │
    ▼
SDKAgent.ts:282  allObservations = getObservationsBySession(memorySessionId)
                 ← 包含 agent_response 类型的 observation ✅
    │
    ▼
SDKAgent.ts:342  prompt = buildSummaryPrompt({
                   user_prompt: userPrompt,
                   observations: observations.map(...)
                 })
                 ← observations 摘要中包含助手对图片的文字描述
    │
    ▼
Summary LLM 能看到 "[agent_response] 助手分析了报错截图..." → 写出有语义的 summary
```

### 5.2 CodeBuddy IDE / Claude Code 链路（图片语义丢失） ❌

```
用户消息（含 image block）
    │
    ├─► [UserPromptSubmit → beforeSubmitPrompt]（hooks-cli.ts:554）
    │     → initSession + 记录 user_prompt 文本
    │     → lacksAgentResponse === true → 额外将 user_prompt 作为 observation 录入
    │       （注意：只是用户提问文本，不是助手回复；attachments 未处理）
    │
    ▼
多模态 LLM 看到图片 + 文本，产出文字回复（含图片内容的描述）
    │
    ╳ ← ❌ 适配器没有注册 afterAgentResponse hook
    │     助手回复的文字从此消失，永远不进入 codebuddy-mem
    │
    ├─► [PostToolUse → afterToolUse] 只记录了工具调用
    │     normalizeInput 根据字段路由到 afterShellExecution / afterMCPExecution / afterFileEdit
    │     （仅记录 shell、MCP、文件编辑等操作观察）
    │
    ├─► [Stop → stop]（hooks-cli.ts:693）
    │     responseText = input.text || input.response || ''
    │     ← Claude Code / CodeBuddy IDE 的 Stop 事件规范中不保证提供 response
    │     ← 大概率 responseText === ''，不会录入任何内容
    │
    ▼
SDKAgent.ts:282  allObservations = getObservationsBySession(memorySessionId)
                 ← 只有 user_prompt 观察 + 工具调用观察
                 ← 没有 agent_response 观察 ❌
    │
    ▼
buildSummaryPrompt({ user_prompt, observations })
    ← observations 摘要只有: "[user_prompt] 用户询问了...", "[shell] 执行了命令...", "[file_edit] 编辑了文件..."
    ← 完全没有助手对图片内容的分析
    │
    ▼
Summary LLM 只能看到工具操作骨架 → 写出 "未保留具体命令、输出或图片中的报错文本"
```

---

## 6. 关于 `attachments` 字段和图片处理

`BeforeSubmitPromptInput` 接口声明了 `attachments` 字段：

```169:185:d:\GitHub\agent-memory\src\hooks-cli.ts
interface BeforeSubmitPromptInput {
  session_id?: string;
  conversation_id?: string;
  // ...
  prompt: string;
  attachments?: any[];      // ← 声明了
  // ...
}
```

但在 `handleBeforeSubmitPrompt` 的实现中（第 554-638 行）：
- `input.attachments` **从未被读取或处理**
- `initSession` 只接收 `{ sessionId, projectPath, prompt }` —— 无 attachments 参数
- 补偿录入的 `addObservation` 也只传 `{ prompt: truncateString(input.prompt, 2000) }`

即使 IDE 传入了图片附件数据，也会被**完全忽略**。

---

## 7. 关于 `last_assistant_message` 死代码

`SDKSession` 接口声明了 `last_assistant_message` 字段：

```20:36:d:\GitHub\agent-memory\src\sdk\prompts.ts
export interface SDKSession {
  id: number;
  memory_session_id: string | null;
  project: string;
  user_prompt: string;
  last_assistant_message?: string;  // ← 声明了
  observations?: Array<{...}>;
}
```

`buildSummaryPrompt` 中也赋值了：

```247:249:d:\GitHub\agent-memory\src\sdk\prompts.ts
export function buildSummaryPrompt(session: SDKSession): string {
  const userPrompt = session.user_prompt || '';
  const lastAssistantMessage = session.last_assistant_message || '';
```

但 `lastAssistantMessage` 变量**从未出现在**返回的 prompt 模板字符串中（第 274-305 行），等价于**死代码**。

同时，`SDKAgent.generateSummary`（第 342-357 行）构造 `buildSummaryPrompt` 参数时：

```342:357:d:\GitHub\agent-memory\src\services\worker\SDKAgent.ts
    const prompt = buildSummaryPrompt({
      id: 0,
      memory_session_id: memorySessionId,
      project,
      user_prompt: userPrompt,
      observations: observations.map(o => ({
        // ...
      }))
      // ❌ 没有传入 last_assistant_message
    });
```

**完全没有**传入 `last_assistant_message` 字段。

---

## 8. recordResponse 管道：纯文本，无多模态

```141:152:d:\GitHub\agent-memory\src\services\worker\client.ts
  async recordResponse(sessionId: string, response: string): Promise<void> {
    try {
      await fetch(`${this.baseUrl}/api/observation`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json; charset=utf-8' },
        body: JSON.stringify({
          sessionId,
          toolName: 'agent_response',
          toolInput: {},
          toolOutput: { response }   // ← 只有纯文本 string
        })
      });
```

整个 `recordResponse → handleObservation → processObservation → buildResponsePrompt` 链路只处理字符串，**无**图片 URL / base64 / 多模态 block 的任何支持。

---

## 9. Summary 生成的完整链路

```
触发点: stop / sessionEnd hook
    │
    ▼
hooks-cli.ts:723 / 809  client.summarizeSession(sessionId)
    │
    ▼
client.ts:228-248  POST /api/session/end { sessionId, reason }
    │
    ▼
WorkerService.ts:269-330  handleSessionEnd()
  ├─ 313: res.statusCode = 200; res.end(...)  ← 立即返回 200，不阻塞 hook
  └─ 318: this.sdkAgent.generateSummary(memorySessionId, session.project)  ← 后台异步
    │
    ▼
SDKAgent.ts:259-416  generateSummary()
  ├─ 263: session = getSessionByMemoryId(memorySessionId)
  ├─ 264: userPrompt = session?.user_prompt || ''
  ├─ 282: allObservations = getObservationsBySession(memorySessionId)
  │        ← 对 Cursor: 包含 agent_response 类型观察 ✅
  │        ← 对 CodeBuddy IDE / Claude Code: 只有 user_prompt + 工具观察 ❌
  ├─ 306: observations = allObservations.slice(-20)  (最多取最近 20 条)
  ├─ 342: prompt = buildSummaryPrompt({
  │         user_prompt: userPrompt,
  │         observations: observations.map(...)
  │       })
  ├─ 361: response = await this.callAI(prompt)
  ├─ 364: parsed = parseSummary(response)
  └─ 388-404: insertSummary({...parsed...})
```

**关键发现**：`generateSummary` 的输入只有两个来源：
1. `sdk_sessions.user_prompt` — 用户原始提问（纯文本，无图片）
2. `observations` 表中的观察记录 — 如果没有 `agent_response` 类型，就缺少助手回复语义

---

## 10. 修复方案（按优先级排列）

### 方案 A：在 Claude Code / CodeBuddy IDE 适配器中补注册 afterAgentResponse（最小改动）

需要确认这些 IDE 的 hooks 系统是否支持发送 agent response 事件。

**Claude Code**：查阅 [Claude Code hooks 文档](https://docs.anthropic.com/en/docs/claude-code/hooks)，确认 Claude Code **不支持** `afterAgentResponse` 事件类型。Claude Code 只支持：`PreToolUse`、`PostToolUse`、`Notification`、`Stop`、`SubagentStop`。因此**此方案对 Claude Code 不可行**。

**CodeBuddy IDE**：需确认 CodeBuddy IDE 是否支持类似事件。如果支持：

```typescript
// src/adapters/codebuddy-ide.ts
const EVENT_MAP: Record<string, string> = {
  // ...existing...
  'AfterAgentResponse': 'afterAgentResponse',  // ← 新增
};

const HOOKS_EVENTS = [
  // ...existing...
  { ideEvent: 'AfterAgentResponse', timeout: 10000 },  // ← 新增
];
```

### 方案 B：在 Stop hook 中主动从 transcript 读取助手回复（Claude Code 专用兜底）

Claude Code 会将对话 transcript 写入 `~/.claude/projects/<project-hash>/` 目录下的 JSONL 文件。可以在 `handleStop` 中主动读取最后一条 assistant message：

```typescript
// hooks-cli.ts handleStop() 中增加（仅 claude-code / claude-internal 适配器）
if (sourceAdapter?.id === 'claude-code' || sourceAdapter?.id === 'claude-internal') {
  const lastAssistantMsg = readLastAssistantFromTranscript(projectPath);
  if (lastAssistantMsg) {
    await client.addObservation({
      sessionId, projectPath, timestamp: Date.now(),
      type: 'agent_response', toolName: 'agent_response',
      toolInput: { event: 'stop', source: 'transcript' },
      toolOutput: { response: truncateString(lastAssistantMsg, 5000) }
    });
  }
}
```

### 方案 C：处理 attachments 中的图片信息

在 `handleBeforeSubmitPrompt` 中处理 `input.attachments`：

```typescript
if (input.attachments?.length) {
  const attachmentInfo = input.attachments.map(a => ({
    type: a.type || a.mime_type || 'unknown',
    name: a.name || a.filename || 'unnamed',
  }));
  await client.addObservation({
    sessionId, projectPath, timestamp: Date.now(),
    type: 'agent_response', toolName: 'user_attachments',
    toolInput: { attachments: attachmentInfo },
    toolOutput: { count: input.attachments.length, types: attachmentInfo.map(a => a.type) }
  });
}
```

### 方案 D：激活 `last_assistant_message` 死代码

1. 在 `buildSummaryPrompt` 的模板中实际使用 `lastAssistantMessage`
2. 在 `SDKAgent.generateSummary` 中查询最近的 `agent_response` observation 并填充此字段

```typescript
// prompts.ts buildSummaryPrompt() 模板中增加
${lastAssistantMessage ? `\n## Agent's Last Response:\n${lastAssistantMessage.substring(0, 2000)}` : ''}
```

### 方案 E：增强 PostToolUse 补偿（适用于所有缺 afterAgentResponse 的适配器）

在 `handleStop` 中汇总本 session 所有 observations，如果发现没有 `agent_response` 类型的 observation，主动从其他来源（如工具输出中的上下文）推断一个粗略的 session 描述。

---

## 11. 速查表

| 问题 | 答案 |
|---|---|
| Claude Code 的多模态模型能看图吗？ | 能。问题不在"看不看得见"。 |
| Stop hook 实现了吗？ | ✅ 已实现（`hooks-cli.ts:693-731`），且尝试录入 response 文本，但依赖 IDE 提供数据。 |
| afterAgentResponse 实现了吗？ | ✅ 已实现（`hooks-cli.ts:646-667`），但**仅 Cursor 注册了该事件**。 |
| Claude Code 支持 afterAgentResponse 吗？ | ❌ Claude Code hooks 规范中无此事件类型。 |
| CodeBuddy IDE 为什么丢失图片语义？ | `EVENT_MAP` 和 `HOOKS_EVENTS` 中缺少 `afterAgentResponse`。 |
| beforeSubmitPrompt 的补偿有用吗？ | 部分有用：确保 session 有 observation，但录入的是 **user_prompt 文本**，不是助手回复。 |
| `attachments` 字段有用到吗？ | ❌ 接口声明了但从未处理（`hooks-cli.ts:180`）。 |
| `last_assistant_message` 有用到吗？ | ❌ 接口声明了但 `buildSummaryPrompt` 未使用；`generateSummary` 也未传入。 |
| recordResponse 支持多模态吗？ | ❌ 只接收纯文本 string（`client.ts:141`）。 |
| Summary 的输入是什么？ | `user_prompt`（纯文本）+ `observations` 列表。无 agent_response observation = 无助手语义。 |
| 最可行的修复方向？ | 对 Claude Code：方案 B（从 transcript 读取）；对 CodeBuddy IDE：方案 A（补注册事件）。 |

---

## 12. 关键源码文件索引

| 文件 | 作用 | 关键行号 |
|---|---|---|
| `src/hooks-cli.ts` | CLI 入口，所有 hook handler | 554（beforeSubmitPrompt）、587（补偿机制）、646（afterAgentResponse）、693（stop） |
| `src/adapters/cursor.ts` | Cursor 事件列表 | 5-18（SUPPORTED_EVENTS）、20-28（HOOKS_EVENTS） |
| `src/adapters/codebuddy-ide.ts` | CodeBuddy IDE 事件列表 | 6-13（EVENT_MAP）、20-27（HOOKS_EVENTS） |
| `src/adapters/claude-code.ts` | Claude Code 事件列表 | 12-19（EVENT_MAP）、36-43（HOOKS_EVENTS）、175（ClaudeInternalAdapter） |
| `src/adapters/codebuddy.ts` | CodeBuddy 插件版事件列表 | 5-18（SUPPORTED_EVENTS）、20-26（HOOKS_EVENTS） |
| `src/adapters/registry.ts` | 适配器注册表与自动检测 | 8-14（adapters 数组）、24-34（detectAdapterByEvent） |
| `src/services/worker/client.ts` | Worker HTTP 客户端 | 141-156（recordResponse）、181-223（addObservation） |
| `src/services/worker/SDKAgent.ts` | AI 调用 & summary 生成 | 140-253（processObservation）、259-416（generateSummary） |
| `src/services/worker/WorkerService.ts` | HTTP 服务端路由 | 269-330（handleSessionEnd）、335-398（handleObservation） |
| `src/sdk/prompts.ts` | Prompt 模板 | 247-306（buildSummaryPrompt）、341-397（buildResponsePrompt） |
| `src/sdk/parser.ts` | XML 解析 | parseSummary / parseObservations |

---

*文档生成时间：2026-04-20 · 基于 cloudboyguo 分支源码 `d:\GitHub\agent-memory\src\` 全量走读*

*v3 基于 cloudboyguo 分支完整重写，覆盖全部 5 个适配器（v1/v2 已废弃，见 git 历史 `20a97b7`）*
