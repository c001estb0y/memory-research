# Claude Code AgentLoop、工具调用与提示词设计

基于 Claude Code 开源快照源码的深度分析。

---

## 一、整体架构概览

Claude Code 是 Anthropic 官方的 CLI 智能编程助手，核心架构是一个「用户输入 → 模型推理 → 工具执行 → 再推理」的循环式 Agent 系统。

核心模块关系如下：

- **入口层**：`entrypoints/cli.tsx` → `main.tsx` → `replLauncher.tsx`
- **用户输入处理**：`processUserInput/` 模块（区分文本、斜杠命令、bash 命令）
- **Agent 主循环**：`query.ts` 中的 `queryLoop` 函数
- **工具编排执行**：`services/tools/toolOrchestration.ts` + `toolExecution.ts`
- **系统提示词生成**：`constants/prompts.ts` + `utils/systemPrompt.ts`
- **上下文压缩**：`services/compact/` 模块（autoCompact、reactiveCompact、snipCompact）
- **子 Agent**：`tools/AgentTool/runAgent.ts`

---

## 二、AgentLoop 主循环设计

### 2.1 核心入口

主循环实现在 `src/query.ts` 中，对外暴露的是 `query()` 异步生成器函数，内部委托给 `queryLoop()`。

```typescript
export async function* query(params: QueryParams): AsyncGenerator<...> {
  const terminal = yield* queryLoop(params, consumedCommandUuids)
  return terminal
}
```

`QueryParams` 定义了主循环需要的全部输入：

- `messages` — 当前对话消息历史
- `systemPrompt` — 系统提示词（字符串数组）
- `userContext` / `systemContext` — 拼装到提示词中的动态上下文
- `canUseTool` — 工具权限判定回调
- `toolUseContext` — 工具执行上下文（包含工具池、模型配置、MCP 客户端等）
- `maxTurns` — 最大轮次限制
- `taskBudget` — token 预算控制

### 2.2 循环状态机

`queryLoop` 内部用一个 `while (true)` 无限循环实现多轮推理。每轮迭代的可变状态封装在 `State` 对象中：

```typescript
type State = {
  messages: Message[]               // 当前消息历史
  toolUseContext: ToolUseContext     // 工具执行上下文
  autoCompactTracking               // 自动压缩追踪状态
  maxOutputTokensRecoveryCount      // 输出 token 超限恢复计数
  hasAttemptedReactiveCompact       // 是否已尝试过响应式压缩
  turnCount: number                 // 当前轮次
  pendingToolUseSummary             // 待处理的工具使用摘要
  stopHookActive: boolean           // 停止钩子是否激活
  transition: Continue | undefined  // 上一次迭代为何继续（用于调试/测试）
}
```

### 2.3 单轮迭代流程

每一轮迭代的完整流程如下：

**阶段一：预处理与压缩**

1. **Snip Compact** — 对历史消息做裁剪式压缩（移除较早的低价值内容）
2. **Microcompact** — 细粒度压缩（如缓存编辑式压缩，裁减旧的 tool result）
3. **Context Collapse** — 上下文折叠（将已归档的消息用摘要替代）
4. **AutoCompact** — 当上下文接近窗口限制时自动触发全量摘要压缩

**阶段二：调用模型**

5. 拼装完整系统提示词（静态前缀 + 动态段落）
6. 将 userContext 注入到消息前缀
7. 发起流式 API 请求（`deps.callModel`），接收模型输出
8. 流式消费 assistant 消息：
   - 收集 `tool_use` 块到 `toolUseBlocks`
   - 如果启用了流式工具执行（`StreamingToolExecutor`），边流式接收边提交工具任务

**阶段三：工具执行**

9. 如果模型输出包含 `tool_use`（`needsFollowUp = true`），执行工具：
   - 使用 `StreamingToolExecutor.getRemainingResults()` 或 `runTools()` 执行
   - 收集工具结果到 `toolResults`
10. 收集附件消息（文件变更通知、队列命令、内存预取结果等）

**阶段四：决定是否继续**

11. 如果没有 tool_use → 进入终止判断
    - 检查 prompt-too-long 错误 → 尝试 reactive compact 恢复
    - 检查 max_output_tokens 超限 → 注入恢复消息重试（最多 3 次）
    - 执行 stop hooks → 可能阻断或继续
    - 检查 token budget → 可能注入 nudge 消息继续
    - 以上都不满足 → 返回 `{ reason: 'completed' }`
12. 如果有 tool_use → 组装下一轮状态，continue 进入下一次迭代
    - 检查 maxTurns 限制
    - 更新 `state.messages = [...messagesForQuery, ...assistantMessages, ...toolResults]`
    - `state.turnCount++`

### 2.4 终止条件

主循环终止的场景包括：

| 终止原因 | 触发条件 |
|---------|---------|
| `completed` | 模型正常结束（无 tool_use），且 stop hook 未阻断 |
| `aborted_streaming` | 用户在模型流式输出时中断 |
| `aborted_tools` | 用户在工具执行时中断 |
| `max_turns` | 达到最大轮次限制 |
| `blocking_limit` | 上下文 token 达到硬限制 |
| `prompt_too_long` | API 返回 prompt-too-long 且恢复失败 |
| `model_error` | API 调用异常 |
| `hook_stopped` | Hook 阻止继续 |
| `stop_hook_prevented` | Stop hook 阻止继续 |
| `image_error` | 图片大小/格式错误 |

### 2.5 上下文压缩机制

Claude Code 实现了多层上下文压缩策略，保障无限上下文对话能力：

- **Snip Compact**：裁剪早期低价值消息（系统提示中告知用户「对话可通过自动摘要无限进行」）
- **Microcompact / Cached Microcompact**：清除旧的 tool_result 内容，只保留最近 N 条的完整结果
- **Context Collapse**：将多轮交互折叠为摘要，保留读时投影的粒度
- **AutoCompact**：当 token 数接近阈值时调用独立的 compact agent 生成对话摘要
- **Reactive Compact**：在 API 返回 413（prompt-too-long）后紧急压缩再重试

各压缩层按顺序执行，互不互斥。

---

## 三、工具调用机制

### 3.1 Tool 类型定义

每个工具是一个实现了 `Tool` 接口的对象，核心方法包括：

| 方法 | 说明 |
|------|------|
| `name` | 工具名称，模型可见 |
| `inputSchema` | Zod schema 定义的输入参数校验 |
| `description()` | 动态生成工具描述（传给 API） |
| `prompt()` | 工具的系统提示词补充（如何使用此工具的指导） |
| `call()` | 实际执行逻辑 |
| `isConcurrencySafe()` | 是否可并发执行 |
| `isReadOnly()` | 是否为只读操作 |
| `checkPermissions()` | 工具级权限检查 |
| `validateInput()` | 输入值校验（Schema 之外的业务校验） |
| `mapToolResultToToolResultBlockParam()` | 将工具结果映射为 API 格式 |

使用 `buildTool()` 工厂函数统一构建，提供安全的默认值。

### 3.2 工具清单

Claude Code 内置的全部工具如下：

**文件操作类**

- `FileReadTool` — 读取文件内容
- `FileWriteTool` — 创建/覆写文件
- `FileEditTool` — 精确编辑文件（查找替换）
- `NotebookEditTool` — Jupyter Notebook 编辑
- `GlobTool` — 按模式搜索文件名
- `GrepTool` — 搜索文件内容（基于 ripgrep）

**Shell 执行类**

- `BashTool` — 执行 bash 命令
- `PowerShellTool` — 执行 PowerShell 命令（Windows）

**Agent 与任务类**

- `AgentTool` — 启动子 Agent（支持 fork 模式和多种 subagent_type）
- `TaskCreateTool` / `TaskGetTool` / `TaskListTool` / `TaskUpdateTool` / `TaskStopTool` / `TaskOutputTool` — 后台任务管理
- `TodoWriteTool` — 创建和管理 TODO 列表

**MCP 相关**

- `MCPTool` — 调用 MCP 服务器工具
- `ListMcpResourcesTool` — 列出 MCP 资源
- `ReadMcpResourceTool` — 读取 MCP 资源
- `McpAuthTool` — MCP 服务器认证

**交互与信息类**

- `AskUserQuestionTool` — 向用户提问
- `WebSearchTool` — 网络搜索
- `WebFetchTool` — 获取网页内容
- `SkillTool` — 执行技能
- `ToolSearchTool` — 搜索可用工具（延迟加载时使用）
- `SendMessageTool` — 发送消息
- `SleepTool` — 休眠等待

**模式切换类**

- `EnterPlanModeTool` / `ExitPlanModeTool` — 进入/退出计划模式
- `EnterWorktreeTool` / `ExitWorktreeTool` — 进入/退出 Git Worktree

**其他**

- `ConfigTool` — 配置管理
- `BriefTool` — 简短交互
- `SyntheticOutputTool` — 合成输出
- `TeamCreateTool` / `TeamDeleteTool` — 团队管理
- `REPLTool` — REPL 模式工具
- `LSPTool` — Language Server Protocol 交互
- `RemoteTriggerTool` — 远程触发
- `ScheduleCronTool` — 定时任务

### 3.3 工具编排：并发 vs 串行

工具编排逻辑在 `toolOrchestration.ts` 中：

1. **分批**（`partitionToolCalls`）：将模型输出的多个 tool_use 按顺序分批
   - 连续的并发安全工具归入同一批（`isConcurrencySafe: true`）
   - 非并发安全工具单独一批（`isConcurrencySafe: false`）
2. **执行**：
   - 并发安全批 → `runToolsConcurrently()`，最多 10 个并行（可通过 `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` 配置）
   - 非并发安全批 → `runToolsSerially()`，逐个执行
3. **上下文更新**：
   - 串行工具执行后立即应用 `contextModifier`
   - 并发工具执行完后按原始顺序批量应用 `contextModifier`

### 3.4 流式工具执行

`StreamingToolExecutor` 实现了边接收模型输出边执行工具的能力：

- 模型流式返回 `tool_use` 块时，立即通过 `addTool()` 加入队列
- 并发安全的工具在流式阶段就开始执行
- 非并发安全的工具等待排他执行
- 如果某个 Bash 工具执行出错，会通过 `siblingAbortController` 中止其他同级工具
- 流式结束后通过 `getRemainingResults()` 收集剩余结果

### 3.5 工具执行生命周期

单个工具的执行流程（`toolExecution.ts`）：

```
1. 查找工具定义（findToolByName）
2. 检查 abort 信号
3. Zod Schema 校验输入
4. validateInput() — 业务级输入校验
5. 运行 PreToolUse hooks
6. 权限检查（规则匹配 + 分类器 + 用户确认）
7. tool.call() — 实际执行
8. 运行 PostToolUse hooks
9. 生成 tool_result 消息返回给模型
```

权限判定的优先级链：

- Hook 决策（PreToolUse hook 可直接 allow/deny）
- 规则匹配（alwaysAllow / alwaysDeny / alwaysAsk 规则）
- 权限模式（default / auto / plan）
- 分类器检查（auto 模式下的安全分类器）
- 用户交互确认

---

## 四、提示词设计

### 4.1 系统提示词结构

系统提示词通过 `getSystemPrompt()` 生成，返回一个字符串数组，每个元素是一个独立的段落。整体分为两大区域：

**静态区域（可全局缓存）**

1. **Intro** — 身份介绍 + 安全约束
2. **System** — 基本行为规范（Markdown 格式、权限模式说明、Hook 说明等）
3. **Doing Tasks** — 任务执行规范（代码风格、最小复杂度原则等）
4. **Actions** — 操作安全性约束（可逆性评估、确认机制等）
5. **Using Your Tools** — 工具使用规范（优先使用专用工具、并行调用指导等）
6. **Tone and Style** — 风格约束（不使用 emoji、引用格式等）
7. **Output Efficiency** — 输出效率约束（简洁直接、避免赘述）

**动态区域（每次请求可能变化）**

8. **Session Guidance** — 会话特定指导（AskUserQuestion 工具说明、AgentTool 用法等）
9. **Memory** — 持久化记忆（CLAUDE.md 内容加载）
10. **Environment** — 运行环境信息（CWD、平台、Shell、模型名称、知识截止日期等）
11. **Language** — 语言偏好设置
12. **Output Style** — 自定义输出风格
13. **MCP Instructions** — MCP 服务器使用说明
14. **Scratchpad** — 临时文件目录说明
15. **Function Result Clearing** — 旧 tool_result 自动清理说明
16. **Summarize Tool Results** — 要求记住重要工具结果

两个区域之间用 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 标记分隔，用于 prompt cache 优化：边界前的内容可跨组织缓存（scope: global），边界后包含用户/会话特定内容。

### 4.2 关键提示词段落解析

**Intro 段落**

```
You are an interactive agent that helps users with software engineering tasks.

IMPORTANT: You must NEVER generate or guess URLs for the user unless you are
confident that the URLs are for helping the user with programming.
```

定义了 Agent 身份和基本安全约束。

**代码风格约束（Doing Tasks 中的核心规则）**

- 不做超出要求的功能添加、重构或"改进"
- 不添加不可能发生的场景的错误处理
- 不为一次性操作创建 helper/utility/abstraction
- 默认不写注释，只在 WHY 不明显时添加
- 完成前验证工作实际可用

**操作安全性约束（Actions 段落）**

系统提示词中对操作安全性有非常详细的指导：

- 评估操作的可逆性和影响范围
- 可自由执行本地可逆操作（编辑文件、运行测试）
- 高风险/不可逆操作必须确认（删除文件/分支、force push、发消息到外部服务等）
- 遇到障碍不使用破坏性手段绕过
- 发现未知状态先调查再操作

**工具使用规范（Using Your Tools 段落）**

- 优先使用专用工具而非 Bash（Read 替代 cat、Edit 替代 sed、Glob 替代 find 等）
- 使用 TodoWriteTool 分解和追踪任务进度
- 无依赖关系的工具调用应该并行执行
- 有依赖关系的工具调用必须串行执行

### 4.3 子 Agent 的提示词

子 Agent 使用的默认系统提示词：

```typescript
export const DEFAULT_AGENT_PROMPT =
  `You are an agent for Claude Code, Anthropic's official CLI for Claude.
   Given the user's message, you should use the tools available to complete the task.
   Complete the task fully—don't gold-plate, but don't leave it half-done.
   When you complete the task, respond with a concise report covering what was done
   and any key findings — the caller will relay this to the user, so it only needs
   the essentials.`
```

子 Agent 的提示词通过 `enhanceSystemPromptWithEnvDetails()` 增强，添加：

- 环境信息（CWD、Git 状态、平台等）
- 子 Agent 专属约束（使用绝对路径、最终响应包含相关文件路径等）

### 4.4 系统提示词优先级

`buildEffectiveSystemPrompt()` 实现了提示词的优先级合并：

1. **Override system prompt** — 最高优先级，完全替换（如 loop mode）
2. **Coordinator system prompt** — 协调模式的专用提示词
3. **Agent system prompt** — 自定义 Agent 的提示词
   - Proactive 模式下：追加到默认提示词后
   - 普通模式下：替换默认提示词
4. **Custom system prompt** — 通过 `--system-prompt` 指定
5. **Default system prompt** — 标准 Claude Code 提示词

`appendSystemPrompt` 始终追加到最后（override 模式除外）。

### 4.5 上下文注入

除了系统提示词，还有两个动态上下文被注入到请求中：

- **userContext** — 通过 `prependUserContext()` 注入到用户消息的前缀位置
- **systemContext** — 通过 `appendSystemContext()` 追加到系统提示词末尾

### 4.6 Attachment 机制

每轮工具执行后，系统还会注入 attachment 消息：

- **文件变更通知** — 编辑过的文件的 diff 信息
- **队列命令** — 用户排队的消息和通知
- **内存预取** — 相关 CLAUDE.md 记忆文件
- **技能发现** — 与当前任务相关的 Skill 推荐

---

## 五、错误恢复与弹性设计

### 5.1 模型 Fallback

当主模型不可用时自动切换到 fallback 模型：

- 通过 `FallbackTriggeredError` 捕获
- 清除已有的 assistant 消息（发送 tombstone）
- 切换模型后重试整个请求

### 5.2 max_output_tokens 恢复

当模型输出被截断时：

1. 首先尝试 escalate（从 8K 默认提升到 64K）
2. 如果 64K 仍被截断，注入恢复消息要求模型继续
3. 恢复消息内容：「Output token limit hit. Resume directly — no apology, no recap...」
4. 最多重试 3 次

### 5.3 prompt-too-long 恢复

当上下文超过模型窗口时：

1. 先尝试 Context Collapse drain（释放已暂存的折叠）
2. 再尝试 Reactive Compact（紧急压缩）
3. 都失败则返回错误给用户

### 5.4 流式中断处理

用户随时可通过 Ctrl+C 中断：

- 如果在流式阶段中断 → 为所有未完成的 tool_use 生成 error 的 tool_result
- 如果在工具执行阶段中断 → StreamingToolExecutor 生成合成的中止结果
- 区分 submit-interrupt（用户提交新消息）和 cancel-interrupt（用户取消）

---

## 六、性能优化设计

### 6.1 Prompt Cache

系统提示词分为 static 和 dynamic 两部分，static 部分使用 `cacheScope: 'global'` 实现跨请求缓存，避免重复计算和传输。

### 6.2 预取机制

多项预取并行执行以降低延迟：

- **内存预取**（`startRelevantMemoryPrefetch`）—— 在循环入口启动，工具执行后消费
- **技能发现预取**（`startSkillDiscoveryPrefetch`）—— 每轮迭代启动
- **工具使用摘要**（`generateToolUseSummary`）—— 后台异步生成，下一轮消费

### 6.3 流式工具执行

通过 `StreamingToolExecutor` 实现模型输出和工具执行的重叠：模型还在输出后续内容时，已完成的 tool_use 就开始执行，大幅降低端到端延迟。

### 6.4 Tool Result Budget

对聚合的 tool result 大小施加预算限制（`applyToolResultBudget`），超大结果会被持久化到磁盘，只在上下文中保留摘要和文件路径。

---

## 七、核心文件索引

| 文件 | 职责 |
|------|------|
| `src/query.ts` | Agent 主循环（queryLoop），流式 API 调用，工具批次处理，错误恢复 |
| `src/QueryEngine.ts` | 查询编排层，处理用户输入到 query 的转换 |
| `src/Tool.ts` | Tool 类型定义、buildTool 工厂函数、ToolUseContext |
| `src/constants/prompts.ts` | 系统提示词各段落的生成函数 |
| `src/utils/systemPrompt.ts` | buildEffectiveSystemPrompt — 提示词优先级合并 |
| `src/services/tools/toolOrchestration.ts` | 工具并发/串行编排（runTools、partitionToolCalls） |
| `src/services/tools/toolExecution.ts` | 单个工具的完整执行流程（权限、Hook、执行、结果） |
| `src/services/tools/StreamingToolExecutor.ts` | 流式工具执行器 |
| `src/tools/AgentTool/runAgent.ts` | 子 Agent 启动与执行 |
| `src/utils/processUserInput/processUserInput.ts` | 用户输入预处理 |
| `src/context.ts` | 用户/系统上下文聚合 |
| `src/services/compact/autoCompact.ts` | 自动上下文压缩 |
| `src/query/stopHooks.ts` | Stop hook 处理逻辑 |
| `src/query/tokenBudget.ts` | Token 预算跟踪与自动继续 |
