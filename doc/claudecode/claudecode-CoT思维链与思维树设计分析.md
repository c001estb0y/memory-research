# Claude Code 中的 CoT 思维链与思维树设计分析

> 基于 Claude Code 源码（2026-03 快照）的深入分析，探讨其中与 Chain-of-Thought (CoT)、Tree-of-Thought (ToT) 及相关推理增强机制的设计模式。

## 1. 总体结论

Claude Code **不包含经典的 CoT prompt 注入或 ToT 树搜索算法**，但在多个层面实现了**功能等价的思维增强机制**：

| 维度 | 经典 CoT/ToT 定义 | Claude Code 的对应实现 |
|------|-------------------|----------------------|
| 逐步推理引导 | "Let's think step by step" | Extended Thinking + Adaptive Thinking |
| 思维预算控制 | 无 | `budget_tokens` / Effort Level 系统 |
| 多路径探索 | ToT 树搜索 | Plan Mode V2 多 Agent 并行规划 |
| 反思与验证 | Self-Reflection | Verification Agent 对抗性审计 |
| 推理深度自适应 | 无 | Adaptive Thinking + Ultrathink 触发词 |
| 任务分解 | CoT 分步 | Agent Loop + Tool-Use 驱动的多轮迭代 |

---

## 2. Extended Thinking —— API 级别的 CoT 实现

### 2.1 ThinkingConfig 类型系统

Claude Code 在 `src/utils/thinking.ts` 中定义了三种思维模式：

```typescript
// src/utils/thinking.ts:10-13
export type ThinkingConfig =
  | { type: 'adaptive' }
  | { type: 'enabled'; budgetTokens: number }
  | { type: 'disabled' }
```

- **`adaptive`**：模型自主决定思考深度（Claude 4.6+ 支持），类似 "动态 CoT"
- **`enabled` + `budgetTokens`**：显式分配思维 token 预算，精确控制推理开销
- **`disabled`**：不思考，直接响应

### 2.2 API 层的思维参数注入

在 `src/services/api/claude.ts` 的核心 API 调用逻辑中（约 L1596-1629），思维配置被转换为实际的 API 参数：

```typescript
// src/services/api/claude.ts 核心逻辑（简化）
const hasThinking =
  thinkingConfig.type !== 'disabled' &&
  !isEnvTruthy(process.env.CLAUDE_CODE_DISABLE_THINKING)

if (hasThinking) {
  if (modelSupportsAdaptiveThinking(options.model)) {
    // 支持自适应思维的模型 → 让模型自主分配思考深度
    thinking = { type: 'adaptive' }
  } else {
    // 不支持自适应的模型 → 显式设置 token 预算
    let thinkingBudget = getMaxThinkingTokensForModel(options.model)
    if (thinkingConfig.type === 'enabled' && thinkingConfig.budgetTokens !== undefined) {
      thinkingBudget = thinkingConfig.budgetTokens
    }
    thinkingBudget = Math.min(maxOutputTokens - 1, thinkingBudget)
    thinking = { budget_tokens: thinkingBudget, type: 'enabled' }
  }
}
```

**关键设计决策**：
- Adaptive Thinking **不设 budget**，完全由模型内部机制控制
- 显式预算模式会做 `Math.min(maxOutputTokens - 1, thinkingBudget)` 的安全钳位
- 环境变量 `CLAUDE_CODE_DISABLE_THINKING` 可全局禁用

### 2.3 Interleaved Thinking（交错思维）

Beta header `interleaved-thinking-2025-05-14` 表明 Claude Code 支持**交错思维**——模型可以在**同一条 assistant 消息内**的多个 tool_use 之间持续产出 thinking block，而非仅在该消息的 content 数组开头思考一次。这比经典 CoT 更灵活：

```typescript
// src/constants/betas.ts:4-5
export const INTERLEAVED_THINKING_BETA_HEADER =
  'interleaved-thinking-2025-05-14'
```

#### 2.3.1 非交错 vs 交错思维的差异

**不启用 interleaved-thinking 时**，模型只能在响应最开头产出一个 thinking block，之后输出 text 和 tool_use：

```
assistant content: [thinking] → [text] → [tool_use]
                    ↑ 只在这里思考一次
```

**启用 interleaved-thinking 后**，模型可以在同一轮响应中交错产出多个 thinking block，穿插在 tool_use 之间：

```
assistant content: [thinking₁] → [tool_use₁] → [thinking₂] → [tool_use₂] → [text]
                    ↑ 思考        ↑ 行动        ↑ 再思考       ↑ 再行动      ↑ 回复
```

#### 2.3.2 API 请求层：如何启用

在 `src/services/api/claude.ts` 的 API 调用逻辑中，interleaved-thinking 作为 beta header 被注入请求：

```json
{
  "model": "claude-sonnet-4-20250514",
  "messages": [...],
  "tools": [...],
  "max_tokens": 16384,
  "thinking": { "type": "enabled", "budget_tokens": 10000 },
  "betas": ["interleaved-thinking-2025-05-14", "prompt-caching-2024-07-31"]
}
```

`thinking` 参数和 `betas` 数组协同生效——`thinking` 决定"是否启用思维及预算"，`betas` 中的 `interleaved-thinking` 决定"思维 block 是否可以交错出现"。

#### 2.3.3 SSE 流式传输中的交错实例

以用户请求"把 src/app.ts 里的 console.log 都换成 logger.info"为例，API 返回的 SSE 流实际是这样的：

```
event: message_start
data: {"type":"message_start","message":{"role":"assistant",...}}

# ──── 第 1 个 thinking block ────
event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"用户要替换 console.log，我需要先读取文件内容确认有哪些位置..."}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

# ──── 第 1 个 tool_use（基于第 1 次思考的行动）────
event: content_block_start
data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_read1","name":"Read","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"file_path\":\"src/app.ts\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

# ──── 第 2 个 thinking block（读到参数后的进一步思考）────
event: content_block_start
data: {"type":"content_block_start","index":2,"content_block":{"type":"thinking","thinking":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":2,"delta":{"type":"thinking_delta","thinking":"同时我应该在项目中搜索是否有 logger 工具已经引入，避免替换后缺少 import..."}}

event: content_block_stop
data: {"type":"content_block_stop","index":2}

# ──── 第 2 个 tool_use（基于第 2 次思考的行动）────
event: content_block_start
data: {"type":"content_block_start","index":3,"content_block":{"type":"tool_use","id":"toolu_grep1","name":"Grep","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":3,"delta":{"type":"input_json_delta","partial_json":"{\"pattern\":\"import.*logger\",\"path\":\"src/\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":3}

# ──── text block（面向用户的输出）────
event: content_block_start
data: {"type":"content_block_start","index":4,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":4,"delta":{"type":"text_delta","text":"让我先读取文件内容并检查 logger 的引入情况。"}}

event: content_block_stop
data: {"type":"content_block_stop","index":4}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}
```

最终解析出的 assistant content 数组结构为：

```json
{
  "role": "assistant",
  "content": [
    { "type": "thinking", "thinking": "用户要替换 console.log，我需要先读取文件..." },
    { "type": "tool_use", "id": "toolu_read1", "name": "Read",
      "input": { "file_path": "src/app.ts" } },
    { "type": "thinking", "thinking": "同时我应该搜索是否有 logger 已经引入..." },
    { "type": "tool_use", "id": "toolu_grep1", "name": "Grep",
      "input": { "pattern": "import.*logger", "path": "src/" } },
    { "type": "text", "text": "让我先读取文件内容并检查 logger 的引入情况。" }
  ],
  "stop_reason": "tool_use"
}
```

#### 2.3.4 Agent Loop 中的处理：StreamingToolExecutor

交错思维的工程价值在于**流式工具执行**——`StreamingToolExecutor` 不需要等整个响应结束，在 tool_use block 的参数流式到达时就可以启动工具执行：

```typescript
// src/query.ts:561-568
let streamingToolExecutor = useStreamingToolExecution
  ? new StreamingToolExecutor(toolUseContext.options.tools, canUseTool, toolUseContext)
  : null
```

实际的并行时序如下：

```
模型流式输出: [thinking₁]...[tool_use₁ Read]...
                                   ↓
                        Read 工具立即开始执行（不等后续 block）
模型继续输出:                    ...[thinking₂]...[tool_use₂ Grep]...
                                                        ↓
                                              Grep 工具立即开始执行
模型继续输出:                                        ...[text]...
                                                        ↓
                                                   响应结束
```

这意味着 thinking block 充当了"边思考边行动"的中间推理层——模型在发出一个工具调用后，可以在工具还没返回结果的情况下，继续思考并发出第二个工具调用。

#### 2.3.5 与非交错模式的对比

| 维度 | 非交错（传统） | 交错思维（Interleaved） |
|------|--------------|----------------------|
| thinking block 数量 | 每轮响应最多 1 个 | 每轮可多个，穿插在 tool_use 之间 |
| 推理时机 | 仅在响应开头 | 每个工具调用前都可以推理 |
| 工具调用规划 | 一次性规划所有工具调用 | 边思考边规划，逐步发出 |
| 流式执行收益 | 有限（所有 tool_use 集中在后半段） | 显著（tool_use 分散，可更早开始执行） |
| 对应 ReAct 模式 | 简化 ReAct：一个 Thought + 批量 Action | 完整 ReAct：Thought₁→Action₁→Thought₂→Action₂ |

#### 2.3.6 关键意义

交错思维将经典 CoT 从"一次性思考完毕再行动"升级为"持续思考与行动的交替循环"。在 Agent 场景中，这更接近人类解决问题的真实方式——先想一步，做一步，看到结果后再想下一步。配合 `StreamingToolExecutor` 的流式工具执行，交错思维还带来了显著的延迟优化：模型不需要想完所有事情才开始执行第一个工具。

### 2.4 Redacted Thinking（脱敏思维）

```typescript
// src/constants/betas.ts:20
export const REDACT_THINKING_BETA_HEADER = 'redact-thinking-2026-02-12'
```

对应 UI 组件 `AssistantRedactedThinkingMessage`（显示 `✻ Thinking…`）和 `AssistantThinkingMessage`（显示 `∴ Thinking` 并可展开完整内容）。这表明系统区分了用户可见的思维和仅用于上下文传递的内部思维。

---

## 3. Effort 系统 —— 推理深度的精细控制

### 3.1 四级 Effort 分层

`src/utils/effort.ts` 定义了一套精细的"努力程度"控制系统：

```typescript
export const EFFORT_LEVELS = ['low', 'medium', 'high', 'max'] as const
```

每个级别对应不同的推理策略：

| Effort | 描述 | 适用场景 |
|--------|------|----------|
| `low` | 快速直接，最小开销 | 简单任务 |
| `medium` | 平衡的标准实现 | 日常开发 |
| `high` | 全面实现+测试+文档 | 复杂功能 |
| `max` | 最深推理（仅 Opus 4.6） | 最高难度问题 |

### 3.2 Effort 与 Thinking 的联动

Effort 系统不是独立于 Thinking 的——它们共同决定模型的推理行为：

- `max` effort → 最大 thinking budget + 最高推理深度
- `medium` effort → 适度 thinking budget（Opus 4.6 默认为 medium，配合 ultrathink 可按需触发 high）
- 环境变量 `MAX_THINKING_TOKENS` 可直接覆盖预算

### 3.3 Ultrathink —— 用户触发的深度思考

```typescript
// src/utils/thinking.ts:19-24
export function isUltrathinkEnabled(): boolean {
  if (!feature('ULTRATHINK')) return false
  return getFeatureValue_CACHED_MAY_BE_STALE('tengu_turtle_carbon', true)
}

export function hasUltrathinkKeyword(text: string): boolean {
  return /\bultrathink\b/i.test(text)
}
```

当用户在输入中包含 `ultrathink` 关键词时，系统会将 effort 从默认 `medium` 提升到 `high`，触发更深层次的思考。这是一种**用户驱动的 CoT 深度调节**机制。

UI 层面，ultrathink 关键词会被**彩虹色高亮渲染**（`getRainbowColor` 函数），给用户明确的视觉反馈。

---

## 4. Plan Mode V2 —— 类 ToT 的多路径规划

### 4.1 多 Agent 并行探索

Plan Mode V2 实现了一种类似 Tree-of-Thought 的**多视角并行规划**机制：

```typescript
// src/utils/planModeV2.ts:5-29
export function getPlanModeV2AgentCount(): number {
  // Max/Team/Enterprise 用户可启动最多 3 个并行规划 Agent
  if (subscriptionType === 'max' && rateLimitTier === 'default_claude_max_20x') return 3
  if (subscriptionType === 'enterprise' || subscriptionType === 'team') return 3
  return 1
}

export function getPlanModeV2ExploreAgentCount(): number {
  // 探索 Agent 固定 3 个并行
  return 3
}
```

这与 Tree-of-Thought 的核心思想高度相似：
- **多个 Agent 同时探索代码库**（explore phase）
- **多个 Agent 从不同视角生成方案**（plan phase）
- **主 Agent 综合各方案做出决策**（selection/voting）

### 4.2 Plan Agent 的结构化推理

Plan Agent 的系统提示（`src/tools/AgentTool/built-in/planAgent.ts`）规定了清晰的四步推理流程：

```
1. Understand Requirements → 理解需求（类似 CoT 的问题分解）
2. Explore Thoroughly → 充分探索（类似 ToT 的搜索阶段）
3. Design Solution → 设计方案（类似 ToT 的方案生成）
4. Detail the Plan → 细化计划（类似 CoT 的分步展开）
```

### 4.3 Interview Phase（访谈阶段）

```typescript
// src/utils/planModeV2.ts:50
export function isPlanModeInterviewPhaseEnabled(): boolean
```

Plan Mode V2 还包含一个**访谈阶段**，Agent 在规划前先向用户提问以澄清需求。这进一步增强了"先想清楚再行动"的 CoT 理念。

---

## 5. Verification Agent —— 对抗性反思验证

### 5.1 设计理念

`src/tools/AgentTool/built-in/verificationAgent.ts` 实现了一个**独立于主 Agent 的验证专家**，其核心理念接近 CoT 文献中的 **self-reflection** 和 **self-consistency**：

```typescript
const VERIFICATION_SYSTEM_PROMPT = `You are a verification specialist.
Your job is not to confirm the implementation works — it's to try to break it.

You have two documented failure patterns:
1. Verification avoidance: finding reasons not to run checks
2. Being seduced by the first 80%: missing subtle issues
`
```

### 5.2 反思机制的实现

Verification Agent 的系统提示中**显式列举了认知偏差并要求对抗**：

```
=== RECOGNIZE YOUR OWN RATIONALIZATIONS ===
- "The code looks correct based on my reading" → reading is not verification. Run it.
- "The implementer's tests already pass" → the implementer is an LLM. Verify independently.
- "This is probably fine" → probably is not verified. Run it.
- "Let me start the server and check the code" → no. Start the server and hit the endpoint.
```

这本质上是在 prompt 层面实现了**强制反思（forced reflection）**——让模型意识到自身可能的推理捷径，然后主动对抗。

### 5.3 VERDICT 输出格式

```
VERDICT: PASS / FAIL / PARTIAL
```

验证结果被结构化为三种明确判定，每个判定必须附带**实际运行的命令和输出证据**。这确保了反思不是空泛的文字游戏，而是有实证支撑的验证。

---

## 6. Agent Loop —— 循环推理框架

### 6.1 多轮迭代的 CoT 展开

`src/query.ts` 中的 `queryLoop` 函数实现了核心的推理循环：

```typescript
// src/query.ts（简化）
async function* queryLoop(params: QueryParams) {
  while (true) {
    // 1. 构建上下文 → 2. 调用模型（含 thinking）→ 3. 执行工具 → 4. 决定是否继续
    
    for await (const message of deps.callModel({
      messages: prependUserContext(messagesForQuery, userContext),
      systemPrompt: fullSystemPrompt,
      thinkingConfig: toolUseContext.options.thinkingConfig,
      tools: toolUseContext.options.tools,
      signal: toolUseContext.abortController.signal,
    })) { ... }
    
    // 如果有 tool_use → 执行工具，组装结果，continue 下一轮
    // 如果无 tool_use → 检查 token 预算，可能注入 nudge 消息继续
    // 如果达到 maxTurns → 终止
  }
}
```

这个循环实现了一种**隐式的 Chain-of-Thought**：
- 每轮迭代 = CoT 中的一个推理步骤
- tool_use = 外部观察（类似 ReAct 模式中的 Observation）
- thinking block = 内部推理（类似 ReAct 模式中的 Thought）
- nudge 消息 = 防止推理链过早终止

### 6.2 Nudge 机制 —— 延续推理链

当模型可能过早终止时，系统会注入 nudge 消息促使继续推理：

```typescript
// src/query.ts 约 L1324-1327
createUserMessage({
  content: decision.nudgeMessage,
  isMeta: true,
})
```

这等价于 CoT 中的 "continue reasoning" 提示。

### 6.3 MaxTurns —— 推理深度限制

```typescript
if (maxTurns && nextTurnCount > maxTurns) {
  yield createAttachmentMessage({
    type: 'max_turns_reached',
    maxTurns,
    turnCount: nextTurnCount,
  })
}
```

类似 ToT 中的搜索深度限制，防止推理循环无限展开。

---

## 7. SubAgent 层面的思维控制

### 7.1 Fork vs Regular SubAgent 的思维策略

`src/tools/AgentTool/runAgent.ts` 中对子 Agent 有精细的思维控制：

```typescript
// src/tools/AgentTool/runAgent.ts 约 L676-684
thinkingConfig: useExactTools
  ? toolUseContext.options.thinkingConfig  // fork 继承父级思维配置（为了 prompt cache 命中）
  : { type: 'disabled' as const },         // 常规子 Agent 禁用思维（节省 token）
```

**设计意图**：
- **Fork 子 Agent**（如 worktree 中的分支执行）继承父级的完整思维能力
- **常规子 Agent**（如 explore、verification）默认禁用 thinking，以减少 token 消耗
- 这是一种**树状推理中的选择性深思**——只在关键路径上启用完整推理

### 7.2 Coordinator Mode —— 分层编排

`src/coordinator/coordinatorMode.ts` 定义了一个更高层次的**编排模式**：

- 主 Agent 作为"协调者"，负责任务分解和分配
- 子 Agent 作为"工人"，独立执行具体任务
- 这种结构天然形成了一个**思维树**——根节点（coordinator）展开为多个子节点（worker agents）

---

## 8. Ultraplan —— 增强版规划关键词

### 8.1 关键词触发机制

`src/utils/ultraplan/keyword.ts` 实现了 `ultraplan` 关键词检测，类似 ultrathink：

```typescript
export function hasUltraplanKeyword(text: string): boolean {
  return findUltraplanTriggerPositions(text).length > 0
}
```

当用户输入 `ultraplan` 时，系统会启动增强版的规划流程（Plan Mode V2 的多 Agent 并行模式）。

### 8.2 触发词安全过滤

```typescript
// 排除以下场景的误触发：
// - 在引号/括号内："ultraplan"、(ultraplan)
// - 路径上下文：src/ultraplan/foo.ts、ultraplan.tsx
// - 命令参数：--ultraplan-mode
// - 问句：ultraplan?
// - 斜杠命令：/rename ultraplan foo
```

这套复杂的过滤逻辑说明 ultraplan 作为**用户主动触发更深推理**的入口，需要高精度识别。

---

## 9. 与经典 CoT/ToT 的对比分析

### 9.1 相似之处

| 经典 CoT/ToT | Claude Code 对应 |
|-------------|-----------------|
| "Let's think step by step" | Extended Thinking（`thinking` block） |
| 多路径搜索 | Plan Mode V2 多 Agent 并行 |
| 深度搜索 vs 广度搜索 | Effort Level（low→max） |
| 自我评估/投票 | Verification Agent VERDICT |
| 推理预算 | `budget_tokens` / `maxTurns` |
| ReAct (Thought-Action-Observation) | Agent Loop (Thinking-ToolUse-ToolResult) |

### 9.2 差异与创新

1. **非 prompt 注入式 CoT**：Claude Code 不在 system prompt 中写 "think step by step"——它依赖 API 原生的 `thinking` 参数，这比 prompt 级 CoT 更底层、更可控

2. **自适应深度**：经典 CoT 的推理深度是固定的（写多少步就多少步），Claude Code 的 Adaptive Thinking 让模型自己决定思考多深

3. **工具化的反思**：经典 self-reflection 是模型内部的文本推理；Claude Code 的 Verification Agent 是一个**独立的、有工具访问权限的**反思实体，可以实际运行代码来验证

4. **用户可控的推理触发**：通过 `ultrathink`、`ultraplan` 关键词和 `/effort` 命令，用户可以**实时调节**推理的深度和广度

5. **经济学视角的推理控制**：通过 Effort 系统和子 Agent 的 thinking 禁用策略，实现了推理成本的精细化管理——这是学术 CoT/ToT 论文通常不讨论的工程问题

---

## 10. 总结

Claude Code 实现了一个**工程化的、多层次的思维增强体系**，虽然没有直接搬用 CoT/ToT 的学术范式，但在以下五个层面实现了功能等价或超越：

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 5: 用户层 — ultrathink / ultraplan / /effort 触发    │
├─────────────────────────────────────────────────────────────┤
│  Layer 4: 编排层 — Coordinator Mode / Plan V2 多Agent并行   │
├─────────────────────────────────────────────────────────────┤
│  Layer 3: 验证层 — Verification Agent 对抗性反思            │
├─────────────────────────────────────────────────────────────┤
│  Layer 2: 循环层 — Agent Loop / nudge / maxTurns           │
├─────────────────────────────────────────────────────────────┤
│  Layer 1: API 层 — Extended Thinking / Adaptive / budget   │
└─────────────────────────────────────────────────────────────┘
```

这种分层设计的优势在于：
- **可组合性**：各层可独立启用/禁用
- **可观测性**：thinking block 可被记录、脱敏、展示
- **成本可控**：通过 Effort 和 SubAgent thinking 策略精细管理 token 消耗
- **用户赋权**：用户可通过关键词和命令主动控制推理深度

从工程实践角度看，Claude Code 证明了**CoT/ToT 的核心价值（深度推理、多路径探索、自我反思）可以在不依赖 prompt 注入的情况下，通过系统架构设计来实现**——这对构建类似的 AI Agent 系统有重要的参考意义。

---

## 附录：关键源码文件索引

| 文件路径 | 核心内容 |
|---------|---------|
| `src/utils/thinking.ts` | ThinkingConfig 类型、ultrathink 检测、模型支持判断 |
| `src/utils/effort.ts` | Effort Level 系统、默认值、模型适配 |
| `src/services/api/claude.ts` | API 层 thinking 参数注入、adaptive vs budget 选择 |
| `src/query.ts` | Agent Loop 主循环（queryLoop）、nudge、maxTurns |
| `src/tools/AgentTool/runAgent.ts` | SubAgent 思维控制策略 |
| `src/tools/AgentTool/built-in/verificationAgent.ts` | 对抗性验证 Agent |
| `src/tools/AgentTool/built-in/planAgent.ts` | Plan Agent 结构化推理 |
| `src/tools/EnterPlanModeTool/prompt.ts` | Plan Mode 触发逻辑与指导 |
| `src/utils/planModeV2.ts` | Plan V2 多 Agent 并行配置 |
| `src/utils/ultraplan/keyword.ts` | ultraplan 关键词触发检测 |
| `src/constants/betas.ts` | Beta headers（interleaved-thinking、redact-thinking 等） |
| `src/coordinator/coordinatorMode.ts` | Coordinator 编排模式 |
| `src/components/ThinkingToggle.tsx` | UI: 思维模式开关 |
| `src/components/messages/AssistantThinkingMessage.tsx` | UI: 思维内容展示 |
