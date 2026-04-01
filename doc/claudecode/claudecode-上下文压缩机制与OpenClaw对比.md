# Claude Code 上下文压缩机制与 OpenClaw 对比

基于 Claude Code 开源快照源码与 OpenClaw 源码的深度分析。

---

## 一、Claude Code 上下文窗口能力

### 1.1 窗口大小

Claude Code 的上下文窗口大小取决于所用模型，核心定义在 `src/utils/context.ts`：

- **默认窗口：200K token**（`MODEL_CONTEXT_WINDOW_DEFAULT = 200_000`）
- **1M 窗口**：通过以下方式启用
  - 模型名带 `[1m]` 后缀（显式 opt-in）
  - Beta header `CONTEXT_1M_BETA_HEADER`
  - Sonnet 4.6 的实验组 `coral_reef_sonnet`
  - 模型能力表 `max_input_tokens` 上报
- **环境变量覆盖**：`CLAUDE_CODE_MAX_CONTEXT_TOKENS`（仅 Anthropic 内部）

### 1.2 输出 token 上限

| 模型 | 默认输出 | 上限输出 |
|------|----------|----------|
| Opus 4.6 | 64K | 128K |
| Sonnet 4.6 | 32K | 128K |
| Opus 4.5 / Sonnet 4 / Haiku 4 | 32K | 64K |
| Claude 3.7 Sonnet | 32K | 64K |

Claude Code 还实现了一个 slot 节约优化：默认 cap 到 8K（`CAPPED_DEFAULT_MAX_TOKENS`），命中 max_output_tokens 后先 escalate 到 64K，再走多轮恢复。

### 1.3 有效上下文窗口

自动压缩的阈值计算基于「有效上下文窗口」，而非原始窗口：

```
有效窗口 = getContextWindowForModel(model) - min(maxOutputTokens, 20_000)
```

其中 20K 是 compact 摘要的输出预留空间（`MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000`，基于 p99.99 compact 输出 17,387 tokens）。

还可被 `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 环境变量进一步收窄。

---

## 二、Claude Code 多层压缩机制

Claude Code 实现了 **五层** 上下文压缩策略，按执行顺序排列：

### 2.1 Snip Compact（裁剪压缩）

**执行时机**：每轮迭代最先执行（在 microcompact 之前）

**策略**：从消息历史中裁剪早期低价值内容。细节实现在 `snipCompact.ts`（源码不在当前快照中）。

**核心行为**（从 `query.ts` 的调用点推断）：
- 受 `HISTORY_SNIP` feature flag 保护
- `snipCompactIfNeeded(messages)` 返回 `{ messages, tokensFreed, boundaryMessage }`
- `tokensFreed` 会传递给后续的自动压缩阈值计算（因为估算器读的是未裁剪前的 usage）
- 可产生 `boundaryMessage` 通知 UI

### 2.2 Microcompact（微压缩）

**执行时机**：在 Snip Compact 之后、AutoCompact 之前

**目标**：清除旧的 `tool_result` 内容，只保留最近 N 条的完整结果

**三条路径**：

**路径 A：时间型 Microcompact**

当距离上次 assistant 消息超过配置阈值（默认 60 分钟）时触发：

- 此时服务端 prompt cache 已过期（5 分钟 TTL）
- 直接将旧的 `tool_result` 内容替换为 `[Old tool result content cleared]`
- 保留最近 N 条完整结果（`keepRecent`，默认 5，最少 1）
- 只清理可压缩工具的结果：FileRead、Bash/PowerShell、Grep、Glob、WebSearch、WebFetch、FileEdit、FileWrite

**路径 B：缓存编辑型 Microcompact（Cached MC）**

利用 API 层的 `cache_edits` 能力，在不破坏 prompt cache 的情况下删除旧 tool_result：

- 不修改本地消息内容
- 通过 `cache_edits` 指令在 API 层删除，维护 `pinnedEdits` 供后续请求复现
- 有 `triggerThreshold` 和 `keepRecent` 配置
- 仅限主线程（防止 fork agent 污染全局状态）

**路径 C：无操作回退**

如果时间型和缓存编辑型都不触发，不做任何 microcompact，由后续的 autocompact 处理上下文压力。

### 2.3 Context Collapse（上下文折叠）

**执行时机**：在 Microcompact 之后、AutoCompact 之前

**策略**：将多轮交互折叠为摘要，但以「读时投影」方式实现——折叠存储在独立的 collapse store 中，不直接修改原始消息数组。

**核心行为**（从 `query.ts` 调用点推断）：
- 受 `CONTEXT_COLLAPSE` feature flag 保护
- `applyCollapsesIfNeeded(messages, toolUseContext, querySource)` — 投影折叠视图
- `recoverFromOverflow(messages, querySource)` — 作为 413 错误的第一道恢复手段
- 与 AutoCompact 互补：如果 collapse 已将 token 降到阈值以下，autocompact 不再触发

### 2.4 AutoCompact（自动全量压缩）

**执行时机**：在上述轻量压缩之后

**触发条件**：

```
tokenCountWithEstimation(messages) - snipTokensFreed >= autoCompactThreshold
```

其中：

```
autoCompactThreshold = 有效窗口 - 13_000
```

对于 200K 窗口的模型：`200_000 - 20_000 - 13_000 = 167_000` token 时触发。

**阈值体系**（200K 窗口为例）：

| 阈值 | 计算方式 | 大约值 | 用途 |
|------|----------|--------|------|
| 自动压缩触发线 | 有效窗口 - 13K | ~167K | 触发 autoCompact |
| 警告线 | 触发线 - 20K | ~147K | UI 显示警告 |
| 错误线 | 触发线 - 20K | ~147K | UI 显示错误 |
| 阻断线（auto off） | 有效窗口 - 3K | ~177K | 停止接受输入 |

**熔断机制**：连续失败 3 次后停止尝试（`MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES`）。

**压缩流程**：

1. 检查是否禁用（`DISABLE_COMPACT` / `DISABLE_AUTO_COMPACT`）
2. 检查连续失败次数（>= 3 则熔断）
3. 先尝试 **Session Memory Compaction**（轻量级，保留最近消息切片）
4. 否则执行 **Full Compaction**（全量对话摘要）

**Full Compaction 的详细过程**：

1. 用 `getCompactPrompt()` 构造摘要请求
2. 移除消息中的图片和技能发现类 attachment
3. 通过 fork 子代理（共享 prompt cache）或直接流式请求生成摘要
4. 摘要最大输出 20K token
5. 如果压缩请求本身遇到 prompt-too-long，按 API round 分组从最旧侧批量截断，最多重试 3 次
6. 模型输出 `<analysis>` + `<summary>` 格式
7. `formatCompactSummary()` 去掉 `<analysis>` 部分，将 `<summary>` 转为 `Summary:\n...`

**压缩后保留什么**：

- **CompactBoundaryMessage** — 压缩边界标记
- **摘要消息** — 一条用户消息（`isCompactSummary: true`），内容为结构化摘要
- **Post-compact 文件附件** — 最多 5 个最近读写的文件，每个最多 5K token，总预算 50K token
- **技能附件** — 每个最多 5K token，总预算 25K token
- **工具/MCP delta** — 延迟加载工具列表变更、Agent 列表变更、MCP 指令变更
- **SessionStart hooks** — 重新执行会话启动钩子

**压缩后丢弃什么**：所有压缩前的原始消息（用户消息、助理消息、工具调用/结果等）。

**摘要消息的内容格式**：

```
This session is being continued from a previous conversation that ran out 
of context. The summary below covers the earlier portion of the conversation.

Summary:
1. Primary Request and Intent: [详细描述]
2. Key Technical Concepts: [技术概念列表]
3. Files and Code Sections: [文件和代码片段]
4. Errors and fixes: [错误和修复]
5. Problem Solving: [问题解决]
6. All user messages: [所有用户消息]
7. Pending Tasks: [待处理任务]
8. Current Work: [当前工作]
9. Optional Next Step: [下一步]

If you need specific details from before compaction, read the full 
transcript at: [transcript_path]

Continue the conversation from where it left off without asking the user 
any further questions. Resume directly — do not acknowledge the summary...
```

### 2.5 Reactive Compact（响应式压缩）

**执行时机**：API 返回 prompt-too-long (413) 错误后

**策略**：这是最后一道防线，在正常压缩未能阻止 413 时紧急触发。

**恢复优先级**：

1. **Context Collapse drain** — 先尝试释放所有已暂存的折叠
2. **Reactive Compact** — 如果 collapse 不够，执行紧急全量压缩
3. 都失败 → 返回 prompt_too_long 错误

**防循环机制**：通过 `hasAttemptedReactiveCompact` 标记防止无限重试。

---

## 三、Token 计数方式

### 3.1 精确计数

从最近的 assistant 消息的 `usage` 字段获取：

```
totalTokens = input_tokens + cache_creation + cache_read + output_tokens
```

### 3.2 粗略估算

当没有 API usage 数据时，使用字符数估算：

```
tokens ≈ content.length / 4  （4 bytes per token）
```

### 3.3 混合策略

`tokenCountWithEstimation()` 从消息列表末尾向前扫描，找到第一条有 API usage 的消息，取其 token 数，再加上后续消息的粗略估算。

---

## 四、与 OpenClaw 的对比

### 4.1 架构对比总览

| 维度 | Claude Code | OpenClaw |
|------|-------------|----------|
| **压缩层数** | 5 层（Snip → Micro → Collapse → Auto → Reactive） | 1 层（AutoCompact + flush） |
| **上下文窗口** | 200K（默认），1M（可选） | 取决于所用模型，无 1M 显式支持 |
| **压缩方式** | LLM 生成结构化摘要 | LLM 生成摘要 + 压缩前 flush 到文件 |
| **记忆持久化** | CLAUDE.md 规则文件 + transcript | Markdown 文件（MEMORY.md + 日记） |
| **压缩前保存** | 无（靠摘要质量 + transcript 回溯） | **Memory Flush**（静默轮让 LLM 写日记） |
| **子 Agent 压缩** | 各自独立运行压缩 | 各自独立运行压缩 |
| **可恢复性** | 可读 transcript 文件恢复细节 | 可读 Markdown 文件恢复细节 |

### 4.2 压缩策略对比

**Claude Code 的多层策略**

Claude Code 的核心思路是「分层卸载，尽量晚做全量压缩」：

1. 先用 Snip 裁剪明显低价值内容（代价最低）
2. 再用 Microcompact 清理旧 tool_result（保留结构、只删内容）
3. 再用 Context Collapse 折叠中段对话（可逆投影）
4. 最后才做 AutoCompact 全量摘要（代价最高，不可逆）
5. 兜底用 Reactive Compact 处理极端情况

这种设计的优势是：大多数情况下不需要触发全量压缩，前几层就能维持足够空间。

**OpenClaw 的 Flush + Compact 策略**

OpenClaw 的核心创新是「压缩前 Flush」机制：

1. 当 token 接近 `softThreshold` 时触发一轮静默运行
2. 模型被指示将重要信息写入 `memory/YYYY-MM-DD.md`
3. Flush 完成后才执行正常的上下文压缩
4. 被压缩丢弃的内容已经持久化到磁盘，可通过 `memory_search` 检索回来

这种设计的优势是：压缩后不会真正丢失信息，记忆成为可搜索的永久存储。

### 4.3 压缩触发条件对比

| 条件 | Claude Code | OpenClaw |
|------|-------------|----------|
| **自动触发** | token 估算 >= `有效窗口 - 13K` | token 接近 `softThreshold`（配置值） |
| **手动触发** | `/compact` 命令 | `/compact` 命令 |
| **时间触发** | 距上次助理消息 > 60 分钟（microcompact） | 无 |
| **413 触发** | Reactive Compact 自动恢复 | 无专门机制 |
| **阻断线** | 有效窗口 - 3K（停止接受输入） | 无明确阻断线文档 |

### 4.4 压缩后恢复能力对比

**Claude Code**：

- 摘要消息中包含 transcript 文件路径
- 模型可通过 `FileRead` 工具回读 transcript 获取被压缩掉的具体细节
- Post-compact 自动恢复最多 5 个最近操作的文件内容（50K token 预算）
- 自动恢复技能上下文（25K token 预算）
- 自动恢复工具/MCP/Agent 列表的增量变更

**OpenClaw**：

- 压缩前的 Memory Flush 已将重要信息写入日记文件
- 模型通过 `memory_search` + `memory_get` 工具检索回来
- 底层有向量 + BM25 混合检索支持语义搜索
- Markdown 文件作为唯一事实来源，人可读可编辑

### 4.5 摘要质量保障对比

**Claude Code**：

- 摘要由同模型（或 fork agent 共享 cache）生成
- 结构化的 9 段式摘要模板：请求意图、技术概念、文件代码、错误修复、问题解决、用户消息、待办任务、当前工作、下一步
- `<analysis>` 块作为思维草稿提高摘要质量（最终被 strip）
- 支持用户自定义压缩指令（如「重点关注 TypeScript 变更」）
- 自动压缩时注入「直接继续、不要寒暄」的指令
- Partial Compact 模式：只压缩部分消息，保留最近的原始对话

**OpenClaw**：

- 依赖标准的 LLM 压缩能力
- 核心优势在于 Flush 机制将信息持久化，降低了对摘要质量的依赖
- 记忆文件本身可被模型主动搜索和读取

### 4.6 上下文窗口利用率对比

**Claude Code**：

- 200K 窗口中有效可用约 180K（扣除摘要输出预留 20K）
- 在 ~167K 时触发自动压缩，给 13K buffer
- 支持 1M 窗口（Sonnet 4.6、Opus 4.6），阈值按比例调整
- 多层压缩使实际可利用率更高（前几层释放空间但不丢信息）

**OpenClaw**：

- 窗口大小取决于所用模型
- 通过 Flush 机制在压缩前保存信息，有效提高了「信息保留率」
- 但没有类似 Microcompact / Context Collapse 的中间层优化

### 4.7 成本对比

| 操作 | Claude Code | OpenClaw |
|------|-------------|----------|
| **Microcompact** | 零额外 API 调用（本地或 cache_edit） | 不适用 |
| **AutoCompact** | 1 次 API 调用（摘要生成，上限 20K output） | 1 次 API 调用（压缩） |
| **Memory Flush** | 不适用 | 1 次额外 API 调用（静默轮写日记） |
| **Post-compact 恢复** | 0 次（从本地缓存读取文件） | 按需通过 memory_search（可能触发嵌入） |

Claude Code 在常规情况下通过 Microcompact 和 Collapse 延迟全量压缩，减少 API 调用次数。OpenClaw 的 Flush 机制虽然多一次 API 调用，但换来了持久化的记忆存储。

---

## 五、核心设计哲学差异

### Claude Code：「精细分层、延迟压缩、transcript 兜底」

- **五层渐进式压缩**：每层代价递增，尽量用低代价的方式释放空间
- **Prompt Cache 友好**：Cached Microcompact 在不破坏缓存的前提下清理内容
- **Transcript 作为安全网**：压缩后仍可通过 FileRead 回溯完整历史
- **结构化摘要模板**：9 段式模板 + analysis 草稿确保摘要覆盖率
- **无独立记忆系统**：记忆依赖 CLAUDE.md 规则文件（手动维护）和 transcript

### OpenClaw：「先保存、再压缩、记忆即文件」

- **Flush-before-Compact**：在压缩前让 LLM 主动将信息写入持久文件
- **Markdown 为唯一事实来源**：记忆是人可读可编辑的文件，索引是可再生缓存
- **混合检索恢复**：向量 + BM25 支持语义检索被压缩掉的内容
- **架构极简**：无需独立记忆提取管线，LLM + 文件工具完成写入
- **长期记忆天然形成**：每次 Flush 积累的日记文件形成了持续增长的知识库

---

## 六、核心源码文件索引

### Claude Code 压缩相关

| 文件 | 职责 |
|------|------|
| `src/utils/context.ts` | 上下文窗口大小、输出 token 上限、1M 检测 |
| `src/services/compact/autoCompact.ts` | 自动压缩触发阈值、警告状态、shouldAutoCompact |
| `src/services/compact/compact.ts` | 核心压缩实现（摘要生成、消息重建、post-compact 恢复） |
| `src/services/compact/prompt.ts` | 压缩提示词模板（9 段式结构化摘要 + analysis 草稿） |
| `src/services/compact/microCompact.ts` | Microcompact 三条路径（时间型、缓存编辑型、回退） |
| `src/services/compact/grouping.ts` | 消息按 API round 分组（PTL 重试时截断） |
| `src/services/compact/postCompactCleanup.ts` | 压缩后清理（文件/技能/工具增量恢复） |
| `src/services/compact/sessionMemoryCompact.ts` | Session Memory 压缩（轻量级，保留尾部切片） |
| `src/services/compact/compactWarningState.ts` | 压缩警告状态管理 |
| `src/query.ts` | 主循环中各压缩层的调用编排 |
| `src/utils/tokens.ts` | Token 计数估算 |

### OpenClaw 压缩/记忆相关

| 文件 | 职责 |
|------|------|
| `src/auto-reply/reply/agent-runner-memory.ts` | 压缩前 Memory Flush 逻辑 |
| `extensions/memory-core/src/flush-plan.ts` | Flush 护栏提示（目标文件、只追加、只读保护） |
| `extensions/memory-core/src/memory/manager.ts` | 混合检索实现（向量 + BM25） |
| `extensions/memory-core/src/prompt-section.ts` | Agent 提示词引导（先搜索再回答） |
| `packages/memory-host-sdk/src/host/memory-schema.ts` | SQLite Schema 定义 |
| `extensions/memory-core/src/memory/qmd-manager.ts` | QMD 后端（外部高性能检索） |
