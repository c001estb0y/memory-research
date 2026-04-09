# Claude Code 上下文压缩机制（含 OpenClaw 对比）

基于 Claude Code 开源快照源码与 OpenClaw 源码的深度分析，结合实际 compact 案例验证。

> **源码验证版本**：`@anthropic-ai/claude-code@2.1.87`（npm 编译版 `cli.js`，12.4MB bundle）。原始分析基于 TypeScript 源码快照，本文档已用编译版逐项交叉验证，并标注了源码快照与编译版之间的差异。

---

## 一、上下文窗口能力

### 1.1 窗口大小

核心定义在 `src/utils/context.ts`：

- **默认窗口：200K token**（`MODEL_CONTEXT_WINDOW_DEFAULT = 200_000`）
- **1M 窗口**：模型名带 `[1m]` 后缀、`CONTEXT_1M_BETA_HEADER`、Sonnet 4.6 实验组 `coral_reef_sonnet`、或模型能力表 `max_input_tokens` 上报
- **环境变量覆盖**：`CLAUDE_CODE_MAX_CONTEXT_TOKENS`（仅 Anthropic 内部）

### 1.2 输出 token 上限

| 模型 | 默认输出 | 上限输出 |
|------|----------|----------|
| Opus 4.6 | 64K | 128K |
| Sonnet 4.6 | 32K | 128K |
| Opus 4.5 / Sonnet 4 / Haiku 4 | 32K | 64K |
| Claude 3.7 Sonnet | 32K | 64K |

默认 cap 到 8192（`CAPPED_DEFAULT_MAX_TOKENS`，按模型映射，如 `claude-opus-4-20250514: 8192`），命中上限后先 escalate 到 64K，再走多轮恢复。

### 1.3 有效上下文窗口

自动压缩阈值基于「有效窗口」，而非原始窗口：

```
有效窗口 = getContextWindowForModel(model) - min(maxOutputTokens, 20_000)
```

20K 是 compact 摘要的输出预留（`MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000`，基于 p99.99 compact 输出 17,387 tokens）。可被 `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 环境变量进一步收窄。

---

## 二、压缩机制总览：编译版实际架构 vs 源码快照

### 2.1 源码快照 vs 编译版的差异

原始 TypeScript 源码快照中描述了 5 层压缩架构，但对 v2.1.87 编译版 `cli.js` 逐项验证后发现：**部分层在编译版中完全不存在**（可能被 tree-shaking 移除或从未编译进公开版本）。

| 压缩层 | 源码快照 | v2.1.87 编译版 | 状态 |
|--------|---------|---------------|------|
| Snip Compact | 存在，`HISTORY_SNIP` flag | `snipCompact`、`HISTORY_SNIP`、`tokensFreed` **均无匹配** | ❌ 不存在 |
| Microcompact 时间型 | 存在 | 存在，但**默认禁用**（`enabled:!1`） | ⚠️ 默认关 |
| Microcompact 缓存编辑型 | 存在，`cache_edits` 能力 | `pinnedEdits`、`cache_edits` header 存在 | ✅ |
| Context Collapse | 存在，`CONTEXT_COLLAPSE` flag | 仅 transcript 状态字段（`contextCollapseCommits`），**压缩函数不存在** | ❌ 不存在 |
| AutoCompact | 存在 | 存在 | ✅ |
| ├─ Session Memory Compaction | 存在 | 存在，受 `tengu_sm_compact` flag 控制 | ⚠️ flag 控制 |
| └─ Full Compaction | 存在 | 存在 | ✅ |
| Reactive Compact | 存在 | `reactiveCompactOnPromptTooLong` 存在 | ✅ |

### 2.2 编译版实际压缩链路

```
┌─────────────────────────────────────────────────────────────┐
│  v2.1.87 编译版 query loop 中的实际压缩执行顺序            │
│                                                             │
│  ① Snip Compact                         ← 代码不存在 ❌    │
│  ② Microcompact                                             │
│     ├─ 时间型       ← 代码存在但默认 enabled:false ⚠️      │
│     └─ 缓存编辑型   ← cache_edits 能力门控 ✅              │
│  ③ Context Collapse                     ← 代码不存在 ❌    │
│  ④ AutoCompact                          ← 核心路径 ✅      │
│     ├─ Session Memory Compaction ← tengu_sm_compact flag ⚠️ │
│     └─ Full Compaction（9 段式摘要）     ← 无条件可用 ✅    │
│  ⑤ Reactive Compact（413 兜底）         ← 无条件可用 ✅    │
│                                                             │
│  ❌ = 编译版中代码不存在（可能被 tree-shaking 或从未编译）  │
│  ⚠️ = 代码存在但受 flag/配置控制，默认可能不启用            │
│  ✅ = 编译版中确认存在且默认可用                            │
└─────────────────────────────────────────────────────────────┘
```

**对大多数用户而言，唯一确定生效的压缩是 AutoCompact（Full Compaction 路径）和 Reactive Compact。** Microcompact 时间型和 Session Memory Compaction 的实际启用取决于服务端 A/B 配置（GrowthBook），用户无法自行控制。

---

## 三、编译版中确认存在的压缩机制

### 3.1 Microcompact 时间型（轻量清理）

> **⚠️ 源码验证注意**：v2.1.87 编译版中该功能默认配置为 `enabled:false`。实际是否启用取决于服务端下发的 GrowthBook 配置，用户无法自行开关。以下描述基于该功能启用时的行为。

**触发条件**：距离上次 assistant 消息超过 60 分钟（此时服务端 prompt cache 已过期），且服务端配置已启用

**做了什么**：把旧的 `tool_result` 内容替换为占位符，保留最近 N 条完整结果

**具体示例**：

假设对话中有 20 次 FileRead 调用。用户离开 1 小时后回来继续对话：

```
压缩前（20 条 tool_result 都保留完整内容）：
┌─ FileRead("config.ts")     → 完整文件内容（500 行）
├─ FileRead("utils.ts")      → 完整文件内容（300 行）
├─ FileRead("api.ts")        → 完整文件内容（800 行）
│  ... 中间 12 条 ...
├─ Grep("handleError")       → 搜索结果（50 行）
├─ FileRead("router.ts")     → 完整文件内容（200 行）
├─ FileRead("auth.ts")       → 完整文件内容（150 行）
├─ Bash("npm test")          → 测试输出（80 行）
└─ FileRead("index.ts")      → 完整文件内容（100 行）

压缩后（只保留最近 5 条，其余替换为占位符）：
┌─ FileRead("config.ts")     → [Old tool result content cleared]
├─ FileRead("utils.ts")      → [Old tool result content cleared]
├─ FileRead("api.ts")        → [Old tool result content cleared]
│  ... 中间 12 条也被清空 ...
├─ Grep("handleError")       → [Old tool result content cleared]
├─ FileRead("router.ts")     → 完整文件内容（200 行）  ← 保留
├─ FileRead("auth.ts")       → 完整文件内容（150 行）  ← 保留
├─ Bash("npm test")          → 测试输出（80 行）       ← 保留
└─ FileRead("index.ts")      → 完整文件内容（100 行）  ← 保留
                              + 第 5 近的一条也保留
```

**关键参数**（v2.1.87 编译版确认）：
- `keepRecent`：默认 5，最少 1 ✅
- `gapThresholdMinutes`：默认 60 分钟 ✅
- `enabled`：默认 `false` ⚠️（需服务端配置启用）
- 只清理特定工具的结果：FileRead、Bash/PowerShell、Grep、Glob、WebSearch、WebFetch、FileEdit、FileWrite
- **不修改消息结构**，只替换 `tool_result` 的 content 字段

**代价**：零 API 调用（纯本地操作），零信息结构损失。丢失的是早期工具输出的原始内容，但对话流程和模型推理过程完整保留。

### 3.2 AutoCompact（核心压缩，不可逆）

**触发条件**：

```
tokenCountWithEstimation(messages) - snipTokensFreed >= autoCompactThreshold
```

其中 `autoCompactThreshold = 有效窗口 - 13_000`。

**阈值体系**（200K 窗口为例）：

| 阈值 | 计算方式 | 大约值 | 用途 |
|------|----------|--------|------|
| 自动压缩触发线 | 有效窗口 - 13K | ~167K | 触发 autoCompact |
| 警告线 | 触发线 - 20K | ~147K | UI 显示警告 |
| 阻断线（auto off） | 有效窗口 - 3K | ~177K | 停止接受输入 |

**熔断**：连续失败 3 次停止尝试（`MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES`）。

**手动触发**：用户在 Claude Code 中输入 `/compact` 命令可随时强制执行。

#### 路径 A：Session Memory Compaction（轻量路径）

> **⚠️ 源码验证注意**：v2.1.87 编译版中此路径受 `tengu_sm_compact` GrowthBook flag 控制，也可通过环境变量 `ENABLE_CLAUDE_CODE_SM_COMPACT` 强制启用或 `DISABLE_CLAUDE_CODE_SM_COMPACT` 强制关闭。默认配置：`minTokens: 10000, minTextBlockMessages: 5`。

**优先尝试**。如果会话存在 session memory（`summary.md`），用已有的 session memory 替代全量摘要，只保留最近的消息切片。

**优势**：复用已有摘要，不需要额外 API 调用生成新摘要。

**局限**：session memory 的覆盖面取决于其更新频率和质量，可能不如 Full Compaction 的 9 段式摘要全面。

#### 路径 B：Full Compaction（全量摘要路径）

Session Memory Compaction 不可用或不充分时回退到此路径。**这是绝大多数用户实际触发的压缩方式。**

**执行步骤**：

```
1. getCompactPrompt() 构造摘要请求
2. 移除消息中的图片和技能发现类 attachment
3. 通过 fork 子代理（共享 prompt cache）生成摘要
4. 模型输出 <analysis>（思维草稿）+ <summary>（正式摘要）
5. formatCompactSummary() 去掉 <analysis>，提取 <summary>
6. 用摘要消息替换所有原始消息
7. 执行 post-compact 恢复（文件、技能、工具增量）
```

**如果压缩请求本身 prompt-too-long**：按 API round 分组从最旧侧批量截断消息，最多重试 3 次。

**摘要输出格式 — 9 段式结构化模板**：

```
This session is being continued from a previous conversation that ran out 
of context. The summary below covers the earlier portion of the conversation.

Summary:
1. Primary Request and Intent: [用户的核心目标]
2. Key Technical Concepts: [涉及的技术概念]
3. Files and Code Sections: [操作过的文件和代码]
4. Errors and fixes: [遇到的错误及修复]
5. Problem Solving: [问题解决过程]
6. All user messages: [所有用户消息]
7. Pending Tasks: [待处理任务]
8. Current Work: [当前进行中的工作]
9. Optional Next Step: [建议的下一步]

If you need specific details from before compaction, read the full 
transcript at: [transcript_path]

Continue the conversation from where it left off without asking the user 
any further questions. Resume directly — do not acknowledge the summary...
```

**压缩后保留什么**：

| 保留项 | 预算 | 说明 |
|--------|------|------|
| CompactBoundaryMessage | — | 压缩边界标记 |
| 摘要消息 | ≤20K token | 9 段式结构化摘要，`isCompactSummary: true` |
| Post-compact 文件附件 | ≤50K token（最多 5 文件，每文件 ≤5K） | 最近读写的文件内容 |
| 技能附件 | ≤25K token | 激活技能的上下文 |
| 工具/MCP delta | — | 延迟加载工具、Agent、MCP 指令的变更 |
| SessionStart hooks | — | 重新执行会话启动钩子 |

**压缩后丢弃什么**：**所有**压缩前的原始消息——用户消息、助理消息、工具调用、工具结果等，全部被摘要替换。

### 3.3 Reactive Compact（413 兜底）

**触发条件**：API 返回 prompt-too-long (413) 错误后

这是最后一道防线。在 AutoCompact 未能阻止 413 时紧急触发。

**恢复流程**：直接执行紧急全量压缩。失败则返回 prompt_too_long 错误。

> **源码快照 vs 编译版差异**：源码快照描述的恢复优先级是「先尝试 Context Collapse drain，再 Reactive Compact」。但 v2.1.87 编译版中 Context Collapse 代码不存在，Reactive Compact 直接执行紧急压缩。

**防循环**：`hasAttemptedReactiveCompact` 标记防止无限重试（v2.1.87 确认存在）。

### 3.4 Transcript 回读机制

Transcript 是 Full Compaction 的安全网。Claude Code 会将每次会话的完整对话实时写入 `.jsonl` 文件。

**机制**：
- 压缩后摘要末尾注入 transcript 文件路径
- 模型理论上可用 `FileRead` 回读 transcript，恢复被压缩掉的具体细节
- Transcript 格式为 JSONL，每行是一条完整的消息 JSON

**实际有效性**（见第五节案例分析）：
- 模型需要**主动意识到**摘要信息不足并**主动去读** transcript
- Transcript 文件通常很大（完整对话的 JSONL），定位特定内容耗时且消耗 token
- 实际使用中，模型几乎总是直接按摘要继续执行，极少回读 transcript

---

## 四、仅存在于源码快照的压缩层（编译版未确认）

以下压缩层在 TypeScript 源码快照中被描述，但在 v2.1.87 编译版 `cli.js` 中**未找到对应的函数实现**。可能的原因：编译时被 tree-shaking 移除（flag 硬编码为 false）、仅存在于 Anthropic 内部构建、或在后续版本中被重新设计。

> **⚠️ 准确性声明**：以下内容来自源码快照分析，无法在编译版中验证。标记为「源码快照描述」的内容应视为参考而非确认事实。

### 4.1 Snip Compact（裁剪压缩）— 编译版中不存在 ❌

**源码快照描述**：受 `HISTORY_SNIP` feature flag 保护，在每轮迭代最先执行。

**v2.1.87 验证结果**：`snipCompact`、`HISTORY_SNIP`、`snipCompactIfNeeded`、`tokensFreed` 在编译版中**均无匹配**。存在一个 `tengu_snip_resume_filtered` 事件，但这是 transcript 恢复过滤逻辑，与上下文压缩无关。

**源码快照中的行为描述**（仅供参考）：
- `snipCompactIfNeeded(messages)` 返回 `{ messages, tokensFreed, boundaryMessage }`
- `tokensFreed` 传递给后续的自动压缩阈值计算
- 策略：从消息历史中裁剪早期低价值内容
- 设计意图：与 Microcompact 互补——Microcompact 只清工具输出内容，Snip 可以裁剪整个低价值消息

### 4.2 Cached Microcompact（缓存编辑型微压缩）

**门控条件**：API 层支持 `cache_edits` 能力

**执行时机**：Microcompact 的路径 B（在时间型之后判断）

**与时间型 Microcompact 的区别**：

| 维度 | 时间型（公开） | 缓存编辑型（实验） |
|------|--------------|-------------------|
| 触发条件 | 距上次消息 > 60 分钟 | 随时（不依赖时间） |
| 修改方式 | 直接替换本地消息内容 | 通过 `cache_edits` 指令在 API 层删除 |
| Prompt Cache | 破坏（本地消息已变） | **不破坏**（本地消息不变） |
| 适用范围 | 所有环境 | 仅支持 cache_edits 的 API |
| 限制 | 仅主线程 | 仅主线程（防止 fork agent 污染） |

**具体示例**：

```
缓存编辑型不修改本地消息数组，而是维护一个 pinnedEdits 列表：

pinnedEdits: [
  { index: 3, action: "delete_content" },   // 第 3 条 tool_result
  { index: 7, action: "delete_content" },   // 第 7 条 tool_result
  { index: 11, action: "delete_content" },  // 第 11 条 tool_result
]

API 请求时，cache_edits 指令让服务端在不失效缓存的情况下裁剪这些内容。
后续请求携带相同的 pinnedEdits 确保一致性。
```

**设计意图**：保留 prompt cache 命中率（cache TTL 5 分钟），减少重新计算缓存的开销。

### 4.3 Context Collapse（上下文折叠）— 编译版中不存在 ❌

**源码快照描述**：受 `CONTEXT_COLLAPSE` feature flag 保护，在 Microcompact 之后、AutoCompact 之前执行。

**v2.1.87 验证结果**：`CONTEXT_COLLAPSE` 作为 feature flag **无匹配**。`applyCollapsesIfNeeded` 和 `recoverFromOverflow` 函数**均不存在**。编译版中存在 `contextCollapseCommits` 和 `contextCollapseSnapshot` 字段，但这些仅作为 **transcript 状态持久化**的数据结构，不是压缩功能。

**源码快照中的行为描述**（仅供参考）：
- `applyCollapsesIfNeeded(messages, toolUseContext, querySource)` — 投影折叠视图
- `recoverFromOverflow(messages, querySource)` — 作为 413 错误的第一道恢复手段
- 策略：将中段多轮交互折叠为摘要，以「读时投影」方式实现
- 折叠存储在独立的 collapse store 中，不直接修改原始消息数组

**如果该功能上线，其意义在于提供可逆压缩**——原始消息仍存储在本地，只是 API 请求时投影为折叠视图。这与 Full Compaction 的不可逆替换形成对比。但在当前编译版中，这一能力不可用。

### 4.4 源码快照中的完整压缩链路（理论设计）

如果 Anthropic 内部全量启用所有 Feature Flag，源码快照描述的压缩链路为：

```
Snip → 时间型 MC → 缓存编辑型 MC → Context Collapse → AutoCompact → Reactive
  ↓         ↓              ↓              ↓              ↓            ↓
裁剪低     清空旧         API层清空      折叠中段       全量摘要     413紧急
价值消息   tool内容       tool内容       为投影视图     (不可逆)     压缩
(不可逆)   (不可逆)       (保留cache)    (可逆)
```

设计思路是「分层卸载、代价递增」：前几层零 API 调用或保留 cache，只有在空间仍然不够时才触发昂贵的 Full Compaction。

**但 v2.1.87 编译版的实际链路更简单**：

```
缓存编辑型 MC → AutoCompact (SM Compact → Full Compaction) → Reactive Compact
      ↓                    ↓                                       ↓
  API层清空旧        轻量摘要或全量9段式                        413紧急压缩
  tool内容           (不可逆)                                  (不可逆)
  (保留cache)
```

Snip 和 Context Collapse 不存在，时间型 Microcompact 默认关闭，Session Memory Compaction 受 flag 控制。普通用户几乎只会触及 **Full Compaction** 和 **Reactive Compact**。

---

## 五、实战案例：一次 Full Compaction 的完整过程

以下是一个真实案例，展示 Full Compaction 如何压缩一个复杂的 Wiki 编译任务对话，以及压缩导致的具体信息损失。

### 5.1 场景描述

**任务**：使用 Claude Code 编译 `Mods/Farm/策划/` 目录的 Wiki（249 个 .md 文件），遵循 LLMwiki 编译方案的三层架构。

**对话历程**：
1. 初始编译请求（并行 agent 处理）
2. ABTest 精度验证设计
3. 多轮纠错（建筑数量错误、BUFF 分类错误、神像等级混淆等）
4. 发现编译流程根本性问题（源文件覆盖不全、概念页面含杜撰内容）
5. 用户撰写了一个严格的三阶段编译 prompt，要求重新编译

在第 5 步之后，对话 token 超过触发线，**Full Compaction 自动执行**。

### 5.2 压缩前：用户的三阶段编译 prompt（原文）

用户最后一条消息是一个精心设计的编译规则 prompt，包含：

```
你是一个知识编译引擎。请严格遵守以下规则：

## 前置阅读
1. 阅读 AIKnowledge/LLMwiki编译方案.md 了解编译方案和分层 Wiki 架构
2. 阅读目标模块的 README 或目录结构了解模块概况
3. 如果目标模块已有 wiki/，阅读 wiki/index.md 了解已有编译成果

## 编译目标
摄入 <目标路径> 下的文档（如 Mods/Farm/策划/策划案/商品需求/）
⚠ 每次只处理一个子目录，不超过 10 个文件

## 编译三阶段（严格按顺序）
### 阶段 1: Source（全覆盖，不可跳过）
1. 列出目标路径下所有文件
2. 逐文件阅读，为每个文件生成 wiki/sources/原始文件名-摘要.md：
   - YAML frontmatter: source_path, date, tags, status
   - 信息密度评级: ★~★★★★★，空文件标注"☆ 空"
   - 关键内容提取清单
3. 空文件也生成 source 标注"空"，但后续不生成 entity/concept

### 阶段 2: Entity + Concept（从 source 中提取，禁止自造）
4. 遍历刚生成的 source 摘要，提取实体 → wiki/entities/系统名.md
5. 提取跨文件模式 → wiki/concepts/概念名.md
6. ⚠ 每个断言必须标注 `来源: [[sources/xxx-摘要]]`
7. ⚠ 禁止写入任何 source 中不存在的信息
8. 如有跨文件的综合分析 → wiki/synthesis/主题名.md
9. 所有配置表字段必须完整保留，不得精简
10. 数值参数必须保留具体数字和默认值

### 阶段 3: Link（链接器）
11. 在相关页面之间添加 [[wikilink]]，确保双向引用
12. 更新 wiki/index.md：新增条目 + 被引用列
13. 追加 wiki/log.md：记录本次摄入的统计
14. 如果提取到跨模块通用概念，提示是否写入顶层 wiki/concepts/

## 完成检查清单
- [ ] source 数量 = 原始文件数量（100% 覆盖）
- [ ] 每个 entity/concept 的每个断言都有 source 引用
- [ ] 无 LLM 自造知识（不存在无 source 的段落）
- [ ] index.md 已更新
- [ ] log.md 已追加记录
```

### 5.3 压缩后：compact summary 中的残留

Full Compaction 将上述 prompt 压缩为以下内容，散布在 9 段式摘要的不同段落中：

**第 6 段 "All user messages"** — 只有一行概括：

> Final message: New compilation prompt with strict 3-phase schema + recompile request for Mods/Farm/策划/

**第 7 段 "Pending Tasks"** — 提取了部分要点：

> - "每次只处理一个子目录，不超过 10 个文件"
> - "阶段 1：Source（全覆盖，不可跳过）"
> - "阶段 2：Entity + Concept（从 source 中提取，禁止自造）"
> - "阶段 3：Link（链接器）"
> - Checklist: source数量=原始文件数量(100%覆盖), 每个entity/concept的每个断言都有source引用, 无LLM自造知识

**第 8 段 "Current Work"** — 提取了一些限制条件的关键词

### 5.4 信息损失对照表

| 原始 prompt 中的规则 | compact 摘要中的状态 | 损失严重度 |
|---------------------|---------------------|-----------|
| 前置阅读三步（读方案、读 README、读已有 index） | **完全丢失** | 🔴 高 |
| Source 的 YAML frontmatter 格式（source_path, date, tags, status） | **完全丢失** | 🔴 高 |
| 信息密度评级规则（★~★★★★★，空文件标 ☆ 空） | **完全丢失** | 🔴 高 |
| 空文件生成 source 但不生成 entity/concept | **完全丢失** | 🟡 中 |
| 断言标注格式：`来源: [[sources/xxx-摘要]]` | **压缩为**"每个断言都有source引用" | 🟡 中 |
| 配置表字段完整保留，不得精简 | **完全丢失** | 🔴 高 |
| 数值参数保留具体数字和默认值 | **完全丢失** | 🔴 高 |
| wikilink 双向引用规则 | **完全丢失** | 🟡 中 |
| 跨模块概念提示写入顶层 wiki/concepts/ | **完全丢失** | 🟡 中 |
| 完成后列出三项输出（页面清单、覆盖率统计、关键发现） | **完全丢失** | 🟡 中 |
| 三阶段的总体结构 | **保留**（作为要点列表） | ✅ |
| 100% 覆盖和禁止自造的核心约束 | **保留** | ✅ |
| 每次不超过 10 个文件 | **保留** | ✅ |

**结论**：compact 保留了任务的「意图骨架」（做什么、大致怎么做），但丢失了几乎所有「执行细节」（格式规范、质量标准、具体约束）。如果模型按照 compact 摘要继续执行，生成的 Wiki 将缺少 YAML frontmatter、信息密度评级、断言标注格式等关键规范——这些正是用户从第一次编译失败中总结出来、写在新 prompt 中的改进要求。

### 5.5 Transcript 回读：理论上的恢复路径

compact 摘要末尾注入了 transcript 路径：

```
C:\Users\minusjiang\.claude-internal\projects\E--UGit-LetsGoEditor-Editor-AIKnowledge\bee4efb6-31cb-4bca-9d14-f4774a031b56.jsonl
```

**理论上**模型可以：
1. 读取这个 `.jsonl` 文件
2. 找到用户最后一条消息
3. 恢复完整的三阶段 prompt

**实际上**这几乎不会发生，因为：
- compact 摘要看起来已经「足够完整」——有三阶段描述、有核心约束，模型没有明确的信号表明缺少了格式规范细节
- Transcript 文件是整个对话的 JSONL（可能数 MB），模型需要消耗大量 token 来读取和定位
- Claude Code 的自动压缩指令明确要求「直接继续、不要寒暄」，引导模型立即执行而非回顾

**根本问题**：Full Compaction 的 9 段式模板设计为通用结构，擅长保留「做了什么、出了什么问题」（回顾性信息），但不擅长保留「接下来应该精确遵守什么规则」（前瞻性指令）。当用户最后一条消息恰好是一个详细的执行规范时，这种不对称损失最为严重。

### 5.6 应对策略

针对这类「精确指令在压缩中丢失」的问题，有效的应对方式：

| 策略 | 做法 | 原理 |
|------|------|------|
| **写入文件** | 将 prompt 存为 `.md` 文件（如 `编译规则.md`） | Post-compact 文件恢复机制会自动注入最近读写的文件（50K 预算） |
| **写入 CLAUDE.md** | 将规则追加到项目的 `CLAUDE.md` | CLAUDE.md 作为系统提示词的一部分，压缩不影响它 |
| **手动 /compact** | 在发送长 prompt 之前手动执行 `/compact` | 确保 prompt 发送时有足够的上下文空间，避免发送后立即被压缩 |
| **拆分会话** | 在新会话中发送 prompt | 新会话没有历史包袱，prompt 不会被压缩 |

最可靠的方式是**将执行规范写入文件**，让它脱离对话消息流。

---

## 六、Token 计数方式

### 6.1 精确计数

从最近的 assistant 消息的 `usage` 字段获取：

```
totalTokens = input_tokens + cache_creation + cache_read + output_tokens
```

### 6.2 粗略估算

无 API usage 数据时：`tokens ≈ content.length / 4`（4 bytes per token）

### 6.3 混合策略

`tokenCountWithEstimation()` 从消息列表末尾向前扫描，找到第一条有 API usage 的消息，取其 token 数，再加上后续消息的粗略估算。

---

## 七、与 OpenClaw 的对比

### 7.1 架构对比总览

| 维度 | Claude Code（v2.1.87 编译版） | Claude Code（源码快照全量） | OpenClaw |
|------|-------------------------------|---------------------------|----------|
| **压缩层数** | 2 层确定（Auto + Reactive），2 层 flag 控制（MC, SM Compact） | 5 层（Snip + MC + Collapse + Auto + Reactive） | 1 层（AutoCompact + flush） |
| **压缩方式** | LLM 生成 9 段式摘要 | 分层卸载 + 9 段式摘要 | LLM 摘要 + 压缩前 flush |
| **压缩前保存** | 无（靠摘要 + transcript 兜底） | 同左 | **Memory Flush**（静默轮写日记） |
| **可逆压缩** | 无（代码不存在） | Context Collapse（可逆投影，源码中存在） | 无 |
| **Prompt Cache** | Cached MC 可保留 cache | 同左 | 无相关优化 |
| **可恢复性** | Transcript 文件回读 | 同左 | Markdown 日记 + 混合检索 |

### 7.2 核心策略差异

**Claude Code 的策略：摘要替代 + transcript 兜底**

```
对话消息 ──→ 9 段式摘要替换 ──→ 丢失原始消息
                                    ↓
                              transcript 磁盘文件 ──→ 可回读（但实际很少用）
```

优势：摘要结构化，覆盖面广。劣势：摘要质量决定一切，精确指令易丢失（见第五节案例）。

**OpenClaw 的策略：先 Flush 持久化，再压缩**

```
对话消息 ──→ Memory Flush（静默轮写日记到 memory/YYYY-MM-DD.md）
                ↓
            ──→ 上下文压缩 ──→ 丢失原始消息
                                    ↓
                              memory_search 检索日记 ──→ 按需恢复
```

优势：压缩前信息已持久化，对摘要质量依赖低。劣势：多一次 API 调用，日记质量取决于 Flush prompt。

**关键差异**：Claude Code 依赖压缩后的摘要质量，OpenClaw 依赖压缩前的 Flush 覆盖度。在第五节的案例中，如果使用 OpenClaw，用户的三阶段 prompt 会在 Flush 阶段被写入日记文件，压缩后可通过 `memory_search` 检索回来——信息损失显著低于 Claude Code 的 Full Compaction。

### 7.3 触发条件对比

| 条件 | Claude Code | OpenClaw |
|------|-------------|----------|
| **自动触发** | token ≥ `有效窗口 - 13K` | token 接近 `softThreshold` |
| **手动触发** | `/compact` 命令 | `/compact` 命令 |
| **时间触发** | > 60 分钟（microcompact） | 无 |
| **413 触发** | Reactive Compact | 无专门机制 |
| **阻断线** | 有效窗口 - 3K | 无明确阻断线 |

### 7.4 成本对比

| 操作 | Claude Code | OpenClaw |
|------|-------------|----------|
| **Microcompact** | 0 次 API 调用（本地操作） | 不适用 |
| **AutoCompact** | 1 次 API 调用（≤20K output） | 1 次 API 调用 |
| **Memory Flush** | 不适用 | 1 次额外 API 调用 |
| **Post-compact 恢复** | 0 次（本地文件读取） | 按需 memory_search |

Claude Code 通过 Microcompact 延迟全量压缩，减少 API 调用。OpenClaw 多一次 Flush 调用，但换来持久化的记忆存储。

---

## 八、ExtractMemories — 持久记忆提取（独立于压缩）

> **重要澄清**：ExtractMemories 和上述压缩机制是**两个完全独立的系统**。ExtractMemories 不是压缩，不释放上下文空间，不生成 9 段式摘要。

### 8.1 定位

| 维度 | AutoCompact（压缩） | ExtractMemories（记忆） |
|------|---------------------|------------------------|
| **目的** | 释放上下文窗口空间 | 将持久知识保存到磁盘 |
| **触发** | token 接近窗口上限 | 每轮查询结束时自动运行 |
| **输出** | 内存中的摘要消息 | 磁盘上的 `.md` 文件 |
| **生存期** | 当前会话 | 永久（跨会话） |
| **可逆性** | 不可逆 | 持久化可编辑 |

### 8.2 触发与执行

每次查询循环结束时 fire-and-forget 执行，使用 forked agent（共享 prompt cache，最多 5 轮工具调用）。

**执行条件**：主代理 + feature gate 开启 + auto memory 启用 + 非 remote + 无重叠执行

**互斥**：如果主代理本轮已写过 memory 文件，跳过提取。

### 8.3 四类型分类

| 类型 | 存什么 | 示例 |
|------|--------|------|
| **user** | 角色、目标、偏好 | "用户是数据科学家，关注可观测性" |
| **feedback** | 工作方式的纠正/确认 | "集成测试必须用真数据库" |
| **project** | 代码/git 无法推导的上下文 | "周四起冻结非关键合并" |
| **reference** | 外部资源指引 | "Pipeline bugs 在 Linear INGEST 项目" |

**明确不保存**：代码模式、架构、Git 历史、调试方案、已在 CLAUDE.md 中记录的内容。

### 8.4 与压缩的关系

两者互补而非替代：

```
AutoCompact → 当前会话上下文不够用 → 生成摘要释放空间
ExtractMemories → 下次会话怎么知道之前学到了什么 → 写 .md 文件供检索
```

详细的 Memory 系统设计见 `claudecode-memory系统设计.md`。

---

## 九、设计哲学与已知局限

### 9.1 Claude Code 的设计哲学

**「摘要替代 + transcript 兜底，分层卸载仍在实验」**

- 编译版现状：大多数用户只触及 Full Compaction（9 段式摘要替换所有原始消息）
- 源码快照中的 5 层渐进卸载设计（Snip → MC → Collapse → Auto → Reactive）在编译版中仅部分实现
- Cached Microcompact 和 Session Memory Compaction 作为轻量替代方案存在但受 flag 控制
- 安全网：Transcript 文件 + post-compact 文件恢复

### 9.2 OpenClaw 的设计哲学

**「先保存、再压缩、记忆即文件」**

- Flush-before-Compact：压缩前让 LLM 主动持久化信息
- Markdown 为唯一事实来源：人可读可编辑
- 混合检索恢复：向量 + BM25 语义搜索被压缩的内容

### 9.3 已知局限

#### Full Compaction 的不对称信息损失

9 段式摘要模板的设计偏向**回顾性信息**（做了什么、出了什么错），对**前瞻性指令**（接下来应遵守什么规则）的保留能力弱。当用户最后一条消息是详细的执行规范时（如第五节案例），格式、标准、约束等细节几乎全部丢失，只保留意图骨架。

#### Transcript 回读的实际失效

虽然 transcript 机制在设计上提供了完整回溯能力，但实际使用中模型极少主动回读 transcript，因为：
1. 摘要看起来「足够完整」，缺乏明确的信息缺失信号
2. Transcript 文件体积大（JSONL 格式），读取成本高
3. 自动压缩指令引导模型「直接继续」而非回顾

#### 编译版缺少可逆压缩

v2.1.87 编译版中不存在 Context Collapse 这样的可逆压缩层。一旦触发 AutoCompact，原始消息永久丢失（除 transcript 备份）。源码快照中描述的 Context Collapse（读时投影、可逆折叠）如果能上线，将显著改善这一问题——大部分情况下只需折叠中段对话，无需触发不可逆的 Full Compaction。

#### 与 OpenClaw Flush 机制的差距

Claude Code 没有「压缩前主动持久化」机制。虽然 ExtractMemories 会提取长期记忆，但它提取的是用户偏好、反馈等跨会话知识，**不会保存当前任务的执行规范**（这被归类为"临时任务细节"而被排除）。这意味着 Full Compaction 后，当前任务的细节只能依赖摘要质量——而摘要恰恰在这方面表现最弱。

---

## 十、核心源码文件索引

### 压缩相关

| 文件 | 职责 | v2.1.87 编译版状态 |
|------|------|-------------------|
| `src/utils/context.ts` | 上下文窗口大小、输出上限、1M 检测 | ✅ 确认存在 |
| `src/services/compact/autoCompact.ts` | 自动压缩触发阈值、shouldAutoCompact | ✅ 确认存在 |
| `src/services/compact/compact.ts` | 核心压缩（摘要生成、消息重建、post-compact 恢复） | ✅ 确认存在 |
| `src/services/compact/prompt.ts` | 9 段式摘要模板 + analysis 草稿 | ✅ 确认存在 |
| `src/services/compact/microCompact.ts` | Microcompact（时间型默认禁用，缓存编辑型存在） | ⚠️ 部分确认 |
| `src/services/compact/sessionMemoryCompact.ts` | Session Memory 轻量压缩 | ⚠️ 受 flag 控制 |
| `src/services/compact/grouping.ts` | 消息按 API round 分组（PTL 重试截断） | ✅ 确认存在 |
| `src/services/compact/postCompactCleanup.ts` | 压缩后文件/技能/工具增量恢复 | ✅ 确认存在 |
| `src/services/compact/compactWarningState.ts` | 压缩警告状态 | ✅ 确认存在 |
| `src/services/compact/snipCompact.ts` | Snip 压缩 | ❌ 编译版不存在 |
| `src/query.ts` | 主循环压缩层编排 | ✅ 确认存在（无 Snip/Collapse） |
| `src/utils/tokens.ts` | Token 计数估算 | ✅ 确认存在 |

### 持久记忆相关（ExtractMemories）

| 文件 | 职责 |
|------|------|
| `src/services/extractMemories/extractMemories.ts` | 记忆提取（触发、overlap guard、forked agent） |
| `src/services/extractMemories/prompts.ts` | 提取 prompt 构建 |
| `src/memdir/memoryTypes.ts` | 四类型分类 + what-not-to-save |
| `src/memdir/memdir.ts` | MEMORY.md 索引管理 |
| `src/memdir/memoryScan.ts` | 目录扫描 + frontmatter 解析 |
| `src/memdir/paths.ts` | 记忆路径计算 |
| `src/query/stopHooks.ts` | 查询结束钩子 |
| `src/utils/forkedAgent.ts` | forked agent 框架 |

### OpenClaw 相关

| 文件 | 职责 |
|------|------|
| `src/auto-reply/reply/agent-runner-memory.ts` | 压缩前 Memory Flush |
| `extensions/memory-core/src/flush-plan.ts` | Flush 护栏 |
| `extensions/memory-core/src/memory/manager.ts` | 混合检索（向量 + BM25） |
| `extensions/memory-core/src/prompt-section.ts` | 提示词引导 |
| `packages/memory-host-sdk/src/host/memory-schema.ts` | SQLite Schema |
| `extensions/memory-core/src/memory/qmd-manager.ts` | QMD 后端 |
