# Claude Code 中的 ReAct 推理-行动循环深度分析

> 基于 Claude Code 源码（2026-03 快照）的逐行级分析，完整拆解其 ReAct（Reasoning + Acting）模式的架构设计与工程实现。

## 1. 什么是 ReAct，为什么它是 Claude Code 的灵魂

ReAct（Reasoning and Acting）是 Yao et al. 2022 提出的 Agent 范式，核心思想是将 LLM 的**推理能力**（Reasoning/Thought）与**行动能力**（Acting/Tool Use）在一个交替循环中统一起来：

```
Thought → Action → Observation → Thought → Action → Observation → ... → Final Answer
```

Claude Code 的整个架构就是围绕这一范式构建的，但做了大量工程化增强。下面逐层拆解。

---

## 2. 核心循环：`queryLoop` —— ReAct 的引擎

### 2.1 入口与状态定义

ReAct 循环的入口在 `src/query.ts` 中的 `queryLoop` 函数。它是一个 **AsyncGenerator**，通过 `yield` 将中间结果（消息、事件）流式输出：

```typescript
// src/query.ts:241-251
async function* queryLoop(
  params: QueryParams,
  consumedCommandUuids: string[],
): AsyncGenerator<
  StreamEvent | RequestStartEvent | Message | TombstoneMessage | ToolUseSummaryMessage,
  Terminal
>
```

循环的可变状态封装在 `State` 对象中，在每轮迭代间传递：

```typescript
// src/query.ts:204-217
type State = {
  messages: Message[]                    // 完整的对话历史
  toolUseContext: ToolUseContext          // 工具上下文（权限、选项等）
  autoCompactTracking: ...               // 自动压缩追踪
  maxOutputTokensRecoveryCount: number   // 输出截断恢复计数
  hasAttemptedReactiveCompact: boolean   // 是否已尝试响应式压缩
  maxOutputTokensOverride: ...           // 输出 token 上限覆盖
  pendingToolUseSummary: ...             // 待处理的工具摘要
  stopHookActive: boolean | undefined    // stop hook 是否激活
  turnCount: number                      // 当前轮次
  transition: Continue | undefined       // 上一轮的转移原因
}
```

### 2.2 主循环结构

整个 ReAct 循环是一个 `while (true)` 无限循环，每轮迭代的五大阶段对应 ReAct 的完整周期：

```
┌─────────────────────────────────────────────────────────────────┐
│                      while (true) {                             │
│                                                                 │
│  ┌─── 阶段 1: 上下文准备 ──────────────────────────────────┐   │
│  │  • 解构 State                                            │   │
│  │  • 应用 toolResultBudget（控制工具结果大小）              │   │
│  │  • 执行 snip / microcompact / contextCollapse            │   │
│  │  • 触发 autocompact（上下文窗口管理）                    │   │
│  │  • 构建完整 systemPrompt                                 │   │
│  └──────────────────────────────────────────────────────────┘   │
│                            ↓                                    │
│  ┌─── 阶段 2: 调用模型（Reasoning / Thinking）──────────────┐  │
│  │  deps.callModel({                                         │  │
│  │    messages,                                              │  │
│  │    systemPrompt,                                          │  │
│  │    thinkingConfig,   ← Extended Thinking                  │  │
│  │    tools,            ← 可用工具列表                       │  │
│  │    effortValue,      ← 推理深度控制                       │  │
│  │  })                                                       │  │
│  │  → 流式接收 assistant messages                            │  │
│  │  → 收集 thinking blocks（推理）+ tool_use blocks（行动） │  │
│  │  → 流式工具执行 (StreamingToolExecutor)                   │  │
│  └──────────────────────────────────────────────────────────┘  │
│                            ↓                                    │
│  ┌─── 阶段 3: 终止判断 ─────────────────────────────────────┐  │
│  │  if (!needsFollowUp) {                                    │  │
│  │    • 处理 prompt-too-long 恢复                            │  │
│  │    • 处理 max_output_tokens 恢复                          │  │
│  │    • 执行 stop hooks                                      │  │
│  │    • 检查 token budget → 可能注入 nudge 继续              │  │
│  │    → return { reason: 'completed' }                       │  │
│  │  }                                                        │  │
│  └──────────────────────────────────────────────────────────┘  │
│                            ↓                                    │
│  ┌─── 阶段 4: 工具执行（Acting / Observation）──────────────┐  │
│  │  for await (update of toolUpdates) {                      │  │
│  │    yield update.message   ← 工具结果作为 Observation      │  │
│  │    toolResults.push(...)                                   │  │
│  │  }                                                        │  │
│  └──────────────────────────────────────────────────────────┘  │
│                            ↓                                    │
│  ┌─── 阶段 5: 准备下一轮 ──────────────────────────────────┐  │
│  │  • 收集 attachments（记忆、技能发现、通知队列）           │  │
│  │  • 检查 maxTurns → 可能终止                              │  │
│  │  • 构建 next State:                                       │  │
│  │    messages: [...旧消息, ...助手消息, ...工具结果]         │  │
│  │    turnCount: turnCount + 1                               │  │
│  │  state = next; continue                                   │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  } // while (true)                                              │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. 阶段详解

### 3.1 阶段 1：上下文准备 —— "世界模型更新"

在调用模型之前，系统对上下文做了多层处理，确保 Agent 看到的"世界状态"是最优的：

**（1）Tool Result Budget（工具结果预算）**

```typescript
// src/query.ts:379-394
messagesForQuery = await applyToolResultBudget(
  messagesForQuery,
  toolUseContext.contentReplacementState,
  persistReplacements ? records => void recordContentReplacement(...) : undefined,
  new Set(toolUseContext.options.tools.filter(t => !Number.isFinite(t.maxResultSizeChars)).map(t => t.name)),
)
```

对过大的工具结果进行裁剪/替换，防止单个 Observation 占用过多上下文。

**（2）History Snip（历史裁剪）**

```typescript
// src/query.ts:401-410
if (feature('HISTORY_SNIP')) {
  const snipResult = snipModule!.snipCompactIfNeeded(messagesForQuery)
  messagesForQuery = snipResult.messages
  snipTokensFreed = snipResult.tokensFreed
}
```

选择性裁剪早期对话历史中信息密度低的部分。

**（3）Microcompact（微压缩）**

```typescript
// src/query.ts:414-419
const microcompactResult = await deps.microcompact(messagesForQuery, toolUseContext, querySource)
messagesForQuery = microcompactResult.messages
```

对工具结果中的冗余内容（如重复的文件输出）进行原位压缩。

**（4）Context Collapse（上下文折叠）**

```typescript
// src/query.ts:440-447
if (feature('CONTEXT_COLLAPSE') && contextCollapse) {
  const collapseResult = await contextCollapse.applyCollapsesIfNeeded(messagesForQuery, toolUseContext, querySource)
  messagesForQuery = collapseResult.messages
}
```

将旧的对话轮次折叠为摘要，保留关键信息。

**（5）Autocompact（自动压缩）**

```typescript
// src/query.ts:454-467
const { compactionResult, consecutiveFailures } = await deps.autocompact(
  messagesForQuery, toolUseContext, { systemPrompt, userContext, ... }, querySource, tracking, snipTokensFreed
)
```

当上下文接近窗口限制时，触发完整的上下文压缩——用一个独立的 LLM 调用生成对话摘要。

**这一层的工程复杂度远超学术 ReAct 论文**。论文假设上下文无限，而真实系统需要在有限窗口中维持推理连贯性。

### 3.2 阶段 2：调用模型 —— Reasoning（思考）

```typescript
// src/query.ts:659-708
for await (const message of deps.callModel({
  messages: prependUserContext(messagesForQuery, userContext),
  systemPrompt: fullSystemPrompt,
  thinkingConfig: toolUseContext.options.thinkingConfig,
  tools: toolUseContext.options.tools,
  signal: toolUseContext.abortController.signal,
  options: {
    model: currentModel,
    effortValue: appState.effortValue,
    fastMode: appState.fastMode,
    toolChoice: undefined,     // 让模型自主决定是否调用工具
    fallbackModel,
    advisorModel: appState.advisorModel,
    taskBudget: params.taskBudget,
    ...
  },
}))
```

这一步将所有上下文发送给 Claude API，模型返回的响应可能包含：


| 响应类型             | ReAct 对应               | 说明                         |
| ---------------- | ---------------------- | -------------------------- |
| `thinking` block | **Thought**            | 模型的内部推理（Extended Thinking） |
| `text` block     | **Final Answer** 或过渡文本 | 面向用户的文本输出                  |
| `tool_use` block | **Action**             | 模型决定调用的工具                  |


**关键设计：交错思维（Interleaved Thinking）**

Claude Code 使用 `interleaved-thinking` beta，允许模型在同一轮响应中交错产出多个 thinking block 和 tool_use block：

```
[thinking] 分析用户请求，确定需要先读取文件...
[tool_use] FileRead { path: "src/main.ts" }
[thinking] 文件内容显示需要修改第 42 行...
[tool_use] FileEdit { path: "src/main.ts", ... }
[text] 我已经完成了修改。
```

这比经典 ReAct 的"一个 Thought + 一个 Action"更灵活——模型可以在一轮中**批量规划多个行动**。

**流式工具执行（Streaming Tool Execution）**

```typescript
// src/query.ts:561-568
let streamingToolExecutor = useStreamingToolExecution
  ? new StreamingToolExecutor(toolUseContext.options.tools, canUseTool, toolUseContext)
  : null
```

关键优化：在模型流式输出 tool_use block 时，`StreamingToolExecutor` 会**立即启动工具执行**，不等待整个响应完成。这意味着：

```
模型输出: [thinking]...[tool_use A]...
                        ↓
                  工具 A 开始执行
模型输出:                    ...[tool_use B]...[text]...
                                  ↓
                           工具 B 开始执行（可能与 A 并行）
```

### 3.3 阶段 3：终止决策 —— "该停还是该继续"

当模型响应中**不包含 tool_use block** 时（`needsFollowUp === false`），系统进入终止决策逻辑。但"不调用工具"不等于"该结束"——还有多种继续的理由：

**（1）Prompt-too-long 恢复**

```typescript
// src/query.ts:1085-1117（简化）
if (isWithheld413) {
  // 先尝试 Context Collapse 排水
  const drained = contextCollapse.recoverFromOverflow(messagesForQuery, querySource)
  if (drained.committed > 0) {
    state = { ...next, transition: { reason: 'collapse_drain_retry' } }
    continue  // ← 重新进入循环
  }
  
  // 再尝试 Reactive Compact
  const compacted = await reactiveCompact.tryReactiveCompact({ ... })
  if (compacted) {
    state = { ...next, transition: { reason: 'reactive_compact_retry' } }
    continue  // ← 重新进入循环
  }
}
```

**（2）Max Output Tokens 恢复**

当模型输出被截断时，系统注入恢复消息让模型继续：

```typescript
// src/query.ts:1224-1251
if (maxOutputTokensRecoveryCount < MAX_OUTPUT_TOKENS_RECOVERY_LIMIT) {  // 最多 3 次
  const recoveryMessage = createUserMessage({
    content: `Output token limit hit. Resume directly — no apology, no recap of what you were doing. ` +
      `Pick up mid-thought if that is where the cut happened. Break remaining work into smaller pieces.`,
    isMeta: true,
  })
  state = { ...next, messages: [...messagesForQuery, ...assistantMessages, recoveryMessage] }
  continue  // ← 重新进入循环
}
```

**（3）Token Budget Nudge**

当启用了 token budget 且模型还有预算未用完时，注入 nudge 消息促使模型继续工作：

```typescript
// src/query.ts:1308-1341
const decision = checkTokenBudget(budgetTracker!, ...)
if (decision.action === 'continue') {
  state = {
    messages: [...messagesForQuery, ...assistantMessages,
      createUserMessage({ content: decision.nudgeMessage, isMeta: true })],
    transition: { reason: 'token_budget_continuation' },
  }
  continue  // ← 重新进入循环
}
```

**（4）Stop Hooks**

用户配置的 hooks 可以检查模型输出并决定是否允许结束：

```typescript
// src/query.ts:1267-1306
const stopHookResult = yield* handleStopHooks(...)
if (stopHookResult.blockingErrors.length > 0) {
  state = { ...next, messages: [..., ...stopHookResult.blockingErrors], stopHookActive: true }
  continue  // ← hook 阻止了终止，注入错误消息让模型重试
}
```

### 3.4 阶段 4：工具执行 —— Acting + Observation

当模型响应包含 tool_use block 时（`needsFollowUp === true`），进入工具执行阶段：

```typescript
// src/query.ts:1380-1408
const toolUpdates = streamingToolExecutor
  ? streamingToolExecutor.getRemainingResults()     // 流式执行的剩余结果
  : runTools(toolUseBlocks, assistantMessages, canUseTool, toolUseContext)  // 传统执行

for await (const update of toolUpdates) {
  if (update.message) {
    yield update.message          // 向外层输出工具结果
    toolResults.push(...)         // 收集为下一轮的 Observation
  }
  if (update.newContext) {
    updatedToolUseContext = { ...update.newContext, queryTracking }
  }
}
```

**工具编排策略（toolOrchestration.ts）**

工具执行并非简单的串行——`runTools` 实现了智能的并发控制：

```typescript
// src/services/tools/toolOrchestration.ts:19-80
export async function* runTools(toolUseMessages, assistantMessages, canUseTool, toolUseContext) {
  for (const { isConcurrencySafe, blocks } of partitionToolCalls(toolUseMessages, currentContext)) {
    if (isConcurrencySafe) {
      // 只读工具（如 FileRead, Grep, Glob）可以并行执行
      for await (const update of runToolsConcurrently(blocks, ...)) { yield update }
    } else {
      // 有副作用的工具（如 FileEdit, Bash）必须串行执行
      for await (const update of runToolsSerially(blocks, ...)) { yield update }
    }
  }
}
```

工具被分为两类：

- **并发安全**（`isConcurrencySafe`）：FileRead、Grep、Glob 等只读工具，最多 10 个并行
- **非并发安全**：FileEdit、FileWrite、Bash 等有副作用的工具，严格串行

### 3.5 阶段 5：准备下一轮 —— 状态传递

工具执行完毕后，系统在发起下一轮前做三件事：

**（1）收集附件（Attachments）**

```typescript
// src/query.ts:1580-1590
for await (const attachment of getAttachmentMessages(
  null, updatedToolUseContext, null, queuedCommandsSnapshot,
  [...messagesForQuery, ...assistantMessages, ...toolResults], querySource
)) {
  yield attachment
  toolResults.push(attachment)
}
```

附件包括：记忆文件、技能发现结果、消息队列通知、文件变更追踪等。这些额外的 Observation 帮助模型在下一轮推理时有更全面的上下文。

**（2）生成工具摘要**

```typescript
// src/query.ts:1469-1481
nextPendingToolUseSummary = generateToolUseSummary({
  tools: toolInfoForSummary,
  signal: toolUseContext.abortController.signal,
  lastAssistantText,
}).then(summary => summary ? createToolUseSummaryMessage(summary, toolUseIds) : null)
```

用一个轻量模型（Haiku）异步生成工具使用摘要，供 mobile UI 等场景使用。

**（3）组装下一轮状态**

```typescript
// src/query.ts:1715-1727
const next: State = {
  messages: [...messagesForQuery, ...assistantMessages, ...toolResults],
  toolUseContext: toolUseContextWithQueryTracking,
  autoCompactTracking: tracking,
  turnCount: nextTurnCount,
  maxOutputTokensRecoveryCount: 0,     // 重置恢复计数
  hasAttemptedReactiveCompact: false,   // 重置压缩标记
  pendingToolUseSummary: nextPendingToolUseSummary,
  transition: { reason: 'next_turn' },
}
state = next  // ← 状态传递到下一轮迭代
```

---

## 4. 工具系统深度解析

### 4.1 Tool 类型定义

`src/Tool.ts` 定义了工具的核心接口（简化）：

```typescript
type Tool = {
  name: string                           // 工具名称
  description: string                    // 工具描述（给模型看）
  inputSchema: ToolInputJSONSchema       // JSON Schema 输入定义
  call(input, context): AsyncGenerator<Message>  // 执行函数
  isReadOnly(): boolean                  // 是否只读（影响并发策略）
  isConcurrencySafe: boolean             // 是否并发安全
  maxResultSizeChars?: number            // 结果大小限制
  backfillObservableInput?(input): void  // 回填可观测的输入字段
}
```

### 4.2 内置工具清单与 ReAct 角色


| 工具名称            | ReAct 角色       | 并发安全 | 说明                  |
| --------------- | -------------- | ---- | ------------------- |
| FileRead        | Observation 采集 | ✅    | 读取文件内容              |
| FileEdit        | Action 执行      | ❌    | 编辑文件                |
| FileWrite       | Action 执行      | ❌    | 创建文件                |
| Bash/PowerShell | Action 执行      | ❌    | 执行 shell 命令         |
| Glob            | Observation 采集 | ✅    | 文件路径搜索              |
| Grep            | Observation 采集 | ✅    | 文件内容搜索              |
| Agent           | 子 ReAct 循环     | ❌    | 启动子 Agent（递归 ReAct） |
| TodoWrite       | 状态管理           | ❌    | 任务列表管理              |
| EnterPlanMode   | 模式切换           | ❌    | 进入规划模式              |
| ExitPlanMode    | 模式切换           | ❌    | 退出规划模式              |
| AskUserQuestion | 交互             | ❌    | 向用户提问               |
| WebSearch       | Observation 采集 | ✅    | 搜索网页                |
| WebFetch        | Observation 采集 | ✅    | 获取网页内容              |
| Sleep           | 等待             | ❌    | 等待指定时间              |


### 4.3 StreamingToolExecutor —— 流水线执行

`StreamingToolExecutor` 是 Claude Code 对 ReAct 的关键工程优化：

```typescript
// src/services/tools/StreamingToolExecutor.ts:40-62
export class StreamingToolExecutor {
  private tools: TrackedTool[] = []
  
  addTool(block: ToolUseBlock, assistantMessage: AssistantMessage): void {
    // 当模型流式输出一个 tool_use block 时立即加入队列
    // 如果是并发安全的工具，立即开始执行
  }
  
  getCompletedResults(): MessageUpdate[] {
    // 在模型流式输出期间，返回已完成的工具结果
  }
  
  getRemainingResults(): AsyncGenerator<MessageUpdate> {
    // 模型输出完毕后，等待所有工具执行完成
  }
}
```

每个工具有四种状态：`queued → executing → completed → yielded`

执行时序示意：

```
时间线 →

模型流:   [think][tool_use:A][text][tool_use:B][tool_use:C][end]
             ↓                      ↓            ↓
工具执行:    A开始 ─────── A完成    B开始 ──     C开始 ──
                                    B完成        C完成
             
结果输出:              [A结果]    [B结果]      [C结果]
```

关键策略：

- **并发安全工具可与其他并发安全工具并行**
- **非并发安全工具独占执行**
- **结果按工具接收顺序输出**（而非完成顺序）
- **如果一个 Bash 工具出错，通过 `siblingAbortController` 取消同批次的兄弟工具**

---

## 5. 与经典 ReAct 的关键差异

### 5.1 多级上下文管理

经典 ReAct 论文假设上下文无限。Claude Code 实现了五层上下文管理：

```
┌─────────────────────────────────────────┐
│ Layer 5: Autocompact（完整上下文摘要）    │ ← 最重的，独立 LLM 调用
├─────────────────────────────────────────┤
│ Layer 4: Context Collapse（上下文折叠）   │ ← 增量式，按轮次折叠
├─────────────────────────────────────────┤
│ Layer 3: Microcompact（工具结果微压缩）   │ ← 原位替换冗余内容
├─────────────────────────────────────────┤
│ Layer 2: History Snip（历史裁剪）         │ ← 选择性丢弃低信息密度部分
├─────────────────────────────────────────┤
│ Layer 1: Tool Result Budget（结果预算）   │ ← 限制单工具结果大小
└─────────────────────────────────────────┘
```

### 5.2 弹性终止决策

经典 ReAct 的终止条件简单——模型不输出 Action 就结束。Claude Code 有**8 种继续/终止的转移类型**：

```typescript
type Continue =
  | { reason: 'next_turn' }                     // 正常的工具结果后继续
  | { reason: 'reactive_compact_retry' }         // 上下文太长，压缩后重试
  | { reason: 'collapse_drain_retry'; ... }      // 折叠排水后重试
  | { reason: 'max_output_tokens_recovery'; ... } // 输出截断恢复
  | { reason: 'max_output_tokens_escalate' }     // 输出上限提升重试
  | { reason: 'stop_hook_blocking' }             // stop hook 阻止了终止
  | { reason: 'token_budget_continuation' }      // token 预算未用完，nudge 继续
  // 以及对应的终止原因：
  // 'completed', 'aborted_streaming', 'aborted_tools', 'max_turns',
  // 'hook_stopped', 'prompt_too_long', 'image_error', 'model_error', ...
```

### 5.3 流式 + 并发工具执行

经典 ReAct 是严格串行的：`Thought → Action → Observation → Thought → ...`

Claude Code 允许：

- 模型**一次输出多个 Action**（多个 tool_use block）
- 只读 Action **并行执行**
- 模型流式输出时**工具已开始执行**（流水线化）
- 同一轮中 thinking 和 tool_use **交错出现**

### 5.4 错误恢复与韧性

经典 ReAct 没有错误恢复机制。Claude Code 实现了多层恢复：


| 错误类型         | 恢复策略                                | 最大重试 |
| ------------ | ----------------------------------- | ---- |
| 模型输出截断       | 注入 "Resume directly" 消息             | 3 次  |
| 上下文太长 (413)  | Context Collapse → Reactive Compact | 逐级   |
| 图片/PDF 太大    | Reactive Compact 剥离媒体               | 1 次  |
| 模型高负载        | Fallback 到备用模型                      | 1 次  |
| Stop Hook 失败 | 注入 hook 错误消息让模型修正                   | 持续   |


### 5.5 递归 ReAct（SubAgent）

通过 `AgentTool`，Claude Code 支持**嵌套的 ReAct 循环**：

```
主 Agent ReAct 循环:
  Thought → Action(AgentTool) → [子 Agent ReAct 循环] → Observation → Thought → ...
```

子 Agent 有独立的上下文、工具集和终止条件，但默认**禁用 thinking**（节省 token），除非是 fork 类型的子 Agent。

---

## 6. System Prompt 中的 ReAct 引导

虽然 Claude Code 不使用经典的 "Let's think step by step" CoT 提示，但其 system prompt（`src/constants/prompts.ts`）中包含了大量**隐式的 ReAct 引导**：

**行动前先读取（Read Before Act）**

```
In general, do not propose changes to code you haven't read. If a user asks about 
or wants you to modify a file, read it first. Understand existing code before 
suggesting modifications.
```

**错误后先诊断再行动（Diagnose Before Retry）**

```
If an approach fails, diagnose why before switching tactics — read the error, 
check your assumptions, try a focused fix. Don't retry the identical action 
blindly, but don't abandon a viable approach after a single failure either.
```

**工具使用优先级引导**

```
- To read files use FileRead instead of cat, head, tail, or sed
- To edit files use FileEdit instead of sed or awk
- To search for files use Glob instead of find or ls
- To search the content of files, use Grep instead of grep or rg
```

**谨慎行动原则**

```
Carefully consider the reversibility and blast radius of actions. Generally you 
can freely take local, reversible actions like editing files or running tests. 
But for actions that are hard to reverse, affect shared systems beyond your local 
environment, or could otherwise be risky or destructive, check with the user 
before proceeding.
```

这些指令共同形成了一套**实操级的 ReAct 行为规范**——不是抽象的"先想后做"，而是具体到"编辑文件前先读取"、"失败后先看错误再重试"。

---

## 7. 完整的 ReAct 数据流图

```
用户输入
   │
   ▼
┌─────────────────────┐
│   queryLoop 入口     │
│   State 初始化       │
└──────────┬──────────┘
           │
    ┌──────▼──────┐
    │ while(true) │◄────────────────────────────────────────┐
    └──────┬──────┘                                         │
           │                                                │
    ┌──────▼──────────────────────┐                         │
    │ 上下文准备                   │                         │
    │ snip→microcompact→collapse  │                         │
    │ →autocompact                │                         │
    └──────┬──────────────────────┘                         │
           │                                                │
    ┌──────▼──────────────────────┐                         │
    │ 调用模型（流式）              │                         │
    │ ┌─────────────────────────┐ │                         │
    │ │ [thinking] 内部推理      │ │ ← Reasoning            │
    │ │ [tool_use] 工具调用声明  │ │ ← Action 声明          │
    │ │ [text] 面向用户的文本    │ │                         │
    │ └─────────────────────────┘ │                         │
    │   ↓ 流式工具执行开始         │                         │
    └──────┬──────────────────────┘                         │
           │                                                │
    ┌──────▼──────────────────────┐                         │
    │ 有 tool_use?                │                         │
    ├──── NO ─────────────┐       │                         │
    │                     │       │                         │
    │  ┌──────────────────▼──┐    │                         │
    │  │ 终止决策             │    │                         │
    │  │ • 413 恢复?         │    │                         │
    │  │ • 截断恢复?         │────┼── continue ─────────────┤
    │  │ • stop hook?       │    │  (各种恢复路径)           │
    │  │ • token budget?    │    │                         │
    │  │ → return 终止       │    │                         │
    │  └─────────────────────┘    │                         │
    │                             │                         │
    ├──── YES ────────────────────┘                         │
    │                                                       │
    ┌──────▼──────────────────────┐                         │
    │ 工具执行                     │                         │
    │ ┌─────────────────────────┐ │                         │
    │ │ 并发安全? → 并行执行     │ │ ← Acting               │
    │ │ 非并发?   → 串行执行     │ │                         │
    │ └─────────────────────────┘ │                         │
    │ → tool_results              │ ← Observation           │
    └──────┬──────────────────────┘                         │
           │                                                │
    ┌──────▼──────────────────────┐                         │
    │ 收集附件（记忆/技能/通知）    │                         │
    │ 检查 maxTurns               │                         │
    │ state = next                │                         │
    └──────┬──────────────────────┘                         │
           │                                                │
           └────────────────────────────────────────────────┘
```

---

## 8. 总结

Claude Code 的 ReAct 实现是目前已知的**工程复杂度最高的 Reasoning + Acting 系统之一**，其核心创新点包括：

1. **五层上下文管理**：从 tool result budget 到 autocompact，确保在有限窗口中维持长程推理连贯性
2. **流水线化工具执行**：`StreamingToolExecutor` 在模型输出期间就开始执行工具，显著降低端到端延迟
3. **智能并发控制**：只读工具并行、写入工具串行、批间错误隔离（siblingAbortController）
4. **弹性终止决策**：8 种以上的 continue/terminate 路径，包括截断恢复、上下文压缩重试、stop hook 阻止等
5. **交错思维**：thinking block 和 tool_use block 可在同一轮中交错，突破传统 ReAct 的"一次一步"限制
6. **递归嵌套**：通过 AgentTool 实现嵌套 ReAct 循环，支持子任务委托
7. **隐式 ReAct 引导**：system prompt 中不写 "think step by step"，而是通过具体的行为规范引导模型遵循 ReAct 模式

---

## 附录：关键源码文件索引


| 文件路径                                          | 核心内容                          |
| --------------------------------------------- | ----------------------------- |
| `src/query.ts`                                | ReAct 主循环 `queryLoop`，~1730 行 |
| `src/services/api/claude.ts`                  | API 调用层，thinking 配置注入         |
| `src/services/tools/toolOrchestration.ts`     | 工具编排（并行/串行分区）                 |
| `src/services/tools/toolExecution.ts`         | 单工具执行逻辑、权限检查                  |
| `src/services/tools/StreamingToolExecutor.ts` | 流式工具执行器                       |
| `src/Tool.ts`                                 | Tool 类型定义、ToolUseContext      |
| `src/constants/prompts.ts`                    | System Prompt 构建              |
| `src/services/compact/autoCompact.ts`         | 自动上下文压缩                       |
| `src/services/compact/reactiveCompact.ts`     | 响应式压缩（413 恢复）                 |
| `src/services/compact/microCompact.ts`        | 工具结果微压缩                       |
| `src/tools/AgentTool/runAgent.ts`             | 子 Agent 执行（递归 ReAct）          |
| `src/utils/thinking.ts`                       | ThinkingConfig、Ultrathink     |
| `src/utils/effort.ts`                         | Effort Level 推理深度控制           |
| `src/utils/attachments.ts`                    | 附件收集（记忆、技能发现等）                |


