# Claude Code 上下文压缩机制与 OpenClaw 对比

基于 Claude Code 开源快照源码与 OpenClaw 源码的深度分析。

---

## 一、Claude Code 上下文窗口能力

### 1.1 窗口大小

Claude Code 的上下文窗口大小取决于所用模型，核心定义在 `src/utils/context.ts`：

- **默认窗口：200K token[模型处理文本的最小单位，英文约 1 token ≈ 4 字符，中文约 1 汉字 ≈ 1.5–2 token]**（`MODEL_CONTEXT_WINDOW_DEFAULT = 200_000`）
- **1M 窗口**：通过以下方式启用
  - 模型名带 `[1m]` 后缀（显式 opt-in[主动选择启用]）
  - Beta header[测试版请求头，用于启用未正式发布的功能] `CONTEXT_1M_BETA_HEADER`
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

Claude Code 还实现了一个 slot[输出槽位] 节约优化：默认 cap[封顶] 到 8K（`CAPPED_DEFAULT_MAX_TOKENS`），命中 max_output_tokens 后先 escalate[逐步升级] 到 64K，再走多轮恢复。

### 1.3 有效上下文窗口

自动压缩的阈值计算基于「有效上下文窗口」，而非原始窗口。模型在做"压缩摘要"时自身也需要输出空间，所以要从总窗口里预留一部分。打个比方：假设你有一个 200 页的笔记本（200K 窗口），让模型把之前的对话"写成摘要"，这个摘要本身也要占页数，所以真正能装对话内容的有效页数要把摘要用掉的扣掉。

```
有效窗口 = getContextWindowForModel(model) - min(maxOutputTokens, 20_000)
```

公式的含义：取"模型最大输出上限"和"20K"中较小的值，从总窗口中扣除。如果模型输出上限本身不到 20K，则按实际上限扣即可。

其中 20K 是 compact[压缩] 摘要的输出预留空间（`MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000`）。这个值来源于统计数据：Anthropic 统计了大量真实压缩操作的输出长度，发现 p99.99[99.99 百分位，即 99.99% 的情况不超过此值] compact 输出为 17,387 tokens，所以预留 20K 是一个非常安全的上限。以 200K 窗口为例，有效可用空间约为 200K - 20K = 180K。

还可被 `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 环境变量进一步收窄。

---

## 二、Claude Code 多层压缩机制

> **重要说明：开源版与内部版的差异**
>
> Claude Code 开源快照中包含五层压缩的**架构引用**，但通过 Bun 编译器的 `feature()` 机制（`import { feature } from 'bun:bundle'`），大部分压缩层在开源构建中被**编译期物理删除**（Dead Code Elimination）。`feature('HISTORY_SNIP')` 等 flag 在开源构建时为 `false`，Bun 将整个 `false ? require('./snipCompact.js') : null` 优化为 `null`，相关的 `require()` 调用和整个 `if` 块从编译产物中消失——因此即使源码文件不存在，程序也能正常编译运行。
>
> 下表展示了各层在两个版本中的实际状态：
>
> | 层 | Feature Flag | 开源版 | Anthropic 内部版 |
> |---|---|---|---|
> | Snip Compact | `HISTORY_SNIP` | 编译删除，源码未公开 | 可用 |
> | Microcompact（时间型） | 无 | **可用** | 可用 |
> | Microcompact（缓存编辑型） | `CACHED_MICROCOMPACT` | 编译删除，源码未公开 | 可用 |
> | Context Collapse | `CONTEXT_COLLAPSE` | 编译删除，源码未公开 | 可用 |
> | AutoCompact | 无 | **可用** | 可用 |
> | Reactive Compact | `REACTIVE_COMPACT` | 编译删除，源码未公开 | 可用 |
>
> **开源版实际只有 2 层压缩在工作：时间型 Microcompact + AutoCompact。** 以下文档描述的是 Anthropic 内部完整版的五层架构。

### 2.0 开源版压缩能力（你编译运行的版本）

开源版 Claude Code 只有两层压缩可用：

**第一层：时间型 Microcompact（纯代码，零 API 调用）**

当距离上次模型回复超过 60 分钟时，代码自动将旧的工具返回内容替换为占位符。无 feature flag 保护，源码完整公开在 `microCompact.ts` 中。

> **示例**：你下午 2 点让 Claude Code 读了 10 个文件，然后去开会。下午 3:30 回来继续对话，此时 prompt cache 早已过期（5 分钟 TTL），Microcompact 自动将那 10 个 FileRead 的返回内容替换为 `[Old tool result content cleared]`，只保留最近 5 条工具结果。你回来后继续工作，释放了大量 token 但不额外花钱。

**第二层：AutoCompact（LLM 生成，1 次 API 调用）**

当 token 超过阈值（200K 窗口约 167K）时，调用模型生成 9 段式结构化摘要。无 feature flag 保护，源码完整公开在 `autoCompact.ts` 和 `compact.ts` 中。

> **示例**：对话持续到 80 轮，token 达到 170K，AutoCompact 触发。模型把全部对话浓缩为一份摘要（约 8K token），加上最近 5 个文件内容，上下文从 170K 降到约 30K。

**开源版缺失的能力**：
- 没有 Snip（模型不能主动裁剪旧消息）
- 没有缓存编辑型 Microcompact（不能在不破坏 prompt cache 的前提下清理内容）
- 没有 Context Collapse（不能可逆地折叠中段对话）
- 没有 Reactive Compact（413 错误后没有自动恢复机制）

这意味着开源版在"前几层轻量压缩延迟全量压缩"方面的能力大幅缩水，更容易触发代价高的 AutoCompact，且无法应对 413 错误。

### 2.0.1 Anthropic 内部完整版概览

Anthropic 内部版实现了 **五层** 上下文压缩策略，按执行顺序排列。

五层的核心设计思想是：**尽量用低代价方式释放空间，只有必须"理解"内容时才调用 LLM**。

| 层 | 压缩方式 | 是否调用 LLM | 核心机制 |
|---|---------|-------------|---------|
| Snip Compact | 模型通过 SnipTool 标记 + 代码执行删除 | 是（搭载在正常回复中，零额外 API 调用） | 模型判断哪些消息低价值，通过工具调用标记删除 |
| Microcompact | 纯代码流程 | 否 | 将旧的工具返回内容替换为占位字符串，或通过 `cache_edits` 在 API 层删除 |
| Context Collapse | 混合 | 折叠摘要可能用 LLM 生成，投影机制为纯代码 | 多轮对话折叠为摘要，以"读时投影"方式呈现，不修改原始消息 |
| AutoCompact | LLM 生成 | 是（1 次 API 调用） | 模型按 9 段式模板输出结构化摘要，替换全部旧消息 |
| Reactive Compact | LLM 生成 | 是（1 次 API 调用） | 本质是紧急触发的 AutoCompact，413 错误后的最后防线 |

> **类比**：想象整理一个堆满文件的书桌。Snip 是助理在正常工作时顺手扔掉明显的废纸（不需要额外花时间）；Microcompact 是把旧文件的内页抽掉、只留封面（知道有这份文件但不保留细节）；Context Collapse 是把几份相关文件装进一个文件袋并贴上标签（随时可打开查看）；AutoCompact 是专门请助理停下来写一份当前工作状态的完整备忘录，然后把所有旧文件归档（需要专门的时间，成本高）；Reactive Compact 是桌子实在放不下了、文件掉地上了，紧急叫助理来收拾。

### 2.1 Snip Compact（裁剪压缩）

> **注意**：此功能受 `feature('HISTORY_SNIP')` 保护，**开源版中被编译删除**。源码文件 `snipCompact.ts`、`snipProjection.ts`、`SnipTool/` 均未公开。以下分析基于开源代码中的调用点引用反推。

**执行时机**：每轮迭代最先执行（在 microcompact 之前）

**压缩方式**：**模型决策 + 代码执行（零额外 API 调用）**。这不是纯代码的规则判断——而是模型在正常回复过程中，通过调用 `SnipTool` 工具来标记哪些消息应该删除，代码负责执行删除和投影。模型是"顺手"完成的，不需要额外的 API 调用。

完整的工作流程：

| 步骤 | 谁做的 | 做什么 |
|------|--------|--------|
| 1. 注入消息 ID | 代码 | 在每条用户消息末尾追加 `[id:abc123]` 标签（源码：`messages.ts` 中的 `appendMessageTagToUserMessage`） |
| 2. 定期提醒 | 代码 | 每 ~10K token 增长后注入 `SNIP_NUDGE_TEXT` 提醒模型清理（源码：`attachments.ts` 中的 `shouldNudgeForSnips`） |
| 3. 决定删什么 | **模型** | 模型在正常回复中调用 `SnipTool`，传入要删除的消息 ID |
| 4. 执行删除 | 代码 | `snipCompactIfNeeded()` 将被标记的消息从 API 视图中移除 |
| 5. UI 保留 | 代码 | `projectSnippedView()` 确保被 snip 的消息在 UI 中仍可滚动查看，但不再发送给 API |

> **代码级示例**：假设对话中有这样的早期对话，每条用户消息都被注入了 ID 标签：
> ```
> messages[0]: user → "帮我看看这个项目的目录结构 [id:a1b2c3]"
> messages[1]: assistant → "好的，让我运行 ls -la..."
> messages[2]: tool_result → (目录列表输出)
> messages[3]: user → "这个 src 文件夹里有什么？ [id:d4e5f6]"
> messages[4]: assistant → "让我看看..."
> messages[5]: tool_result → (src 目录列表)
> ```
> 在后续回复中，模型判断这些早期探索已经没有价值，于是在正常回复中"顺手"调用：
> ```
> SnipTool({ message_ids: ["a1b2c3", "d4e5f6"] })
> ```
> 代码收到后，将 `messages[0]`–`messages[5]` 从 API 视图中移除（但 UI 滚动历史中保留）。下一轮请求中模型就看不到这些消息了。

**核心行为**（从 `query.ts` 的调用点推断）：
- 受 `HISTORY_SNIP` feature flag[功能开关，用布尔值控制功能是否启用] 保护（**开源版中此 flag 为 false，整个功能被编译删除**）
- `snipCompactIfNeeded(messages)` 返回 `{ messages, tokensFreed, boundaryMessage }`
- `tokensFreed` 会传递给后续的自动压缩阈值计算（因为估算器读的是未裁剪前的 usage）
- 可产生 `boundaryMessage`[边界消息] 通知 UI[用户界面]

> **示例**：你让 Claude Code 帮你重构一个项目，前 30 轮对话中有很多早期的探索性提问（"这个文件是干什么的？""帮我看看目录结构"）。这些早期消息已经没有价值了——目录结构你早就清楚了，探索阶段的问答不影响当前的重构工作。Snip Compact 会直接把这些早期低价值消息从历史中裁掉，比如释放了 8K token，而你完全感受不到任何变化。

### 2.2 Microcompact（微压缩）

> **开源版状态**：时间型（路径 A）**可用**，源码公开在 `microCompact.ts`。缓存编辑型（路径 B）受 `feature('CACHED_MICROCOMPACT')` 保护，**开源版中被编译删除**。

**执行时机**：在 Snip Compact 之后、AutoCompact 之前

**压缩方式**：**纯代码流程，不调用 LLM**。遍历消息数组，找到旧的工具返回结果，将其内容字段直接替换为一个占位字符串。本质上就是一行字符串替换操作。

> **代码级示例**：假设对话中第 10 轮读取了一个文件：
> ```
> // 压缩前
> messages[20] = {
>   role: "tool",
>   tool_use_id: "read_file_abc",
>   content: "import express from 'express';\nconst app = express();\n...(300行代码)..."
> }
>
> // Microcompact 执行后（时间型）
> messages[20] = {
>   role: "tool",
>   tool_use_id: "read_file_abc",       // ← 保留：知道调用了什么工具
>   content: "[Old tool result content cleared]"  // ← 替换：300行代码变成一行占位符
> }
> ```
> 工具调用的"骨架"（调用了什么工具、传了什么参数）完整保留，只是把返回的大段内容清空了。模型仍然知道"之前读过 app.ts 这个文件"，但不再占用那 300 行代码的 token。

**目标**：清除旧的 `tool_result`[工具执行后返回给模型的结果] 内容，只保留最近 N 条的完整结果

**三条路径**：

**路径 A：时间型 Microcompact**

当距离上次 assistant[助理/模型] 消息超过配置阈值（默认 60 分钟）时触发：

- 此时服务端 prompt cache[提示缓存，服务端缓存相同前缀以降低延迟和成本] 已过期（5 分钟 TTL[Time To Live，缓存存活时间]）
- 直接将旧的 `tool_result` 内容替换为 `[Old tool result content cleared]`
- 保留最近 N 条完整结果（`keepRecent`[保留最近 N 条]，默认 5，最少 1）
- 只清理可压缩工具的结果：FileRead、Bash/PowerShell、Grep、Glob、WebSearch、WebFetch、FileEdit、FileWrite

**路径 B：缓存编辑型 Microcompact（Cached MC）**（开源版中被编译删除）

利用 API 层的 `cache_edits`[缓存编辑，不破坏缓存前提下通过指令删除/修改内容] 能力，在不破坏 prompt cache 的情况下删除旧 tool_result：

- 不修改本地消息内容
- 通过 `cache_edits` 指令在 API 层删除，维护 `pinnedEdits`[固定的编辑指令列表] 供后续请求复现
- 有 `triggerThreshold`[触发阈值] 和 `keepRecent` 配置
- 仅限主线程（防止 fork agent[分叉代理，主进程派生的子代理] 污染全局状态）

**路径 C：无操作回退**

如果时间型和缓存编辑型都不触发，不做任何 microcompact，由后续的 autocompact 处理上下文压力。

> **示例**：你让 Claude Code 逐个修复 10 个 bug。每次修复时，模型都会用 `FileRead` 读取源文件（返回几百行代码）、用 `Bash` 运行测试（返回大段测试输出）。到第 8 个 bug 时，前面 7 个 bug 的 FileRead 结果和 Bash 输出仍然原封不动地占着上下文。Microcompact 会把前 5 个 bug 的工具返回内容替换为 `[Old tool result content cleared]`——只删除工具输出的具体内容（几百行代码、测试日志），但保留"调用了什么工具、传了什么参数"的结构信息。最近 3 个 bug 的工具结果完整保留（`keepRecent = 5` 但这里只有 3 个在最近窗口内）。这样可能一下子释放 30K+ token，而模型仍然知道之前做过哪些操作。

### 2.3 Context Collapse（上下文折叠）

> **注意**：此功能受 `feature('CONTEXT_COLLAPSE')` 保护，**开源版中被编译删除**。源码目录 `services/contextCollapse/` 未公开。

**执行时机**：在 Microcompact 之后、AutoCompact 之前

**压缩方式**：**混合方式**。折叠摘要的生成可能借助 LLM（需要理解对话内容才能总结），但"投影"机制本身是纯代码——类似数据库的 View（视图），原始数据不动，只在读取时呈现折叠后的版本。

> **代码级示例**：假设对话中第 10–17 条消息是一段关于"是否用 Redis 做缓存"的讨论：
> ```
> // 原始消息数组（不修改）
> messages[10]: user → "我们需要缓存吗？"
> messages[11]: assistant → "看你的场景，有几个选项..."
> messages[12]: user → "Redis 和 Memcached 哪个好？"
> messages[13]: assistant → "对于你的用例，推荐 Redis，因为..."
> messages[14]: user → "Redis 的持久化怎么配？"
> messages[15]: assistant → "有 RDB 和 AOF 两种方式..."
> messages[16]: user → "好，就用 Redis + AOF"
> messages[17]: assistant → "好的，我来配置..."
>
> // collapse store 中存储一条折叠记录
> collapse = {
>   range: [10, 17],
>   summary: "讨论并决定使用 Redis 做缓存，采用 AOF 持久化方式"
> }
>
> // 发送 API 请求时的"投影视图"（读时替换）
> projected_messages[10]: system → "讨论并决定使用 Redis 做缓存，采用 AOF 持久化方式"
> // messages[11]–[17] 不出现在投影中
> projected_messages[11]: ... (原 messages[18] 的内容)
> ```
> 8 条消息变成 1 条摘要，但原始 8 条消息仍完整保存在内存中。如果后续需要"Redis 持久化怎么配的来着？"，系统可以展开折叠恢复原文。

**策略**：将多轮交互折叠为摘要，但以「读时投影」方式实现——折叠存储在独立的 collapse store 中，不直接修改原始消息数组。

**核心行为**（从 `query.ts` 调用点推断）：
- 受 `CONTEXT_COLLAPSE` feature flag 保护
- `applyCollapsesIfNeeded(messages, toolUseContext, querySource)` — 投影折叠视图
- `recoverFromOverflow(messages, querySource)` — 作为 413 错误[HTTP 状态码，请求体太大] 的第一道恢复手段
- 与 AutoCompact 互补：如果 collapse 已将 token 降到阈值以下，autocompact 不再触发

> **示例**：接上面的场景，Microcompact 清理了旧工具输出后仍然有 150K token。对话中第 3-6 轮是一段关于"数据库选型讨论"的交互（用户问了几个问题、模型给了建议、最终决定用 PostgreSQL），这段讨论的结论早已体现在后续代码中。Context Collapse 会把这 4 轮交互折叠为一句摘要："用户与助理讨论了数据库选型，最终决定使用 PostgreSQL"。关键是：原始的 4 轮消息并没有被删除，只是在"读取视图"中被替换为摘要。如果后面需要回顾选型细节，系统可以"展开"这个折叠，恢复原始内容。这可能释放了 10K token，且完全可逆。

### 2.4 AutoCompact（自动全量压缩）

> **开源版状态**：**可用**，无 feature flag 保护。源码完整公开在 `autoCompact.ts` 和 `compact.ts` 中。

**执行时机**：在上述轻量压缩之后

**压缩方式**：**必须调用 LLM（1 次 API 调用）**。这是第一个真正需要模型"理解"对话内容的层级。代码将整段对话历史 + 一套摘要提示词发给模型（或通过 fork 子代理共享 prompt cache），模型阅读完所有内容后输出结构化摘要。

> **代码级示例**：假设对话已有 80 轮，代码做了以下操作：
> ```
> // 1. 构造请求：把全部对话 + 摘要提示词打包
> const prompt = getCompactPrompt();  // 9 段式摘要模板
> const request = [...allMessages, { role: "user", content: prompt }];
>
> // 2. 发给模型，模型输出（约 8K token）：
> // <analysis>
> // 用户在做 Express → Fastify 迁移，已完成 15 个路由...
> // 核心难点是中间件兼容性...
> // </analysis>
> // <summary>
> // 1. Primary Request and Intent: 将 Express 项目迁移到 Fastify...
> // 2. Key Technical Concepts: 路由迁移、中间件 → hooks...
> // ...（共 9 段）
> // </summary>
>
> // 3. 代码处理：去掉 <analysis>（思维草稿），只保留 <summary>
> const summary = formatCompactSummary(modelOutput);
>
> // 4. 重建消息数组：
> messages = [
>   boundaryMessage,       // 压缩边界标记
>   { role: "user", content: summary },  // 摘要（约 8K token）
>   ...recentFileAttachments,  // 最近 5 个文件内容（最多 50K token）
>   ...skillAttachments,       // 技能上下文（最多 25K token）
> ];
> // 从 170K token → 约 30K token
> ```
> 注意 `<analysis>` 部分的作用：它让模型先"打草稿"梳理重点，提高摘要质量，但最终会被代码剥除，不占用压缩后的上下文空间。

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
2. 移除消息中的图片和技能发现类 attachment[附件]
3. 通过 fork 子代理（共享 prompt cache）或直接流式请求生成摘要
4. 摘要最大输出 20K token
5. 如果压缩请求本身遇到 prompt-too-long[提示词过长错误]，按 API round[一次完整的 API 请求-响应周期] 分组从最旧侧批量截断，最多重试 3 次
6. 模型输出 `<analysis>`[分析/思维草稿] + `<summary>`[摘要] 格式
7. `formatCompactSummary()` 去掉 `<analysis>` 部分，将 `<summary>` 转为 `Summary:\n...`

**压缩后保留什么**：

- **CompactBoundaryMessage** — 压缩边界标记
- **摘要消息** — 一条用户消息（`isCompactSummary: true`），内容为结构化摘要
- **Post-compact[压缩后阶段] 文件附件** — 最多 5 个最近读写的文件，每个最多 5K token，总预算 50K token
- **技能附件** — 每个最多 5K token，总预算 25K token
- **工具/MCP[Model Context Protocol，模型上下文协议] delta[增量变更]** — 延迟加载工具列表变更、Agent 列表变更、MCP 指令变更
- **SessionStart hooks[钩子，在特定时机自动执行的回调函数]** — 重新执行会话启动钩子

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

> **示例**：你已经和 Claude Code 对话了 80 轮，前三层压缩都做过了但上下文仍然达到 170K token（超过 167K 触发线）。AutoCompact 启动，模型把这 80 轮对话浓缩成一段结构化摘要，大致如下：
>
> *"1. 用户请求：将旧的 Express 项目迁移到 Fastify 框架。2. 技术要点：路由从 app.get() 迁移到 fastify.route()，中间件改为 hooks... 3. 已修改文件：src/routes/user.ts、src/middleware/auth.ts、package.json... 4. 已解决的错误：TypeScript 类型不兼容、测试超时... 7. 待处理：还剩 src/routes/admin.ts 未迁移。8. 当前工作：正在处理 admin 路由的权限校验逻辑。"*
>
> 压缩完成后，之前的 80 轮原始消息全部丢弃，取而代之的是这段摘要 + 最近读写的 5 个文件内容。上下文从 170K 骤降到约 30K，模型根据摘要中的"当前工作"直接继续干活，你不会感知到任何中断。

**压缩前后发给 API 的 JSON 数据对比**：

以下用具体的 JSON 结构展示 AutoCompact 前后，实际发送给大模型 API 的请求数据变化。

*压缩前（~170K token）*——80 轮对话的完整消息数组：

```json
{
  "model": "claude-sonnet-4-20250514",
  "max_tokens": 16384,
  "system": "You are Claude Code...(系统提示词，约 5K token)",
  "messages": [
    // 第 1 轮
    { "role": "user", "content": "帮我把 Express 项目迁移到 Fastify" },
    { "role": "assistant", "content": "好的，我先看看项目结构...",
      "tool_use": [{ "name": "FileRead", "input": {"path": "src/app.js"} }] },
    { "role": "user", "content": [{ "type": "tool_result",
      "content": "const express = require('express');\nconst app = express();\n..." }] },

    // 第 2 轮
    { "role": "assistant", "content": "我看到你用了 express.Router..." },
    { "role": "user", "content": "先从 routes/users.js 开始" },
    { "role": "assistant", "content": "...",
      "tool_use": [{ "name": "FileRead", "input": {"path": "routes/users.js"} }] },
    { "role": "user", "content": [{ "type": "tool_result",
      "content": "...200行完整代码..." }] },

    // ... 中间省略第 3–79 轮 ...
    // 每轮都包含完整的 FileRead/FileWrite/Bash 调用和返回结果
    // 消息总数约 200-300 条

    // 第 80 轮
    { "role": "assistant", "content": "routes/orders.js 迁移完成，测试通过" },
    { "role": "user", "content": "继续迁移 routes/payments.js" }
  ]
}
```

*压缩后（~30K token）*——整个消息数组被替换为 4 条消息：

```json
{
  "model": "claude-sonnet-4-20250514",
  "max_tokens": 16384,
  "system": "You are Claude Code...(系统提示词不变)",
  "messages": [
    // 消息 1：压缩边界标记（boundaryMessage）
    { "role": "user",
      "content": "<boundary>\nThis is a continuation of an existing conversation...\nTranscript path: /tmp/.claude/transcript_abc123.jsonl" },

    // 消息 2：9 段式结构化摘要（约 8K token）
    { "role": "user",
      "content": "1. Primary Request and Intent:\n用户要求将 Express 项目（20个路由文件）迁移到 Fastify...\n\n2. Key Technical Concepts:\n路由迁移: express.Router → fastify.register, 中间件 → hooks...\n\n3. Files and Code Sections:\n- routes/users.js: 已迁移，用 fastify-sensible 替代 express-validator\n- routes/orders.js: 已迁移，改用 schema validation\n- routes/payments.js: 待迁移\n...\n\n7. Pending Tasks:\n还剩 routes/payments.js 和 routes/admin.js 未迁移\n\n8. Current Work:\n刚完成 routes/orders.js 的迁移和测试\n\n9. Optional Next Step:\n继续迁移 routes/payments.js" },

    // 消息 3：最近操作的文件完整内容（约 20K token）
    { "role": "user",
      "content": "[Post-compact file attachments]\n\n--- routes/orders.js (最新版) ---\nconst fp = require('fastify-plugin');\nmodule.exports = async function(fastify) {\n  fastify.route({ method: 'GET', url: '/orders', ... });\n};\n\n--- routes/payments.js (待迁移) ---\nconst express = require('express');\nconst router = express.Router();\n...\n\n--- fastify-app.js ---\n...(共最多5个文件，每个最多 5K token)" },

    // 消息 4：技能上下文（如有）
    { "role": "user",
      "content": "[Skill attachments]\nTypeScript strict mode config...(约 25K token 预算)" }
  ]
}
```

压缩前后的核心区别：

| 维度 | 压缩前 | 压缩后 |
|------|--------|-------|
| **消息条数** | 200–300 条（user/assistant 交替） | 4 条（全部是 user 角色） |
| **总 token** | ~170K | ~30K |
| **对话历史** | 每一轮完整的 user → assistant → tool_result 全部保留 | 替换为 1 条结构化摘要（9 段模板） |
| **代码内容** | 散落在各 tool_result 中，包含大量早已过时的旧版本 | 只保留最近 5 个文件的**最新版本** |
| **边界标记** | 无 | 有 `<boundary>` 消息，告诉模型这是接续对话，附 transcript 路径 |
| **角色分布** | user 和 assistant 严格交替 | 全部是 user 消息（摘要、文件、技能都以 user 角色注入） |
| **模型视角** | 看到完整的 80 轮逐条对话 | 看到"一份项目进度报告 + 当前文件 + 下一步指令" |

关键设计点：压缩后**没有任何 assistant 消息**，模型看到的是一串 user 消息。最后那句"Resume directly — do not acknowledge the summary"指示模型直接继续工作，不要说"好的，我看到了你的摘要"之类的废话，让用户完全无感知压缩的发生。

### 2.5 Reactive Compact（响应式压缩）

> **注意**：此功能受 `feature('REACTIVE_COMPACT')` 保护，**开源版中被编译删除**。源码文件 `reactiveCompact.ts` 未公开。开源版遇到 413 错误时没有自动恢复机制。

**执行时机**：API 返回 prompt-too-long (413) 错误后

**压缩方式**：**分两步——先纯代码尝试，不够再调用 LLM**。第一步是纯代码操作：把 Context Collapse 中暂存的所有折叠全部释放（drain），让投影视图生效以减小 token 量。如果这一步就够了，不需要调用 LLM。如果还不够，第二步才执行紧急全量压缩（和 AutoCompact 机制相同，需要 1 次 API 调用）。

> **代码级示例**：
> ```
> try {
>   const response = await callAPI(messages);  // 发送请求
> } catch (error) {
>   if (error.status === 413) {
>     // 第一步：纯代码操作，释放所有暂存的折叠
>     const drained = contextCollapse.drainAll();
>     if (drained.tokensSaved >= overflow) {
>       // 够了！不需要调 LLM，直接重试
>       return await callAPI(drained.messages);
>     }
>
>     // 第二步：不够，紧急调用 LLM 做全量压缩
>     if (!hasAttemptedReactiveCompact) {
>       hasAttemptedReactiveCompact = true;  // 防无限循环
>       const compacted = await fullCompaction(messages);  // 1次API调用
>       return await callAPI(compacted.messages);
>     }
>
>     // 都失败了，报错
>     throw new PromptTooLongError();
>   }
> }
> ```

**策略**：这是最后一道防线，在正常压缩未能阻止 413 时紧急触发。

**恢复优先级**：

1. **Context Collapse drain[排空/释放]** — 先尝试释放所有已暂存的折叠
2. **Reactive Compact** — 如果 collapse 不够，执行紧急全量压缩
3. 都失败 → 返回 prompt_too_long 错误

**防循环机制**：通过 `hasAttemptedReactiveCompact` 标记防止无限重试。

> **示例**：极端情况下，用户在一条消息中粘贴了一段超长的日志（50K token），加上已有的上下文，总量突然飙到 195K。前面的 Snip、Microcompact、Collapse 都来不及处理（它们在构造请求之前运行，但这次新消息本身就太大了），请求发到 API 后直接被拒绝返回 413 错误。此时 Reactive Compact 作为最后防线启动：先尝试把所有 Context Collapse 中暂存的折叠全部释放（drain），如果还不够，就紧急执行一次全量压缩。压缩后再重新发送请求。如果连这次压缩都失败了，系统才会向用户报错。

### 2.6 完整示例：一次长对话中五层压缩的协作过程（Anthropic 内部版）

> **注意**：以下示例描述的是 Anthropic 内部完整版的行为。开源版只有 Microcompact 和 AutoCompact 两步，没有 Snip、Collapse 和 Reactive 的介入。

下面用一个贯穿始终的例子，展示内部版五层压缩如何依次介入：

**场景**：你让 Claude Code 帮你把一个 Express 项目迁移到 Fastify，项目有 20 个路由文件。模型使用 200K 窗口（有效窗口 180K，自动压缩触发线 167K）。

**第 1–10 轮（上下文约 40K token）**：一切正常，无压缩发生。你和模型讨论了迁移策略，模型读取了几个文件。

**第 11–30 轮（上下文约 90K token）**：你逐个迁移路由文件，每次模型都 FileRead 源文件、修改、Bash 运行测试。

- **Snip Compact 介入**：系统每 ~10K token 增长注入一次 nudge 提醒，模型在回复中"顺手"调用 SnipTool，将第 1–3 轮的探索性对话（带有 `[id:xxx]` 标签的"帮我看看项目结构""列出所有路由文件"等消息）标记为删除。代码执行删除后释放约 5K token。
- 上下文降到约 85K，远低于触发线，继续工作。

**第 31–50 轮（上下文约 140K token）**：迁移继续，大量工具结果积累。

- **Microcompact 介入**：第 11–25 轮的 FileRead 和 Bash 输出已经很旧了（那些文件后来又被改过多次），内容被替换为 `[Old tool result content cleared]`，只保留最近 5 轮的完整工具结果。释放约 35K token。
- 上下文降到约 105K，继续工作。

**第 51–70 轮（上下文约 155K token）**：Microcompact 能清理的都清理了，但对话本身很长。

- **Context Collapse 介入**：第 10–30 轮之间有一段"讨论 TypeScript 严格模式配置"的来回对话（8 轮），结论早已应用到代码中。这 8 轮被折叠为一句摘要："讨论并启用了 TypeScript strict 模式，修改了 tsconfig.json"。原始消息保留在内存中，但 API 请求中只发送摘要。释放约 12K token，且完全可逆。
- 上下文降到约 143K，仍低于 167K 触发线，继续工作。

**第 71–85 轮（上下文约 170K token）**：终于超过 167K 触发线。

- **AutoCompact 介入**：模型将全部 85 轮对话生成一份结构化摘要（约 8K token），加上最近操作的 5 个文件内容（约 20K token），重建上下文。
- 上下文从 170K 骤降到约 30K，模型根据摘要继续工作，你无感知。

**第 86 轮：用户粘贴超长日志（意外情况）**：

- 尽管刚压缩过，用户一次性粘贴了 180K 的错误日志，请求被 API 拒绝（413 错误）。
- **Reactive Compact 介入**：先尝试释放 Context Collapse 中所有暂存的折叠（drain），如果还不够则紧急执行全量压缩，为超长输入腾出空间。

整个过程中，前三层（Snip → Micro → Collapse）像"小修小补"一样持续维护空间，大多数时候不需要触发代价高昂的全量压缩。只有当它们都不够用时，AutoCompact 才作为"大扫除"出场。Reactive Compact 则是极端情况下的最后安全网。

**如果是开源版**，同样的场景只有两个转折点：第 31–50 轮如果恰好距上次回复超过 60 分钟则时间型 Microcompact 介入；否则一路积累到 ~167K 时直接触发 AutoCompact 全量压缩，中间没有缓冲。遇到 413 错误时也没有自动恢复。

---

## 三、Token 计数方式

### 3.1 精确计数

从最近的 assistant 消息的 `usage`[用量统计] 字段获取：

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

> **注意**：下表的 Claude Code 列描述的是 Anthropic **内部完整版**。开源版仅有 2 层（时间型 Microcompact + AutoCompact），详见第二节 2.0 的说明。

| 维度 | Claude Code（内部完整版） | Claude Code（开源版） | OpenClaw |
|------|--------------------------|----------------------|----------|
| **压缩层数** | 5 层（Snip → Micro → Collapse → Auto → Reactive） | 2 层（时间型 Micro → Auto） | 1 层（AutoCompact + flush[刷写/落盘，将内存数据写入磁盘持久保存]） |
| **上下文窗口** | 200K（默认），1M（可选） | 同左 | 取决于所用模型，无 1M 显式支持 |
| **压缩方式** | 前三层：模型决策/纯代码/混合，后两层：LLM 生成摘要 | 第一层纯代码，第二层 LLM 生成摘要 | LLM 生成摘要 + 压缩前 flush 到文件 |
| **记忆持久化** | CLAUDE.md 规则文件 + transcript[完整对话记录文件] | 同左 | Markdown[轻量级标记语言] 文件（MEMORY.md + 日记） |
| **压缩前保存** | 无（靠摘要质量 + transcript 回溯） | 同左 | **Memory Flush[记忆刷写]**（静默轮让 LLM 写日记） |
| **413 自动恢复** | 有（Reactive Compact） | **无** | 无专门机制 |
| **子 Agent 压缩** | 各自独立运行压缩 | 同左 | 各自独立运行压缩 |
| **可恢复性** | 可读 transcript 文件恢复细节 | 同左 | 可读 Markdown 文件恢复细节 |

### 4.2 压缩策略对比

**Claude Code 的多层策略（Anthropic 内部完整版）**

内部版的核心思路是「分层卸载，尽量晚做全量压缩」：

1. 先用 Snip 裁剪明显低价值内容（模型在正常回复中"顺手"通过 SnipTool 标记删除，零额外 API 调用）
2. 再用 Microcompact 清理旧 tool_result（纯代码替换，保留结构、只删内容）
3. 再用 Context Collapse 折叠中段对话（LLM 生成摘要 + 纯代码投影，可逆）
4. 最后才做 AutoCompact 全量摘要（LLM 生成结构化摘要，代价最高，不可逆）
5. 兜底用 Reactive Compact 处理极端 413 情况（先纯代码 drain，不够再调 LLM）

这种设计的优势是：前三层以极低代价持续释放空间，大多数情况下不需要触发代价高昂的全量压缩。

**Claude Code 开源版的策略**

开源版由于 `feature()` 编译时删除，只剩 2 层：

1. 时间型 Microcompact（纯代码，距上次回复 >60 分钟才触发）
2. AutoCompact 全量摘要

中间缓冲层全部缺失，上下文增长到触发线就直接做全量压缩，无法渐进释放。遇到 413 错误也没有自动恢复机制。

**OpenClaw 的 Flush + Compact 策略**

OpenClaw 的核心创新是「压缩前 Flush」机制：

1. 当 token 接近 `softThreshold`[软阈值] 时触发一轮静默运行
2. 模型被指示将重要信息写入 `memory/YYYY-MM-DD.md`
3. Flush 完成后才执行正常的上下文压缩
4. 被压缩丢弃的内容已经持久化到磁盘，可通过 `memory_search` 检索回来

这种设计的优势是：压缩后不会真正丢失信息，记忆成为可搜索的永久存储。

### 4.3 压缩触发条件对比

| 条件 | Claude Code（内部版） | Claude Code（开源版） | OpenClaw |
|------|----------------------|----------------------|----------|
| **Snip 持续触发** | 每 ~10K token 增长注入 nudge，模型决定是否调用 SnipTool | ~~编译移除~~ | 无 |
| **自动触发（AutoCompact）** | token 估算 >= `有效窗口 - 13K` | 同左 | token 接近 `softThreshold`（配置值） |
| **手动触发** | `/compact` 命令 | `/compact` 命令 | `/compact` 命令 |
| **时间触发** | 距上次助理消息 > 60 分钟（Microcompact） | 同左 | 无 |
| **413 触发** | Reactive Compact 自动恢复 | **无**（缺此层） | 无专门机制 |
| **阻断线** | 有效窗口 - 3K（停止接受输入） | 同左 | 无明确阻断线文档 |

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
- 底层有向量[将文本转为高维向量，通过相似度做语义搜索] + BM25[Best Matching 25，经典文本检索算法，基于词频计算相关性] 混合检索支持语义搜索
- Markdown 文件作为唯一事实来源，人可读可编辑

### 4.5 摘要质量保障对比

**Claude Code**：

- 摘要由同模型（或 fork agent 共享 cache）生成
- 结构化的 9 段式摘要模板：请求意图、技术概念、文件代码、错误修复、问题解决、用户消息、待办任务、当前工作、下一步
- `<analysis>` 块作为思维草稿提高摘要质量（最终被 strip[剥除/去掉]）
- 支持用户自定义压缩指令（如「重点关注 TypeScript 变更」）
- 自动压缩时注入「直接继续、不要寒暄」的指令
- Partial Compact[部分压缩] 模式：只压缩部分消息，保留最近的原始对话

**OpenClaw**：

- 依赖标准的 LLM 压缩能力
- 核心优势在于 Flush 机制将信息持久化，降低了对摘要质量的依赖
- 记忆文件本身可被模型主动搜索和读取

### 4.6 上下文窗口利用率对比

**Claude Code（内部完整版）**：

- 200K 窗口中有效可用约 180K（扣除摘要输出预留 20K）
- 在 ~167K 时触发自动压缩，给 13K buffer[缓冲余量]
- 支持 1M 窗口（Sonnet 4.6、Opus 4.6），阈值按比例调整
- 五层压缩使实际可利用率更高：Snip、Microcompact、Context Collapse 三层在触发全量压缩前就能渐进释放空间，且 Snip 和 Collapse 是可逆/不丢关键信息的

**Claude Code（开源版）**：

- 窗口大小和阈值与内部版相同
- 但只有时间型 Microcompact（触发条件苛刻）和 AutoCompact，中间缓冲层缺失
- 实际上大多数时候只靠 AutoCompact，利用率不如内部版（更频繁触发代价高的全量压缩）

**OpenClaw**：

- 窗口大小取决于所用模型
- 通过 Flush 机制在压缩前保存信息，有效提高了「信息保留率」
- 但没有类似 Microcompact / Context Collapse 的中间层优化

### 4.7 成本对比

| 操作 | Claude Code（内部版） | Claude Code（开源版） | OpenClaw |
|------|----------------------|----------------------|----------|
| **Snip Compact** | 零额外 API 调用（搭载在正常回复中） | ~~编译移除~~ | 不适用 |
| **Microcompact** | 零额外 API 调用（本地替换或 cache_edit） | 零额外 API（仅时间型） | 不适用 |
| **Context Collapse** | 折叠摘要可能 1 次 LLM 调用，投影本身零成本 | ~~编译移除~~ | 不适用 |
| **AutoCompact** | 1 次 API 调用（摘要生成，上限 20K output） | 同左 | 1 次 API 调用（压缩） |
| **Reactive Compact** | 0–1 次 API 调用（先 drain，不够再调 LLM） | ~~编译移除~~ | 不适用 |
| **Memory Flush** | 不适用 | 不适用 | 1 次额外 API 调用（静默轮写日记） |
| **Post-compact 恢复** | 0 次（从本地缓存读取文件） | 同左 | 按需通过 memory_search（可能触发嵌入） |

内部版 Claude Code 通过 Snip、Microcompact 和 Collapse 三层低成本操作延迟全量压缩，显著减少 API 调用次数。开源版缺失这些中间层，更频繁触发 AutoCompact，API 调用成本更高。OpenClaw 的 Flush 机制虽然多一次 API 调用，但换来了持久化的记忆存储。

---

## 五、核心设计哲学差异

### Claude Code（Anthropic 内部完整版）：「精细分层、延迟压缩、transcript 兜底」

- **五层渐进式压缩**：Snip（模型决策标记删除）→ Microcompact（纯代码替换）→ Context Collapse（LLM 摘要 + 代码投影，可逆）→ AutoCompact（LLM 全量摘要，不可逆）→ Reactive Compact（413 兜底），每层代价递增
- **Prompt Cache 友好**：Cached Microcompact（`cache_edits`）在不破坏缓存的前提下清理内容
- **Transcript 作为安全网**：压缩后仍可通过 FileRead 回溯完整历史
- **结构化摘要模板**：9 段式模板 + analysis 草稿确保摘要覆盖率
- **无独立记忆系统**：记忆依赖 CLAUDE.md 规则文件（手动维护）和 transcript

> **开源版的局限**：由于 `feature()` 编译时删除，开源版仅保留时间型 Microcompact + AutoCompact 两层。缺失 Snip 的渐进裁剪、Context Collapse 的可逆折叠、cached-editing Microcompact 的缓存友好清理、以及 Reactive Compact 的 413 自动恢复。核心"分层延迟压缩"的优势大幅缩水，实际行为更接近 OpenClaw 的"单层压缩"模式。

### OpenClaw：「先保存、再压缩、记忆即文件」

- **Flush-before-Compact[先保存再压缩]**：在压缩前让 LLM 主动将信息写入持久文件
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
| `src/services/compact/grouping.ts` | 消息按 API round 分组（PTL[Prompt Too Long，提示词过长] 重试时截断） |
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
| `packages/memory-host-sdk/src/host/memory-schema.ts` | SQLite[嵌入式轻量级数据库] Schema[表结构] 定义 |
| `extensions/memory-core/src/memory/qmd-manager.ts` | QMD 后端（外部高性能检索） |
